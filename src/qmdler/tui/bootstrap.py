"""后端探测与自动拉起.

TUI 是纯客户端. 启动时先探测后端: 探不到就在子进程里拉起一个本地服务,
对用户透明; 退出时若是自己拉起的就一并收掉.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..core.config import paths

logger = logging.getLogger(__name__)

STARTUP_TIMEOUT = 30.0


@dataclass
class Backend:
    """一个可用的后端."""

    base_url: str
    #: 由本进程拉起的子进程; 复用已有后端时为 None.
    process: subprocess.Popen[bytes] | None = None

    @property
    def spawned(self) -> bool:
        """是否是本进程拉起的."""
        return self.process is not None

    @property
    def ws_url(self) -> str:
        """WebSocket 地址."""
        return self.base_url.replace("http://", "ws://").replace("https://", "wss://") + "/ws"

    async def shutdown(self) -> None:
        """退出时收掉自己拉起的后端."""
        if self.process is None:
            return
        self.process.terminate()
        try:
            await asyncio.get_running_loop().run_in_executor(None, self.process.wait, 10)
        except Exception:
            with contextlib.suppress(Exception):
                self.process.kill()
        _clear_endpoint()


async def probe(base_url: str, timeout: float = 2.0) -> bool:
    """探测一个地址上是否已有 qmdler 后端."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{base_url}/api/health")
            return response.status_code == 200 and response.json().get("app") == "qmdler"
    except (httpx.HTTPError, ValueError):
        return False


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _endpoint_path() -> Path:
    return paths.endpoint_file()


def _read_endpoint() -> str:
    path = _endpoint_path()
    if not path.exists():
        return ""
    try:
        return str(json.loads(path.read_text("utf-8")).get("base_url", ""))
    except (OSError, ValueError):
        return ""


def _write_endpoint(base_url: str, pid: int) -> None:
    path = _endpoint_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"base_url": base_url, "pid": pid}), "utf-8")


def _clear_endpoint() -> None:
    _endpoint_path().unlink(missing_ok=True)


async def ensure_backend(
    explicit_url: str = "",
    *,
    host: str = "127.0.0.1",
    port: int = 8770,
    autostart: bool = True,
) -> Backend:
    """返回一个可用的后端.

    顺序: 显式指定 → 上次记录的地址 → 配置里的默认端口 → 拉起新的.

    Raises:
        RuntimeError: 后端拉不起来.
    """
    if explicit_url:
        url = explicit_url.rstrip("/")
        if await probe(url):
            return Backend(base_url=url)
        raise RuntimeError(f"指定的后端不可用: {url}")

    for candidate in (_read_endpoint(), f"http://{host}:{port}"):
        if candidate and await probe(candidate.rstrip("/")):
            logger.info("复用已有后端: %s", candidate)
            return Backend(base_url=candidate.rstrip("/"))

    if not autostart:
        raise RuntimeError(f"后端未运行: http://{host}:{port}")

    return await spawn_backend(host)


async def spawn_backend(host: str = "127.0.0.1") -> Backend:
    """在子进程里拉起一个本地后端."""
    port = _free_port()
    base_url = f"http://{host}:{port}"
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "qmdler.server.app:app",
        "--host",
        host,
        "--port",
        str(port),
        "--log-level",
        "warning",
    ]
    logger.info("正在拉起本地后端: %s", base_url)
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read().decode("utf-8", "replace") if process.stderr else ""
            raise RuntimeError(f"后端启动失败:\n{stderr[-2000:]}")
        if await probe(base_url, timeout=1.0):
            _write_endpoint(base_url, process.pid)
            return Backend(base_url=base_url, process=process)
        await asyncio.sleep(0.4)

    process.terminate()
    raise RuntimeError(f"后端在 {STARTUP_TIMEOUT:.0f} 秒内没有就绪")
