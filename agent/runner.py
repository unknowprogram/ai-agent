"""执行模型自主选择的联网和文档工具，普通和流式聊天共用有限循环。"""

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass

from pydantic import ValidationError

from agent.llm import LLMReply, ask_llm, stream_llm
from core.exceptions import ContextTooLargeError, LLMResponseError
from tools.documents import DOCUMENT_TOOL, DocumentSearchArguments, search_documents
from tools.document_summary import SUMMARY_TOOL, SUMMARY_OUTPUT_TOKENS, DocumentSummaryArguments, SummaryResult, summarize_document
from tools.web_search import SEARCH_TOOL, SearchArguments, web_search

MAX_TOOL_ROUNDS = 2
MAX_TOOL_CALLS = 3


TOOL_ARGUMENTS = {"web_search": SearchArguments, "search_documents": DocumentSearchArguments, "summarize_document": DocumentSummaryArguments}


@dataclass(frozen=True)
class AgentResult:
    answer: LLMReply
    sources: list[dict[str, str]]
    messages: list[dict]
    searched: bool
    document_sources: list[dict]
    document_summaries: list[dict]


def _json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False)


def _document_sources(messages: list[dict]) -> list[dict]:
    """引用仅来自实际回传给模型的文档工具片段，保留预算裁剪后的正文。"""
    sources = {}
    for message in messages:
        if message["role"] != "tool":
            continue
        data = json.loads(message["content"])
        if data.get("source_type") not in {"document", "document_summary"}:
            continue
        for item in data.get("results", []):
            key = (item["file_id"], item["chunk_index"])
            previous = sources.get(key)
            if previous is None or len(item["content"]) > len(previous["content"]):
                sources[key] = item
    return list(sources.values())


def _document_summaries(messages: list[dict]) -> list[dict]:
    result = {}
    for message in messages:
        if message["role"] != "tool":
            continue
        data = json.loads(message["content"])
        if data.get("source_type") == "document_summary":
            result[(data["file_id"], data["query"])] = {
                key: data[key] for key in ("file_id", "filename", "query", "summary", "coverage", "sections")
            }
    return list(result.values())


