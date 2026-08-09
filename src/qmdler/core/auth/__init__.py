"""凭证与登录子系统.

与下载引擎解耦: 引擎只向 :class:`AuthManager` 要一份当前有效的 Credential.
"""

from .manager import AuthManager, NotLoggedInError
from .qrimage import render_terminal_qr
from .sessions import LoginCoordinator, PhoneLoginState, QrLoginState, qrcode_data_uri
from .store import CredentialStore, expires_at, has_known_expiry, is_valid, normalize

__all__ = [
    "AuthManager",
    "CredentialStore",
    "LoginCoordinator",
    "NotLoggedInError",
    "PhoneLoginState",
    "QrLoginState",
    "expires_at",
    "has_known_expiry",
    "is_valid",
    "normalize",
    "qrcode_data_uri",
    "render_terminal_qr",
]
