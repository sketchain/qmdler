"""写入音频标签.

容器格式由音质档位决定 (见 ``SongFileType`` 的扩展名列), 所以四套写法都要覆盖:

* **FLAC**  —— Vorbis Comment + ``METADATA_BLOCK_PICTURE``
* **MP3**   —— ID3v2.4, 封面 ``APIC``, 歌词 ``USLT``
* **M4A/MP4** —— atom, 封面 ``covr``, 歌词 ``©lyr``
* **OGG**   —— Vorbis Comment, 封面按 FLAC 的 Picture 结构 base64 编码
* **.nac**  —— 腾讯自研容器, mutagen 不支持, 直接跳过并标注

写 tag 失败不让整首歌算失败: 音频文件保留, 记录里标注「tag 写入失败」.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mutagen import File as MutagenFile
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (
    APIC,
    COMM,
    TALB,
    TCOM,
    TCON,
    TDRC,
    TEXT,
    TIT2,
    TPE1,
    TPE2,
    TPOS,
    TRCK,
    TXXX,
    USLT,
    ID3NoHeaderError,
)
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggvorbis import OggVorbis

from ..config.schema import TagConfig
from ..models import SongEntry

logger = logging.getLogger(__name__)

#: mutagen 无法处理的容器.
UNSUPPORTED_SUFFIXES: frozenset[str] = frozenset({".nac"})


class TagWriteError(RuntimeError):
    """写 tag 失败."""


@dataclass(slots=True)
class TagPayload:
    """要写进文件的全部标签数据."""

    entry: SongEntry
    quality_label: str = ""
    composer: str = ""
    lyricist: str = ""
    genre: str = ""
    lyrics: str = ""
    cover: bytes = b""
    cover_mime: str = "image/jpeg"
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def artists(self) -> list[str]:
        """歌手名列表."""
        return [name for name in self.entry.singer_names if name.strip()]

    @property
    def album_artist(self) -> str:
        """专辑艺术家 = 第一位歌手."""
        artists = self.artists
        return artists[0] if artists else ""

    @property
    def year(self) -> str:
        """发行年份."""
        raw = (self.entry.year or "").strip()
        return raw.split("-")[0] if raw else ""


def detect_duration(path: Path) -> float:
    """读取音频实际时长 (秒). 读不出来返回 0.

    用于试听检测的第四层: 实际时长明显短于 ``track.interval`` 即为试听片段.
    """
    try:
        audio = MutagenFile(str(path))
    except Exception as exc:
        logger.debug("读取时长失败 %s: %s", path, exc)
        return 0.0
    if audio is None or audio.info is None:
        return 0.0
    return float(getattr(audio.info, "length", 0.0) or 0.0)


def _flac_picture(cover: bytes, mime: str) -> Picture:
    picture = Picture()
    picture.type = 3  # front cover
    picture.mime = mime
    picture.desc = "Cover"
    picture.data = cover
    return picture


def _vorbis_common(payload: TagPayload, config: TagConfig) -> dict[str, list[str]]:
    """FLAC / OGG 共用的 Vorbis Comment 字段."""
    tags: dict[str, list[str]] = {}
    if config.title and payload.entry.title:
        tags["TITLE"] = [payload.entry.title]
    if config.artist and payload.artists:
        tags["ARTIST"] = payload.artists
    if config.album and payload.entry.album_name:
        tags["ALBUM"] = [payload.entry.album_name]
    if config.album_artist and payload.album_artist:
        tags["ALBUMARTIST"] = [payload.album_artist]
    if config.composer and payload.composer:
        tags["COMPOSER"] = [payload.composer]
    if config.lyricist and payload.lyricist:
        tags["LYRICIST"] = [payload.lyricist]
    if config.year and payload.year:
        tags["DATE"] = [payload.year]
    if config.track_number and payload.entry.track_no > 0:
        tags["TRACKNUMBER"] = [str(payload.entry.track_no)]
    if config.disc_number and payload.entry.disc_no > 0:
        tags["DISCNUMBER"] = [str(payload.entry.disc_no)]
    if config.genre and payload.genre:
        tags["GENRE"] = [payload.genre]
    if config.lyrics and payload.lyrics:
        tags["LYRICS"] = [payload.lyrics]
    if payload.quality_label:
        tags["COMMENT"] = [f"qmdler / {payload.quality_label}"]
    if config.replaygain:
        gain = payload.entry.replaygain
        if gain is not None:
            tags["REPLAYGAIN_TRACK_GAIN"] = [f"{gain[0]:.2f} dB"]
            tags["REPLAYGAIN_TRACK_PEAK"] = [f"{gain[1]:.6f}"]
    for key, value in payload.extra.items():
        tags[key.upper()] = [value]
    return tags


def _write_flac(path: Path, payload: TagPayload, config: TagConfig) -> None:
    audio = FLAC(str(path))
    audio.delete()
    for key, values in _vorbis_common(payload, config).items():
        audio[key] = values
    if config.cover and payload.cover:
        audio.clear_pictures()
        audio.add_picture(_flac_picture(payload.cover, payload.cover_mime))
    audio.save()


def _write_ogg(path: Path, payload: TagPayload, config: TagConfig) -> None:
    audio = OggVorbis(str(path))
    for key, values in _vorbis_common(payload, config).items():
        audio[key] = values
    if config.cover and payload.cover:
        # OGG 没有独立的图片块, 约定是把 FLAC 的 Picture 结构 base64 后放进
        # METADATA_BLOCK_PICTURE.
        picture = _flac_picture(payload.cover, payload.cover_mime)
        audio["METADATA_BLOCK_PICTURE"] = [base64.b64encode(picture.write()).decode("ascii")]
    audio.save()


def _write_mp3(path: Path, payload: TagPayload, config: TagConfig) -> None:
    try:
        audio = MP3(str(path))
    except ID3NoHeaderError:  # pragma: no cover - mutagen 通常会自动建头
        audio = MP3(str(path))
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    assert tags is not None
    tags.delete()

    if config.title and payload.entry.title:
        tags.add(TIT2(encoding=3, text=payload.entry.title))
    if config.artist and payload.artists:
        tags.add(TPE1(encoding=3, text=payload.artists))
    if config.album and payload.entry.album_name:
        tags.add(TALB(encoding=3, text=payload.entry.album_name))
    if config.album_artist and payload.album_artist:
        tags.add(TPE2(encoding=3, text=payload.album_artist))
    if config.composer and payload.composer:
        tags.add(TCOM(encoding=3, text=payload.composer))
    if config.lyricist and payload.lyricist:
        tags.add(TEXT(encoding=3, text=payload.lyricist))
    if config.year and payload.year:
        tags.add(TDRC(encoding=3, text=payload.year))
    if config.track_number and payload.entry.track_no > 0:
        tags.add(TRCK(encoding=3, text=str(payload.entry.track_no)))
    if config.disc_number and payload.entry.disc_no > 0:
        tags.add(TPOS(encoding=3, text=str(payload.entry.disc_no)))
    if config.genre and payload.genre:
        tags.add(TCON(encoding=3, text=payload.genre))
    if config.lyrics and payload.lyrics:
        tags.add(USLT(encoding=3, lang="chi", desc="", text=payload.lyrics))
    if payload.quality_label:
        tags.add(COMM(encoding=3, lang="chi", desc="", text=f"qmdler / {payload.quality_label}"))
    if config.cover and payload.cover:
        tags.add(APIC(encoding=3, mime=payload.cover_mime, type=3, desc="Cover", data=payload.cover))
    if config.replaygain:
        gain = payload.entry.replaygain
        if gain is not None:
            tags.add(TXXX(encoding=3, desc="REPLAYGAIN_TRACK_GAIN", text=f"{gain[0]:.2f} dB"))
            tags.add(TXXX(encoding=3, desc="REPLAYGAIN_TRACK_PEAK", text=f"{gain[1]:.6f}"))

    # ID3v2.4
    audio.save(v2_version=4)


def _write_mp4(path: Path, payload: TagPayload, config: TagConfig) -> None:
    audio = MP4(str(path))
    tags = audio.tags
    if tags is None:
        audio.add_tags()
        tags = audio.tags
    assert tags is not None
    tags.clear()

    if config.title and payload.entry.title:
        tags["\xa9nam"] = [payload.entry.title]
    if config.artist and payload.artists:
        tags["\xa9ART"] = payload.artists
    if config.album and payload.entry.album_name:
        tags["\xa9alb"] = [payload.entry.album_name]
    if config.album_artist and payload.album_artist:
        tags["aART"] = [payload.album_artist]
    if config.composer and payload.composer:
        tags["\xa9wrt"] = [payload.composer]
    if config.lyricist and payload.lyricist:
        # MP4 没有标准的作词 atom, 走自定义 freeform.
        tags["----:com.apple.iTunes:LYRICIST"] = [payload.lyricist.encode("utf-8")]
    if config.year and payload.year:
        tags["\xa9day"] = [payload.year]
    if config.track_number and payload.entry.track_no > 0:
        tags["trkn"] = [(payload.entry.track_no, 0)]
    if config.disc_number and payload.entry.disc_no > 0:
        tags["disk"] = [(payload.entry.disc_no, 0)]
    if config.genre and payload.genre:
        tags["\xa9gen"] = [payload.genre]
    if config.lyrics and payload.lyrics:
        tags["\xa9lyr"] = [payload.lyrics]
    if payload.quality_label:
        tags["\xa9cmt"] = [f"qmdler / {payload.quality_label}"]
    if config.cover and payload.cover:
        image_format = MP4Cover.FORMAT_PNG if payload.cover_mime == "image/png" else MP4Cover.FORMAT_JPEG
        tags["covr"] = [MP4Cover(payload.cover, imageformat=image_format)]
    if config.replaygain:
        gain = payload.entry.replaygain
        if gain is not None:
            tags["----:com.apple.iTunes:replaygain_track_gain"] = [f"{gain[0]:.2f} dB".encode()]
            tags["----:com.apple.iTunes:replaygain_track_peak"] = [f"{gain[1]:.6f}".encode()]

    audio.save()


_WRITERS: dict[str, Any] = {
    ".flac": _write_flac,
    ".ogg": _write_ogg,
    ".mp3": _write_mp3,
    ".m4a": _write_mp4,
    ".mp4": _write_mp4,
}


def supports(path: Path | str) -> bool:
    """该容器格式是否支持写 tag."""
    suffix = Path(path).suffix.lower()
    return suffix in _WRITERS


def write_tags(path: Path, payload: TagPayload, config: TagConfig) -> None:
    """写入标签.

    Raises:
        TagWriteError: 容器不支持, 或写入过程出错.
    """
    suffix = path.suffix.lower()
    if suffix in UNSUPPORTED_SUFFIXES:
        raise TagWriteError(f"{suffix} 是非标准容器，mutagen 不支持，已跳过 tag 写入")
    writer = _WRITERS.get(suffix)
    if writer is None:
        raise TagWriteError(f"未知容器格式 {suffix}，已跳过 tag 写入")
    try:
        writer(path, payload, config)
    except Exception as exc:
        raise TagWriteError(f"写入标签失败: {exc}") from exc
