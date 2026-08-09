"""CDN 池.

``purl`` 是**相对路径**, 必须先 ``client.song.get_cdn_dispatch()`` 拿到 ``sip``
域名列表, ``cdn + purl`` 才能下载.

策略 (按实测结果调整过, 不是简单随机挑一个):

* 任务开始时 dispatch 一次并缓存;
* 每首歌拿到一个**有序**的候选列表 —— 已验证可用的排前面, 没试过的居中,
  曾经拒绝过的垫底; 同一档内随机, 分散压力;
* 403 记 ``rejections`` 而**不是** ``failures``: 实测同一条 purl 在 6 个节点里
  往往只有 1 个放行, 其余全 403, 但这 6 个节点的 keepalive 探针都返回 200 ——
  节点是好的, 只是不服务这条链接. 换节点重试即可, 不必重新取 vkey;
* 连接层面连续失败 (超时/5xx) 才剔除节点; 全部失效时重新 dispatch;
* dispatch 整个失败时用库内置的兜底域名.
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
    #: 连接层面的失败 (超时、5xx).
    failures: int = 0
    #: 拒绝服务本次 purl 的次数 (403). 与节点健康无关 —— 实测中同一个 purl
    #: 在多数节点上都是 403, 只有其中一个能放行, 但所有节点的 keepalive 探针
    #: 都返回 200. 详见 ``CdnManager.candidates``.
    rejections: int = 0
    #: 成功服务过的次数. 服务过一次的节点大概率还能继续服务.
    successes: int = 0
    last_used: float = 0.0

    @property
    def healthy(self) -> bool:
        """是否还在可用集合里."""
        return self.failures < MAX_CONSECUTIVE_FAILURES

    @property
    def tier(self) -> int:
        """候选优先级: 0 已验证可用 / 1 未试过 / 2 曾经拒绝过."""
        if self.successes > 0:
            return 0
        if self.rejections == 0:
            return 1
        return 2


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
        """选一个节点. 全部失效时重新 dispatch."""
        candidates = await self.candidates()
        return candidates[0]

    async def candidates(self) -> list[CdnNode]:
        """返回本次下载要依次尝试的节点顺序.

        **不是简单随机挑一个.** 实测 (未登录取试听档, 2026-08) 发现: 同一个
        ``purl`` 在 dispatch 返回的 6 个节点里只有 1 个放行, 其余全部 403 ——
        但这 6 个节点的 keepalive 探针 (``GetCdnDispatchResponse.test_file``)
        都返回 200. 也就是说 403 是「这个节点不给这条 purl」, 不是节点挂了,
        更不是 vkey 过期.

        所以策略是: 同一条 purl 依次换节点重试 (不花任何额外 API 请求),
        而不是一遇 403 就重新取 vkey —— 后者会把每首歌的接口请求数翻好几倍,
        正是最该避免的风控特征.

        顺序: 已验证可用 → 没试过 → 曾经拒绝过; 同一档内随机, 分散压力.
        """
        await self.ensure()
        healthy = self._pool.healthy_nodes
        if not healthy:
            logger.info("全部 CDN 节点连续失败, 重新 dispatch")
            await self.dispatch()
            healthy = self._pool.healthy_nodes or [CdnNode(base=FALLBACK_DOMAIN)]

        ordered = list(healthy)
        random.shuffle(ordered)
        ordered.sort(key=lambda node: (node.tier, -node.successes))
        for node in ordered:
            node.last_used = time.time()
        return ordered

    @staticmethod
    def join(node: CdnNode, purl: str) -> str:
        """拼出完整下载地址. ``purl`` 是相对路径."""
        return node.base.rstrip("/") + "/" + purl.lstrip("/")

    def report_failure(self, node: CdnNode) -> None:
        """报告一次连接层面的失败 (超时 / 5xx)."""
        node.failures += 1
        if not node.healthy:
            logger.warning("CDN 节点连续失败 %d 次, 已剔除: %s", node.failures, node.base)

    def report_rejection(self, node: CdnNode) -> None:
        """报告一次 403: 该节点不给这条 purl.

        不计入 ``failures`` —— 节点本身是好的, 只是不服务这条链接.
        """
        node.rejections += 1

    def report_success(self, node: CdnNode) -> None:
        """报告一次成功, 清零失败计数并记一次成功."""
        node.failures = 0
        node.successes += 1

    @property
    def snapshot(self) -> dict[str, object]:
        """给 UI 的快照."""
        return {
            "total": len(self._pool.nodes),
            "healthy": len(self._pool.healthy_nodes),
            "fetched_at": self._pool.fetched_at,
            "nodes": [
                {
                    "base": node.base,
                    "failures": node.failures,
                    "rejections": node.rejections,
                    "successes": node.successes,
                }
                for node in self._pool.nodes
            ],
        }
