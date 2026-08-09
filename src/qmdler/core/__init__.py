"""qmdler 核心.

下载引擎、任务调度、元数据处理、文件名生成、持久化都在这里.

**这个包不得 import ``qmdler.server`` / ``qmdler.web`` / ``qmdler.tui``.**
两个前端都是纯客户端, 通过 HTTP/WS 连同一个后端.
"""

from .events import Event, EventBus, EventKind
from .models import ItemStatus, SongEntry, SourceType, SubStatus, TaskStatus

__all__ = [
    "Event",
    "EventBus",
    "EventKind",
    "ItemStatus",
    "SongEntry",
    "SourceType",
    "SubStatus",
    "TaskStatus",
]
