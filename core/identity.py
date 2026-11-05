"""请求用户上下文；ASGI 中间件覆盖完整响应生命周期（包括 SSE）。"""

from contextvars import ContextVar

from fastapi import HTTPException

current_user_id: ContextVar[str | None] = ContextVar("current_user_id", default=None)


def require_user_id() -> str:
    user_id = current_user_id.get()
    if not user_id:
        raise HTTPException(401, "请先登录。")
    return user_id
