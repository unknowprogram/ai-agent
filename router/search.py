"""可在 Swagger 中单独调用的联网搜索接口。"""

from fastapi import APIRouter

from core.exception_handlers import ERROR_RESPONSES
from schemas.search import SearchRequest, SearchResponse
from tools.web_search import web_search

router = APIRouter(prefix="/api", tags=["联网搜索"])


@router.post("/search", response_model=SearchResponse, responses=ERROR_RESPONSES)
async def create_search(request: SearchRequest) -> SearchResponse:
    results = await web_search(request.query, request.max_results)
    return SearchResponse(query=request.query, results=results)
