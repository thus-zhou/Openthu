"""可替换 HTTP transport。

生产默认使用 requests 并始终传入 ``verify=True``；测试通过 FakeTransport 注入离线响应，
不需要触网。WebProvider 不直接依赖某个 HTTP 库的响应构造细节。
"""

from __future__ import annotations

from typing import Any, Protocol

from .errors import ProviderNotConfigured, ProviderNetworkError


class TransportResponse(Protocol):
    status_code: int
    url: str
    headers: Any
    text: str
    content: bytes

    def json(self) -> Any:
        ...


class HttpTransport(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> TransportResponse:
        ...


class RequestsTransport:
    """requests.Session 的 TLS 安全包装。"""

    def __init__(self, session: Any | None = None) -> None:
        try:
            import requests
        except ImportError as exc:  # pragma: no cover - requests is installed in target env
            raise ProviderNotConfigured("真实 provider 需要 requests。") from exc
        self.session = session or requests.Session()

    def request(self, method: str, url: str, **kwargs: Any) -> TransportResponse:
        kwargs.setdefault("verify", True)
        try:
            return self.session.request(method, url, **kwargs)
        except Exception as exc:
            raise ProviderNetworkError(f"请求网络学堂失败：{type(exc).__name__}: {exc}") from exc
