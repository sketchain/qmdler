# SPDX-License-Identifier: GPL-3.0-or-later
"""五种登录方式.

1. QQ 扫码 (``QRLoginType.QQ``)
2. 微信扫码 (``QRLoginType.WX``)
3. QQ 音乐 APP 扫码 (``QRLoginType.MOBILE``, 走 MQTT 长连接实时推送, 不是轮询)
4. 手机号 + 短信验证码 (``PhoneLoginSession``)
5. Credential 直接导入

扫码统一用 ``QRCodeLoginSession.iter_events()`` 消费事件流, 把 SCAN / CONF /
DONE / REFUSE / TIMEOUT 映射到 UI 状态.

注意 MOBILE 与 QQ/WX 的事件语义略有差异 (见源码 ``login.py``):
MQTT 连接建立后先产出一次 ``SCAN`` 表示「等待扫描」, 收到 ``scanned`` 推送才
产出 ``CONF``; QQ/WX 轮询则是 ``SCAN`` = 未扫描、``CONF`` = 已扫描待确认.
两者在 UI 上的展示文案一致.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from qqmusic_api import Client, Credential
from qqmusic_api.core.exceptions import LoginError, LoginRateLimitError, NetworkError
from qqmusic_api.models.login import (
    QR,
    PhoneLoginEvents,
    QRCodeLoginEvents,
    QRLoginType,
)
from qqmusic_api.modules.login_utils import PhoneLoginSession, PollInterval, QRCodeLoginSession

from ..config.schema import AuthConfig
from ..events import EventBus, EventKind
from .manager import AuthManager
from .qrimage import art_width, try_render
from .store import normalize

logger = logging.getLogger(__name__)

QrStatus = Literal["waiting", "scanned", "confirmed", "done", "refused", "timeout", "error"]

#: 扫码类型 → 展示名.
_QR_TYPE_NAMES: dict[str, str] = {"qq": "QQ", "wx": "微信", "mobile": "QQ 音乐 APP"}

#: 二维码事件 → (UI 状态, 提示文案)
_QR_STATUS_MAP: dict[QRCodeLoginEvents, tuple[QrStatus, str]] = {
    QRCodeLoginEvents.SCAN: ("waiting", "等待扫描"),
    QRCodeLoginEvents.CONF: ("scanned", "已扫描，请在手机上确认"),
    QRCodeLoginEvents.DONE: ("done", "登录成功"),
    QRCodeLoginEvents.REFUSE: ("refused", "已在手机上拒绝登录"),
    QRCodeLoginEvents.TIMEOUT: ("timeout", "二维码已过期，请刷新"),
}


@dataclass
class QrLoginState:
    """一次扫码登录会话的状态."""

    session_id: str
    login_type: str
    status: QrStatus = "waiting"
    message: str = "正在获取二维码"
    png: bytes = b""
    mimetype: str = "image/png"
    ascii_art: str = ""
    #: 字符画还原失败时的原因; 非空表示只能走图片 / 文件 / 载荷这三条路.
    ascii_error: str = ""
    #: 图片端点的完整可访问地址 (含实际监听的 IP:端口).
    image_url: str = ""
    #: 全部候选地址 (含局域网 IP), 手机要用其中一条.
    image_urls: list[str] = field(default_factory=list)
    #: ``QR.save()`` 落盘后的绝对路径.
    saved_path: str = ""
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0

    def as_dict(self, *, include_ascii: bool = True) -> dict[str, Any]:
        """转为 REST/WS 出参. PNG 不内联, 走单独的图片端点."""
        payload: dict[str, Any] = {
            "session_id": self.session_id,
            "login_type": self.login_type,
            "status": self.status,
            "message": self.message,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "has_image": bool(self.png),
            "mimetype": self.mimetype,
            "image_url": self.image_url,
            "image_urls": self.image_urls,
            "saved_path": self.saved_path,
        }
        if include_ascii:
            payload["ascii"] = self.ascii_art
            payload["ascii_error"] = self.ascii_error
            payload["ascii_width"] = art_width(self.ascii_art)
        return payload


@dataclass
class PhoneLoginState:
    """一次手机号登录会话的状态."""

    session_id: str
    phone: str
    country_code: int
    status: Literal["idle", "sent", "captcha", "frequency", "done", "error"] = "idle"
    message: str = ""
    captcha_url: str = ""
    created_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        """转为 REST/WS 出参."""
        return {
            "session_id": self.session_id,
            "phone": self.phone,
            "country_code": self.country_code,
            "status": self.status,
            "message": self.message,
            "captcha_url": self.captcha_url,
            "created_at": self.created_at,
        }


class LoginCoordinator:
    """管理进行中的登录会话, 并把状态广播给两个前端."""

    def __init__(self, client: Client, auth: AuthManager, bus: EventBus, config: AuthConfig) -> None:
        """初始化."""
        self._client = client
        self._auth = auth
        self._bus = bus
        self._config = config
        self._qr: dict[str, QrLoginState] = {}
        self._qr_tasks: dict[str, asyncio.Task[None]] = {}
        self._phone: dict[str, tuple[PhoneLoginState, PhoneLoginSession]] = {}
        #: 对外可访问的 base 列表（含实际监听的 IP:端口与局域网地址）。
        self._public_bases: list[str] = []
        #: 二维码落盘目录。
        self._qr_dir: Path | None = None
        #: 最近一次扫码会话，供 ``/api/auth/qrcode.png`` 不带参数时使用。
        self._latest_qr: str = ""

    def configure_endpoint(self, public_bases: list[str], qr_dir: Path | None = None) -> None:
        """告诉协调器对外地址与落盘目录.

        地址要含实际监听的 IP:端口 —— TUI/CLI 把它原样打印出来, 人直接用浏览器
        或手机打开就能扫。
        """
        self._public_bases = [base.rstrip("/") for base in public_bases if base]
        if qr_dir is not None:
            self._qr_dir = qr_dir

    def note_request_base(self, base: str) -> None:
        """记下客户端实际用的 base, 排到候选首位 —— 这条一定是通的。"""
        cleaned = base.rstrip("/")
        if not cleaned:
            return
        if cleaned in self._public_bases:
            self._public_bases.remove(cleaned)
        self._public_bases.insert(0, cleaned)

    def _save_qr(self, qrcode: QR) -> str:
        """落盘一份二维码图片, 返回绝对路径."""
        if self._qr_dir is None:
            return ""
        try:
            saved = qrcode.save(self._qr_dir)
        except OSError as exc:
            logger.warning("二维码落盘失败: %s", exc)
            return ""
        return str(saved.resolve()) if saved else ""

    # -- 扫码 ---------------------------------------------------------------- #

    async def start_qrcode(self, login_type: str) -> QrLoginState:
        """开始一次扫码登录, 返回初始状态 (含二维码)."""
        try:
            qr_type = QRLoginType(login_type)
        except ValueError as exc:
            raise ValueError(f"不支持的扫码类型: {login_type}") from exc

        session_id = uuid.uuid4().hex
        state = QrLoginState(session_id=session_id, login_type=qr_type.value)
        self._qr[session_id] = state
        self._latest_qr = session_id

        session = QRCodeLoginSession(
            self._client.login,
            qr_type,
            interval=PollInterval(default=1.5, scanned=0.8, error=3.0),
            timeout_seconds=self._config.qrcode_timeout,
        )

        try:
            qrcode = await session.get_qrcode()
        except (LoginError, NetworkError) as exc:
            state.status = "error"
            state.message = f"获取二维码失败: {exc}"
            self._emit(state)
            return state

        state.png = qrcode.data
        state.mimetype = qrcode.mimetype or "image/png"
        # 字符画只是三条出口之一, 且必须通过自检才输出.
        state.ascii_art, state.ascii_error = try_render(qrcode.data)
        state.image_urls = [f"{base}/api/auth/qrcode.png?session_id={session_id}" for base in self._public_bases]
        state.image_url = state.image_urls[0] if state.image_urls else ""
        state.saved_path = self._save_qr(qrcode)
        state.expires_at = time.time() + self._config.qrcode_timeout
        state.status = "waiting"
        state.message = f"请使用「{_QR_TYPE_NAMES[qr_type.value]}」扫描二维码"
        self._emit(state)

        task = asyncio.create_task(self._consume_qr(state, session), name=f"qmdler-qrlogin-{session_id}")
        self._qr_tasks[session_id] = task
        return state

    async def _consume_qr(self, state: QrLoginState, session: QRCodeLoginSession) -> None:
        """消费扫码事件流, 把状态映射到 UI."""
        try:
            async for result in session.iter_events():
                status, message = _QR_STATUS_MAP.get(result.event, ("error", "未知状态"))
                state.status = status
                state.message = message

                if result.event is QRCodeLoginEvents.DONE:
                    if result.credential is None:
                        state.status = "error"
                        state.message = "登录成功但未返回凭证"
                    else:
                        await self._auth.set_credential(result.credential)
                        state.message = "登录成功"
                    self._emit(state)
                    break

                self._emit(state)
                if result.event in (QRCodeLoginEvents.REFUSE, QRCodeLoginEvents.TIMEOUT):
                    break
        except asyncio.CancelledError:
            raise
        except LoginRateLimitError as exc:
            state.status = "error"
            state.message = f"登录操作过于频繁，请稍后再试：{exc}"
            self._emit(state)
        except (LoginError, NetworkError) as exc:
            state.status = "error"
            state.message = f"登录失败：{exc}"
            self._emit(state)
        except Exception as exc:
            logger.exception("扫码登录流程异常")
            state.status = "error"
            state.message = f"登录流程异常：{exc}"
            self._emit(state)
        finally:
            self._qr_tasks.pop(state.session_id, None)

    async def refresh_qrcode(self, session_id: str) -> QrLoginState:
        """二维码过期后一键刷新: 关掉旧会话, 用同样的类型开一个新的."""
        old = self._qr.get(session_id)
        login_type = old.login_type if old else QRLoginType.QQ.value
        await self.cancel_qrcode(session_id)
        return await self.start_qrcode(login_type)

    async def cancel_qrcode(self, session_id: str) -> None:
        """取消一次扫码会话."""
        task = self._qr_tasks.pop(session_id, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._qr.pop(session_id, None)

    def get_qrcode(self, session_id: str) -> QrLoginState | None:
        """取扫码会话状态."""
        return self._qr.get(session_id)

    def qrcode_image(self, session_id: str = "") -> tuple[bytes, str]:
        """取二维码图片字节与 MIME 类型.

        不传 ``session_id`` 时返回最近一次会话的 —— ``/api/auth/qrcode.png``
        这个「直接用浏览器打开就行」的地址要能免参数访问.
        """
        state = self._qr.get(session_id or self._latest_qr)
        if state is None:
            return b"", ""
        return state.png, state.mimetype

    def latest_qrcode(self) -> QrLoginState | None:
        """最近一次扫码会话."""
        return self._qr.get(self._latest_qr)

    def decode_payload(self, session_id: str = "") -> tuple[str, str]:
        """解出二维码载荷 —— 仅诊断用, 依赖 dev 才装的 zxing-cpp."""
        from .qrpayload import decode_payload

        state = self._qr.get(session_id or self._latest_qr)
        if state is None or not state.png:
            return "", "没有可用的二维码会话"
        return decode_payload(state.png)

    # -- 手机号 -------------------------------------------------------------- #

    async def start_phone(self, phone: int | str, country_code: int = 86) -> PhoneLoginState:
        """发送短信验证码.

        非成功分支有两种:
        ``CAPTCHA`` 需要人机验证 (把 ``result.info`` 里的链接给用户),
        ``FREQUENCY`` 是频率限制.
        """
        session_id = uuid.uuid4().hex
        session = PhoneLoginSession(self._client.login, phone=phone, country_code=country_code)
        state = PhoneLoginState(session_id=session_id, phone=str(phone), country_code=country_code)
        self._phone[session_id] = (state, session)

        try:
            result = await session.send_authcode()
        except LoginRateLimitError as exc:
            state.status = "frequency"
            state.message = f"操作过于频繁，请稍后再试：{exc}"
            self._emit_phone(state)
            return state
        except (LoginError, NetworkError) as exc:
            state.status = "error"
            state.message = f"发送验证码失败：{exc}"
            self._emit_phone(state)
            return state

        if result.event is PhoneLoginEvents.SEND:
            state.status = "sent"
            state.message = "验证码已发送"
        elif result.event is PhoneLoginEvents.CAPTCHA:
            state.status = "captcha"
            state.captcha_url = result.info or ""
            state.message = "需要完成人机验证，请打开下方链接完成后重试"
        elif result.event is PhoneLoginEvents.FREQUENCY:
            state.status = "frequency"
            state.message = "请求过于频繁，请稍后再试"
        else:  # pragma: no cover - 库只定义了上面三种
            state.status = "error"
            state.message = "未知的验证码发送结果"

        self._emit_phone(state)
        return state

    async def submit_phone_code(self, session_id: str, auth_code: str) -> PhoneLoginState:
        """提交短信验证码完成登录."""
        entry = self._phone.get(session_id)
        if entry is None:
            raise KeyError("登录会话不存在或已过期")
        state, session = entry

        try:
            credential = await session.authorize(auth_code)
        except LoginRateLimitError as exc:
            state.status = "frequency"
            state.message = f"操作过于频繁，请稍后再试：{exc}"
            self._emit_phone(state)
            return state
        except (LoginError, NetworkError) as exc:
            state.status = "error"
            state.message = f"验证失败：{exc}"
            self._emit_phone(state)
            return state

        await self._auth.set_credential(credential)
        state.status = "done"
        state.message = "登录成功"
        self._emit_phone(state)
        self._phone.pop(session_id, None)
        return state

    def get_phone(self, session_id: str) -> PhoneLoginState | None:
        """取手机登录会话状态."""
        entry = self._phone.get(session_id)
        return entry[0] if entry else None

    # -- 凭证导入 ------------------------------------------------------------ #

    async def import_credential(self, data: dict[str, Any]) -> Credential:
        """直接导入 Credential.

        接受 ``Credential`` 的任意子集; 至少要有 ``musicid`` 和 ``musickey``.

        只粘贴 musicid + musickey 时凭证里没有 ``encrypt_uin``, 「我喜欢」/
        「收藏的歌单」/ 账号信息这三个接口需要 euin, 会不可用 —— 这一点由
        ``AuthManager.resolve_euin`` 兜底并在 UI 上提示.
        """
        if not data.get("musicid") or not data.get("musickey"):
            raise ValueError("凭证至少需要 musicid 和 musickey")
        credential = normalize(Credential.model_validate(data))
        await self._auth.set_credential(credential)
        return credential

    async def import_credential_file(self, path: str | Path) -> Credential:
        """从 JSON 文件导入 Credential."""
        raw = Path(path).expanduser().read_text("utf-8")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("凭证文件必须是一个 JSON 对象")
        return await self.import_credential(payload)

    # -- 广播 ---------------------------------------------------------------- #

    def _emit(self, state: QrLoginState) -> None:
        self._bus.emit(EventKind.LOGIN_EVENT, {"kind": "qrcode", **state.as_dict()})

    def _emit_phone(self, state: PhoneLoginState) -> None:
        self._bus.emit(EventKind.LOGIN_EVENT, {"kind": "phone", **state.as_dict()})

    async def shutdown(self) -> None:
        """取消全部进行中的扫码会话."""
        for session_id in list(self._qr_tasks):
            await self.cancel_qrcode(session_id)


def qrcode_data_uri(png: bytes) -> str:
    """把二维码 PNG 包成 data URI (WebUI 内联展示用)."""
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")
