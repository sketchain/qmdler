"""WebSocket 广播.

任务状态、进度、日志都通过这里推给两个前端. 同时开着 WebUI 和 TUI 时, 任一端的
暂停/取消都会经由同一个 ``EventBus`` 广播出去, 另一端立即可见.

新连上来的客户端先收一帧全量快照, 再收最近的历史事件, 然后才是增量 —— 这样迟到
的客户端不会看到一个空界面.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..core.events import Event
from .context import AppContext

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """事件推送."""
    context: AppContext | None = getattr(websocket.app.state, "context", None)
    if context is None:  # pragma: no cover
        await websocket.close(code=1013)
        return

    await websocket.accept()
    bus = context.bus

    try:
        await websocket.send_json({"kind": "snapshot", "payload": context.snapshot(), "ts": 0})
        for event in bus.history(limit=context.settings.server.event_replay_limit):
            await websocket.send_json(event.as_dict())
    except (WebSocketDisconnect, RuntimeError):
        return

    async with bus.subscribe() as queue:
        pump = asyncio.create_task(_pump(websocket, queue))
        try:
            # 同时读客户端的心跳/关闭帧, 否则断线检测不到.
            while True:
                await websocket.receive_text()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump


async def _pump(websocket: WebSocket, queue: asyncio.Queue[Event]) -> None:
    """把事件队列里的内容写到 WS."""
    try:
        while True:
            event = await queue.get()
            await websocket.send_json(event.as_dict())
    except (WebSocketDisconnect, RuntimeError):
        return
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("WS 推送中断", exc_info=True)
