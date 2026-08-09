"""测试夹具."""

from __future__ import annotations

from typing import Any

import pytest

from qmdler.core.models import SingerRef, SongEntry


@pytest.fixture
def sample_song() -> dict[str, Any]:
    """一首典型歌曲的快照数据."""
    return {
        "songmid": "003w2xz20QlUZt",
        "songid": 1234567,
        "title": "测试歌曲",
        "singers": [{"mid": "0025NhlN2yWrP4", "name": "歌手甲"}, {"mid": "0025NhlN2yWrP5", "name": "歌手乙"}],
        "album_name": "测试专辑",
        "album_mid": "000abcdefg",
        "media_mid": "003w2xz20QlUZt",
        "song_type": 0,
        "interval": 240,
        "track_no": 3,
        "disc_no": 1,
        "year": "2021-08-06",
        "status": 0,
        "sa": 0,
        "sizes": {"FLAC": 38412345, "MP3_320": 9812345, "MP3_128": 3924937, "OGG_640": 0},
        "pay": {"pay_month": 1, "pay_play": 0, "pay_down": 1, "price_track": 0},
        "size_try": 1200000,
        "try_begin_ms": 60000,
        "try_end_ms": 90000,
        "vs": [],
        "vi": [0, 0, 0, 0, 60000, 90000],
        "vf": [-3.5, 0.98, 7.2],
    }


@pytest.fixture
def entry(sample_song: dict[str, Any]) -> SongEntry:
    """SongEntry 实例."""
    return SongEntry.from_dict(sample_song)


@pytest.fixture
def minimal_entry() -> SongEntry:
    """字段大量缺失的歌曲, 用来测兜底默认值."""
    return SongEntry(
        songmid="",
        songid=0,
        title="",
        singers=[SingerRef(mid="", name="")],
        album_name="",
        album_mid="",
        media_mid="",
        song_type=0,
        interval=0,
        track_no=0,
        disc_no=0,
        year="",
        status=0,
        sa=0,
        sizes={},
        pay={},
        size_try=0,
        try_begin_ms=0,
        try_end_ms=0,
        vs=[],
        vi=[],
        vf=[],
    )
