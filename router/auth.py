"""同源 Cookie 登录、注册与退出。"""

import asyncio

from fastapi import APIRouter, Depends, Request, Response

from schemas.auth import LoginRequest, RegisterRequest, SessionResponse
from service.auth import COOKIE_NAME, SESSION_SECONDS, csrf_token, secure_cookie
from storage import users
from core.auth import get_current_user

router = APIRouter(prefix="/api/auth", tags=["用户登录"])


def _session_response(response: Response, user: dict, token: str) -> dict:
    response.set_cookie(COOKIE_NAME, token, max_age=SESSION_SECONDS, httponly=True,
                        secure=secure_cookie(), samesite="lax", path="/")
    response.headers["Cache-Control"] = "no-store"
    return {"user": user, "csrf_token": csrf_token(token)}


@router.post("/register", status_code=201, response_model=SessionResponse,
             responses={409: {"description": "用户名已被使用"}, 422: {"description": "注册信息不符合要求"}})
async def register(body: RegisterRequest, request: Request, response: Response):
    user, token = await asyncio.to_thread(users.register, body.username, body.password,
                                         previous_token=request.cookies.get(COOKIE_NAME))
    return _session_response(response, user, token)


@router.post("/login", response_model=SessionResponse)
async def login(body: LoginRequest, request: Request, response: Response):
    user, token = await asyncio.to_thread(users.login, body.username, body.password)
    # 登录时轮换会话，防止会话固定；清理本浏览器原有会话。
    previous = request.cookies.get(COOKIE_NAME)
    if previous:
        await asyncio.to_thread(users.delete_session, previous)
    return _session_response(response, user, token)


@router.get("/me", response_model=SessionResponse, dependencies=[Depends(get_current_user)])
async def me(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return {"user": request.state.user, "csrf_token": csrf_token(request.state.session_token)}


@router.post("/logout", status_code=204, dependencies=[Depends(get_current_user)])
async def logout(request: Request):
    await asyncio.to_thread(users.delete_session, request.state.session_token)
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    response.delete_cookie(COOKIE_NAME, path="/", secure=secure_cookie(), httponly=True, samesite="lax")
    return response
