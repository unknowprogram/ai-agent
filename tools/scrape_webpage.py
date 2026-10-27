
"""网页抓取工具：获取网页 HTML 并提取可阅读正文。"""

import asyncio
import ipaddress
import socket
from contextlib import aclosing
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field

from tools.webpage_summary import MAX_WEBPAGE_TEXT_CHARS, summarize_webpage


# 单次最多读取 2 MB，避免响应过大。
MAX_HTML_BYTES = 2 * 1024 * 1024

# 正文留在工具处理流程内，分段归纳后才进入聊天上下文。
MAX_CONTENT_CHARS = MAX_WEBPAGE_TEXT_CHARS

# 只允许常见网页端口。
ALLOWED_PORTS = {80, 443}

USER_AGENT = (
    "Mozilla/5.0 (compatible; AI-Agent-WebReader/1.0)"
)


class ScrapeArguments(BaseModel):
    """大模型调用网页抓取工具时的参数。"""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(
        min_length=8,
        max_length=2048,
        description="需要抓取的完整 HTTP 或 HTTPS 网页 URL",
    )


SCRAPE_TOOL = {
    "type": "function",
    "function": {
        "name": "scrape_webpage",
        "description": (
            "抓取指定网页并提取正文。"
            "当用户提供具体网页 URL，或需要读取搜索结果中的"
            "网页详细内容时调用。长网页会分段阅读后返回摘要及覆盖范围。不能用它代替互联网搜索。"
        ),
        "parameters": ScrapeArguments.model_json_schema(),
    },
}


def _validate_url(url: str) -> str:
    """校验 URL，拒绝本机、内网和非 HTTP(S) 地址。"""

    url = url.strip()
    parsed = urlsplit(url)

    if parsed.scheme not in {"http", "https"}:
        raise ValueError("只允许 HTTP 或 HTTPS 网页。")

    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("网页 URL 格式无效。")

    if parsed.port is not None and parsed.port not in ALLOWED_PORTS:
        raise ValueError("不允许访问该端口。")

    host = parsed.hostname.rstrip(".").lower()

    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("不允许访问本机地址。")

    # 解析域名对应的 IP，拒绝内网、环回及其他非公网地址。
    try:
        addresses = socket.getaddrinfo(
            host,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise ValueError("无法解析网页域名。") from exc

    if not addresses:
        raise ValueError("网页域名没有可用地址。")

    for item in addresses:
        ip = ipaddress.ip_address(item[4][0])


        # 显式拒绝回环、私有、链路本地、保留、多播及未指定地址，防止 SSRF
        if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
                or ip.is_unspecified
        ):
            raise ValueError("不允许访问内网或非公网 IP 地址。")

    return url


def _extract_text(html: bytes, content_type: str) -> dict:
    """解析 HTML，提取标题、正文和页面链接。"""

    if "html" not in content_type.lower():
        raise ValueError("目标地址返回的不是 HTML 页面。")

    soup = BeautifulSoup(html, "html.parser")

    title = (
        soup.title.get_text(" ", strip=True)
        if soup.title
        else ""
    )

    # 移除通常不属于文章正文的元素。
    for element in soup.select(
        "script, style, noscript, iframe, "
        "svg, nav, footer, header, aside, form"
    ):
        element.decompose()

    # 优先提取文章主体，找不到再退回整个页面。
    main = (
        soup.find("article")
        or soup.find("main")
        or soup.body
        or soup
    )

    content = main.get_text("\n", strip=True)

    # 清除空白行，减少传给大模型的无效文本。
    lines = [
        line.strip()
        for line in content.splitlines()
        if line.strip()
    ]
    content = "\n".join(lines)

    links = []
    for item in main.find_all("a", href=True)[:30]:
        links.append({
            "text": item.get_text(" ", strip=True)[:200],
            "href": item["href"][:2048],
        })

    return {
        "title": title[:300],
        "content": content[:MAX_CONTENT_CHARS],
        "extracted_chars": len(content),
        "links": links,
        "truncated": len(content) > MAX_CONTENT_CHARS,
    }


def _scrape(url: str) -> dict:
    """同步抓取实现，由异步包装函数在线程中调用。"""

    url = _validate_url(url)

    try:
        # 不自动跟随重定向，避免跳转到未经校验的内网地址。
        with requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=(5, 15),
            stream=True,
            allow_redirects=False,
        ) as response:

            if 300 <= response.status_code < 400:
                raise ValueError(
                    "网页发生重定向，请使用最终网页 URL 重试。"
                )

            response.raise_for_status()

            content_type = response.headers.get(
                "Content-Type", ""
            )

            if "html" not in content_type.lower():
                raise ValueError("目标地址返回的不是 HTML 页面。")

            chunks = []
            total_bytes = 0

            for chunk in response.iter_content(8192):
                if not chunk:
                    continue

                total_bytes += len(chunk)

                if total_bytes > MAX_HTML_BYTES:
                    raise ValueError("网页内容超过 2 MB 限制。")

                chunks.append(chunk)

            html = b"".join(chunks)

            # 尽量尊重网页声明的编码。
            encoding = response.encoding or "utf-8"
            text = html.decode(encoding, errors="replace")

            return {
                "source_type": "webpage",
                "url": url,
                **_extract_text(
                    text.encode("utf-8"),
                    content_type,
                ),
            }

    except requests.Timeout as exc:
        raise ValueError("抓取网页超时，请稍后重试。") from exc
    except requests.RequestException as exc:
        raise ValueError("网页请求失败，请检查 URL 或网络。") from exc


async def read_webpage(url: str, question: str = ""):
    """报告读取、归纳与合并进度，关闭生成器即取消摘要请求。"""
    yield "progress", {"name": "scrape_webpage", "stage": "fetching", "message": "正在读取网页内容……"}
    data = await asyncio.to_thread(_scrape, url)
    async with aclosing(summarize_webpage(data, question)) as events:
        async for event in events:
            yield event


async def scrape_webpage(url: str, question: str = "") -> dict:
    """兼容独立调用，返回已经适配上下文长度的阅读结果。"""
    async with aclosing(read_webpage(url, question)) as events:
        async for event, value in events:
            if event == "result":
                return value.data
