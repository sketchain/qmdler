"""归档离线校验.

外部命令 (``ffprobe`` / ``ffmpeg`` / ``flac``) 缺席时必须**跳过并说明**,
而不是报错或者假装通过 —— 这条比校验本身还重要, 因为它们不是运行时依赖.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from qmdler.core import archive_verify

AUDIO = Path(__file__).resolve().parents[1] / "data" / "audio"

_HAVE_FFPROBE = shutil.which("ffprobe") is not None
_HAVE_FLAC = shutil.which("flac") is not None


def test_part_file_is_failed(tmp_path: Path) -> None:
    """``.part`` 是没下完的残片, 不是成品."""
    path = tmp_path / "song.flac.part"
    path.write_bytes(b"\x00" * 16)
    verdict = archive_verify.verify_file(path)
    assert verdict.status == "failed"
    assert verdict.checks[0][0] == "残片"


def test_nac_is_skipped_not_failed(tmp_path: Path) -> None:
    """``.nac`` 无从校验, 记 skipped —— 判 failed 会让用户以为文件坏了."""
    path = tmp_path / "song.nac"
    path.write_bytes(b"\x00" * 16)
    verdict = archive_verify.verify_file(path)
    assert verdict.status == "skipped"
    assert "腾讯自研" in verdict.checks[0][2]


def test_broken_container_is_failed(tmp_path: Path) -> None:
    """后缀像音频、内容是垃圾 —— 必须判 failed."""
    path = tmp_path / "song.flac"
    path.write_bytes(b"not a flac at all" * 8)
    verdict = archive_verify.verify_file(path)
    assert verdict.status == "failed"


def test_real_file_passes(tmp_path: Path) -> None:
    """真文件至少要过 mutagen 那一关 (无外部依赖)."""
    path = tmp_path / "silence.flac"
    shutil.copy(AUDIO / "silence.flac", path)
    verdict = archive_verify.verify_file(path)
    states = {name: state for name, state, _ in verdict.checks}
    assert states["mutagen 时长"] == "ok"
    assert verdict.status != "failed"


def test_missing_external_tools_are_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """三个外部命令都没装时, 对应检查记 skipped, 整体仍算通过."""
    monkeypatch.setattr(archive_verify, "have", lambda _command: False)
    path = tmp_path / "silence.flac"
    shutil.copy(AUDIO / "silence.flac", path)

    verdict = archive_verify.verify_file(path)
    states = {name: state for name, state, _ in verdict.checks}
    assert states["ffprobe 解析"] == "skipped"
    assert states["ffmpeg 全量解码"] == "skipped"
    assert states["FLAC MD5 签名"] == "skipped"
    assert verdict.status == "ok", "缺工具不等于文件有问题"


@pytest.mark.skipif(not _HAVE_FFPROBE, reason="没装 ffprobe")
def test_ffprobe_reads_stream_info(tmp_path: Path) -> None:
    """装了 ffprobe 就要读出编码与采样率."""
    path = tmp_path / "silence.flac"
    shutil.copy(AUDIO / "silence.flac", path)
    verdict = archive_verify.verify_file(path)
    assert verdict.codec == "flac"
    assert verdict.sample_rate == 44100
    assert verdict.channels == 2


@pytest.mark.skipif(not _HAVE_FLAC, reason="没装 flac")
def test_flac_md5_signature_is_the_criterion(tmp_path: Path) -> None:
    """判据是 PCM 的 MD5 对不对得上 STREAMINFO 签名."""
    path = tmp_path / "silence.flac"
    shutil.copy(AUDIO / "silence.flac", path)
    verdict = archive_verify.verify_file(path)
    states = {name: state for name, state, _ in verdict.checks}
    assert states["FLAC MD5 签名"] == "ok"


def test_iter_skips_unrelated_files(tmp_path: Path) -> None:
    """只挑音频、``.nac`` 与 ``.part``, 其余不看."""
    (tmp_path / "cover.jpg").write_bytes(b"x")
    (tmp_path / "歌词.lrc").write_text("[00:00.00]x", encoding="utf-8")
    (tmp_path / "a.flac").write_bytes(b"x")
    (tmp_path / "b.nac").write_bytes(b"x")
    (tmp_path / "c.mp3.part").write_bytes(b"x")

    names = {path.name for path in archive_verify.iter_audio_files(tmp_path)}
    assert names == {"a.flac", "b.nac", "c.mp3.part"}


def test_verify_tree_is_recursive(tmp_path: Path) -> None:
    """子目录也要扫到."""
    nested = tmp_path / "歌单" / "专辑"
    nested.mkdir(parents=True)
    shutil.copy(AUDIO / "silence.flac", nested / "song.flac")
    verdicts = archive_verify.verify_tree(tmp_path)
    assert len(verdicts) == 1
    assert verdicts[0].path.name == "song.flac"
