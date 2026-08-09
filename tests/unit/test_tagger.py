"""标签写入 —— 用**真实音频文件**跑, 不 mock mutagen.

这个模块之前一条用例都没有, 代价是 **MP3 的 tag 从来就没写进去过**:
``MP3.tags`` 是个 ``ID3`` 对象, 但 mutagen 不会把文件名传给它 ——
``audio.tags.filename`` 恒为 ``None``, 于是 ``tags.delete()`` 直接抛
``TypeError: Missing filename or fileobj argument``. 全 mock 的用例发现不了
这种事, 所以这里坚持用 ``tests/data/audio/`` 下的真文件.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from mutagen import File as MutagenFile
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.oggvorbis import OggVorbis

from qmdler.core.config.schema import TagConfig
from qmdler.core.metadata import tagger
from qmdler.core.models import SongEntry

AUDIO = Path(__file__).resolve().parents[1] / "data" / "audio"

#: 一张最小的合法 JPEG (SOI + 一段数据 + EOI), 只用来验证嵌入路径.
COVER = b"\xff\xd8\xff\xdb" + bytes(512) + b"\xff\xd9"


def make_entry() -> SongEntry:
    """造一首歌."""
    return SongEntry.from_dict(
        {
            "songmid": "mid1",
            "songid": 1,
            "title": "标题",
            "singers": [{"mid": "s1", "name": "歌手甲"}, {"mid": "s2", "name": "歌手乙"}],
            "album_name": "专辑名",
            "album_mid": "a1",
            "media_mid": "mid1",
            "song_type": 0,
            "interval": 200,
            "track_no": 7,
            "disc_no": 2,
            "year": "2021-06-01",
            "status": 0,
            "sa": 0,
            "sizes": {"MP3_128": 1},
            "pay": {},
            "size_try": 0,
            "try_begin_ms": 0,
            "try_end_ms": 0,
            "vs": [],
            "vi": [],
            "vf": [],
        },
    )


def make_payload() -> tagger.TagPayload:
    """一份写满的标签数据."""
    return tagger.TagPayload(
        entry=make_entry(),
        quality_label="SQ 无损 FLAC",
        composer="作曲人",
        lyricist="作词人",
        genre="流行",
        lyrics="[00:00.00]第一行\n[00:05.00]第二行\n",
        cover=COVER,
        cover_mime="image/jpeg",
    )


@pytest.fixture
def config() -> TagConfig:
    """默认全开."""
    return TagConfig()


def copy_sample(name: str, tmp_path: Path) -> Path:
    """把素材复制一份到临时目录再改."""
    target = tmp_path / name
    shutil.copy(AUDIO / name, target)
    return target


# --------------------------------------------------------------------------- #
# 四种容器都要能写进去、能读回来、还能被 mutagen 重新解析
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ["silence.flac", "silence.mp3", "silence.ogg", "silence.m4a"])
def test_write_tags_round_trip(name: str, tmp_path: Path, config: TagConfig) -> None:
    """写完能读回来, 且容器仍然可解析."""
    path = copy_sample(name, tmp_path)
    tagger.write_tags(path, make_payload(), config)

    audio = MutagenFile(path)
    assert audio is not None, "写完之后 mutagen 认不出这个文件了"
    assert audio.info.length > 0, "音频流被写坏了"


def test_flac_tags_and_cover(tmp_path: Path, config: TagConfig) -> None:
    """FLAC: Vorbis comment + 图片块."""
    path = copy_sample("silence.flac", tmp_path)
    tagger.write_tags(path, make_payload(), config)

    audio = FLAC(path)
    assert audio["title"] == ["标题"]
    assert audio["artist"] == ["歌手甲", "歌手乙"]
    assert audio["albumartist"] == ["歌手甲"]
    assert audio["composer"] == ["作曲人"]
    assert audio["lyricist"] == ["作词人"]
    assert audio["date"] == ["2021"]
    assert audio["tracknumber"] == ["7"]
    assert audio["discnumber"] == ["2"]
    assert len(audio.pictures) == 1
    assert audio.pictures[0].mime == "image/jpeg"
    assert audio.pictures[0].data == COVER


def test_mp3_tags_and_cover(tmp_path: Path, config: TagConfig) -> None:
    """MP3: ID3v2.4 + APIC. 这条用例就是那个 bug 的守门人."""
    path = copy_sample("silence.mp3", tmp_path)
    tagger.write_tags(path, make_payload(), config)

    audio = MP3(path)
    assert audio.tags is not None
    assert audio.tags.version[:2] == (2, 4), "必须是 ID3v2.4"
    assert audio.tags["TIT2"].text == ["标题"]
    assert audio.tags["TPE1"].text == ["歌手甲", "歌手乙"]
    assert audio.tags["APIC:Cover"].data == COVER
    assert audio.tags["USLT::chi"].text.startswith("[00:00.00]")


def test_mp3_with_preexisting_tags_is_cleaned_and_rewritten(
    tmp_path: Path,
    config: TagConfig,
) -> None:
    """QQ 返回的 MP3 自带 ID3v2 + ID3v1, 必须清干净再写.

    回归: 旧实现在这里抛 ``TypeError: Missing filename or fileobj argument``,
    每一首 MP3 的 tag 都写不进去.
    """
    path = copy_sample("preexisting-id3.mp3", tmp_path)
    before = MP3(path)
    assert before.tags["TIT2"].text == ["服务端原有标题"], "素材本身要带预置 tag"

    tagger.write_tags(path, make_payload(), config)

    audio = MP3(path)
    assert audio.tags["TIT2"].text == ["标题"], "服务端原有的值必须被覆盖"
    assert audio.tags["TPE1"].text == ["歌手甲", "歌手乙"]
    assert audio.tags["TCON"].text == ["流行"], "服务端原来的 Blues 必须没了"
    # ID3v1 会被部分播放器优先读取, 留着就会显示没清干净的旧值.
    assert path.read_bytes()[-128:-125] != b"TAG", "ID3v1 应当被移除"


def test_ogg_tags_and_cover(tmp_path: Path, config: TagConfig) -> None:
    """OGG: 图片走 base64 的 METADATA_BLOCK_PICTURE."""
    path = copy_sample("silence.ogg", tmp_path)
    tagger.write_tags(path, make_payload(), config)

    audio = OggVorbis(path)
    assert audio["title"] == ["标题"]
    assert audio["metadata_block_picture"], "封面没写进去"


def test_mp4_tags_and_cover(tmp_path: Path, config: TagConfig) -> None:
    """M4A: covr + 自定义 freeform 作词."""
    path = copy_sample("silence.m4a", tmp_path)
    tagger.write_tags(path, make_payload(), config)

    audio = MP4(path)
    assert audio.tags is not None
    assert audio.tags["\xa9nam"] == ["标题"]
    assert audio.tags["trkn"] == [(7, 0)]
    assert bytes(audio.tags["covr"][0]) == COVER
    assert audio.tags["----:com.apple.iTunes:LYRICIST"][0] == b"\xe4\xbd\x9c\xe8\xaf\x8d\xe4\xba\xba"


# --------------------------------------------------------------------------- #
# 开关与不支持的容器
# --------------------------------------------------------------------------- #


def test_disabled_fields_are_not_written(tmp_path: Path) -> None:
    """配置里关掉的字段不能偷偷写进去."""
    config = TagConfig(cover=False, lyrics=False, genre=False, composer=False)
    path = copy_sample("silence.flac", tmp_path)
    tagger.write_tags(path, make_payload(), config)

    audio = FLAC(path)
    assert audio.pictures == []
    assert "lyrics" not in audio
    assert "genre" not in audio
    assert "composer" not in audio
    assert audio["title"] == ["标题"], "没关的字段照写"


def test_nac_is_rejected(tmp_path: Path, config: TagConfig) -> None:
    """.nac 是腾讯自研容器, mutagen 不支持, 必须明确报错而不是静默跳过."""
    path = tmp_path / "song.nac"
    path.write_bytes(b"\x00" * 64)
    assert not tagger.supports(path)
    with pytest.raises(tagger.TagWriteError, match="nac"):
        tagger.write_tags(path, make_payload(), config)


def test_unknown_container_is_rejected(tmp_path: Path, config: TagConfig) -> None:
    """没见过的后缀同样明确报错."""
    path = tmp_path / "song.xyz"
    path.write_bytes(b"\x00" * 64)
    with pytest.raises(tagger.TagWriteError, match="未知容器"):
        tagger.write_tags(path, make_payload(), config)


# --------------------------------------------------------------------------- #
# 时长探测
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ["silence.flac", "silence.mp3", "silence.ogg", "silence.m4a"])
def test_detect_duration_reads_real_files(name: str) -> None:
    """四种容器都能读出时长 (素材是 0.25 秒)."""
    assert 0.1 < tagger.detect_duration(AUDIO / name) < 1.0


def test_detect_duration_returns_zero_for_unreadable(tmp_path: Path) -> None:
    """读不出来返回 0 —— 调用方据此把第 4 层标成 unverified, 不能当成通过."""
    path = tmp_path / "song.nac"
    path.write_bytes(b"\x00" * 64)
    assert tagger.detect_duration(path) == 0.0
