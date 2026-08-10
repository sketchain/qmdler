# SPDX-License-Identifier: GPL-3.0-or-later
"""登录界面: 五种方式."""

from __future__ import annotations

from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from ..client import ApiClient

_QR_TEXT = {
    "waiting": "等待扫描…",
    "scanned": "已扫描，请在手机上确认",
    "confirmed": "已确认",
    "done": "登录成功",
    "refused": "已在手机上拒绝登录",
    "timeout": "二维码已过期，按 R 刷新",
    "error": "出错了",
}


class LoginScreen(ModalScreen[bool]):
    """登录模态框."""

    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "dismiss_screen", "关闭"),
        ("r", "refresh_qr", "刷新二维码"),
    ]

    def __init__(self, api: ApiClient) -> None:
        """初始化."""
        super().__init__()
        self._api = api
        self._session_id = ""
        self._phone_session = ""
        self._mode = "menu"

    def compose(self) -> ComposeResult:
        """构造界面."""
        with Vertical(id="login-box"):
            yield Label("登录 QQ 音乐", classes="pane-title")
            with Horizontal(classes="button-row"):
                yield Button("QQ 扫码", id="qq", variant="primary")
                yield Button("微信扫码", id="wx")
                yield Button("APP 扫码", id="mobile")
            with Horizontal(classes="button-row"):
                yield Button("手机验证码", id="phone")
                yield Button("导入凭证", id="import")
                yield Button("关闭", id="close")

            with VerticalScroll(id="login-body"):
                yield Static("", id="qr-art")
                yield Static("选择一种登录方式。", id="qr-hint")
                yield Input(placeholder="手机号", id="phone-number", classes="hidden")
                yield Input(placeholder="短信验证码", id="phone-code", classes="hidden")
                yield Input(placeholder="musicid", id="cred-musicid", classes="hidden")
                yield Input(placeholder="musickey (Q_H_L_… / W_X_…)", id="cred-musickey", classes="hidden")
                yield Button("提交", id="submit", classes="hidden")

    def on_mount(self) -> None:
        """初始隐藏输入控件."""
        self._show_inputs()

    # -- 交互 ---------------------------------------------------------------- #

    def _show_inputs(self, *ids: str) -> None:
        for widget_id in ("phone-number", "phone-code", "cred-musicid", "cred-musickey", "submit"):
            widget = self.query_one(f"#{widget_id}")
            widget.display = widget_id in ids

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """按钮事件."""
        button_id = event.button.id or ""
        if button_id == "close":
            self.dismiss(False)
        elif button_id in ("qq", "wx", "mobile"):
            self._mode = button_id
            self._show_inputs()
            await self._start_qr(button_id)
        elif button_id == "phone":
            self._mode = "phone"
            self._show_inputs("phone-number", "submit")
            self.query_one("#qr-art", Static).update("")
            self.query_one("#qr-hint", Static).update("输入手机号后按「提交」发送验证码。")
        elif button_id == "import":
            self._mode = "import"
            self._show_inputs("cred-musicid", "cred-musickey", "submit")
            self.query_one("#qr-art", Static).update("")
            self.query_one("#qr-hint", Static).update(
                "只填 musicid + musickey 也能用，但凭证里没有 encrypt_uin，\n"
                "「我喜欢」「收藏的歌单」和账号信息会不可用。",
            )
        elif button_id == "submit":
            await self._submit()

    async def _submit(self) -> None:
        hint = self.query_one("#qr-hint", Static)
        try:
            if self._mode == "phone" and not self._phone_session:
                phone = self.query_one("#phone-number", Input).value.strip()
                result = await self._api.send_phone_code(phone)
                self._phone_session = result.get("session_id", "")
                hint.update(self._phone_message(result))
                if result.get("status") == "sent":
                    self._show_inputs("phone-number", "phone-code", "submit")
            elif self._mode == "phone":
                code = self.query_one("#phone-code", Input).value.strip()
                result = await self._api.verify_phone_code(self._phone_session, code)
                hint.update(result.get("message", ""))
                if result.get("status") == "done":
                    self.dismiss(True)
            elif self._mode == "import":
                payload = {
                    "musicid": int(self.query_one("#cred-musicid", Input).value.strip() or 0),
                    "musickey": self.query_one("#cred-musickey", Input).value.strip(),
                }
                await self._api.import_credential(payload)
                self.dismiss(True)
        except Exception as exc:
            hint.update(f"[red]{exc}[/red]")

    @staticmethod
    def _phone_message(result: dict[str, Any]) -> str:
        status = result.get("status")
        message = result.get("message", "")
        if status == "captcha":
            return f"{message}\n验证链接：{result.get('captcha_url', '')}"
        if status == "frequency":
            return f"[yellow]{message}[/yellow]"
        return message

    async def _start_qr(self, login_type: str) -> None:
        art = self.query_one("#qr-art", Static)
        hint = self.query_one("#qr-hint", Static)
        art.update("正在获取二维码…")
        try:
            if self._session_id:
                await self._api.cancel_qrcode(self._session_id)
            result = await self._api.start_qrcode(login_type)
        except Exception as exc:
            art.update("")
            hint.update(f"[red]获取二维码失败：{exc}[/red]")
            return

        self._session_id = result.get("session_id", "")
        self._render_qr(result)

    def _render_qr(self, payload: dict[str, Any]) -> None:
        """渲染二维码.

        字符画只是三条出口之一, 而且必须通过自检才画出来 —— 宁可只给地址,
        也不打印一张可能扫不出来的图.
        """
        art = self.query_one("#qr-art", Static)
        hint = self.query_one("#qr-hint", Static)
        ascii_art = payload.get("ascii") or ""
        ascii_error = payload.get("ascii_error") or ""
        needed = int(payload.get("ascii_width") or 0)
        width = self.size.width

        if ascii_error:
            art.update(f"[yellow]终端二维码不可用：{ascii_error}\n请用下面的地址在浏览器/手机上打开。[/yellow]")
        elif not ascii_art:
            art.update("[yellow]终端二维码不可用，请用下面的地址在浏览器/手机上打开。[/yellow]")
        elif needed > max(20, width - 2):
            art.update(
                f"[yellow]终端太窄，放不下二维码（需要 {needed} 列，当前 {width} 列）。\n"
                "请用下面的地址打开，或加宽终端。[/yellow]",
            )
        else:
            art.update(ascii_art)

        status = payload.get("status", "waiting")
        lines = [f"{_QR_TEXT.get(status, status)}   （按 R 刷新二维码）"]
        for url in payload.get("image_urls") or ([payload["image_url"]] if payload.get("image_url") else []):
            lines.append(f"  图片地址: {url}")
        if payload.get("saved_path"):
            lines.append(f"  已保存到: {payload['saved_path']}")
        hint.update("\n".join(lines))

    def on_login_event(self, payload: dict[str, Any]) -> None:
        """由 App 转发过来的 WS 登录事件."""
        if payload.get("kind") != "qrcode" or payload.get("session_id") != self._session_id:
            return
        self._render_qr(payload)
        if payload.get("status") == "done":
            self.dismiss(True)

    async def action_refresh_qr(self) -> None:
        """刷新二维码."""
        if not self._session_id:
            return
        try:
            result = await self._api.refresh_qrcode(self._session_id)
        except Exception as exc:
            self.query_one("#qr-hint", Static).update(f"[red]{exc}[/red]")
            return
        self._session_id = result.get("session_id", "")
        self._render_qr(result)

    def action_dismiss_screen(self) -> None:
        """关闭."""
        self.dismiss(False)
