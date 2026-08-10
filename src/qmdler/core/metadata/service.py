# SPDX-License-Identifier: GPL-3.0-or-later
"""附加元数据的获取与落盘.

歌词、封面、作曲/作词、流派都在这里, 统一走**元数据轻量限速** (0.5~1s),
不套用下载之间那 3 分钟的间隔.

三条会产生额外请求的路径默认关闭, 只在用户真的用到时才发:

* ``{作曲}`` / ``{作词}`` → ``song.get_producer(mid)``, 只能按歌请求;
* ``{流派}``           → ``album.get_detail(album_mid)``, **按 album_mid 缓存**,
  整张专辑只请求一次;
* 歌词                 → ``lyric.get_lyric(...)``.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from qqmusic_api import Client

from ..config.schema import CoverConfig, DownloadConfig, LyricConfig, TagConfig
from ..models import SongEntry, SubStatus
from . import lyric as lyric_utils
from .cover import EMPTY_COVER, CoverFetcher, CoverResult

logger = logging.getLogger(__name__)

#: 这个模块里的每一次外部请求都 ``except Exception``, 是有意的:
#:
#: 实测撞到过 ``song.get_producer`` 抛 pydantic ``ValidationError`` ——
#: 上游模型的 ``Lst`` 字段没标 Optional, 服务端回 ``null`` 就炸.
#: 它既不是 ``BaseApiException`` 也不是网络错误, 只 catch 那两类就会漏,
#: 而漏出去的后果是**整首歌被记成 failed**, 尽管音频早就完整落盘了.
#:
#: 附加内容的失败模式来自「上游模型 × 服务端返回」的组合, 列不全.
#: 这里的取舍很明确: 宁可静默降级成「没拿到」, 也不能拖垮主流程.
#: 引擎侧 ``_best_effort`` 还有一层同样的兜底.
_WHY_BROAD = __doc__

#: ``song.get_producer`` 返回的分组标题里, 这些算作曲 / 作词.
_COMPOSER_TITLES = ("作曲", "谱曲", "Composer")
_LYRICIST_TITLES = ("作词", "填词", "Lyricist")


@dataclass(slots=True)
class LyricBundle:
    """一首歌的歌词产物."""

    status: SubStatus = SubStatus.SKIPPED
    #: 要写进 tag 的文本 (原文, 可能已按双语策略合并).
    embed_text: str = ""
    #: 要落盘的文件: ``{相对文件名后缀: 内容}``, 如 ``{"": 原文, ".zh": 译文}``.
    files: dict[str, str] = field(default_factory=dict)
    message: str = ""


@dataclass(slots=True)
class ExtraTags:
    """需要额外请求才能拿到的 tag 字段."""

    composer: str = ""
    lyricist: str = ""
    genre: str = ""


class MetadataService:
    """歌词 / 封面 / 附加 tag 字段."""

    def __init__(
        self,
        client: Client,
        http: httpx.AsyncClient,
        download_config: DownloadConfig,
    ) -> None:
        """初始化."""
        self._client = client
        self._http = http
        self._config = download_config
        self._cover = CoverFetcher(http, delay=self._delay)
        #: album_mid → 流派文本. 整张专辑只请求一次.
        self._genre_cache: dict[str, str] = {}

    async def _delay(self) -> None:
        delay = random.uniform(self._config.metadata_delay_min, self._config.metadata_delay_max)
        if delay > 0:
            await asyncio.sleep(delay)

    # -- 歌词 ---------------------------------------------------------------- #

    async def fetch_lyric(self, entry: SongEntry, config: LyricConfig) -> LyricBundle:
        """拉取并渲染歌词.

        该歌无歌词时记为「无歌词」继续, **不算失败**.
        """
        if not config.enabled:
            return LyricBundle(status=SubStatus.SKIPPED)

        await self._delay()
        try:
            response = await self._client.lyric.get_lyric(
                entry.songmid,
                qrc=config.qrc,
                trans=config.translation,
                roma=config.romaji,
                singing_annotations=config.singing_annotations,
            )
        except Exception as exc:  # 附加内容, 见下方 _WHY_BROAD
            logger.warning("获取歌词失败 %s: %s", entry.songmid, exc)
            return LyricBundle(status=SubStatus.FAILED, message=str(exc))

        # 库已经自动解密, 这里拿到的是明文.
        raw_original = response.lyric or ""
        if lyric_utils.is_empty(raw_original):
            return LyricBundle(status=SubStatus.NONE, message="该歌曲无歌词")

        bundle = LyricBundle(status=SubStatus.OK)
        original_lines = lyric_utils.parse_any(raw_original)
        translation_lines = lyric_utils.parse_any(response.trans) if config.translation else []

        if config.output_format == "raw_qrc":
            bundle.files[""] = raw_original
            bundle.embed_text = lyric_utils.to_line_lrc(original_lines)
        else:
            merged = lyric_utils.merge_bilingual(
                original_lines,
                translation_lines,
                config.bilingual_mode,
                config.output_format,
            )
            bundle.files[""] = merged
            bundle.embed_text = merged

        if config.translation and translation_lines and config.bilingual_mode == "separate_file":
            bundle.files[".zh"] = lyric_utils.render(translation_lines, config.output_format)

        if config.romaji and response.roma:
            roma_lines = lyric_utils.parse_any(response.roma)
            if roma_lines:
                bundle.files[".roma"] = lyric_utils.render(roma_lines, config.output_format)

        if config.singing_annotations and response.singing_annotations_lyric:
            annotation_lines = lyric_utils.parse_any(response.singing_annotations_lyric)
            if annotation_lines:
                bundle.files[".sing"] = lyric_utils.render(annotation_lines, config.output_format)

        if config.multi_style_translation and response.has_multi_trans and entry.songid:
            await self._delay()
            try:
                multi = await self._client.lyric.get_multi_style_trans_lyric(entry.songid)
            except Exception as exc:  # 附加内容, 见下方 _WHY_BROAD
                logger.debug("多风格翻译获取失败 %s: %s", entry.songmid, exc)
            else:
                for item in getattr(multi, "lyrics", None) or []:
                    lines = lyric_utils.parse_any(item.lyric)
                    if lines:
                        suffix = f".{item.style_name or item.style}"
                        bundle.files[suffix] = lyric_utils.render(lines, config.output_format)

        return bundle

    @staticmethod
    def write_lyric_files(bundle: LyricBundle, audio_path: Path, config: LyricConfig) -> list[Path]:
        """把歌词写成独立 ``.lrc`` 文件 (与音频同目录同名)."""
        if not config.save_as_file or bundle.status is not SubStatus.OK:
            return []
        written: list[Path] = []
        extension = ".qrc" if config.output_format == "raw_qrc" else ".lrc"
        for suffix, content in bundle.files.items():
            if not content.strip():
                continue
            target = audio_path.with_suffix("")
            path = target.parent / f"{target.name}{config.file_suffix}{suffix}{extension}"
            path.write_text(content, encoding="utf-8")
            written.append(path)
        return written

    # -- 封面 ---------------------------------------------------------------- #

    async def fetch_cover(self, entry: SongEntry, config: CoverConfig, *, for_embed: bool) -> CoverResult:
        """取封面. 嵌入与单独保存可以用不同尺寸."""
        if not config.enabled:
            return EMPTY_COVER
        size = config.embed_size if for_embed else config.save_size
        return await self._cover.fetch(entry, size)

    @staticmethod
    def write_cover_file(cover: CoverResult, path: Path) -> Path | None:
        """把封面写成独立图片文件."""
        if not cover.ok:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(cover.data)
        return path

    # -- 额外 tag 字段 --------------------------------------------------------- #

    async def fetch_extra_tags(self, entry: SongEntry, config: TagConfig) -> ExtraTags:
        """按需拉取作曲 / 作词 / 流派."""
        extra = ExtraTags()
        if not config.enabled:
            return extra

        if config.needs_producer:
            extra.composer, extra.lyricist = await self._fetch_producer(entry)
        if config.needs_album_detail:
            extra.genre = await self._fetch_genre(entry)
        return extra

    async def _fetch_producer(self, entry: SongEntry) -> tuple[str, str]:
        """作曲 / 作词.

        ``Song`` 模型上没有这两个字段, 只能走 ``song.get_producer``; 该接口按歌
        请求, 无法跨曲复用.
        """
        await self._delay()
        try:
            response = await self._client.song.get_producer(entry.songmid)
        except Exception as exc:  # 附加内容, 见下方 _WHY_BROAD
            logger.debug("获取制作人失败 %s: %s", entry.songmid, exc)
            return "", ""

        composer: list[str] = []
        lyricist: list[str] = []
        for group in response.data:
            names = [producer.name for producer in group.producers if producer.name]
            if any(keyword in group.title for keyword in _COMPOSER_TITLES):
                composer.extend(names)
            elif any(keyword in group.title for keyword in _LYRICIST_TITLES):
                lyricist.extend(names)
        return "、".join(dict.fromkeys(composer)), "、".join(dict.fromkeys(lyricist))

    async def _fetch_genre(self, entry: SongEntry) -> str:
        """流派文本.

        ``Song.genre`` 是个 int ID, 而它引用的「文档 5.3.1 映射表」在库源码和官方
        文档站里都不存在 —— 不臆造映射表, 直接用 ``album.get_detail`` 返回的流派
        **文本**. 按 album_mid 缓存, 整张专辑只请求一次.
        """
        album_mid = entry.album_mid
        if not album_mid:
            return ""
        cached = self._genre_cache.get(album_mid)
        if cached is not None:
            return cached

        await self._delay()
        try:
            response = await self._client.album.get_detail(album_mid)
        except Exception as exc:  # 附加内容, 见下方 _WHY_BROAD
            logger.debug("获取专辑详情失败 %s: %s", album_mid, exc)
            self._genre_cache[album_mid] = ""
            return ""

        genre = (response.album.genre or "").strip()
        self._genre_cache[album_mid] = genre
        return genre

    def clear_caches(self) -> None:
        """清空封面与流派缓存 (任务结束时调用)."""
        self._cover.clear()
        self._genre_cache.clear()
