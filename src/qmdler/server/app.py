# SPDX-License-Identifier: GPL-3.0-or-later
"""FastAPI 应用."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..core.config import ConfigStore
from .context import build_context
from .routers import auth, config, health, sources, tasks
from .ws import router as ws_router

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def create_app(preset: str | None = None) -> FastAPI:
    """建 FastAPI 应用.

    lifespan 里持有全部单例: 一个 ``Client``、一个 ``Repository``、一个引擎、
    一个凭证刷新后台任务.
    """

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with build_context(preset) as context:
            app.state.context = context
            logger.info("qmdler 服务已就绪")
            try:
                yield
            finally:
                app.state.context = None

    app = FastAPI(
        title="qmdler",
        version=__version__,
        description="QQ 音乐歌单批量下载器 — WebUI 与 TUI 共用的后端",
        lifespan=lifespan,
    )

    origins = ConfigStore().load(preset).server.cors_origins
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def _record_public_base(request: Request, call_next: Any) -> Any:
        """记下客户端实际用的 base —— 二维码图片地址要给一条一定打得开的。"""
        context = getattr(app.state, "context", None)
        if context is not None:
            context.login.note_request_base(str(request.base_url))
        return await call_next(request)

    api_routers = (health.router, auth.router, sources.router, tasks.router, config.router)
    for router in api_routers:
        app.include_router(router, prefix="/api")
    app.include_router(ws_router)

    if WEB_DIR.exists():
        # PWA 需要 manifest 和 service worker 在根路径可达.
        @app.get("/manifest.webmanifest", include_in_schema=False)
        async def manifest() -> FileResponse:
            return FileResponse(WEB_DIR / "manifest.webmanifest", media_type="application/manifest+json")

        @app.get("/sw.js", include_in_schema=False)
        async def service_worker() -> FileResponse:
            return FileResponse(WEB_DIR / "sw.js", media_type="application/javascript")

        app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")

    return app


app = create_app()
