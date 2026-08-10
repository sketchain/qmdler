# SPDX-License-Identifier: GPL-3.0-or-later
"""歌单来源.

八种来源统一产出 ``list[SongEntry]``.

**分页必须翻完**, 但必须带 limit: 这些接口都是 ``ItemPaginatedCgiRequest``,
库自带的跨页展开不设 limit 就是无限拉.

翻页一律走 :meth:`SourceService._paginate` —— 它在页与页之间插限速、支持取消、
每页推进度. 库自带的那个一次性收集器会把几十页**背靠背**打完, 不要直接用.

**关于 query_song**: 列表接口返回的已经是完整的 ``Song`` 对象 —— ``Song.file`` /
``Song.pay`` 在模型上是必填字段, 所以 ``size_*`` / ``media_mid`` / ``interval`` /
``sa`` / ``status`` 在拉列表时就全部到手了. 因此默认**不**为每首歌调用
``query_song``, 只在 ``media_mid`` 缺失或来源是「手动粘贴 mid」时逐首懒补全
(配合轻量限速). 批量补全是高级选项, 默认关闭.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from qqmusic_api import Client
from qqmusic_api.core.exceptions import ApiDataError, BaseApiException
from qqmusic_api.modules.search import SearchType
from qqmusic_api.modules.song import SongQueryInfo

from ..auth.manager import AuthManager
from ..config.schema import DownloadConfig
from ..events import EventBus, EventKind
from ..models import SongEntry, SourceType, from_api_song

logger = logging.getLogger(__name__)

#: 单次 query_song 的 mid 上限. 库里定义了 ``_GET_SONG_URLS_MAX_MID = 100``
#: 且 docstring 声明会抛 ValueError, 但 0.7.2 的函数体里**没有**任何校验,
#: 所以我们自己分批.
QUERY_SONG_MAX_MIDS = 100

#: 各来源的拉取上限. 不能只拿第一页, 但也不能无限拉.
#:
#: **默认值即上限**, 两者是同一个常量 —— REST 的 ``Field(default=…, le=…)``
#: 直接引用它, 不在两处各写一个数.
#:
#: 为什么是 10000 而不是更小: 这个工具就是给歌单批量归档用的, 五千首级别的
#: 歌单不罕见 (实测的收藏歌单就有 5075 首). 默认吃不下, 主要场景就不成立.
#: 而截断的代价是**用户以为下完了整个歌单, 实际少了几千首, 可能几个月后才发现**——
#: 这比"误点一次多翻几十页"严重得多, 何况翻页已经带限速 (5000 首约 80 秒)
#: 且随时可以取消.
#:
#: ⚠️ 即便 10000 也仍可能被更大的歌单撑爆, 所以 :class:`SourceResult` 的
#: ``truncated`` 与真实 ``total`` 必须保留, 两个前端都要**常驻**显示.
DEFAULT_LIMIT = 10000

#: 每页条数. 太小会翻很多页 (每页一次请求), 太大容易被判异常.
PAGE_SIZE = 100


class FetchCancelledError(RuntimeError):
    """拉取被用户取消. 不返回半截列表 —— 半截列表比没有更危险."""


@dataclass(slots=True)
class SourceResult:
    """一次来源拉取的结果."""

    source_type: SourceType
    identifier: str
    name: str
    entries: list[SongEntry]
    total: int
    truncated: bool
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        """转为 REST 出参 (不含歌曲明细, 明细走 items 端点)."""
        return {
            "source_type": self.source_type.value,
            "identifier": self.identifier,
            "name": self.name,
            "count": len(self.entries),
            "total": self.total,
            "truncated": self.truncated,
            "note": self.note,
        }


class SourceService:
    """从各种来源拉歌."""

    def __init__(
        self,
        client: Client,
        auth: AuthManager,
        download_config: DownloadConfig,
        bus: EventBus | None = None,
    ) -> None:
        """初始化."""
        self._client = client
        self._auth = auth
        self._config = download_config
        self._bus = bus
        self._cancel_requested = False

    # -- 轻量限速 ------------------------------------------------------------ #

    async def _metadata_delay(self) -> None:
        """元数据类请求的独立轻量限速 —— 不套用 3 分钟的下载间隔."""
        delay = random.uniform(self._config.metadata_delay_min, self._config.metadata_delay_max)
        if delay > 0:
            await asyncio.sleep(delay)

    # -- 取消 ---------------------------------------------------------------- #

    def cancel_fetch(self) -> None:
        """请求取消当前的拉取. 下一个翻页检查点生效.

        **要广播** —— 「任一端的操作对另一端立即可见」也包括这个: 不推的话
        另一端还停在「正在拉取…」上干等.

        ⚠️ 取消是**服务级**的: 两个前端同时在拉时, 任何一端点取消都会把两边
        都掐掉. 本工具是单用户本地服务, 这个取舍可以接受, 但别当成没有.
        """
        self._cancel_requested = True
        if self._bus is not None:
            self._bus.emit(EventKind.SOURCE_PROGRESS, {"cancelled": True, "done": True})

    def _check_cancelled(self) -> None:
        if self._cancel_requested:
            raise FetchCancelledError("拉取已取消")

    # -- 翻页 ---------------------------------------------------------------- #

    async def _paginate(
        self,
        request: Any,
        *,
        limit: int,
        kind: str,
        name: str = "",
    ) -> AsyncIterator[Any]:
        """跨页迭代**单条**数据项, 并在页与页之间做三件事.

        1. **限速**: ``paginate()`` 自己是连着打的 —— 5075 首的歌单按每页 100 算
           就是 51 次背靠背的请求, 是很明显的风控特征. 这里在**请求下一页之前**
           插入与元数据请求同一套的轻量延迟 (生成器是惰性的, 所以在循环末尾
           await 就等于在下一次请求之前 await).
        2. **可取消**: 51 页耗时不短, 用户必须能中断. 取消时抛
           :class:`FetchCancelledError`, **不返回半截列表**.
        3. **报进度**: 每页推一次, 否则界面要干等几十秒不知道发生了什么.
        """
        self._cancel_requested = False
        yielded = 0
        async for response in request.paginate():
            self._check_cancelled()
            items = request.items_extractor(response) or []
            for item in items:
                if yielded >= limit:
                    return
                yield item
                yielded += 1
            if yielded >= limit:
                return
            if self._bus is not None:
                self._bus.emit(
                    EventKind.SOURCE_PROGRESS,
                    {"kind": kind, "name": name, "loaded": yielded, "done": False},
                )
            # 惰性生成器: 这里 await 就是在「请求下一页」之前 await.
            await self._metadata_delay()
            self._check_cancelled()

    # -- 账号相关 ------------------------------------------------------------ #

    async def created_songlists(self) -> list[dict[str, Any]]:
        """账号自建歌单列表.

        ``get_created_songlist(uin)`` 要的是明文 musicid, 不是 euin, 因此手动导入
        的凭证也能用.
        """
        credential = await self._auth.get_valid_credential()
        response = await self._client.user.get_created_songlist(credential.musicid)
        return [
            {
                "id": item.id,
                "dirid": item.dirid,
                "title": item.title,
                "picurl": item.picurl,
                "songnum": item.songnum,
            }
            for item in response.playlists
        ]

    async def fav_songlists(self, limit: int = 200) -> list[dict[str, Any]]:
        """账号收藏的他人歌单. 需要 euin.

        走 :meth:`_paginate` 而不是 ``collect_items`` —— 后者会把几页背靠背打完,
        与其它来源不一致. 页数少不是免限速的理由.
        """
        euin = await self._require_euin()
        request = self._client.user.get_fav_songlist(euin, page=1, num=PAGE_SIZE)
        items = [
            item
            async for item in self._paginate(request, limit=limit, kind="fav_songlist", name="收藏歌单")
        ]
        return [
            {
                "id": item.id,
                "dirid": item.dirid,
                "title": item.title,
                "picurl": item.picurl,
                "songnum": item.songnum,
            }
            for item in items
        ]

    async def _require_euin(self) -> str:
        """取 euin, 缺失时给出可操作的错误说明."""
        await self._auth.get_valid_credential()
        euin = await self._auth.resolve_euin()
        if not euin:
            raise ApiDataError(
                "当前凭证缺少 encrypt_uin（euin），"
                "「我喜欢」与「收藏的歌单」无法使用。请改用扫码/手机号登录，"
                "或在导入凭证时一并填写 encrypt_uin。",
            )
        return euin

    # -- 八种来源 ------------------------------------------------------------ #

    async def from_fav_song(self, limit: int = DEFAULT_LIMIT) -> SourceResult:
        """「我喜欢」(内部 dirid=201)."""
        euin = await self._require_euin()
        request = self._client.user.get_fav_song(euin, page=1, num=PAGE_SIZE)
        entries = [
            from_api_song(song)
            async for song in self._paginate(request, limit=limit, kind="fav_song", name="我喜欢")
        ]
        return SourceResult(
            source_type=SourceType.FAV_SONG,
            identifier="201",
            name="我喜欢",
            entries=entries,
            total=len(entries),
            truncated=len(entries) >= limit,
        )

    async def from_songlist(self, songlist_id: int, limit: int = DEFAULT_LIMIT) -> SourceResult:
        """歌单 ID / 分享链接."""
        self._cancel_requested = False
        request = self._client.songlist.get_detail(songlist_id, num=PAGE_SIZE, page=1)
        # 用 paginate() 单趟走完: 既拿到每页的歌, 也顺手取到第一页里的歌单名和总数,
        # 不额外多打一次第一页. 这条不能复用 _paginate() —— 它还要顺手取歌单名与
        # 服务端声明的 total, 所以在这里把限速/取消/进度三件事重做一遍.
        entries: list[SongEntry] = []
        title = ""
        total = 0
        async for response in request.paginate():
            self._check_cancelled()
            if not title:
                title = response.info.title
                total = response.total
            entries.extend(from_api_song(song) for song in response.songs)
            if len(entries) >= limit:
                del entries[limit:]
                break
            if self._bus is not None:
                self._bus.emit(
                    EventKind.SOURCE_PROGRESS,
                    {"kind": "songlist", "name": title, "loaded": len(entries),
                     "total": total, "done": False},
                )
            await self._metadata_delay()
            self._check_cancelled()
        return SourceResult(
            source_type=SourceType.SONGLIST,
            identifier=str(songlist_id),
            name=title or f"歌单 {songlist_id}",
            entries=entries,
            total=total or len(entries),
            truncated=len(entries) < total,
        )

    async def from_album(self, album: str | int, limit: int = DEFAULT_LIMIT) -> SourceResult:
        """专辑."""
        request = self._client.album.get_song(album, num=PAGE_SIZE, page=1)
        entries = [
            from_api_song(song)
            async for song in self._paginate(request, limit=limit, kind="album", name=str(album))
        ]
        name = entries[0].album_name if entries else str(album)
        return SourceResult(
            source_type=SourceType.ALBUM,
            identifier=str(album),
            name=name or str(album),
            entries=entries,
            total=len(entries),
            truncated=len(entries) >= limit,
        )

    async def from_singer(self, singer_mid: str, limit: int = DEFAULT_LIMIT) -> SourceResult:
        """歌手全部作品."""
        request = self._client.singer.get_songs_list(singer_mid, num=PAGE_SIZE, page=1)
        entries = [
            from_api_song(song)
            async for song in self._paginate(request, limit=limit, kind="singer", name=singer_mid)
        ]
        name = entries[0].singer_names[0] if entries and entries[0].singer_names else singer_mid
        return SourceResult(
            source_type=SourceType.SINGER,
            identifier=singer_mid,
            name=name,
            entries=entries,
            total=len(entries),
            truncated=len(entries) >= limit,
        )

    async def from_search(self, keyword: str, limit: int = 200) -> SourceResult:
        """搜索结果."""
        request = self._client.search.search_by_type(
            keyword,
            SearchType.SONG,
            num=min(PAGE_SIZE, 50),
            page=1,
        )
        entries = [
            from_api_song(song)
            async for song in self._paginate(request, limit=limit, kind="search", name=keyword)
            if hasattr(song, "mid")
        ]
        return SourceResult(
            source_type=SourceType.SEARCH,
            identifier=keyword,
            name=f"搜索：{keyword}",
            entries=entries,
            total=len(entries),
            truncated=len(entries) >= limit,
        )

    async def from_mids(self, mids: list[str], name: str = "手动列表") -> SourceResult:
        """手动粘贴的 mid 列表.

        这是唯一必须调 ``query_song`` 的来源 —— 只有 mid, 没有任何详情.
        默认逐首请求并配合轻量限速; 批量是高级选项.
        """
        entries = await self.complete_by_mids(mids)
        missing = len(mids) - len(entries)
        return SourceResult(
            source_type=SourceType.MANUAL,
            identifier=",".join(mids[:5]) + ("…" if len(mids) > 5 else ""),
            name=name,
            entries=entries,
            total=len(mids),
            truncated=False,
            note=f"{missing} 个 mid 未查到详情" if missing > 0 else "",
        )

    # -- 详情补全 ------------------------------------------------------------ #

    async def complete_by_mids(self, mids: list[str]) -> list[SongEntry]:
        """用 ``query_song`` 按 mid 补全详情."""
        if not mids:
            return []
        await self._auth.get_valid_credential()

        if self._config.batch_query_song:
            return await self._complete_batched(mids)
        return await self._complete_one_by_one(mids)

    async def _complete_one_by_one(self, mids: list[str]) -> list[SongEntry]:
        entries: list[SongEntry] = []
        for index, mid in enumerate(mids):
            if index:
                await self._metadata_delay()
            try:
                response = await self._client.song.query_song([SongQueryInfo(mid=mid)])
            except BaseApiException as exc:
                logger.warning("查询歌曲详情失败 mid=%s: %s", mid, exc)
                continue
            entries.extend(from_api_song(song) for song in response.tracks)
        return entries

    async def _complete_batched(self, mids: list[str]) -> list[SongEntry]:
        """高级选项: 批量补全. 单次不超过接口上限, 库不校验所以自己分批."""
        size = min(self._config.batch_query_size, QUERY_SONG_MAX_MIDS)
        entries: list[SongEntry] = []
        for index in range(0, len(mids), size):
            if index:
                await self._metadata_delay()
            chunk = mids[index : index + size]
            try:
                response = await self._client.song.query_song([SongQueryInfo(mid=mid) for mid in chunk])
            except BaseApiException as exc:
                logger.warning("批量查询歌曲详情失败 (%d 个): %s", len(chunk), exc)
                continue
            entries.extend(from_api_song(song) for song in response.tracks)
        return entries

    async def ensure_media_mid(self, entry: SongEntry) -> SongEntry:
        """懒补全: 只有 ``media_mid`` 缺失时才补一次详情.

        ``media_mid`` 是拼 filename 用的, 缺了取链会退化成
        ``{前缀}{mid}{mid}{扩展名}`` 的形式.
        """
        if entry.media_mid:
            return entry
        completed = await self.complete_by_mids([entry.songmid])
        return completed[0] if completed else entry
