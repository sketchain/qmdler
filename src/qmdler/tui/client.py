"""TUI 的 HTTP + WebSocket 客户端.

调的是与 WebUI 完全相同的一组端点 —— TUI 里没有任何一行下载逻辑.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import websockets

logger = logging.getLogger(__name__)


class ApiClient:
    """REST 客户端."""

    def __init__(self, base_url: str) -> None:
        """初始化."""
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(base_url=f"{self.base_url}/api", timeout=30.0)

    async def close(self) -> None:
        """关闭连接."""
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._http.request(method, path, **kwargs)
        if response.status_code >= 400:
            detail: Any = response.text
            with contextlib.suppress(ValueError):
                detail = response.json().get("detail", detail)
            raise RuntimeError(str(detail))
        if not response.content:
            return None
        return response.json()

    # -- 通用 ---------------------------------------------------------------- #

    async def health(self) -> dict[str, Any]:
        """健康检查."""
        return await self._request("GET", "/health")

    async def snapshot(self) -> dict[str, Any]:
        """全量快照."""
        return await self._request("GET", "/snapshot")

    # -- 登录 ---------------------------------------------------------------- #

    async def auth_state(self) -> dict[str, Any]:
        """登录态."""
        return await self._request("GET", "/auth/state")

    async def profile(self, refresh: bool = False) -> dict[str, Any]:
        """账号信息."""
        return await self._request("GET", "/auth/profile", params={"refresh": refresh})

    async def start_qrcode(self, login_type: str) -> dict[str, Any]:
        """开始扫码登录."""
        return await self._request("POST", "/auth/qrcode", json={"login_type": login_type})

    async def refresh_qrcode(self, session_id: str) -> dict[str, Any]:
        """刷新二维码."""
        return await self._request("POST", f"/auth/qrcode/{session_id}/refresh")

    async def cancel_qrcode(self, session_id: str) -> None:
        """取消扫码会话."""
        await self._request("DELETE", f"/auth/qrcode/{session_id}")

    async def send_phone_code(self, phone: str, country_code: int = 86) -> dict[str, Any]:
        """发送短信验证码."""
        return await self._request("POST", "/auth/phone/code", json={"phone": phone, "country_code": country_code})

    async def verify_phone_code(self, session_id: str, code: str) -> dict[str, Any]:
        """提交短信验证码."""
        return await self._request("POST", "/auth/phone/verify", json={"session_id": session_id, "code": code})

    async def import_credential(self, payload: dict[str, Any]) -> dict[str, Any]:
        """导入凭证."""
        return await self._request("POST", "/auth/credential", json=payload)

    async def import_credential_file(self, path: str) -> dict[str, Any]:
        """从文件导入凭证."""
        return await self._request("POST", "/auth/credential/file", json={"path": path})

    async def logout(self) -> dict[str, Any]:
        """登出."""
        return await self._request("POST", "/auth/logout")

    # -- 来源 ---------------------------------------------------------------- #

    async def created_songlists(self) -> list[dict[str, Any]]:
        """自建歌单."""
        return await self._request("GET", "/sources/created")

    async def fav_songlists(self) -> list[dict[str, Any]]:
        """收藏歌单."""
        return await self._request("GET", "/sources/fav-songlists")

    async def resolve(self, text: str) -> dict[str, Any]:
        """解析分享链接."""
        return await self._request("POST", "/sources/resolve", json={"text": text})

    async def fetch_source(self, payload: dict[str, Any]) -> dict[str, Any]:
        """拉取来源曲目."""
        return await self._request("POST", "/sources/fetch", json=payload)

    # -- 任务 ---------------------------------------------------------------- #

    async def list_tasks(self) -> list[dict[str, Any]]:
        """任务列表."""
        return await self._request("GET", "/tasks")

    async def create_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        """创建任务."""
        return await self._request("POST", "/tasks", json=payload)

    async def list_items(self, task_id: str, limit: int = 1000) -> list[dict[str, Any]]:
        """任务条目."""
        return await self._request("GET", f"/tasks/{task_id}/items", params={"limit": limit})

    async def set_selection(self, task_id: str, item_ids: list[int] | None, selected: bool) -> Any:
        """设置勾选."""
        return await self._request(
            "POST",
            f"/tasks/{task_id}/selection",
            json={"item_ids": item_ids, "selected": selected},
        )

    async def invert_selection(self, task_id: str) -> Any:
        """反选."""
        return await self._request("POST", f"/tasks/{task_id}/selection/invert")

    async def filter_selection(self, task_id: str, keyword: str, select_matched: bool = True) -> Any:
        """按关键词过滤勾选."""
        return await self._request(
            "POST",
            f"/tasks/{task_id}/selection/filter",
            json={"keyword": keyword, "select_matched": select_matched},
        )

    async def start_task(self, task_id: str) -> Any:
        """开始任务."""
        return await self._request("POST", f"/tasks/{task_id}/start")

    async def pause_task(self, task_id: str) -> Any:
        """暂停任务."""
        return await self._request("POST", f"/tasks/{task_id}/pause")

    async def cancel_task(self, task_id: str) -> Any:
        """取消任务."""
        return await self._request("POST", f"/tasks/{task_id}/cancel")

    async def skip_wait(self, task_id: str) -> Any:
        """立即下载下一首."""
        return await self._request("POST", f"/tasks/{task_id}/skip-wait")

    async def retry_failed(self, task_id: str) -> Any:
        """只重试失败项."""
        return await self._request("POST", f"/tasks/{task_id}/retry-failed")

    async def report(self, task_id: str) -> dict[str, Any]:
        """汇总报告."""
        return await self._request("GET", f"/tasks/{task_id}/report")

    # -- 配置 ---------------------------------------------------------------- #

    async def get_config(self) -> dict[str, Any]:
        """当前配置."""
        return await self._request("GET", "/config")

    async def patch_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        """更新配置."""
        return await self._request("PATCH", "/config", json={"patch": patch})

    async def qualities(self) -> dict[str, Any]:
        """音质表."""
        return await self._request("GET", "/config/qualities")

    async def presets(self) -> dict[str, Any]:
        """配置预设."""
        return await self._request("GET", "/config/presets")

    async def activate_preset(self, name: str) -> dict[str, Any]:
        """切换预设."""
        return await self._request("POST", "/config/presets/activate", json={"name": name})

    async def preview_template(self, payload: dict[str, Any]) -> dict[str, Any]:
        """模板预览."""
        return await self._request("POST", "/config/templates/preview", json=payload)

    async def check_path(self, path: str, create: bool = False) -> dict[str, Any]:
        """校验目录."""
        return await self._request("POST", "/config/path/check", json={"path": path, "create": create})

    async def list_dirs(self, path: str) -> dict[str, Any]:
        """列出子目录 (目录选择器补全用)."""
        return await self._request("GET", "/config/path/list", params={"path": path})


class EventStream:
    """WebSocket 事件流, 自动重连."""

    def __init__(self, ws_url: str, on_event: Callable[[dict[str, Any]], Awaitable[None] | None]) -> None:
        """初始化."""
        self._url = ws_url
        self._on_event = on_event
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self.connected = False

    def start(self) -> None:
        """起后台任务."""
        self._task = asyncio.create_task(self._run(), name="qmdler-tui-ws")

    async def stop(self) -> None:
        """停掉后台任务."""
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        backoff = 1.0
        while not self._closed:
            try:
                async with websockets.connect(self._url, ping_interval=25) as socket:
                    self.connected = True
                    backoff = 1.0
                    async for raw in socket:
                        try:
                            frame = json.loads(raw)
                        except ValueError:
                            continue
                        result = self._on_event(frame)
                        if asyncio.iscoroutine(result):
                            await result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("WS 断开, %.0fs 后重连: %s", backoff, exc)
            self.connected = False
            if self._closed:
                return
            await asyncio.sleep(backoff)
            backoff = min(30.0, backoff * 2)
