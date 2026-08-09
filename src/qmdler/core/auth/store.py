"""凭证持久化.

务必整体序列化 ``Credential`` 的全部字段, 而不是只存 musicid / musickey /
refresh_key / refresh_token / encrypt_uin 五个:

``LoginApi.refresh_credential`` 会按 ``login_type`` 分三套参数, 微信 (1) 用
``openid`` / ``unionid`` / ``refresh_token``, QQ (2) 用 ``openid`` /
``access_token`` / ``expired_at``. 少存任何一个, 下次刷新都会失败.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from qqmusic_api import Credential

logger = logging.getLogger(__name__)

#: 本地判断「即将过期」时用到的两个字段. 手动导入的凭证里它们是 0,
#: 此时 ``is_expired()`` 恒为 True, 必须区别对待, 否则定时任务会无限刷新.
_EXPIRY_FIELDS = ("musickey_create_time", "key_expires_in")


def normalize(credential: Credential) -> Credential:
    """归一化凭证.

    ``get_song_urls`` 取的是 ``credential.str_musicid``, 且**没有**
    ``or str(musicid)`` 兜底 (对比 ``refresh_credential`` 里就写了).
    手动构造的 ``Credential(musicid=..., musickey=...)`` 其 ``str_musicid``
    是空串, 会让取链请求带上空 uin. 所以加载/登录后一律在这里补齐.

    ``Credential`` 是 ``frozen=True`` 的 pydantic 模型, 只能 ``model_copy``.
    """
    if credential.musicid and not credential.str_musicid:
        return credential.model_copy(update={"str_musicid": str(credential.musicid)})
    return credential


def has_known_expiry(credential: Credential) -> bool:
    """凭证是否带有可用的过期时间戳.

    手动粘贴 musicid + musickey 的凭证没有这两个字段, ``is_expired()`` 会恒为
    True —— 那不是「已过期」, 而是「过期时间未知」.
    """
    return all(getattr(credential, field, 0) for field in _EXPIRY_FIELDS)


def expires_at(credential: Credential) -> int | None:
    """凭证的过期时间戳 (秒). 未知时返回 ``None``."""
    if not has_known_expiry(credential):
        return None
    return int(credential.musickey_create_time + credential.key_expires_in)


def is_valid(credential: Credential | None) -> bool:
    """是否是一份形式上可用的凭证."""
    return bool(credential and credential.musicid and credential.musickey)


class CredentialStore:
    """凭证文件读写 (权限 600)."""

    def __init__(self, path: Path) -> None:
        """记录凭证文件路径."""
        self.path = path

    def load(self) -> Credential | None:
        """加载凭证. 文件不存在或损坏时返回 ``None``."""
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("凭证文件损坏, 已忽略: %s", exc)
            return None
        if not isinstance(data, dict):
            return None
        try:
            credential = Credential.model_validate(data)
        except Exception as exc:
            logger.warning("凭证文件字段异常, 已忽略: %s", exc)
            return None
        if not is_valid(credential):
            return None
        return normalize(credential)

    def save(self, credential: Credential) -> None:
        """原子写入凭证, 并把权限收紧到 600."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = credential.model_dump(mode="json")
        # 先落到同目录的临时文件, chmod 之后再 rename —— 中途崩溃不会留下一个
        # 半截的、权限敞开的凭证文件.
        descriptor, name = tempfile.mkstemp(dir=self.path.parent, prefix=".credential-", suffix=".tmp")
        temp_path = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.chmod(temp_path, stat.S_IRUSR | stat.S_IWUSR)
            temp_path.replace(self.path)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
        # 目标文件已存在时 replace 会保留原权限, 再收一次保险.
        os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)

    def clear(self) -> None:
        """删除凭证文件."""
        self.path.unlink(missing_ok=True)
