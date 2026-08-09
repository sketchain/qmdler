"""凭证模块.

与下载引擎完全解耦: 引擎只调 :meth:`AuthManager.get_valid_credential`, 不关心
刷新时机.

刷新是**主动**的, 不是错误驱动的:

* 常驻后台定时任务按 ``auth.refresh_check_interval`` 周期检查, 用
  ``credential.is_expired()`` 本地判断, 接近过期就提前刷新;
* 启动时、每个任务开始前各做一次服务端校验 (``client.login.check_expired``);
* 刷新成功立刻写回 0600 文件并向两个前端广播;
* 捕获 ``CredentialExpiredError`` 只是最后一道兜底, 不是常规刷新路径.

0.7.2 的实际 API 与官方文档不一致, 这里按源码走:

* **没有** ``Credential.refresh(client)``. 只有
  ``await client.login.refresh_credential(cred)``, 且 ``Credential`` 是
  ``frozen=True``, 返回的是新对象, 必须回写 ``client.credential``.
* **没有** ``client.user.get_euin(musicid)``. euin 只能取
  ``credential.encrypt_uin``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from qqmusic_api import Client, Credential
from qqmusic_api.core.exceptions import (
    CredentialExpiredError,
    CredentialRefreshError,
    NetworkError,
)

from ..config.schema import AuthConfig
from ..events import EventBus, EventKind
from .store import CredentialStore, expires_at, has_known_expiry, is_valid, normalize

logger = logging.getLogger(__name__)

T = TypeVar("T")


class NotLoggedInError(RuntimeError):
    """尚未登录, 或凭证已失效需要重新登录."""


class AuthManager:
    """凭证的唯一权威来源."""

    def __init__(
        self,
        client: Client,
        store: CredentialStore,
        config: AuthConfig,
        bus: EventBus,
    ) -> None:
        """初始化."""
        self._client = client
        self._store = store
        self._config = config
        self._bus = bus
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._needs_login = False
        self._last_refresh: float = 0.0
        self._last_check: float = 0.0
        self._profile: dict[str, Any] = {}

    # -- 生命周期 ------------------------------------------------------------ #

    async def start(self) -> None:
        """加载凭证, 做一次服务端校验, 并起后台刷新任务."""
        credential = self._store.load()
        if credential is not None:
            self._client.credential = credential
            logger.info("已加载本地凭证 musicid=%s", credential.musicid)
            with contextlib.suppress(Exception):
                await self.verify(refresh_if_expired=True)
        self._task = asyncio.create_task(self._refresh_loop(), name="qmdler-credential-refresh")
        self._broadcast()

    async def stop(self) -> None:
        """停止后台刷新任务."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    # -- 对外接口 ------------------------------------------------------------ #

    @property
    def credential(self) -> Credential:
        """当前凭证 (可能是空的)."""
        return self._client.credential

    @property
    def logged_in(self) -> bool:
        """是否处于可用的登录态."""
        return is_valid(self._client.credential) and not self._needs_login

    async def get_valid_credential(self) -> Credential:
        """返回一份当前有效的凭证. 引擎只需要调这一个方法.

        Raises:
            NotLoggedInError: 未登录, 或刷新失败需要重新登录.
        """
        if self._needs_login or not is_valid(self._client.credential):
            raise NotLoggedInError("未登录或凭证已失效, 请重新登录")

        credential = self._client.credential
        if has_known_expiry(credential):
            deadline = expires_at(credential) or 0
            if time.time() >= deadline - self._config.refresh_margin_seconds:
                await self.refresh()
        return self._client.credential

    async def ensure_before_task(self) -> Credential:
        """任务开始前的主动服务端校验."""
        await self.verify(refresh_if_expired=True)
        return await self.get_valid_credential()

    async def verify(self, *, refresh_if_expired: bool = False) -> bool:
        """服务端校验凭证是否过期.

        Returns:
            True 表示当前凭证仍然有效.
        """
        if not is_valid(self._client.credential):
            return False
        try:
            expired = await self._client.login.check_expired(self._client.credential)
        except NetworkError as exc:
            # 网络问题不能当成凭证失效, 否则一断网就把用户踢下线.
            logger.warning("凭证服务端校验失败 (网络): %s", exc)
            return True
        self._last_check = time.time()
        if not expired:
            self._needs_login = False
            self._broadcast()
            return True
        logger.info("服务端判定凭证已过期")
        if refresh_if_expired:
            try:
                await self.refresh(force=True)
            except (CredentialRefreshError, NotLoggedInError):
                return False
            return True
        return False

    async def refresh(self, *, force: bool = False) -> Credential:
        """刷新凭证. 全程加锁, 避免并发请求同时触发刷新.

        Raises:
            NotLoggedInError: 没有可刷新的凭证.
            CredentialRefreshError: 刷新被服务端拒绝, 需要重新登录.
        """
        async with self._lock:
            credential = self._client.credential
            if not is_valid(credential):
                raise NotLoggedInError("没有可刷新的凭证")

            # 等锁期间可能已经被别的协程刷过了.
            if not force and time.time() - self._last_refresh < 30:
                return self._client.credential

            try:
                new_credential = await self._client.login.refresh_credential(credential)
            except CredentialRefreshError as exc:
                self._needs_login = True
                self._bus.log(f"凭证刷新失败, 需要重新登录: {exc}", level="error")
                self._broadcast()
                raise
            except NetworkError as exc:
                self._bus.log(f"凭证刷新遇到网络错误, 稍后重试: {exc}", level="warning")
                raise

            new_credential = normalize(new_credential)
            self._client.credential = new_credential
            self._store.save(new_credential)
            self._last_refresh = time.time()
            self._needs_login = False
            logger.info("凭证已刷新, 新的过期时间=%s", expires_at(new_credential))
            self._bus.log("凭证已刷新", level="info")
            self._broadcast()
            return new_credential

    async def set_credential(self, credential: Credential) -> Credential:
        """登录成功后写入新凭证."""
        credential = normalize(credential)
        self._client.credential = credential
        self._store.save(credential)
        self._needs_login = False
        self._profile = {}
        self._bus.log(f"登录成功 musicid={credential.musicid}", level="info")
        self._broadcast()
        return credential

    async def logout(self) -> None:
        """登出并清空本地凭证."""
        if is_valid(self._client.credential):
            with contextlib.suppress(Exception):
                await self._client.login.logout(self._client.credential)
        self._client.credential = Credential()
        self._store.clear()
        self._needs_login = False
        self._profile = {}
        self._bus.log("已登出", level="info")
        self._broadcast()

    async def resolve_euin(self) -> str:
        """取加密 UIN.

        0.7.2 没有 ``user.get_euin``, euin 的唯一来源是登录响应里的
        ``encryptUin``. 手动粘贴 musicid + musickey 导入的凭证没有这个字段,
        此时若带着 refresh_token 就刷一次换一份完整凭证, 否则返回空串, 由调用方
        向用户说明「该凭证缺少 encrypt_uin」.
        """
        credential = self._client.credential
        if credential.encrypt_uin:
            return credential.encrypt_uin
        if credential.refresh_key or credential.refresh_token:
            with contextlib.suppress(CredentialRefreshError, NetworkError, NotLoggedInError):
                refreshed = await self.refresh(force=True)
                if refreshed.encrypt_uin:
                    return refreshed.encrypt_uin
        return ""

    async def with_retry(self, call: Callable[[], Awaitable[T]]) -> T:
        """兜底: 请求撞上 ``CredentialExpiredError`` 时刷新一次并重试.

        这条路径只应在定时刷新没跟上时触发, 不是常规刷新方式.
        """
        try:
            return await call()
        except CredentialExpiredError as exc:
            self._bus.log(f"请求遭遇凭证过期 (定时刷新未跟上), 立即刷新后重试: {exc}", level="warning")
            await self.refresh(force=True)
            return await call()

    # -- 账号信息 ------------------------------------------------------------ #

    async def load_profile(self, *, refresh: bool = False) -> dict[str, Any]:
        """拉取昵称与会员状态.

        会员等级直接决定能拿到哪些音质, 但**不是万能的**: 单曲付费、音质文件本就
        不存在、地区版权限制、设备受限, 这些和会员等级无关. UI 上不要暗示
        「有 SVIP 就能下任意音质」.
        """
        if self._profile and not refresh:
            return self._profile

        if not is_valid(self._client.credential):
            return {}

        euin = await self.resolve_euin()
        profile: dict[str, Any] = {
            "musicid": self._client.credential.musicid,
            "encrypt_uin": euin,
            "euin_available": bool(euin),
            "nickname": "",
            "avatar": "",
            "vip": {},
        }

        requests = []
        if euin:
            requests.append(self._client.user.get_homepage(euin))
        requests.append(self._client.user.get_vip_info())

        # 并发请求一律带 return_exceptions=True: 默认 False 时抛的是 ExceptionGroup,
        # 而且已成功的结果会被一并丢弃.
        results = await self._client.gather(requests, return_exceptions=True)

        offset = 0
        if euin:
            homepage = results[0]
            offset = 1
            if not isinstance(homepage, Exception):
                profile["nickname"] = homepage.base_info.name
                profile["avatar"] = homepage.base_info.avatar
                if homepage.base_info.encrypted_uin:
                    profile["encrypt_uin"] = homepage.base_info.encrypted_uin
            else:
                logger.warning("获取用户主页失败: %s", homepage)

        vip = results[offset]
        if not isinstance(vip, Exception):
            profile["vip"] = {
                "svip": vip.svip,
                "star": vip.star,
                "ystar": vip.ystar,
                "vip": vip.identity.vip,
                "huge_vip": vip.identity.huge_vip,
                "huge_vip_end": vip.identity.huge_vip_end,
                "level": vip.identity.level,
                "icon": vip.identity.icon,
                "music_level": vip.userinfo.music_level,
                "expire": vip.userinfo.expire,
            }
        else:
            logger.warning("获取会员信息失败: %s", vip)

        self._profile = profile
        self._broadcast()
        return profile

    # -- 状态 ---------------------------------------------------------------- #

    def state(self) -> dict[str, Any]:
        """给前端的登录态快照."""
        credential = self._client.credential
        return {
            "logged_in": self.logged_in,
            "needs_login": self._needs_login,
            "musicid": credential.musicid,
            "login_type": credential.login_type,
            "has_encrypt_uin": bool(credential.encrypt_uin),
            "expires_at": expires_at(credential),
            "expiry_known": has_known_expiry(credential),
            "last_refresh": self._last_refresh,
            "last_check": self._last_check,
            "profile": self._profile,
        }

    def _broadcast(self) -> None:
        self._bus.emit(EventKind.AUTH_STATE, self.state())

    # -- 后台任务 ------------------------------------------------------------ #

    async def _refresh_loop(self) -> None:
        """常驻定时任务: 周期性检查并提前刷新."""
        interval = max(60.0, self._config.refresh_check_interval)
        while True:
            try:
                await asyncio.sleep(interval)
                credential = self._client.credential
                if not is_valid(credential) or self._needs_login:
                    continue
                if not has_known_expiry(credential):
                    # 过期时间未知 (手动导入的凭证), 只能靠服务端校验.
                    await self.verify(refresh_if_expired=True)
                    continue
                deadline = expires_at(credential) or 0
                if time.time() >= deadline - self._config.refresh_margin_seconds:
                    logger.info("凭证接近过期, 提前刷新")
                    await self.refresh()
            except asyncio.CancelledError:
                raise
            except CredentialRefreshError:
                # 已在 refresh() 里置位并广播, 这里不再重复.
                continue
            except Exception as exc:
                logger.warning("凭证定时刷新出错: %s", exc)
