# SPDX-License-Identifier: GPL-3.0-or-later
"""汇总报告界面."""

from __future__ import annotations

from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static


class ReportScreen(ModalScreen[None]):
    """汇总报告模态框."""

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "dismiss_screen", "关闭")]

    def __init__(self, report: dict[str, Any]) -> None:
        """初始化."""
        super().__init__()
        self._report = report

    def compose(self) -> ComposeResult:
        """构造界面."""
        with Vertical(id="report-box"):
            yield Label("汇总报告", classes="pane-title")
            yield Static(self._report.get("summary", ""), id="report-summary")
            with VerticalScroll():
                yield Static(self._section("试听 / 降级（不计入成功）", self._report.get("trials", [])))
                yield Static(self._section("失败", self._report.get("failures", [])))
                yield Static(self._section("受限 / 无版权", self._report.get("unavailable", [])))
            with Horizontal(classes="button-row"):
                yield Button("关闭", id="close", variant="primary")

    @staticmethod
    def _section(title: str, rows: list[dict[str, Any]]) -> str:
        if not rows:
            return f"[dim]{title}：无[/dim]\n"
        lines = [f"[bold]{title}（{len(rows)}）[/bold]"]
        lines.extend(f"  · {row.get('title', '')} — {row.get('reason', '')}" for row in rows)
        return "\n".join(lines) + "\n"

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """关闭按钮."""
        if event.button.id == "close":
            self.dismiss(None)

    def action_dismiss_screen(self) -> None:
        """关闭."""
        self.dismiss(None)
