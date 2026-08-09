"""封面下载.

尺寸是**固定六档** (``CoverSize = Literal[150, 300, 500, 800, 1200, 1500]``),
UI 里做成下拉选择, 不让用户填任意数字.

``Song.cover_url()`` 的内部逻辑是专辑封面优先, 没有则退到首个可用歌手封面,
两者都没有时返回空字符串 —— 必须处理空值.

拼出来的 URL 还要**实际请求验证**: 高尺寸不一定存在, 404 时自动降到下一档.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from ..models import SongEntry

logger = logging.getLogger(__name__)

#: 六档固定尺寸, 由大到小.
COVER_SIZES: tuple[int, ...] = (1500, 1200, 800, 500, 300, 150)


@dataclass(slots=True)
class CoverResult:
    """一次封面获取的结果."""

    data: bytes
    size: int
    url: str
    mime: str = "image/jpeg"

    @property
    def ok(self) -> bool:
        """是否拿到了图片."""
        return bool(self.data)


EMPTY_COVER = CoverResult(data=b"", size=0, url="")


def candidate_sizes(preferred: int) -> list[int]:
    """从首选尺寸开始, 依次降档的候选列表."""
    ordered = [size for size in COVER_SIZES if size <= preferred]
    if not ordered:
        ordered = list(COVER_SIZES)
    return ordered


class CoverFetcher:
    """按尺寸取封面, 404 自动降档."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        """初始化."""
        self._http = http
        #: 同一张专辑封面在整张专辑内复用, 避免重复下载.
        self._cache: dict[tuple[str, int], CoverResult] = {}

    async def fetch(self, entry: SongEntry, preferred_size: int) -> CoverResult:
        """取封面.

        Args:
            entry: 歌曲快照.
            preferred_size: 首选尺寸, 必须是六档之一.
        """
        cache_key = (entry.album_mid or entry.songmid, preferred_size)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        for size in candidate_sizes(preferred_size):
            url = entry.cover_url(size)
            if not url:
                # 专辑与歌手都没有 mid, 再降档也没用.
                break
            try:
                response = await self._http.get(url)
            except httpx.HTTPError as exc:
                logger.debug("封面请求失败 %s: %s", url, exc)
                continue
            if response.status_code == 200 and response.content:
                result = CoverResult(
                    data=response.content,
                    size=size,
                    url=url,
                    mime=response.headers.get("content-type", "image/jpeg").split(";")[0].strip(),
                )
                self._cache[cache_key] = result
                return result
            logger.debug("封面 %dpx 不存在 (HTTP %s), 降到下一档", size, response.status_code)

        self._cache[cache_key] = EMPTY_COVER
        return EMPTY_COVER

    def clear(self) -> None:
        """清空缓存."""
        self._cache.clear()
