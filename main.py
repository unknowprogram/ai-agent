"""应用入口：python main.py 或 uvicorn main:app --reload。"""

from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from core.exception_handlers import register_exception_handlers
from router.chat import router as chat_router
from router.search import router as search_router
from router.conversations import router as conversations_router
from router.files import router as files_router
from router.images import router as images_router
from router.auth import router as auth_router
from core.auth import AuthenticationMiddleware, get_current_user

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="智能助手", version="0.1.0")
register_exception_handlers(app)
app.add_middleware(AuthenticationMiddleware)
app.include_router(auth_router)
app.include_router(chat_router, dependencies=[Depends(get_current_user)])
app.include_router(search_router, dependencies=[Depends(get_current_user)])
app.include_router(conversations_router, dependencies=[Depends(get_current_user)])
app.include_router(files_router, dependencies=[Depends(get_current_user)])
app.include_router(images_router, dependencies=[Depends(get_current_user)])
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(BASE_DIR / "static" / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/login", include_in_schema=False)
async def login_page():
    return FileResponse(BASE_DIR / "static" / "login.html", headers={"Cache-Control": "no-store"})


@app.get("/register", include_in_schema=False)
async def register_page():
    return RedirectResponse("/login#register", status_code=303, headers={"Cache-Control": "no-store"})


@app.get("/health", tags=["状态"])
async def health():
    """仅检查应用存活，不发起收费的模型请求。"""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
