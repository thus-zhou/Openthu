"""网络学堂本地助手的第一阶段核心 API。"""

from .core import LearnAssistant
from .errors import LearnAssistantError

__all__ = ["LearnAssistant", "LearnAssistantError"]

__version__ = "0.1.0"
