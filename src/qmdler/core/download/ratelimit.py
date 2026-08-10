# SPDX-License-Identifier: GPL-3.0-or-later
"""下载间隔与轻量限速.

两套完全独立的节奏, 不能一刀切:

* **下载间隔** —— 每首歌之间, 默认 180 秒, 支持固定值或随机区间. UI 上显示倒计时,
  用户可以「立即下载下一首」跳过等待.
* **元数据轻量限速** —— query_song / 歌词 / 封面 / 制作人这类请求, 0.5~1 秒即可.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable

from ..config.schema import DownloadConfig


class IntervalWaiter:
    """歌与歌之间的间隔等待器, 可被打断."""

    def __init__(self, config: DownloadConfig, on_tick: Callable[[float, float], None] | None = None) -> None:
        """初始化.

        Args:
            config: 下载配置.
            on_tick: 每秒回调 ``(剩余秒数, 总秒数)``, 用于推送倒计时.
        """
        self._config = config
        self._on_tick = on_tick
        self._skip = asyncio.Event()
        self._remaining = 0.0
        self._total = 0.0
        self._waiting = False

    def next_interval(self) -> float:
        """算出下一次的间隔秒数."""
        config = self._config
        if config.random_interval:
            low = max(0.0, config.interval_min_seconds)
            high = max(low, config.interval_max_seconds)
            return random.uniform(low, high)
        return max(0.0, config.interval_seconds)

    def skip(self) -> None:
        """立即下载下一首, 跳过剩余等待."""
        self._skip.set()

    @property
    def waiting(self) -> bool:
        """当前是否正在等待."""
        return self._waiting

    @property
    def remaining(self) -> float:
        """剩余秒数."""
        return self._remaining

    @property
    def total(self) -> float:
        """本次等待的总秒数."""
        return self._total

    async def wait(self, seconds: float | None = None) -> bool:
        """等待间隔.

        Returns:
            True 表示等满了, False 表示被「立即下载」跳过.
        """
        duration = self.next_interval() if seconds is None else seconds
        self._skip.clear()
        self._total = duration
        self._remaining = duration
        self._waiting = True

        deadline = time.monotonic() + duration
        try:
            while True:
                remaining = deadline - time.monotonic()
                self._remaining = max(0.0, remaining)
                if remaining <= 0:
                    return True
                if self._on_tick is not None:
                    self._on_tick(self._remaining, self._total)
                try:
                    await asyncio.wait_for(self._skip.wait(), timeout=min(1.0, remaining))
                except TimeoutError:
                    continue
                return False
        finally:
            self._waiting = False
            self._remaining = 0.0
            self._skip.clear()


class MetadataLimiter:
    """元数据类请求的轻量限速."""

    def __init__(self, config: DownloadConfig) -> None:
        """初始化."""
        self._config = config
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """在两次元数据请求之间插入一个短暂间隔."""
        async with self._lock:
            gap = random.uniform(self._config.metadata_delay_min, self._config.metadata_delay_max)
            elapsed = time.monotonic() - self._last
            if elapsed < gap:
                await asyncio.sleep(gap - elapsed)
            self._last = time.monotonic()