def _fit_messages(build_messages: Callable, tool_messages: list[dict], searched: bool) -> list[dict]:
    """先由上下文构建器裁剪历史和附件，仍超预算时压缩工具摘要。"""
    while True:
        try:
            return build_messages(tool_messages, searched)
        except ContextTooLargeError:
            changed = False
            for message in tool_messages:
                if message["role"] != "tool":
                    continue
                data = json.loads(message["content"])
                results = data.get("results", [])
                if not results:
                    continue
                longest = max(results, key=lambda item: len(item["content"]))
                if len(longest["content"]) > 100:
                    longest["content"] = longest["content"][:len(longest["content"]) // 2]
                else:
                    results.pop()
                data["truncated"] = True
                message["content"] = _json(data)
                changed = True
                break
            if not changed:
                raise


async def run_agent(
    build_messages: Callable[[list[dict], bool], list[dict]],
    *, search_mode: bool | None = None, streaming: bool = False,
    file_ids: list[str] | None = None,
    is_disconnected: Callable[[], Awaitable[bool]] | None = None,
) -> AsyncIterator[tuple[str, dict | AgentResult]]:
    tool_messages = []
    sources = []
    searched = False
    call_count = 0
    seen_queries = {}
    seen_summaries = {}
    total_usage = None
    file_ids = list(file_ids or [])

    for round_index in range(MAX_TOOL_ROUNDS + 1):
        if is_disconnected and await is_disconnected():
            return
        within_budget = round_index < MAX_TOOL_ROUNDS and call_count < MAX_TOOL_CALLS
        tools = []
        if within_budget and search_mode is not False:
            tools.append(SEARCH_TOOL)
        if within_budget and file_ids:
            tools.extend([DOCUMENT_TOOL, SUMMARY_TOOL])
        enabled = bool(tools)
        tools = tools or None
        names = {tool["function"]["name"] for tool in tools or []}
        choice = {"type": "function", "function": {"name": "web_search"}} if "web_search" in names and search_mode is True and round_index == 0 else "auto"
        if not enabled and tool_messages:
            last_result = json.loads(tool_messages[-1]["content"])
            last_result["tool_budget"] = (
                "本轮外层工具调用已结束。请根据已有摘要及覆盖统计回答；已扫描完成的文件无需再次逐段检索。"
                if _document_summaries(tool_messages) else
                "本轮工具调用次数已达上限，请根据已有结果回答并说明资料不足的部分。"
            )
            tool_messages[-1]["content"] = _json(last_result)
        messages = _fit_messages(build_messages, tool_messages, searched)
        model_options = {"max_tokens": SUMMARY_OUTPUT_TOKENS} if _document_summaries(tool_messages) else {}

        answer = None
        if streaming:
            async with aclosing(stream_llm(messages, tools=tools, tool_choice=choice, **model_options)) as chunks:
                async for chunk in chunks:
                    if is_disconnected and await is_disconnected():
                        return
                    if isinstance(chunk, str):
                        yield "delta", {"text": chunk}
                    else:
                        answer = chunk
        else:
            answer = await ask_llm(messages, tools=tools, tool_choice=choice, **model_options)
        if answer is None:
            raise LLMResponseError()
        if answer.usage:
            if total_usage is None:
                total_usage = {key: 0 for key in answer.usage}
            for key, value in answer.usage.items():
                total_usage[key] += value
        if not answer.tool_calls:
            if not answer.reply.strip():
                raise LLMResponseError()
            final = LLMReply(answer.reply, answer.model, total_usage)
            summaries = _document_summaries(messages)
            if summaries and answer.finish_reason not in {None, "stop"}:
                raise LLMResponseError()
            yield "result", AgentResult(final, sources, messages, searched, _document_sources(messages), summaries)
            return
        if not enabled:
            raise LLMResponseError()

        tool_messages.append({
            "role": "assistant", "content": answer.reply or None,
            "tool_calls": [call.as_dict() for call in answer.tool_calls],
        })
        # 中间的工具调用说明不是最终回答；客户端从这里重置正文。
        yield "tool_start", {"name": "", "query": "", "reset": True}
        for call in answer.tool_calls:
            if is_disconnected and await is_disconnected():
                return
            data = None
            if call_count >= MAX_TOOL_CALLS:
                data = {"error": "工具调用次数已达上限，请根据已有结果回答。"}
            else:
                call_count += 1
                if call.name not in names:
                    data = {"error": "该工具未开放，请仅调用系统本轮提供的工具。"}
                else:
                    try:
                        arguments = TOOL_ARGUMENTS[call.name].model_validate_json(call.arguments)
                    except ValidationError:
                        data = {"error": "工具参数无效，请按工具定义修正 query、数量或 file_id；不允许额外字段。"}
                    else:
                        if call.name in {"search_documents", "summarize_document"} and arguments.file_id and str(arguments.file_id) not in file_ids:
                            data = {"error": "该文件不在当前会话范围，只能使用系统提供的文件清单。"}
            if data is None:
                query = arguments.query
                yield "tool_start", {"name": call.name, "query": query, "reset": False}
                key = (call.name, arguments.model_dump_json().casefold())
                if call.name == "summarize_document":
                    if key in seen_summaries:
                        summary = seen_summaries[key]
                    else:
                        summary = None
                        async with aclosing(summarize_document(query, str(arguments.file_id))) as events:
                            async for event_name, event_data in events:
                                if is_disconnected and await is_disconnected():
                                    return
                                if event_name == "progress":
                                    yield "tool_progress", {"name": call.name, **event_data}
                                else:
                                    summary = event_data
                        if not isinstance(summary, SummaryResult):
                            raise LLMResponseError()
                        seen_summaries[key] = summary
                        if summary.usage:
                            if total_usage is None:
                                total_usage = {item: 0 for item in summary.usage}
                            for item, value in summary.usage.items():
                                total_usage[item] += value
                    data = dict(summary.data)
                    results = data["results"]
                elif key in seen_queries:
                    results = seen_queries[key]
                else:
                    if call.name == "web_search":
                        results = await web_search(query, arguments.max_results)
                    else:
                        scope = [str(arguments.file_id)] if arguments.file_id else file_ids
                        results = await search_documents(query, scope, arguments.limit)
                    seen_queries[key] = results
                if call.name == "web_search":
                    searched = True
                    for source in results:
                        if not any(item["url"] == source["url"] for item in sources):
                            sources.append(source)
                    data = {"source_type": "web", "query": query, "results": [
                        {"title": item["title"][:200], "url": item["url"], "content": item["content"][:1000]}
                        for item in results
                    ]}
                elif call.name == "search_documents":
                    data = {"source_type": "document", "query": query, "results": results}
            tool_messages.append({"role": "tool", "tool_call_id": call.id, "content": _json(data)})
            if "results" in data:
                yield "sources", {"sources": sources, "searched": searched, "document_sources": _document_sources(tool_messages), "document_summaries": _document_summaries(tool_messages)}
    raise LLMResponseError()
