"""应用上下文.

整个进程只有一个: 一个 ``Client``、一个 ``Repository``、一个 ``EventBus``、
一个下载引擎. WebUI 和 TUI 都连到它上面, 所以任一端的暂停/取消对另一端立即可见.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from qqmusic_api import Client

from ..core.auth.manager import AuthManager
from ..core.auth.sessions import LoginCoordinator
from ..core.auth.store import CredentialStore
from ..core.client import ClientHolder
from ..core.config import ConfigStore, Settings, paths
from ..core.download.engine import DownloadEngine
from ..core.events import EventBus, EventKind
from ..core.fsutil import expand
from ..core.metadata.service import MetadataService
from ..core.sources.service import SourceService
from ..core.storage.repository import Repository

logger = logging.getLogger(__name__)

#: 下载用的 UA. 与库的 API 请求分开, 走的是 CDN 直连.
DOWNLOAD_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)


@dataclass
class AppContext:
    """全局单例集合."""

    settings: Settings
    config_store: ConfigStore
    bus: EventBus
    repo: Repository
    client: Client
    http: httpx.AsyncClient
    auth: AuthManager
    login: LoginCoordinator
    sources: SourceService
    metadata: MetadataService
    engine: DownloadEngine
    _holder: ClientHolder

    async def reload_settings(self, settings: Settings) -> None:
        """热更新配置."""
        self.settings = settings
        self.engine.apply_settings(settings)
        self.bus.emit(EventKind.CONFIG_CHANGED, settings.as_dict())

    def snapshot(self) -> dict[str, Any]:
        """给刚连上来的客户端一份全量快照."""
        return {
            "auth": self.auth.state(),
            "engine": self.engine.state.as_dict(),
            "settings": self.settings.as_dict(),
        }


@contextlib.asynccontextmanager
async def build_context(preset: str | None = None) -> AsyncIterator[AppContext]:
    """建立并持有全部单例."""
    paths.ensure_dirs()
    config_store = ConfigStore()
    settings = config_store.load(preset)

    bus = EventBus(history_size=settings.server.event_replay_limit)

    repo = Repository(paths.database_file())
    await repo.connect()

    credential_store = CredentialStore(paths.credential_file())
    holder = ClientHolder(
        device_path=paths.device_file(),
        config=settings.auth,
        credential=credential_store.load(),
    )
    client = await holder.__aenter__()

    http = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.download.request_timeout, connect=15.0),
        headers={"User-Agent": DOWNLOAD_UA},
        follow_redirects=True,
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
    )

    auth = AuthManager(client, credential_store, settings.auth, bus)
    login = LoginCoordinator(client, auth, bus, settings.auth)
    sources = SourceService(client, auth, settings.download)
    metadata = MetadataService(client, http, settings.download)
    engine = DownloadEngine(client, auth, repo, bus, settings, metadata, sources, http)

    context = AppContext(
        settings=settings,
        config_store=config_store,
        bus=bus,
        repo=repo,
        client=client,
        http=http,
        auth=auth,
        login=login,
        sources=sources,
        metadata=metadata,
        engine=engine,
        _holder=holder,
    )

    await auth.start()
    await _cleanup_orphan_parts(context)

    try:
        yield context
    finally:
        with contextlib.suppress(Exception):
            await engine.cancel()
        with contextlib.suppress(Exception):
            await login.shutdown()
        with contextlib.suppress(Exception):
            await auth.stop()
        await http.aclose()
        await holder.__aexit__(None, None, None)
        await repo.close()


async def _cleanup_orphan_parts(context: AppContext) -> None:
    """启动时清理无主的 ``.part``.

    数据库里没有对应 ``downloading`` 条目的残片直接删掉, 否则下次续传会接到一个
    上上次的残片上, 拼出一个损坏文件.
    """
    if not context.settings.download.cleanup_orphan_parts:
        return

    from ..core.download.fetcher import cleanup_orphan_parts
    from ..core.models import ItemStatus

    roots: set[Path] = {expand(context.settings.paths.save_root)}
    keep: set[Path] = set()
    for task in await context.repo.unfinished_tasks():
        roots.add(expand(task.save_root))
        for item in await context.repo.list_items(task.id, statuses=[ItemStatus.DOWNLOADING]):
            if item.target_path:
                keep.add(Path(item.target_path + ".part"))

    removed = cleanup_orphan_parts(sorted(roots), keep=keep)
    if removed:
        logger.info("已清理 %d 个孤儿 .part 文件", len(removed))
        context.bus.log(f"启动清理：删除了 {len(removed)} 个无主的 .part 残片", level="info")
