"""分享链接 / 各类 ID 的解析."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..models import SourceType

#: 歌单: y.qq.com/n/ryqq/playlist/7392570702 、i.y.qq.com/n2/m/share/details/taoge.html?id=xxx
_SONGLIST_PATTERNS = [
    re.compile(r"/playlist/(\d+)"),
    re.compile(r"[?&](?:id|disstid)=(\d+)"),
    re.compile(r"/taoge[^?]*\?.*?\bid=(\d+)"),
]

#: 专辑: y.qq.com/n/ryqq/albumDetail/000xxxx 、.../album/000xxxx.html
_ALBUM_PATTERNS = [
    re.compile(r"/album(?:Detail)?/([0-9A-Za-z]+)"),
    re.compile(r"[?&]albummid=([0-9A-Za-z]+)"),
]

#: 歌手: y.qq.com/n/ryqq/singer/0025NhlN2yWrP4
_SINGER_PATTERNS = [
    re.compile(r"/singer/([0-9A-Za-z]+)"),
    re.compile(r"[?&]singermid=([0-9A-Za-z]+)"),
]

#: 单曲: y.qq.com/n/ryqq/songDetail/003w2xz20QlUZt
_SONG_PATTERNS = [
    re.compile(r"/song(?:Detail)?/([0-9A-Za-z]+)"),
    re.compile(r"[?&]songmid=([0-9A-Za-z]+)"),
]

#: 一行一个或逗号/空格分隔的 mid.
_MID_SPLIT_RE = re.compile(r"[\s,;，、]+")
_MID_RE = re.compile(r"^[0-9A-Za-z]{10,20}$")


@dataclass(slots=True)
class ResolvedSource:
    """解析结果."""

    source_type: SourceType
    identifier: str
    raw: str

    @property
    def is_numeric(self) -> bool:
        """标识符是否是纯数字 ID."""
        return self.identifier.isdigit()


def _first_match(text: str, patterns: list[re.Pattern[str]]) -> str:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return ""


def resolve(text: str) -> ResolvedSource | None:
    """从一段输入里解析出歌单 / 专辑 / 歌手 / 单曲.

    支持完整分享链接、短链文本、以及裸 ID.
    """
    raw = (text or "").strip()
    if not raw:
        return None

    # 纯数字 → 歌单 ID
    if raw.isdigit():
        return ResolvedSource(SourceType.SONGLIST, raw, raw)

    songlist_id = _first_match(raw, _SONGLIST_PATTERNS)
    if songlist_id:
        return ResolvedSource(SourceType.SONGLIST, songlist_id, raw)

    album_mid = _first_match(raw, _ALBUM_PATTERNS)
    if album_mid:
        return ResolvedSource(SourceType.ALBUM, album_mid, raw)

    singer_mid = _first_match(raw, _SINGER_PATTERNS)
    if singer_mid:
        return ResolvedSource(SourceType.SINGER, singer_mid, raw)

    song_mid = _first_match(raw, _SONG_PATTERNS)
    if song_mid:
        return ResolvedSource(SourceType.MANUAL, song_mid, raw)

    # 裸 mid (歌单 mid 不存在, 只可能是专辑/歌手/单曲, 交给调用方指定类型)
    if _MID_RE.match(raw):
        return ResolvedSource(SourceType.MANUAL, raw, raw)

    return None


def parse_mid_list(text: str) -> list[str]:
    """解析手动粘贴的 mid 列表.

    每行一个, 也接受逗号/空格/顿号分隔; 会顺带从分享链接里抠出 songmid.
    """
    mids: list[str] = []
    seen: set[str] = set()
    for token in _MID_SPLIT_RE.split(text or ""):
        candidate = token.strip()
        if not candidate:
            continue
        if not _MID_RE.match(candidate):
            extracted = _first_match(candidate, _SONG_PATTERNS)
            candidate = extracted
        if candidate and _MID_RE.match(candidate) and candidate not in seen:
            seen.add(candidate)
            mids.append(candidate)
    return mids
