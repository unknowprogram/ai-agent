"""业务异常定义，与路由和模型调用解耦。"""


class LLMConfigError(RuntimeError):
    """模型配置缺失。"""


class LLMResponseError(RuntimeError):
    """模型没有返回可展示的文本。"""


class SearchError(RuntimeError):
    """搜索服务连接失败或返回无效结果。"""


class SearchConfigError(SearchError):
    """搜索密钥缺失。"""


class SearchAuthenticationError(SearchError):
    """搜索密钥无效。"""


class SearchRateLimitError(SearchError):
    """搜索频率或账户额度受限。"""


class SearchTimeoutError(SearchError):
    """搜索服务响应超时。"""


class DatabaseConfigError(RuntimeError):
    """数据库配置无效或缺少认证依赖。"""


class ConversationNotFoundError(RuntimeError):
    """会话不存在或已被删除。"""


class ConversationConflictError(RuntimeError):
    """生成回答期间，同一会话的历史已被另一请求修改。"""

class ContextTooLargeError(RuntimeError):
    """系统提示词和当前问题超过上下文预算。"""

    def __init__(self, *, budget_details: dict | None = None):
        super().__init__("模型输入超过上下文预算。")
        self.budget_details = budget_details

class UnsupportedFileError(RuntimeError):
    """不支持的文件类型。"""


class FileTooLargeError(RuntimeError):
    """文件大小或解析规模超过限制。"""


class FileParseError(RuntimeError):
    """文件损坏、加密或解析失败。"""


class FileTextUnavailableError(RuntimeError):
    """没有提取到可用文字。"""


class DocumentNotFoundError(RuntimeError):
    """文件不存在。"""


class DocumentLimitError(RuntimeError):
    """会话关联文件数量超过限制。"""


class FileDataUnavailableError(RuntimeError):
    """文件元数据存在，但原始表格数据缺失。"""


class SemanticSearchUnavailableError(RuntimeError):
    """向量模型未准备好或无法完成推理。"""
