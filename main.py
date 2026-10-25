"""应用入口：python main.py 或 uvicorn main:app --reload。"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from core.exception_handlers import register_exception_handlers
from router.chat import router as chat_router
from router.search import router as search_router
from router.conversations import router as conversations_router
from router.files import router as files_router

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="智能助手", version="0.1.0")
register_exception_handlers(app)
app.include_router(chat_router)
app.include_router(search_router)
app.include_router(conversations_router)
app.include_router(files_router)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/health", tags=["状态"])
async def health():
    """仅检查应用存活，不发起收费的模型请求。"""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
