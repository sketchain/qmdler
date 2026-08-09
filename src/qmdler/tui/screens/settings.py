"""设置界面: 音质优先级链 (上下键排序)、间隔、模板预览、保存目录."""

from __future__ import annotations

from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, ListItem, ListView, Static, Switch

from ..client import ApiClient


class SettingsScreen(ModalScreen[bool]):
    """设置模态框."""

    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "dismiss_screen", "关闭"),
        ("shift+up", "move_up", "上移档位"),
        ("shift+down", "move_down", "下移档位"),
    ]

    def __init__(self, api: ApiClient, config: dict[str, Any], catalog: list[dict[str, Any]]) -> None:
        """初始化."""
        super().__init__()
        self._api = api
        self._config = config
        self._catalog = catalog
        self._chain: list[str] = list(config.get("quality", {}).get("chain", []))

    def compose(self) -> ComposeResult:
        """构造界面."""
        download = self._config.get("download", {})
        naming = self._config.get("naming", {})
        paths = self._config.get("paths", {})
        quality = self._config.get("quality", {})

        with Vertical(id="settings-box"):
            yield Label("设置", classes="pane-title")
            with VerticalScroll():
                yield Label("音质优先级链（Shift+↑/↓ 调整顺序）")
                yield ListView(id="chain-list")

                yield Label("每首之间的间隔（秒）")
                yield Input(value=str(download.get("interval_seconds", 180)), id="interval")

                with Horizontal():
                    yield Label("随机区间  ")
                    yield Switch(value=bool(download.get("random_interval")), id="random-interval")
                with Horizontal():
                    yield Label("拒绝试听文件  ")
                    yield Switch(value=bool(quality.get("reject_trial", True)), id="reject-trial")
                with Horizontal():
                    yield Label("下载歌词  ")
                    yield Switch(value=bool(self._config.get("lyric", {}).get("enabled")), id="lyric-enabled")
                with Horizontal():
                    yield Label("下载封面  ")
                    yield Switch(value=bool(self._config.get("cover", {}).get("enabled")), id="cover-enabled")

                yield Label("目录模板")
                yield Input(value=naming.get("dir_template", ""), id="dir-template")
                yield Label("文件名模板")
                yield Input(value=naming.get("file_template", ""), id="file-template")
                yield Static("", id="preview")

                yield Label("保存根目录")
                yield Input(value=paths.get("save_root", ""), id="save-root")
                yield Static("", id="path-check")

            with Horizontal(classes="button-row"):
                yield Button("保存", id="save", variant="primary")
                yield Button("校验目录", id="check-path")
                yield Button("关闭", id="close")

    async def on_mount(self) -> None:
        """填充列表并刷新预览."""
        self._refresh_chain()
        await self._refresh_preview()

    # -- 音质链 -------------------------------------------------------------- #

    def _label_of(self, code: str) -> str:
        for entry in self._catalog:
            if entry["code"] == code:
                return str(entry["label"])
        return code

    def _refresh_chain(self) -> None:
        view = self.query_one("#chain-list", ListView)
        index = view.index or 0
        view.clear()
        for rank, code in enumerate(self._chain, start=1):
            view.append(ListItem(Label(f"{rank}. {self._label_of(code)}")))
        if self._chain:
            view.index = min(index, len(self._chain) - 1)

    def _move(self, delta: int) -> None:
        view = self.query_one("#chain-list", ListView)
        index = view.index
        if index is None:
            return
        target = index + delta
        if not (0 <= target < len(self._chain)):
            return
        self._chain[index], self._chain[target] = self._chain[target], self._chain[index]
        self._refresh_chain()
        self.query_one("#chain-list", ListView).index = target

    def action_move_up(self) -> None:
        """上移当前档位."""
        self._move(-1)

    def action_move_down(self) -> None:
        """下移当前档位."""
        self._move(1)

    # -- 预览与校验 ----------------------------------------------------------- #

    async def _refresh_preview(self) -> None:
        try:
            result = await self._api.preview_template(
                {
                    "dir_template": self.query_one("#dir-template", Input).value,
                    "file_template": self.query_one("#file-template", Input).value,
                    "quality": self._chain[0] if self._chain else "FLAC",
                    "root": self.query_one("#save-root", Input).value,
                },
            )
        except Exception as exc:
            self.query_one("#preview", Static).update(f"[red]预览失败：{exc}[/red]")
            return
        unknown = result.get("unknown_file_placeholders", []) + result.get("unknown_dir_placeholders", [])
        warning = f"\n[yellow]未知占位符：{'、'.join(unknown)}[/yellow]" if unknown else ""
        self.query_one("#preview", Static).update(f"预览：{result['full']}{warning}")

    async def on_input_changed(self, event: Input.Changed) -> None:
        """模板改一个字就刷新预览."""
        if event.input.id in ("dir-template", "file-template", "save-root"):
            await self._refresh_preview()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """按钮事件."""
        button_id = event.button.id or ""
        if button_id == "close":
            self.dismiss(False)
        elif button_id == "check-path":
            result = await self._api.check_path(self.query_one("#save-root", Input).value, create=False)
            free = result["free_bytes"] / 1e9
            status = "[green]可用[/green]" if result["ok"] else f"[red]不可用：{result['message']}[/red]"
            self.query_one("#path-check", Static).update(f"{status}  剩余 {free:.1f} GB")
        elif button_id == "save":
            await self._save()

    async def _save(self) -> None:
        try:
            interval = float(self.query_one("#interval", Input).value or 180)
        except ValueError:
            interval = 180.0
        patch = {
            "quality": {
                "chain": self._chain,
                "reject_trial": self.query_one("#reject-trial", Switch).value,
            },
            "download": {
                "interval_seconds": interval,
                "random_interval": self.query_one("#random-interval", Switch).value,
            },
            "naming": {
                "dir_template": self.query_one("#dir-template", Input).value,
                "file_template": self.query_one("#file-template", Input).value,
            },
            "paths": {"save_root": self.query_one("#save-root", Input).value},
            "lyric": {"enabled": self.query_one("#lyric-enabled", Switch).value},
            "cover": {"enabled": self.query_one("#cover-enabled", Switch).value},
        }
        try:
            await self._api.patch_config(patch)
        except Exception as exc:
            self.query_one("#path-check", Static).update(f"[red]保存失败：{exc}[/red]")
            return
        self.dismiss(True)

    def action_dismiss_screen(self) -> None:
        """关闭."""
        self.dismiss(False)
