"""统一管理异常、HTTP 状态码、提示文案和处理器注册。"""

import logging
from dataclasses import dataclass
from http import HTTPStatus

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from openai import (
    APIConnectionError, APIError, APITimeoutError, AuthenticationError, RateLimitError,
)
from pymysql import MySQLError

from core.exceptions import (
    LLMConfigError, LLMResponseError, SearchAuthenticationError, SearchConfigError,
    SearchError, SearchRateLimitError, SearchTimeoutError,
    ConversationConflictError, ConversationNotFoundError, DatabaseConfigError,
    UnsupportedFileError,
    FileTooLargeError,
    FileParseError,
    FileTextUnavailableError,
    DocumentNotFoundError,
    DocumentLimitError,
    FileDataUnavailableError,
    SemanticSearchUnavailableError,


)
from schemas.error import ErrorResponse

from core.exceptions import ContextTooLargeError
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ErrorSpec:
    status_code: int
    detail: str


# 异常类型、状态码和对外提示只在此处维护，避免泄露上游原始错误。
ERROR_SPECS: dict[type[Exception], ErrorSpec] = {
    LLMConfigError: ErrorSpec(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "请在 .env 中配置 DeepSeek 密钥、接口地址和模型。",
    ),
    LLMResponseError: ErrorSpec(
        status.HTTP_502_BAD_GATEWAY, "模型服务暂时不可用，请稍后重试。",
    ),
    AuthenticationError: ErrorSpec(
        status.HTTP_502_BAD_GATEWAY, "模型服务认证失败，请检查服务端密钥。",
    ),
    RateLimitError: ErrorSpec(
        status.HTTP_429_TOO_MANY_REQUESTS, "模型服务请求受限，请稍后重试或检查账户额度。",
    ),
    APITimeoutError: ErrorSpec(
        status.HTTP_504_GATEWAY_TIMEOUT, "模型响应超时，请稍后重试。",
    ),
    APIConnectionError: ErrorSpec(
        status.HTTP_502_BAD_GATEWAY, "无法连接模型服务，请检查服务端网络和接口地址。",
    ),
    APIError: ErrorSpec(
        status.HTTP_502_BAD_GATEWAY, "模型服务暂时不可用，请稍后重试。",
    ),
    SearchConfigError: ErrorSpec(
        status.HTTP_503_SERVICE_UNAVAILABLE, "请在 .env 中配置 TAVILY_API_KEY。",
    ),
    SearchAuthenticationError: ErrorSpec(
        status.HTTP_502_BAD_GATEWAY, "搜索服务认证失败，请检查服务端 Tavily 密钥。",
    ),
    SearchRateLimitError: ErrorSpec(
        status.HTTP_429_TOO_MANY_REQUESTS, "搜索请求受限，请稍后重试或检查 Tavily 账户额度。",
    ),
    SearchTimeoutError: ErrorSpec(
        status.HTTP_504_GATEWAY_TIMEOUT, "搜索服务响应超时，请稍后重试。",
    ),
    SearchError: ErrorSpec(
        status.HTTP_502_BAD_GATEWAY, "无法获取搜索结果，请检查服务端网络或稍后重试。",
    ),
    DatabaseConfigError: ErrorSpec(
        status.HTTP_503_SERVICE_UNAVAILABLE, "数据库配置无效，请检查服务端 MySQL 配置和认证依赖。",
    ),
    MySQLError: ErrorSpec(
        status.HTTP_503_SERVICE_UNAVAILABLE, "会话存储暂时不可用，请检查 MySQL 连接、权限及表结构。",
    ),
    ConversationNotFoundError: ErrorSpec(
        status.HTTP_404_NOT_FOUND, "会话不存在或已删除，请开启新对话。",
    ),
    ConversationConflictError: ErrorSpec(
        status.HTTP_409_CONFLICT, "这段会话已被其他请求更新，请重新加载会话后重试。",
    ),
    Exception: ErrorSpec(
        status.HTTP_500_INTERNAL_SERVER_ERROR, "服务内部错误，请稍后重试。",
    ),
    ContextTooLargeError: ErrorSpec(
        status.HTTP_400_BAD_REQUEST,
        "问题与参考资料过多，请缩短问题或缩小本次分析范围后重试。",
    ),
    UnsupportedFileError: ErrorSpec(
        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        "目前支持 TXT、MD、PDF、DOCX、XLSX、XLS、CSV 文件。",
    ),
    FileTooLargeError: ErrorSpec(
        status.HTTP_413_CONTENT_TOO_LARGE,
        "文件大小、页数或解析内容超过限制。",
    ),
    FileParseError: ErrorSpec(
        status.HTTP_400_BAD_REQUEST,
        "文件解析失败，请检查文件是否损坏、加密或格式不正确。",
    ),
    FileTextUnavailableError: ErrorSpec(
        status.HTTP_400_BAD_REQUEST,
        "未提取到可用文字。图片或扫描件请先进行 OCR 识别。",
    ),
    DocumentNotFoundError: ErrorSpec(
        status.HTTP_404_NOT_FOUND,
        "文件不存在，请重新上传。",
    ),
    DocumentLimitError: ErrorSpec(
        status.HTTP_400_BAD_REQUEST,
        "每个会话最多关联 5 个文件。",
    ),
    FileDataUnavailableError: ErrorSpec(
        status.HTTP_400_BAD_REQUEST, "原始表格数据缺失，请重新上传该文件。",
    ),
    SemanticSearchUnavailableError: ErrorSpec(
        status.HTTP_503_SERVICE_UNAVAILABLE, "语义模型未准备好，请配置本地模型或使用关键词检索。",
    ),
}

# Swagger 的错误响应与实际处理共用一份状态码来源。
ERROR_RESPONSES = {
    spec.status_code: {
        "model": ErrorResponse,
        "description": HTTPStatus(spec.status_code).phrase,
    }
    for spec in ERROR_SPECS.values()
}


def resolve_exception(request: Request, exc: Exception) -> ErrorSpec:
    # 按继承关系查找，保证超时等具体异常优先于 APIError 等父类。
    spec = next(ERROR_SPECS[cls] for cls in type(exc).__mro__ if cls in ERROR_SPECS)
    if isinstance(exc, ContextTooLargeError) and exc.budget_details:
        logger.warning("Context budget exceeded: %s", exc.budget_details)
    logger.log(
        logging.ERROR if spec.status_code >= 500 else logging.WARNING,
        "%s %s failed: %s (HTTP %s)",
        request.method, request.url.path, type(exc).__name__, spec.status_code,
    )
    return spec


async def handle_exception(request: Request, exc: Exception) -> JSONResponse:
    spec = resolve_exception(request, exc)
    return JSONResponse(status_code=spec.status_code, content={"detail": spec.detail})


def register_exception_handlers(app: FastAPI) -> None:
    """注册模型、业务及未知异常；保留 FastAPI 的参数校验和 HTTP 异常响应。"""
    for exception_type in ERROR_SPECS:
        app.add_exception_handler(exception_type, handle_exception)
