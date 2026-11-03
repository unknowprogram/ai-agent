"""LangChain 工具适配：复用原业务实现，注入服务端文件范围及进度。"""

import asyncio
from collections.abc import Callable
from contextlib import aclosing
from uuid import UUID

from langchain_core.tools import StructuredTool

from agent.state import AgentContext
from core.exceptions import LLMResponseError
from tools.analyze_spreadsheet import SPREADSHEET_TOOL, SpreadsheetArguments, analyze_spreadsheet_file
from tools.analyze_image import IMAGE_TOOL, ImageArguments, analyze_image_file
from tools.calculate import CALCULATE_TOOL, CalculateArguments, calculate
from tools.document_summary import (
    SUMMARY_EVIDENCE_CHARS, SUMMARY_TOOL, DocumentSummaryArguments, SummaryResult, summarize_document,
)
from tools.documents import DOCUMENT_TOOL, DocumentSearchArguments, search_documents
from tools.scrape_webpage import SCRAPE_TOOL, ScrapeArguments, read_webpage
from tools.web_search import SEARCH_TOOL, SearchArguments, web_search
from tools.webpage_summary import WebpageResult
from tools.generate_image import GENERATE_IMAGE_TOOL, GenerateImageArguments, generate_image
from storage.generated_images import check_schema


def _scope(file_id: UUID | None, allowed_ids: list[str]) -> list[str]:
    """省略 ID 时查询会话可用文件；指定 ID 时再次核对服务端范围。"""
    if file_id is None:
        return list(allowed_ids)
    if str(file_id) not in allowed_ids:
        raise ValueError("该文件不在当前会话范围，只能使用系统提供的文件清单。")
    return [str(file_id)]


def build_tools(context: AgentContext, progress: Callable[[dict], None], *,
                summary_evidence_chars: int = SUMMARY_EVIDENCE_CHARS) -> dict[str, StructuredTool]:
    """注册本次请求可用的工具，闭包注入文件范围、当前问题和进度回调。"""
    # adapter 只转换接口与结果结构，计算、检索和文件解析复用 tools/ 原代码。
    def calculate_adapter(expression: str, decimal_places: int = 28) -> dict:
        return {"source_type": "calculation", **calculate(expression, decimal_places)}

    async def documents_adapter(query: str, limit: int = 4, file_id: UUID | None = None):
        result = await search_documents(query, _scope(file_id, context.text_file_ids), limit)
        if result["retrieval"]["degraded"]:
            progress({"name": "search_documents", "message": result["retrieval"]["warning"]})
        return result

    async def spreadsheet_adapter(file_id: UUID, **kwargs):
        _scope(file_id, context.spreadsheet_ids)
        return await analyze_spreadsheet_file(str(file_id), **kwargs)

    async def summary_adapter(file_id: UUID, query: str, final_answer: bool = True):
        _scope(file_id, context.text_file_ids)
        result = None
        async with aclosing(summarize_document(
            query, str(file_id), final_answer=final_answer, question=context.question,
            evidence_chars=summary_evidence_chars,
        )) as events:
            async for name, data in events:
                await context.check_connected()
                if name == "progress":
                    # 将摘要分段与合并进度发送给图，避免等待期间前端误判超时。
                    progress({"name": "summarize_document", **data})
                else:
                    result = data
        if not isinstance(result, SummaryResult):
            raise LLMResponseError()
        return result

    async def webpage_adapter(url: str):
        result = None
        async with aclosing(read_webpage(url, context.question)) as events:
            async for name, data in events:
                await context.check_connected()
                if name == "progress":
                    progress(data)
                else:
                    result = data
        if not isinstance(result, WebpageResult):
            raise LLMResponseError()
        return result

    async def image_adapter(file_id: UUID, query: str):
        # 先核对会话范围；模型不能访问其他会话的图片。
        _scope(file_id, context.image_file_ids)
        await context.check_connected()
        result = await analyze_image_file(str(file_id), query)
        await context.check_connected()
        return result

    async def generate_image_adapter(prompt: str, size: str = "1024x1024"):
        await context.check_connected()
        await asyncio.to_thread(check_schema)
        await context.check_connected()
        result = await generate_image(prompt, size)
        await context.check_connected()
        return result

    entries = [
        (CALCULATE_TOOL, CalculateArguments, calculate_adapter, False),
        (SCRAPE_TOOL, ScrapeArguments, webpage_adapter, True),
        (GENERATE_IMAGE_TOOL, GenerateImageArguments, generate_image_adapter, True),
    ]
    if context.search_mode is not False:
        entries.append((SEARCH_TOOL, SearchArguments, web_search, True))
    if context.text_file_ids:
        # 文本检索与摘要只对文本文档开放，表格统计使用独立的分析工具。
        entries.extend([
            (DOCUMENT_TOOL, DocumentSearchArguments, documents_adapter, True),
            (SUMMARY_TOOL, DocumentSummaryArguments, summary_adapter, True),
        ])
    if context.spreadsheet_ids:
        entries.append((SPREADSHEET_TOOL, SpreadsheetArguments, spreadsheet_adapter, True))
    if context.image_file_ids:
        # 继续通过 StructuredTool 注册，无需重复添加 @tool 装饰器。
        entries.append((IMAGE_TOOL, ImageArguments, image_adapter, True))
    return {
        # 复用原工具描述和 Pydantic 参数模型，保留字段限制及禁止额外参数的规则。
        definition["function"]["name"]: StructuredTool.from_function(
            name=definition["function"]["name"],
            description=definition["function"]["description"], args_schema=arguments,
            **({"coroutine": function} if asynchronous else {"func": function}),
        )
        for definition, arguments, function, asynchronous in entries
    }
