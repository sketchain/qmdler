# SPDX-License-Identifier: GPL-3.0-or-later
"""真实响应快照的契约测试.

``tests/fixtures/api/`` 现在是空的 —— 见那边的 README: 与其塞几个自己编的 JSON
把凭空 mock 换个地方再凭空一次, 不如空着, 让这些用例整体 skip.

快照一放进去, 这里就自动开始跑, 用真实结构验证我们的模型解析没跑偏.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "api"

SKIP_REASON = "tests/fixtures/api/ 里还没有真实响应快照（见该目录 README）"


def load(name: str) -> Any:
    """加载一个快照; 缺失就 skip 掉当前用例."""
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(f"{SKIP_REASON}: 缺 {name}")
    return json.loads(path.read_text("utf-8"))


def available(name: str) -> bool:
    """快照是否存在."""
    return (FIXTURES / name).exists()


# --------------------------------------------------------------------------- #
# 机制自检 —— 这几条不依赖快照, 永远要跑
# --------------------------------------------------------------------------- #


def test_fixture_dir_is_declared() -> None:
    """fixtures 目录要么不存在, 要么只放脱敏快照, 且必须有 README 说明来历."""
    readme = FIXTURES.parent / "README.md"
    assert readme.exists(), "tests/fixtures/README.md 说明了这些快照怎么来的，不能删"


def test_redaction_keeps_structure_and_types() -> None:
    """脱敏保留结构与字段类型, 只抹内容.

    换成 ``null`` 会让解析代码走上与线上不同的分支, 快照就白做了.
    """
    from scripts.redact_snapshot import redact

    raw = {
        "midurlinfo": [
            {
                "songmid": "003w2xz20QlUZt",
                "filename": "M500003w2xz20QlUZt.mp3",
                "purl": "M500003w2xz20QlUZt.mp3?guid=012e7f69&vkey=A4439DFB3D85&uin=123456&fromtag=3",
                "vkey": "A4439DFB3D855A9A93440F6A59061668937AA45152FCF2D7",
                "ekey": "",
                "result": 0,
            },
        ],
        "uin": 123456789,
        "expiration": 3600,
    }
    cleaned = redact(raw)

    item = cleaned["midurlinfo"][0]
    assert set(item) == set(raw["midurlinfo"][0]), "字段集合不能变"
    assert isinstance(item["vkey"], str), "字符串要换成字符串，不能换成 null"
    assert isinstance(cleaned["uin"], int), "数字要换成数字"
    assert item["result"] == 0, "非敏感字段原样保留"
    assert cleaned["expiration"] == 3600
    # purl 保留四字符档位编码 —— 第 1 层试听校验就靠它
    assert item["purl"].startswith("M500")
    assert item["purl"].endswith(".mp3")
    assert "vkey=" not in item["purl"]
    assert item["songmid"] == "003w2xz20QlUZt", "songmid 不是敏感信息，保留便于对照"


def test_redaction_self_check_catches_leaks() -> None:
    """脱敏脚本自己要能发现没抹干净的情况."""
    from scripts.redact_snapshot import find_leaks

    assert find_leaks('{"x": "A4439DFB3D855A9A93440F6A59061668937AA45152FCF2D7"}')
    assert find_leaks('{"k": "Q_H_L_abcdefg"}')
    assert find_leaks('{"u": "http://x/y.mp3?vkey=ABCDEF123"}')
    assert not find_leaks('{"purl": "M500<redacted>.mp3", "result": 0}')


# --------------------------------------------------------------------------- #
# 契约 —— 有快照才跑
# --------------------------------------------------------------------------- #


def test_query_song_parses_into_song_entry() -> None:
    """真实 query_song 响应能解析成 SongEntry, 且关键字段都在."""
    from qqmusic_api.models.song import QuerySongResponse

    from qmdler.core.models import from_api_song

    payload = load("query_song.json")
    response = QuerySongResponse.model_validate(payload)
    assert response.tracks, "快照里应当至少有一首歌"

    entry = from_api_song(response.tracks[0])
    assert entry.songmid
    assert entry.interval > 0
    # size_* 全 0 说明我们把档位表接错了位置
    assert any(size > 0 for size in entry.sizes.values()), "所有档位大小都是 0，档位映射可能错了"
    assert entry.media_mid, "media_mid 缺失会让取链退化成 mid+mid 形式"


def test_song_urls_parses_and_prefix_check_works() -> None:
    """真实取链响应能解析, 且第 1 层前缀校验能在真数据上跑通."""
    from qqmusic_api.models.song import GetSongUrlsResponse

    from qmdler.core.download import verify

    payload = load("song_urls.json")
    response = GetSongUrlsResponse.model_validate(payload)
    assert response.data

    info = response.data[0]
    if info.result == 0 and info.purl:
        prefix = verify.extract_prefix(info.purl)
        assert len(prefix) == 4, f"从真实 purl 里抠出的前缀不对: {prefix!r}"


def test_cdn_dispatch_parses() -> None:
    """真实 CDN dispatch 响应能解析出节点列表."""
    from qqmusic_api.models.song import GetCdnDispatchResponse

    payload = load("cdn_dispatch.json")
    response = GetCdnDispatchResponse.model_validate(payload)
    assert response.sip, "sip 为空的话 purl 拼不出完整地址"
    assert all(base.startswith("http") for base in response.sip)


def test_songlist_detail_parses_into_entries() -> None:
    """真实歌单详情能解析, 且 file/pay 确实是填好的.

    整个「默认不调 query_song」的设计就建立在这个前提上, 值得用真快照钉住.
    """
    from qqmusic_api.models.songlist import GetSonglistDetailResponse

    from qmdler.core.models import from_api_song

    payload = load("songlist_detail.json")
    response = GetSonglistDetailResponse.model_validate(payload)
    assert response.songs, "快照里应当有歌"

    entry = from_api_song(response.songs[0])
    assert entry.interval > 0
    assert any(size > 0 for size in entry.sizes.values()), (
        "列表接口返回的 Song 里 size_* 全是 0 —— 那「默认不调 query_song」的前提就不成立了"
    )


def test_lyric_is_already_decrypted() -> None:
    """库会自动解密歌词, 快照里应当是明文."""
    from qqmusic_api.models.lyric import GetLyricResponse

    from qmdler.core.metadata import lyric as lyric_utils

    payload = load("lyric.json")
    response = GetLyricResponse.model_validate(payload)
    if not response.lyric:
        pytest.skip("这首歌没有歌词")
    lines = lyric_utils.parse_any(response.lyric)
    assert lines, "解析不出歌词行，说明拿到的可能还是密文"
