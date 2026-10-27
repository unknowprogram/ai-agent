"""执行模型自主选择的联网和文档工具，普通和流式聊天共用有限循环。"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass

from pydantic import ValidationError

from agent.llm import LLMReply, ask_llm, stream_llm
from core.exceptions import (
    ContextTooLargeError, FileDataUnavailableError, FileParseError, FileTooLargeError,
    LLMResponseError, SemanticSearchUnavailableError,
)
from storage import documents
from tools.analyze_spreadsheet import SPREADSHEET_TOOL, SPREADSHEET_TYPES, SpreadsheetArguments, analyze_spreadsheet_file
from tools.calculate import CALCULATE_TOOL, CalculateArguments, calculate
from tools.semantic_search import SEMANTIC_TOOL, SemanticSearchArguments, semantic_search
from tools.documents import DOCUMENT_TOOL, DocumentSearchArguments, search_documents
from tools.document_summary import SUMMARY_TOOL, SUMMARY_OUTPUT_TOKENS, DocumentSummaryArguments, SummaryResult, summarize_document
from tools.web_search import SEARCH_TOOL, SearchArguments, web_search
from tools.scrape_webpage import SCRAPE_TOOL, ScrapeArguments, read_webpage
from tools.webpage_summary import WebpageResult

MAX_TOOL_ROUNDS = 2
MAX_TOOL_CALLS = 3


TOOL_ARGUMENTS = {
    "web_search": SearchArguments, "search_documents": DocumentSearchArguments,
    "summarize_document": DocumentSummaryArguments, "semantic_search": SemanticSearchArguments,
    "analyze_spreadsheet": SpreadsheetArguments, "calculate": CalculateArguments,
    "scrape_webpage": ScrapeArguments,
}


async def _tool_events(coroutine, name: str):
    """耗时的本地推理和表格统计也发出心跳，避免浏览器误判为停滞。"""
    task = asyncio.create_task(coroutine)
    try:
        while not task.done():
            done, _ = await asyncio.wait({task}, timeout=15)
            if not done:
                yield "progress", {"name": name, "message": "工具仍在处理，请稍候……"}
        yield "result", await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def _trim_webpage(data: dict) -> bool:
    """先减少附加链接和标题，再缩减正文；来源 URL 始终保持完整。"""
    links = data.get("links", [])
    if links:
        data["links"] = links[:len(links) // 2]
        data["links_truncated"] = True
    elif len(data.get("title", "")) > 100:
        title = data["title"]
        data["title"] = title[:max(100, len(title) // 2)]
    elif len(data.get("content", "")) > 100:
        content = data["content"]
        data["content"] = content[:max(100, len(content) // 2)]
    else:
        return False
    data["truncated"] = True
    return True


def _trim_spreadsheet(data: dict) -> bool:
    """只裁剪展示字段，保持 JSON 有效、统计范围和统计值不变。"""
    for key in ("preview", "numeric_summary", "rows", "sheets", "columns"):
        items = data.get(key)
        if isinstance(items, list) and len(items) > 1:
            data[key] = items[:max(1, len(items) // 2)]
            data["display_truncated"] = True
            return True
    for key in ("preview", "numeric_summary", "rows"):
        for row in data.get(key, []):
            if not isinstance(row, dict):
                continue
            for field, value in row.items():
                if isinstance(value, str) and len(value) > 200:
                    row[field] = value[:max(200, len(value) // 2)]
                    data["display_truncated"] = True
                    return True
            if len(row) > 2:
                for field in list(row)[max(2, len(row) // 2):]:
                    del row[field]
                data["display_truncated"] = True
                return True
    for key in ("dtypes", "missing"):
        fields = data.get(key)
        if isinstance(fields, dict) and len(fields) > 1:
            data[key] = dict(list(fields.items())[:max(1, len(fields) // 2)])
            data["display_truncated"] = True
            return True
    return False


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
            # 中间说明不是最终答案；保留 tool_calls 以维持工具调用协议。
            for message in tool_messages:
                if message["role"] == "assistant" and message.get("tool_calls") and message.get("content"):
                    message["content"] = None
                    changed = True
            if changed:
                continue
            for message in tool_messages:
                if message["role"] != "tool":
                    continue
                data = json.loads(message["content"])
                if data.get("source_type") == "spreadsheet" and _trim_spreadsheet(data):
                    message["content"] = _json(data)
                    changed = True
                    break
                # 网页抓取结果使用顶层 content，而搜索/文档结果使用 results。
                if data.get("source_type") == "webpage":
                    if _trim_webpage(data):
                        message["content"] = _json(data)
                        changed = True
                        break
                    continue

                results = data.get("results", [])
                if not results:
                    continue
                longest = max(results, key=lambda item: len(item.get("content", "")))
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
    file_metadata: list[dict] | None = None,
    is_disconnected: Callable[[], Awaitable[bool]] | None = None,
    question: str = "",
) -> AsyncIterator[tuple[str, dict | AgentResult]]:
    tool_messages = []
    sources = []
    searched = False
    call_count = 0
    seen_queries = {}
    seen_summaries = {}
    total_usage = None
    file_ids = list(file_ids or [])
    # 文件类型来自服务器，模型不能通过自行传入 ID 扩大范围。
    if file_metadata is None:
        file_metadata = await asyncio.to_thread(documents.get_document_metadata, file_ids) if file_ids else []
    metadata = {item["file_id"]: item for item in file_metadata if item["file_id"] in file_ids}
    text_file_ids = [key for key in file_ids if metadata.get(key, {}).get("file_type") in {"txt", "md", "pdf", "docx"}]
    spreadsheet_ids = [key for key in file_ids if metadata.get(key, {}).get("file_type") in SPREADSHEET_TYPES]

    for round_index in range(MAX_TOOL_ROUNDS + 1):
        if is_disconnected and await is_disconnected():
            return
        within_budget = round_index < MAX_TOOL_ROUNDS and call_count < MAX_TOOL_CALLS
        tools = []
        if within_budget and search_mode is not False:
            tools.append(SEARCH_TOOL)
        if within_budget:
            tools.append(SCRAPE_TOOL)
            tools.append(CALCULATE_TOOL)
            if text_file_ids:
                tools.extend([DOCUMENT_TOOL, SUMMARY_TOOL, SEMANTIC_TOOL])
            if spreadsheet_ids:
                tools.append(SPREADSHEET_TOOL)
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
                total_usage[key] = total_usage.get(key, 0) + value
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
                        data = {"error": "工具参数无效，请按对应工具定义修正参数；不允许额外字段。"}
                    else:
                        requested_id = getattr(arguments, "file_id", None)
                        scope = spreadsheet_ids if call.name == "analyze_spreadsheet" else text_file_ids
                        if requested_id and str(requested_id) not in scope:
                            data = {"error": "该文件不在当前会话范围，只能使用系统提供的文件清单。"}
            if data is None:
                query = (
                        getattr(arguments, "query", None)
                        or getattr(arguments, "expression", None)
                        or getattr(arguments,"url",None)
                )
                if query is None:
                    query = metadata[str(arguments.file_id)]["filename"] + " / " + arguments.operation
                yield "tool_start", {"name": call.name, "query": query, "reset": False}
                # 表名、列名、筛选值区分大小写，不能对整个参数 JSON 做 casefold。
                key = (call.name, arguments.model_dump_json())
                if call.name in {"calculate", "semantic_search", "analyze_spreadsheet"}:
                    try:
                        if key in seen_queries:
                            data = seen_queries[key]
                        elif call.name == "calculate":
                            data = {"source_type": "calculation", **calculate(**arguments.model_dump())}
                        else:
                            if call.name == "semantic_search":
                                scope = [str(arguments.file_id)] if arguments.file_id else text_file_ids
                                coroutine = semantic_search(arguments.query, scope, arguments.limit)
                            else:
                                coroutine = analyze_spreadsheet_file(
                                    str(arguments.file_id), **arguments.model_dump(exclude={"file_id"}),
                                )
                            async with aclosing(_tool_events(coroutine, call.name)) as events:
                                async for event_name, event_data in events:
                                    if is_disconnected and await is_disconnected():
                                        return
                                    if event_name == "progress":
                                        yield "tool_progress", event_data
                                    else:
                                        data = event_data
                            if call.name == "semantic_search":
                                data = {"source_type": "document", "query": query, "results": data}
                        seen_queries[key] = data
                    except SemanticSearchUnavailableError:
                        data = {"error": "语义模型未准备好，可改用 search_documents 关键词检索；不能声称语义检索成功。"}
                    except FileDataUnavailableError:
                        data = {"error": "原始表格数据缺失，请用户重新上传文件。"}
                    except FileTooLargeError:
                        data = {"error": "表格大小或解析规模超过限制，请缩小文件。"}
                    except FileParseError:
                        data = {"error": "表格文件解析失败，请检查文件格式。"}
                    except ValueError as exc:
                        data = {"error": str(exc)[:300], "name": call.name}
                    tool_messages.append({"role": "tool", "tool_call_id": call.id, "content": _json(data)})
                    if data.get("source_type") == "document":
                        yield "sources", {"sources": sources, "searched": searched, "document_sources": _document_sources(tool_messages), "document_summaries": _document_summaries(tool_messages)}
                    continue
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
                    try:
                        if call.name == "web_search":
                            results = await web_search(query, arguments.max_results)
                        elif call.name == "scrape_webpage":
                            page = None
                            async with aclosing(read_webpage(arguments.url, question)) as events:
                                async for event_name, event_data in events:
                                    if is_disconnected and await is_disconnected():
                                        return
                                    if event_name == "progress":
                                        yield "tool_progress", event_data
                                    else:
                                        page = event_data
                            if not isinstance(page, WebpageResult):
                                raise LLMResponseError()
                            results = page.data
                            if page.usage:
                                if total_usage is None:
                                    total_usage = {}
                                for item, value in page.usage.items():
                                    total_usage[item] = total_usage.get(item, 0) + value
                        else:
                            scope = [str(arguments.file_id)] if arguments.file_id else text_file_ids
                            results = await search_documents(query, scope, arguments.limit)
                        seen_queries[key] = results
                    except ValueError as exc:
                        data = {"error": str(exc)[:300], "name": call.name}
                        tool_messages.append({
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": _json(data),
                        })
                        continue
                if call.name == "web_search":
                    searched = True
                    for source in results:
                        if not any(item["url"] == source["url"] for item in sources):
                            sources.append(source)
                    data = {"source_type": "web", "query": query, "results": [
                        {"title": item["title"][:200], "url": item["url"], "content": item["content"][:1000]}
                        for item in results
                    ]}
                elif call.name == "scrape_webpage":
                    data = results
                    # 将实际抓取的网页作为来源返回给客户端，避免把网页正文误当作搜索摘要。
                    source = {
                        "title": str(results.get("title", ""))[:200],
                        "url": str(results.get("url", arguments.url)),
                        "content": str(results.get("content", ""))[:1000],
                    }
                    if source["url"] and not any(
                        item.get("url") == source["url"] for item in sources
                    ):
                        sources.append(source)
                elif call.name == "search_documents":
                    data = {"source_type": "document", "query": query, "results": results}
            tool_messages.append({"role": "tool", "tool_call_id": call.id, "content": _json(data)})
            if "results" in data or data.get("source_type") == "webpage":
                yield "sources", {"sources": sources, "searched": searched, "document_sources": _document_sources(tool_messages), "document_summaries": _document_summaries(tool_messages)}
    raise LLMResponseError()
