"""CDN 池.

``purl`` 是**相对路径**, 必须先 ``client.song.get_cdn_dispatch()`` 拿到 ``sip``
域名列表, ``cdn + purl`` 才能下载.

策略: 任务开始时获取并缓存; 每首歌从列表里**随机**选一个 (分散压力); 某个 CDN
连续失败就剔除; 全部失效时重新 dispatch; dispatch 整个失败时用库内置的兜底域名.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field

from qqmusic_api import Client
from qqmusic_api.core.exceptions import BaseApiException

logger = logging.getLogger(__name__)

#: 库里写死的兜底域名 (``SongApi._SONG_URL_FALLBACK_DOMAIN``).
FALLBACK_DOMAIN = "https://isure.stream.qqmusic.qq.com/"

#: 连续失败多少次就把这个 CDN 剔除.
MAX_CONSECUTIVE_FAILURES = 3


@dataclass(slots=True)
class CdnNode:
    """一个 CDN 节点."""

    base: str
    failures: int = 0
    last_used: float = 0.0

    @property
    def healthy(self) -> bool:
        """是否还在可用集合里."""
        return self.failures < MAX_CONSECUTIVE_FAILURES


@dataclass(slots=True)
class CdnPool:
    """CDN 池."""

    nodes: list[CdnNode] = field(default_factory=list)
    fetched_at: float = 0.0
    expiration: int = 0

    @property
    def healthy_nodes(self) -> list[CdnNode]:
        """健康节点."""
        return [node for node in self.nodes if node.healthy]

    @property
    def stale(self) -> bool:
        """缓存是否过期."""
        if not self.fetched_at:
            return True
        ttl = self.expiration if self.expiration > 0 else 3600
        return time.time() - self.fetched_at > ttl


class CdnManager:
    """管理 CDN 列表的获取、选择与剔除."""

    def __init__(self, client: Client) -> None:
        """初始化."""
        self._client = client
        self._pool = CdnPool()

    async def ensure(self, *, force: bool = False) -> CdnPool:
        """确保有一份可用的 CDN 列表."""
        if not force and self._pool.healthy_nodes and not self._pool.stale:
            return self._pool
        await self.dispatch()
        return self._pool

    async def dispatch(self) -> CdnPool:
        """重新拉取 CDN 列表."""
        try:
            response = await self._client.song.get_cdn_dispatch()
        except BaseApiException as exc:
            logger.warning("CDN dispatch 失败, 使用兜底域名: %s", exc)
            self._pool = CdnPool(nodes=[CdnNode(base=FALLBACK_DOMAIN)], fetched_at=time.time())
            return self._pool

        bases = [base for base in response.sip if base.startswith("http")]
        if not bases:
            logger.warning("CDN dispatch 返回空列表, 使用兜底域名")
            bases = [FALLBACK_DOMAIN]

        self._pool = CdnPool(
            nodes=[CdnNode(base=base) for base in bases],
            fetched_at=time.time(),
            expiration=response.expiration,
        )
        logger.info("已获取 %d 个 CDN 节点", len(self._pool.nodes))
        return self._pool

    async def pick(self) -> CdnNode:
        """随机选一个健康节点. 全部失效时重新 dispatch."""
        await self.ensure()
        healthy = self._pool.healthy_nodes
        if not healthy:
            logger.info("全部 CDN 节点连续失败, 重新 dispatch")
            await self.dispatch()
            healthy = self._pool.healthy_nodes or [CdnNode(base=FALLBACK_DOMAIN)]
        node = random.choice(healthy)
        node.last_used = time.time()
        return node

    @staticmethod
    def join(node: CdnNode, purl: str) -> str:
        """拼出完整下载地址. ``purl`` 是相对路径."""
        return node.base.rstrip("/") + "/" + purl.lstrip("/")

    def report_failure(self, node: CdnNode) -> None:
        """报告一次失败."""
        node.failures += 1
        if not node.healthy:
            logger.warning("CDN 节点连续失败 %d 次, 已剔除: %s", node.failures, node.base)

    def report_success(self, node: CdnNode) -> None:
        """报告一次成功, 清零失败计数."""
        node.failures = 0

    @property
    def snapshot(self) -> dict[str, object]:
        """给 UI 的快照."""
        return {
            "total": len(self._pool.nodes),
            "healthy": len(self._pool.healthy_nodes),
            "fetched_at": self._pool.fetched_at,
            "nodes": [{"base": node.base, "failures": node.failures} for node in self._pool.nodes],
        }
