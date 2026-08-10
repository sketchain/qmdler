# SPDX-License-Identifier: GPL-3.0-or-later
"""CDN 池.

``purl`` 是**相对路径**, 必须先 ``client.song.get_cdn_dispatch()`` 拿到 ``sip``
域名列表, ``cdn + purl`` 才能下载.

====================================================================
关于 403 —— 这是本模块唯一无法从代码推导出的经验知识, 改动前务必读完
====================================================================

**403 表示「这个节点不服务这条 purl」, 既不是链接失效, 也不是节点故障.**

实测依据 (2026-08, 未登录取试听档, 12 首不同的歌):

1. dispatch 返回 6 个节点, 用 ``GetCdnDispatchResponse.test_file`` 这个 keepalive
   探针逐个探, **6/6 全部返回 200** —— 排除「节点挂了」;
2. 同一条 purl 在放行节点上可以稳定、完整地下下来 (下载 + 续传 + 逐字节比对均通过)
   —— 排除「vkey 过期」;
3. 12 条不同的 purl, 放行分布高度一致::

       https://sjy6.stream.qqmusic.qq.com/   12/12 放行
       http://sjy6.stream.qqmusic.qq.com/    10/12 放行
       http://aqqmusic.tc.qq.com/             0/12
       https://aqqmusic.tc.qq.com/            0/12
       http://ws.stream.qqmusic.qq.com/       0/12
       http://ws6.stream.qqmusic.qq.com/      0/12

   随机挑一个的首次命中率 30.6%, 固定挑历史最优节点则是 100%.

**因此: 遇到 403 换节点重试同一条 purl (零额外 API 请求), 不要重新取 vkey.**
把 403 当成链接失效去重新取 vkey, 会让每首歌多打好几次 ``get_song_urls`` ——
正是最典型的风控特征. 这个坑踩过一次, 别再把 403 加回 ``STALE_LINK_STATUSES``.

由第 3 点还可以看出: 放行与否在不同 purl 之间是稳定的, 更像是客户端/出口 IP 维度
的属性, 而不是 (purl, node) 的一次性绑定. 所以节点级偏好是有价值的 (30.6% → 100%),
但**必须是可以变的**: 用「最近一次成功」+「自上次成功以来的连续拒绝数」来排序,
一次成功即清零拒绝计数, 网络环境变了能自动跟着换.

真正一次性的是**本首歌本条 purl 已经拒过的节点**, 那个由调用方按 purl 维护,
处理完这首歌即丢弃, 不跨曲目累积 (否则跑十几首之后所有节点都带着拒绝记录,
排序就退化成随机了).

其余策略:

* 任务开始时 dispatch 一次并缓存;
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

#: 连接层面连续失败多少次就把这个 CDN 剔除 (403 不计入).
MAX_CONSECUTIVE_FAILURES = 3

#: 「最近成功过」的有效期 (秒). 超过就退回「未知」重新试探, 免得网络环境变了
#: 还死守着一个早就不放行的节点.
SUCCESS_TTL = 30 * 60

#: 自上次成功以来连续拒绝多少次, 就不再把它当优选节点.
MAX_REJECTIONS_BEFORE_DEMOTE = 2


@dataclass(slots=True)
class CdnNode:
    """一个 CDN 节点."""

    base: str
    #: 连接层面的失败 (超时、5xx). 只有这个会导致节点被剔除.
    failures: int = 0
    #: **自上次成功以来**的连续 403 次数. 一次成功即清零 —— 不是永久污点,
    #: 否则跑一阵之后所有节点都带着拒绝记录, 排序退化成随机.
    rejections_since_success: int = 0
    #: 累计成功次数 (仅用于观察).
    successes: int = 0
    #: 最近一次成功的时间戳. 排序看的是它, 不是累计次数.
    last_success: float = 0.0
    last_used: float = 0.0

    @property
    def healthy(self) -> bool:
        """是否还在可用集合里 (只看连接层面的失败)."""
        return self.failures < MAX_CONSECUTIVE_FAILURES

    @property
    def recently_served(self) -> bool:
        """最近是否成功服务过, 且此后没有连续被拒."""
        if not self.last_success or time.time() - self.last_success > SUCCESS_TTL:
            return False
        return self.rejections_since_success < MAX_REJECTIONS_BEFORE_DEMOTE

    @property
    def tier(self) -> int:
        """候选优先级: 0 最近成功过 / 1 情况未知 / 2 最近连续被拒.

        注意三档都是**可逆**的: 成功清零拒绝计数, 成功记录也会随
        :data:`SUCCESS_TTL` 过期退回「未知」.
        """
        if self.recently_served:
            return 0
        if self.rejections_since_success >= MAX_REJECTIONS_BEFORE_DEMOTE:
            return 2
        return 1


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

        # 去重: dispatch 实测会把同一个域名返回多次 (2026-08 实测 7 个节点里
        # ``http://aqqmusic.tc.qq.com/`` 占了 3 个). 不去重的话同档内随机等于
        # 给重复的那个加权, 而它恰好是不放行的那个 —— 白白多试两次.
        bases = list(dict.fromkeys(base for base in response.sip if base.startswith("http")))
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

    async def candidates(self, exclude: set[str] | None = None) -> list[CdnNode]:
        """返回本次下载要依次尝试的节点顺序.

        Args:
            exclude: **本条 purl** 已经拒过的节点 base. 由调用方按 purl 维护,
                这首歌处理完就丢掉 —— 绝不能攒到节点上跨曲目累积, 否则跑一阵
                之后所有节点都带着拒绝记录, 排序就退化成随机了.

        顺序: 最近成功过 → 情况未知 → 最近连续被拒; 同档内随机, 分散压力.
        三档都是可逆的, 见 :attr:`CdnNode.tier`. 依据见模块 docstring.
        """
        await self.ensure()
        healthy = self._pool.healthy_nodes
        if not healthy:
            logger.info("全部 CDN 节点连续失败, 重新 dispatch")
            await self.dispatch()
            healthy = self._pool.healthy_nodes or [CdnNode(base=FALLBACK_DOMAIN)]

        skip = exclude or set()
        ordered = [node for node in healthy if node.base not in skip]
        random.shuffle(ordered)
        ordered.sort(key=lambda node: (node.tier, -node.last_success))
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
        """报告一次 403: 该节点不服务这条 purl.

        **不计入** ``failures`` —— 节点本身是好的 (keepalive 探针 200),
        只是不给这条链接. 记的是「自上次成功以来」的连续拒绝数, 成功即清零,
        所以这不是永久污点.
        """
        node.rejections_since_success += 1

    def report_success(self, node: CdnNode) -> None:
        """报告一次成功: 清零失败与拒绝计数, 刷新最近成功时间."""
        node.failures = 0
        node.rejections_since_success = 0
        node.successes += 1
        node.last_success = time.time()

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
                    "rejections_since_success": node.rejections_since_success,
                    "successes": node.successes,
                    "last_success": node.last_success,
                    "tier": node.tier,
                }
                for node in self._pool.nodes
            ],
        }
