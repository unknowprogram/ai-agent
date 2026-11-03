"""LangGraph 的本轮状态与请求上下文；会话原文仍由 MySQL 管理。"""

from collections.abc import Awaitable, Callable
# 原实现：from dataclasses import dataclass
from dataclasses import dataclass, field
from typing import TypedDict

from agent.llm import LLMReply

MAX_TOOL_ROUNDS = 2
MAX_TOOL_CALLS = 3
# 一轮模型可请求多个工具；轮数与调用总数分别限制，不能用图的步数替代。


@dataclass(frozen=True)
class AgentResult:
    """交给服务层的最终结果；服务层提交 MySQL 后再向前端发送 done。"""

    answer: LLMReply
    sources: list[dict[str, str]]
    messages: list[dict]
    searched: bool
    document_sources: list[dict]
    document_summaries: list[dict]
    generated_images: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class AgentContext:
    """请求期间不变的配置，通过 Runtime 注入节点，不混入模型可修改的状态。"""

    # 每轮模型调用都复用现有上下文构建器，统一裁剪历史与工具结果。
    build_messages: Callable[[list[dict], bool], list[dict]]
    search_mode: bool | None
    streaming: bool
    file_metadata: dict[str, dict]
    # 文件范围由服务端解析并按类型划分，不接受模型自行扩展。
    text_file_ids: list[str]
    spreadsheet_ids: list[str]
    question: str
    is_disconnected: Callable[[], Awaitable[bool]] | None = None
    # 默认值兼容原有调用；图片范围仍由服务端确定。
    image_file_ids: list[str] = field(default_factory=list)

    async def check_connected(self) -> None:
        if self.is_disconnected and await self.is_disconnected():
            raise ClientDisconnected()


class ClientDisconnected(Exception):
    """图执行发现浏览器已断开；不返回结果，不保存本轮问答。"""


class AgentState(TypedDict):
    """在 model 与 tools 节点之间传递的本轮数据；不替代 MySQL 会话历史。"""

    # 保留 assistant.tool_calls 与 tool.tool_call_id 的完整配对。
    tool_messages: list[dict]
    sources: list[dict[str, str]]
    generated_images: list[dict]
    searched: bool
    call_count: int
    round_index: int
    # 缓存键是工具名与已校验的参数 JSON，只在本轮请求内复用。
    cache: dict[tuple[str, str], object]
    total_usage: dict[str, int] | None
    answer: LLMReply | None
    # messages 是本次实际发送给模型、已按预算裁剪过的消息。
    messages: list[dict]
    enabled_tools: list[str]
    # 只有模型产生最终正文时才设置 result；工具中间过程不入库。
    result: AgentResult | None


def initial_state() -> AgentState:
    # 每次请求创建独立容器，避免并发请求共享工具缓存与来源。
    return {
        "tool_messages": [], "sources": [], "generated_images": [], "searched": False,
        "call_count": 0, "round_index": 0, "cache": {}, "total_usage": None,
        "answer": None, "messages": [], "enabled_tools": [], "result": None,
    }
