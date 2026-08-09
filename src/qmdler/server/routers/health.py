"""健康检查与快照."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ... import __version__
from ..deps import Ctx

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(context: Ctx) -> dict[str, Any]:
    """健康检查. TUI 启动时用它探测后端是否已在运行."""
    return {
        "ok": True,
        "app": "qmdler",
        "version": __version__,
        "logged_in": context.auth.logged_in,
        "engine_running": context.engine.busy,
    }


@router.get("/snapshot")
async def snapshot(context: Ctx) -> dict[str, Any]:
    """全量快照: 登录态 + 引擎状态 + 配置."""
    return context.snapshot()
