# SPDX-License-Identifier: GPL-3.0-or-later
"""Textual TUI —— 纯客户端, 与 WebUI 连同一个后端."""

from .app import QmdlerApp, run_tui
from .bootstrap import Backend, ensure_backend

__all__ = ["Backend", "QmdlerApp", "ensure_backend", "run_tui"]
