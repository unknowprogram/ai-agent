"""纯 ASGI 认证中间件；上下文在 SSE 完成后才释放，并传播到 to_thread。"""

import asyncio
import hmac
from urllib.parse import urlsplit

from fastapi import Header, HTTPException, Request, Security
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.security import APIKeyCookie

from core.identity import current_user_id
from service.auth import COOKIE_NAME, csrf_token
from storage.users import get_session

session_cookie = APIKeyCookie(name=COOKIE_NAME, auto_error=False)


def get_current_user(request: Request, session: str | None = Security(session_cookie),
                     x_csrf_token: str | None = Header(default=None, description="修改接口需填写登录或 /api/auth/me 返回的 csrf_token")) -> dict:
    """路由依赖及 OpenAPI 认证说明；实际校验由 ASGI 中间件统一完成。"""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, "请先登录。")
    return user


def check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    source = origin or referer
    if source:
        parsed = urlsplit(source)
        expected = urlsplit(str(request.base_url))
        if parsed.scheme != expected.scheme or parsed.netloc != expected.netloc:
            raise HTTPException(403, "请求来源校验失败。")
    elif request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "请求来源校验失败。")


class AuthenticationMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        if path != "/" and not path.startswith("/api/"):
            return await self.app(scope, receive, send)
        request = Request(scope, receive=receive)
        context_token = None
        try:
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                check_origin(request)
            if path in {"/api/auth/login", "/api/auth/register"}:
                return await self.app(scope, receive, send)
            session_token = request.cookies.get(COOKIE_NAME, "")
            user = await asyncio.to_thread(get_session, session_token) if session_token else None
            if not user:
                if path == "/":
                    return await RedirectResponse("/login", status_code=303)(scope, receive, send)
                raise HTTPException(401, "请先登录。")
            if request.headers.get("x-user-id") and request.headers["x-user-id"] != user["id"]:
                raise HTTPException(401, "账号已切换，请重新加载页面。")
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                supplied = request.headers.get("x-csrf-token", "")
                if not hmac.compare_digest(supplied.encode("utf-8"), csrf_token(session_token).encode("ascii")):
                    raise HTTPException(403, "登录校验已失效，请刷新页面。")
            request.state.user = user
            request.state.session_token = session_token
            context_token = current_user_id.set(user["id"])
        except HTTPException as exc:
            return await JSONResponse({"detail": exc.detail}, status_code=exc.status_code)(scope, receive, send)
        except Exception as exc:
            from core.exception_handlers import handle_exception
            response = await handle_exception(request, exc)
            return await response(scope, receive, send)
        try:
            async def private_send(message):
                if message["type"] == "http.response.start":
                    headers = [(key, value) for key, value in message.get("headers", [])
                               if key.lower() != b"cache-control"]
                    headers.append((b"cache-control", b"private, no-store"))
                    message = {**message, "headers": headers}
                await send(message)

            await self.app(scope, receive, private_send)
        finally:
            if context_token is not None:
                current_user_id.reset(context_token)
