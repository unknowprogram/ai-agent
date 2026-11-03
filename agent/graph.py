"""LangGraph 模型与工具节点：承接原 runner 的预算、缓存和引用规则。"""

import asyncio
import json
from contextlib import aclosing
from copy import deepcopy

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from pydantic import ValidationError

from agent.llm import LLMReply, ask_llm, stream_llm
from agent.state import AgentContext, AgentResult, AgentState, MAX_TOOL_CALLS, MAX_TOOL_ROUNDS
from agent.tool_registry import build_tools
from core.exceptions import (
    ContextTooLargeError, FileDataUnavailableError, FileParseError, FileTooLargeError,
    LLMResponseError, SemanticSearchUnavailableError,
)
from service.context import SUMMARY_CONTEXT_CHARS
from tools.document_summary import SUMMARY_EVIDENCE_CHARS, SUMMARY_OUTPUT_TOKENS, SummaryResult
from tools.webpage_summary import WebpageResult
from tools.analyze_image import ImageAnalysisResult


def _json(data) -> str:
    return json.dumps(data, ensure_ascii=False)


def _merge_document_sources(*groups: list[dict]) -> list[dict]:
    sources = {}
    for group in groups:
        for item in group:
            key = (item["file_id"], item["chunk_index"])
            previous = sources.get(key)
            if previous is None or len(item["content"]) > len(previous["content"]):
                sources[key] = item
    return list(sources.values())


def _document_sources(messages: list[dict]) -> list[dict]:
    results = []
    for message in messages:
        if message["role"] != "tool":
            continue
        data = json.loads(message["content"])
        # 原实现：if data.get("source_type") not in {"document", "document_summary"}:
        if data.get("source_type") not in {"document", "document_summary", "image"}:
            continue
        results.extend(data.get("results", []))
    return _merge_document_sources(results)


def _document_summaries(messages: list[dict], cache: dict | None = None) -> list[dict]:
    summaries = {}
    for message in messages:
        if message["role"] != "tool":
            continue
        data = json.loads(message["content"])
        if data.get("source_type") == "document_summary":
            summaries[(data["file_id"], data["query"])] = {
                key: data.get(key, []) if key == "sections" else data[key]
                for key in ("file_id", "filename", "query", "summary", "coverage", "sections")
            }
    # 模型只需要摘要要点；完整主题清单从本轮工具产物恢复给界面。
    for result in (cache or {}).values():
        if isinstance(result, SummaryResult):
            data = result.data
            key = data["file_id"], data["query"]
            if key in summaries:
                summaries[key] = {field: deepcopy(data[field]) for field in
                                  ("file_id", "filename", "query", "summary", "coverage", "sections")}
    return list(summaries.values())


def _trim_webpage(data: dict) -> bool:
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


def _fit_messages(build_messages, tool_messages: list[dict], searched: bool) -> list[dict]:
    """沿用原上下文裁剪规则，保留工具 ID 配对、统计值和完整来源 URL。"""
    while True:
        try:
            return build_messages(tool_messages, searched)
        except ContextTooLargeError:
            changed = False
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


def _add_usage(total: dict[str, int] | None, usage: dict[str, int] | None):
    if usage is None:
        return total
    combined = dict(total or {})
    for key, value in usage.items():
        combined[key] = combined.get(key, 0) + value
    return combined


