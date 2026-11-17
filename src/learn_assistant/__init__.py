"""网络学堂本地助手的第一阶段核心 API。"""

from .core import LearnAssistant
from .errors import LearnAssistantError

__all__ = ["LearnAssistant", "LearnAssistantError"]

# One product version for the whole distribution; see thu_assistant.version.
from thu_assistant.version import __version__  # noqa: E402  (after the API exports)
