"""进程内事件总线.

下载引擎 / 凭证模块只管 ``publish``, 由 ``server.ws`` 订阅后广播给全部前端.
core 不认识 WebSocket, 这是 core 与 server 的唯一耦合点.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class EventKind(str, Enum):
    """事件类型."""

    TASK_STATE = "task_state"
    ITEM_STATE = "item_state"
    PROGRESS = "progress"
    COUNTDOWN = "countdown"
    LOG = "log"
    REPORT = "report"
    AUTH_STATE = "auth_state"
    LOGIN_EVENT = "login_event"
    CONFIG_CHANGED = "config_changed"


@dataclass(slots=True)
class Event:
    """一条事件."""

    kind: EventKind
    payload: dict[str, Any]
    task_id: str | None = None
    item_id: int | None = None
    level: str = "info"
    ts: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        """转为 WS 帧."""
        return {
            "kind": self.kind.value,
            "payload": self.payload,
            "task_id": self.task_id,
            "item_id": self.item_id,
            "level": self.level,
            "ts": self.ts,
        }


class EventBus:
    """一个发布者、多个订阅者的广播总线.

    订阅者各自持有一个有界队列: 慢消费者 (比如卡住的手机浏览器) 只会丢自己的事件,
    不会阻塞下载引擎.
    """

    def __init__(self, queue_size: int = 512, history_size: int = 200) -> None:
        """初始化.

        Args:
            queue_size: 每个订阅者的队列容量.
            history_size: 保留多少条历史事件供迟到的客户端回放.
        """
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._queue_size = queue_size
        self._history: list[Event] = []
        self._history_size = history_size
        self._lock = asyncio.Lock()

    def publish(self, event: Event) -> None:
        """发布一条事件. 永不阻塞、永不抛出."""
        self._history.append(event)
        if len(self._history) > self._history_size:
            del self._history[: len(self._history) - self._history_size]

        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # 丢最旧的一条给新事件腾位置; 慢消费者只影响它自己.
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(event)

    def emit(
        self,
        kind: EventKind,
        payload: dict[str, Any],
        *,
        task_id: str | None = None,
        item_id: int | None = None,
        level: str = "info",
    ) -> None:
        """构造并发布事件的快捷方法."""
        self.publish(Event(kind=kind, payload=payload, task_id=task_id, item_id=item_id, level=level))

    def log(self, message: str, *, level: str = "info", task_id: str | None = None, item_id: int | None = None) -> None:
        """发一条日志事件, 同时写入 Python logging.

        事件也要脱敏 —— 它会经 WS 推到两个前端、还会落进 ``task_events`` 表.
        """
        from .redact import redact_text

        message = redact_text(message)
        logger.log(getattr(logging, level.upper(), logging.INFO), message)
        self.emit(EventKind.LOG, {"message": message}, task_id=task_id, item_id=item_id, level=level)

    def history(self, limit: int | None = None) -> list[Event]:
        """返回最近的历史事件, 供迟到的客户端回放."""
        if limit is None:
            return list(self._history)
        return list(self._history[-limit:])

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[Event]]:
        """订阅事件流."""
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._queue_size)
        async with self._lock:
            self._subscribers.add(queue)
        try:
            yield queue
        finally:
            async with self._lock:
                self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        """当前订阅者数量."""
        return len(self._subscribers)
