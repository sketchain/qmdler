# SPDX-License-Identifier: GPL-3.0-or-later
"""FastAPI 服务层: 把 core 暴露成 REST + WebSocket."""

from .app import create_app
from .context import AppContext, build_context

__all__ = ["AppContext", "build_context", "create_app"]
