"""领域错误类型。

这些异常会被 CLI 和 MCP 入口转换为结构化错误，不会把堆栈或会话数据暴露给用户。
"""


class LearnAssistantError(Exception):
    """所有可预期的助手错误的基类。"""


class ValidationError(LearnAssistantError):
    """输入参数或本地文件不符合要求。"""


class AuthenticationRequired(LearnAssistantError):
    """真实提供方需要用户在受控浏览器中完成登录。"""


class ProviderNotConfigured(LearnAssistantError):
    """真实提供方尚未配置网络学堂接口适配器。"""


class ProviderNetworkError(LearnAssistantError):
    """访问真实提供方时发生网络层错误。"""


class InterfaceDriftError(LearnAssistantError):
    """远端响应与已知接口契约不一致，禁止静默使用 mock 数据。"""


class ProviderRateLimited(InterfaceDriftError):
    """上游明确返回 HTTP 429。"""


class ProviderMaintenance(InterfaceDriftError):
    """上游明确返回服务维护页面。"""


class UnsafeDestinationError(ValidationError):
    """下载目录或相对路径不安全。"""


class DownloadRejectedError(LearnAssistantError):
    """下载内容疑似登录页或错误页，而不是课程文件。"""
