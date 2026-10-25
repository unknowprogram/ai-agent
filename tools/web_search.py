"""通过 Tavily REST API 搜索网页，无需额外的搜索 SDK。"""

import asyncio
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from dotenv import load_dotenv

from core.exceptions import (
    SearchAuthenticationError, SearchConfigError, SearchError,
    SearchRateLimitError, SearchTimeoutError,
)

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
TAVILY_SEARCH_URL = "https://api.tavily.com/search"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def _search(query: str, max_results: int, api_key: str) -> list[dict[str, str]]:
    payload = json.dumps({
        "query": query,
        "search_depth": "basic",
        "max_results": max_results,
        "include_answer": False,
        "include_raw_content": False,
    }).encode("utf-8")
    request = Request(
        TAVILY_SEARCH_URL,
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise SearchAuthenticationError() from exc
        if exc.code in (429, 432, 433):
            raise SearchRateLimitError() from exc
        if exc.code in (408, 504):
            raise SearchTimeoutError() from exc
        raise SearchError() from exc
    except TimeoutError as exc:
        raise SearchTimeoutError() from exc
    except URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise SearchTimeoutError() from exc
        raise SearchError() from exc
    except OSError as exc:
        raise SearchError() from exc

    if len(raw) > MAX_RESPONSE_BYTES:
        raise SearchError()
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise ValueError("Invalid search response")
        results = []
        for item in data["results"][:max_results]:
            if not isinstance(item, dict):
                raise ValueError("Invalid search result")
            url = item.get("url", "")
            title = item.get("title") or url
            content = item.get("content") or ""
            if not all(isinstance(value, str) for value in (url, title, content)):
                raise ValueError("Invalid search result fields")
            parsed_url = urlsplit(url)
            if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
                raise ValueError("Invalid search result URL")
            results.append({"title": title, "url": url, "content": content[:3000]})
        return results
    except (ValueError, UnicodeDecodeError) as exc:
        raise SearchError() from exc


async def web_search(query: str, max_results: int = 5) -> list[dict[str, str]]:
    """返回标题、链接及摘要；在线程中发起请求，避免阻塞异步接口。"""
    query = query.strip()
    if not 1 <= len(query) <= 400 or not 1 <= max_results <= 10:
        raise ValueError("搜索词须为 1–400 字符，结果数量须为 1–10。")
    api_key = os.getenv("TAVILY_API_KEY", "").strip()
    if not api_key:
        raise SearchConfigError()
    return await asyncio.to_thread(_search, query, max_results, api_key)
