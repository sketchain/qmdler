# SPDX-License-Identifier: GPL-3.0-or-later
"""全局唯一 ``Client`` 实例的持有者.

整个应用生命周期内只维持**一个** ``Client``: 它内部是一个带令牌桶限速的
``niquests.AsyncSession``, 每次请求新建既浪费连接池, 也会让限速失效.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import TracebackType

from qqmusic_api import Client, Credential

from .config.schema import AuthConfig

logger = logging.getLogger(__name__)


class ClientHolder:
    """管理唯一 ``Client`` 的生命周期."""

    def __init__(self, device_path: Path, config: AuthConfig, credential: Credential | None = None) -> None:
        """初始化.

        Args:
            device_path: 设备信息文件路径. 必须持久化 —— 不传的话设备指纹每次重启
                都会变, 等同于频繁换设备登录, 极易触发风控.
            config: 认证相关配置 (含限速参数).
            credential: 启动时已有的凭证.
        """
        self._device_path = device_path
        self._config = config
        self._initial_credential = credential
        self._client: Client | None = None

    async def __aenter__(self) -> Client:
        """建立客户端."""
        self._device_path.parent.mkdir(parents=True, exist_ok=True)
        self._client = Client(
            credential=self._initial_credential,
            device_path=str(self._device_path),
            # 库默认 10 req/s、突发 50, 对这种批量场景太激进了.
            rate=self._config.api_rate,
            capacity=self._config.api_capacity,
        )
        logger.debug("Client 已建立, device_path=%s", self._device_path)
        return self._client

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """关闭客户端."""
        if self._client is not None:
            await self._client.close()
            self._client = None

    @property
    def client(self) -> Client:
        """已建立的客户端."""
        if self._client is None:
            raise RuntimeError("ClientHolder 尚未进入上下文")
        return self._client
