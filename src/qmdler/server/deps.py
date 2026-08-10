# SPDX-License-Identifier: GPL-3.0-or-later
"""FastAPI 依赖."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from .context import AppContext


def get_context(request: Request) -> AppContext:
    """从 app.state 取应用上下文."""
    context: AppContext | None = getattr(request.app.state, "context", None)
    if context is None:  # pragma: no cover - lifespan 之外不该被调到
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "服务尚未就绪")
    return context


Ctx = Annotated[AppContext, Depends(get_context)]
