"""创建当前运行模式的数据提供方。"""

from __future__ import annotations

import os

from .providers.mock import MockProvider
from .providers.web import WebProvider


def create_provider(name: str | None = None):
    selected = (name or os.environ.get("LEARN_ASSISTANT_PROVIDER") or "unavailable").casefold()
    if selected == "mock":
        return MockProvider()
    if selected in {"web", "live", "real"}:
        return WebProvider()
    # 默认不伪装成已连网；WebProvider 会在真正访问数据时给出明确的未配置错误。
    return WebProvider()