async def model_node(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    """准备上下文、调用模型；输出工具请求或最终回答，执行路径交给条件边选择。"""
    context = runtime.context
    await context.check_connected()
    writer = get_stream_writer()
    tool_messages = deepcopy(state["tool_messages"])
    # 第一步：按业务预算开放工具。耗尽后仍执行一次无工具的最终模型调用。
    within_budget = state["round_index"] < MAX_TOOL_ROUNDS and state["call_count"] < MAX_TOOL_CALLS
    registry = build_tools(context, lambda data: writer(("tool_progress", data))) if within_budget else {}
    choice = (
        {"type": "function", "function": {"name": "web_search"}}
        if "web_search" in registry and context.search_mode is True and state["round_index"] == 0
        else "auto"
    )
    if not registry and tool_messages:
        last_result = json.loads(tool_messages[-1]["content"])
        last_result["tool_budget"] = (
            "本轮外层工具调用已结束。请根据已有摘要及覆盖统计回答；已扫描完成的文件无需再次逐段检索。"
            if _document_summaries(tool_messages) else
            "本轮工具调用次数已达上限，请根据已有结果回答并说明资料不足的部分。"
        )
        tool_messages[-1]["content"] = _json(last_result)
    # 第二步：复用历史与工具结果的裁剪规则，再确定摘要回答的输出上限。
    messages = _fit_messages(context.build_messages, tool_messages, state["searched"])
    options = {"max_tokens": SUMMARY_OUTPUT_TOKENS} if _document_summaries(tool_messages) else {}
    tools = list(registry.values()) or None
    answer = None
    # 第三步：只通过 custom 事件转发主回答正文，隔离摘要内部的模型输出。
    if context.streaming:
        async with aclosing(stream_llm(messages, tools=tools, tool_choice=choice, **options)) as chunks:
            async for chunk in chunks:
                await context.check_connected()
                if isinstance(chunk, str):
                    writer(("delta", {"text": chunk}))
                else:
                    answer = chunk
    else:
        answer = await ask_llm(messages, tools=tools, tool_choice=choice, **options)
    await context.check_connected()
    if not isinstance(answer, LLMReply):
        raise LLMResponseError()
    usage = _add_usage(state["total_usage"], answer.usage)
    update = {
        "answer": answer, "messages": messages, "tool_messages": tool_messages,
        "total_usage": usage, "enabled_tools": list(registry),
    }
    if not answer.tool_calls:
        # 第四步：组装最终结果；数据库保存由 service/chat.py 在图外完成。
        if not answer.reply.strip():
            raise LLMResponseError()
        summaries = _document_summaries(messages, state["cache"])
        if summaries and answer.finish_reason not in {None, "stop"}:
            raise LLMResponseError()
        update["result"] = AgentResult(
            LLMReply(answer.reply, answer.model, usage, finish_reason=answer.finish_reason),
            state["sources"], messages, state["searched"],
            _merge_document_sources(_document_sources(messages), state["summary_sources"]), summaries,
            generated_images=deepcopy(state["generated_images"]),
        )
    elif not registry:
        raise LLMResponseError()
    return update


async def _invoke_tool(tool, arguments: dict, context: AgentContext, progress):
    # 原说明：调用 LangChain 工具，为本地推理和表格统计补充心跳并清理取消的任务。
    """调用工具，为本地推理、表格统计和视觉识别补充心跳及取消处理。"""
    # 原实现：if tool.name not in {"semantic_search", "analyze_spreadsheet"}:
    if tool.name not in {"search_documents", "semantic_search", "analyze_spreadsheet", "analyze_image", "generate_image"}:
        return await tool.ainvoke(arguments)
    task = asyncio.create_task(tool.ainvoke(arguments))
    try:
        while not task.done():
            done, _ = await asyncio.wait({task}, timeout=15)
            await context.check_connected()
            if not done:
                progress({
                    "name": tool.name,
                    "message": "图片仍在生成，请稍候……" if tool.name == "generate_image" else "工具仍在处理，请稍候……",
                })
        return await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def _summary_evidence_budget(state: AgentState) -> int:
    """按当前问题与本轮摘要数量分配素材预算，历史可由上下文构建器裁剪。"""
    answer = state["answer"]
    count = sum(call.name == "summarize_document" for call in answer.tool_calls) if answer else 0
    if not count:
        return SUMMARY_EVIDENCE_CHARS
    current = next((item["content"] for item in reversed(state["messages"]) if item["role"] == "user"), "")
    fixed = len(current) + sum(len(item["content"]) for item in state["messages"] if item["role"] == "system")
    previous = sum(len(item.get("content") or "") + len(_json(item.get("tool_calls", [])))
                   for item in state["tool_messages"])
    calls = sum(len(call.arguments) + 128 for call in answer.tool_calls)
    # 每份文件预留文件名、查询、完整覆盖统计及工具消息协议的空间。
    available = SUMMARY_CONTEXT_CHARS - fixed - previous - calls - 1800 * count
    return max(1000, min(SUMMARY_EVIDENCE_CHARS, available // count))


async def tools_node(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    """校验并顺序执行本轮工具，维护缓存、用量、来源与配对的工具消息。"""
    context = runtime.context
    await context.check_connected()
    writer = get_stream_writer()
    progress = lambda data: writer(("tool_progress", data))
    registry = build_tools(context, progress, summary_evidence_chars=_summary_evidence_budget(state))
    tool_messages = deepcopy(state["tool_messages"])
    sources = deepcopy(state["sources"])
    summary_sources = deepcopy(state["summary_sources"])
    generated_images = deepcopy(state["generated_images"])
    cache = dict(state["cache"])
    call_count, searched, usage = state["call_count"], state["searched"], state["total_usage"]
    answer = state["answer"]
    if answer is None:
        raise LLMResponseError()
    # 工具选择就是意图判断。组合调用及已有工具结果时仅提取素材，避免重复成文或提前结束。
    can_finish_summary = len(answer.tool_calls) == 1 and not state["tool_messages"]
    direct_reply = None
    tool_messages.append({
        "role": "assistant", "content": answer.reply or None,
        "tool_calls": [call.as_dict() for call in answer.tool_calls],
    })
    writer(("tool_start", {"name": "", "query": "", "reset": True}))
    for call in answer.tool_calls:
        await context.check_connected()
        data = None
        # 先检查次数、开放列表、参数结构和文件范围，全部通过才执行工具。
        if call_count >= MAX_TOOL_CALLS:
            data = {"error": "工具调用次数已达上限，请根据已有结果回答。"}
        else:
            call_count += 1
            if call.name not in state["enabled_tools"] or call.name not in registry:
                data = {"error": "该工具未开放，请仅调用系统本轮提供的工具。"}
            else:
                tool = registry[call.name]
                try:
                    arguments = tool.args_schema.model_validate_json(call.arguments)
                except ValidationError:
                    data = {"error": "工具参数无效，请按对应工具定义修正参数；不允许额外字段。"}
                else:
                    requested_id = getattr(arguments, "file_id", None)
                    # 原实现：scope = context.spreadsheet_ids if call.name == "analyze_spreadsheet" else context.text_file_ids
                    if call.name == "analyze_spreadsheet":
                        scope = context.spreadsheet_ids
                    elif call.name == "analyze_image":
                        scope = context.image_file_ids
                    else:
                        scope = context.text_file_ids
                    if requested_id and str(requested_id) not in scope:
                        data = {"error": "该文件不在当前会话范围，只能使用系统提供的文件清单。"}
                    if (
                        call.name == "generate_image" and generated_images
                        and (call.name, arguments.model_dump_json()) not in cache
                    ):
                        data = {"error": "本轮已经生成一张图片。如需重新生成，请在下一条消息中提出。"}
                    if call.name == "summarize_document" and not can_finish_summary:
                        arguments = arguments.model_copy(update={"final_answer": False})
        if data is None:
            query = (
                getattr(arguments, "query", None) or getattr(arguments, "expression", None)
                or getattr(arguments, "prompt", None)
                or getattr(arguments, "url", None)
            )
            if query is None:
                query = context.file_metadata[str(arguments.file_id)]["filename"] + " / " + arguments.operation
            display_query = query[:160] + "…" if call.name == "generate_image" and len(query) > 160 else query
            writer(("tool_start", {"name": call.name, "query": display_query, "reset": False}))
            # 参数保留大小写，避免把不同表名、列名或筛选值误认为相同调用。
            key = (call.name, arguments.model_dump_json())
            try:
                cached = key in cache
                result = cache[key] if cached else await _invoke_tool(
                    tool, arguments.model_dump(), context, progress,
                )
                await context.check_connected()
                if not cached:
                    cache[key] = result
                    # 原实现：if isinstance(result, (SummaryResult, WebpageResult)):
                    if isinstance(result, (SummaryResult, WebpageResult, ImageAnalysisResult)):
                        # 缓存命中没有产生新模型请求，摘要用量只在首次执行时累计。
                        usage = _add_usage(usage, result.usage)
                if call.name == "web_search":
                    searched = True
                    for source in result:
                        if not any(item["url"] == source["url"] for item in sources):
                            sources.append(dict(source))
                    data = {"source_type": "web", "query": query, "results": [
                        {"title": item["title"][:200], "url": item["url"], "content": item["content"][:1000]}
                        for item in result
                    ]}
                # 原实现：elif isinstance(result, (SummaryResult, WebpageResult)):
                elif isinstance(result, (SummaryResult, WebpageResult, ImageAnalysisResult)):
                    data = deepcopy(result.data)
                    if isinstance(result, SummaryResult):
                        summary_sources = _merge_document_sources(summary_sources, data.pop("results", []))
                        data.pop("sections", None)
                        data["final_answer"] = result.answer is not None
                        if can_finish_summary and result.answer is not None:
                            direct_reply = result.answer
                    if isinstance(result, WebpageResult):
                        source = {
                            "title": str(data.get("title", ""))[:200],
                            "url": str(data.get("url", arguments.url)),
                            "content": str(data.get("content", ""))[:1000],
                        }
                        if source["url"] and not any(item["url"] == source["url"] for item in sources):
                            sources.append(source)
                else:
                    data = deepcopy(result)
                if data.get("source_type") == "generated_image":
                    for image in data["images"]:
                        if not any(item["image_id"] == image["image_id"] for item in generated_images):
                            generated_images.append(deepcopy(image))
                    # 原件已保存，此事件允许前端在最终文字回复前展示图片。
                    writer(("generated_images", {"generated_images": deepcopy(generated_images)}))
                    # 完整描述及生图用量留在独立结果中，不重复挤占文字模型上下文。
                    data["images"] = [
                        {key: image[key] for key in ("image_id", "url", "model", "width", "height", "watermark")}
                        for image in data["images"]
                    ]
                    data["message"] = "图片已生成并展示在界面，可预览和下载。简短说明完成即可，不要推测图片细节。"
            except SemanticSearchUnavailableError:
                data = {"error": "语义模型未准备好，可改用 search_documents 关键词检索；不能声称语义检索成功。"}
            except (FileDataUnavailableError, FileTooLargeError, FileParseError) as exc:
                if call.name not in {"calculate", "semantic_search", "analyze_spreadsheet"}:
                    raise
                messages = {
                    FileDataUnavailableError: "原始表格数据缺失，请用户重新上传文件。",
                    FileTooLargeError: "表格大小或解析规模超过限制，请缩小文件。",
                    FileParseError: "表格文件解析失败，请检查文件格式。",
                }
                data = {"error": messages[type(exc)]}
            except ValueError as exc:
                if call.name == "summarize_document":
                    raise
                data = {"error": str(exc)[:300], "name": call.name}
        tool_messages.append({"role": "tool", "tool_call_id": call.id, "content": _json(data)})
        # 即使工具被拒绝，也回传具有相同 ID 的错误消息，保持模型工具协议完整。
        if "results" in data or data.get("source_type") in {"webpage", "document_summary"}:
            writer(("sources", {
                "sources": deepcopy(sources), "searched": searched,
                "document_sources": _merge_document_sources(_document_sources(tool_messages), summary_sources),
                "document_summaries": _document_summaries(tool_messages, cache),
            }))
    update = {
        "tool_messages": tool_messages, "sources": sources, "summary_sources": summary_sources,
        "generated_images": generated_images, "searched": searched,
        "call_count": call_count, "round_index": state["round_index"] + 1,
        "cache": cache, "total_usage": usage,
    }
    if direct_reply is not None:
        if direct_reply.finish_reason not in {None, "stop"} or direct_reply.tool_calls or not direct_reply.reply.strip():
            raise LLMResponseError()
        final_answer = LLMReply(direct_reply.reply, direct_reply.model, usage, finish_reason=direct_reply.finish_reason)
        messages = [*state["messages"], *tool_messages]
        if context.streaming:
            writer(("delta", {"text": final_answer.reply}))
        update.update({
            "answer": final_answer, "messages": messages,
            "result": AgentResult(
                final_answer, sources, messages, searched,
                _merge_document_sources(_document_sources(tool_messages), summary_sources),
                _document_summaries(tool_messages, cache), generated_images=generated_images,
            ),
        })
    return update


def route_after_model(state: AgentState) -> str:
    """模型提出工具请求就进入 tools，否则结束图执行。"""
    return "tools" if state["answer"] and state["answer"].tool_calls else "end"


def route_after_tools(state: AgentState) -> str:
    return "end" if state["result"] is not None else "model"


def build_graph():
    """普通任务循环调用模型和工具；单份摘要从 tools 直接进入 END。"""
    builder = StateGraph(AgentState, context_schema=AgentContext)
    builder.add_node("model", model_node)
    builder.add_node("tools", tools_node)
    builder.add_edge(START, "model")
    builder.add_conditional_edges("model", route_after_model, {"tools": "tools", "end": END})
    builder.add_conditional_edges("tools", route_after_tools, {"model": "model", "end": END})
    return builder.compile()


AGENT_GRAPH = build_graph()
# 编译后的图可以复用；每次调用分别提供 initial_state 和 AgentContext。
