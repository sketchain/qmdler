# SPDX-License-Identifier: GPL-3.0-or-later
"""健康检查与快照."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Response, status

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


@router.get("/auth/qrcode.png", include_in_schema=True)
async def qrcode_png(context: Ctx, session_id: str = "") -> Response:
    """二维码原始图片.

    这是「首选出口」: 地址够短, 直接用浏览器或手机打开就能扫, 不用对着终端
    字符画将就。不带 ``session_id`` 时给最近一次会话的二维码。

    ``Content-Type`` 取自 ``QR.mimetype`` —— QQ 给的是 PNG, 微信给的是 JPEG。
    """
    data, mimetype = context.login.qrcode_image(session_id)
    if not data:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "还没有进行中的扫码会话")
    return Response(
        content=data,
        media_type=mimetype or "image/png",
        headers={"Cache-Control": "no-store"},
    )
