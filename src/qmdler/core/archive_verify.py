"""归档后的离线校验 —— **刻意不接进下载流程**.

下载流程里的四层校验只看「这文件是不是完整的正片」, 靠的是元数据比对,
代价接近于零. 这里做的是另一件事: **把音频真正解一遍**, 确认容器有效、
帧没坏. 代价是整首解码, 所以只能由用户在归档后按需跑:

    qmdler verify ~/Music/我的歌单

为什么不能进下载流程
====================

**``flac -t`` 会把正常的普通 FLAC 判成失败.** 实测 (2026-08, 5 首):
QQ 的普通 FLAC 在最后一帧之后带了几个多余字节 (文件尾是 ``… 0e 55 ff f0``,
``ff f0`` 是半截帧同步字), 官方解码器走到那里就 ``LOST_SYNC`` 然后 EOF.
但**音频一位都不少** —— 解出的 PCM 字节数与 STREAMINFO 声明完全一致,
且 PCM 的 MD5 与 STREAMINFO 内嵌的签名逐位吻合. 母带 (``AI00``) 则完全干净.

所以正确的严格判据是「解码 PCM 的 MD5 == STREAMINFO 签名」, 而不是
``flac -t`` 的退出码. 本模块就按这个判据来, 并把 ``flac -t`` 的抱怨降级成提示.

一条通用原则: 「工具解不了」≠「文件坏了」
==========================================

**只有「文件本身不对」才判 ``failed``. 「本机这套工具没能力校验它」一律判
``skipped`` / ``note``, 并说明原因.**

这不是洁癖 —— 判错的代价是告诉用户「你的文件坏了」, 而他的文件好好的.
实测已经踩到两次, 而且都是先误报、再回头查才发现的:

* **内嵌封面的 EXIF**: QQ 的封面 JPEG 自带损坏的 EXIF TIFF 头, ffmpeg 会打
  ``mjpeg: invalid TIFF header in EXIF data``. 20 首里 4 首被误判成音频损坏.
  (顺带一提, ``-map 0:a`` / ``-vn`` 挡不住 —— 见 :func:`_decode`.)
* **缺解码器**: 杜比全景声 (``ATMOS_DB``) 是 ``.mp4`` 里的 AC-4, 而 ffmpeg 6.1
  没有 AC-4 解码器, 报 ``no decoder found for: ac4`` 并连带打出一串后果性错误.

**再遇到新容器时按同一逻辑处理, 不要逐个特判.** 先问一句:
「这条错误说的是文件的问题, 还是我这套工具的问题?」后者就降级, 别判 failed.
新增归类规则请加到 :data:`_COVER_DECODER_RE` / :data:`_NO_DECODER_RE` 这类
**按成因分类的**模式里, 而不是按某个具体编码名去打补丁.

依赖
====

``ffprobe`` / ``ffmpeg`` (可选) 与 ``flac`` (可选) 都是外部命令, **不是本项目的
运行时依赖**. 缺哪个就跳过哪一项, 并在结果里说明 —— 不装也能跑, 只是校验得浅些.

**声道数、采样率、位深只做展示, 不参与任何判定.** 实测 ``OGG_640`` 是 4 声道
而其余档位是 2 声道 —— 拿声道数去判「对不对」就会误伤多声道档位.
(``channels`` 唯一的实质用途是 FLAC 那里算 PCM 应有的字节数, 那是算术不是判定.)
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .metadata.tagger import UNSUPPORTED_SUFFIXES, detect_duration

#: 会去校验的容器. 其余后缀 (含 ``.part`` 残片) 一律不看.
AUDIO_SUFFIXES: frozenset[str] = frozenset({".flac", ".mp3", ".ogg", ".m4a", ".mp4"})

#: 单个外部命令的超时 (秒). 母带全量解码在慢盘上也就几十秒.
COMMAND_TIMEOUT = 600


@dataclass(slots=True)
class FileVerdict:
    """一个文件的校验结论."""

    path: Path
    #: ``ok`` / ``failed`` / ``skipped``
    status: str = "ok"
    duration: float = 0.0
    codec: str = ""
    sample_rate: int = 0
    channels: int = 0
    #: 逐项检查的结论, 顺序即执行顺序.
    checks: list[tuple[str, str, str]] = field(default_factory=list)

    def add(self, name: str, state: str, detail: str = "") -> None:
        """记一项检查. ``state`` 为 ``ok`` / ``failed`` / ``skipped`` / ``note``."""
        self.checks.append((name, state, detail))
        if state == "failed":
            self.status = "failed"

    def as_dict(self) -> dict[str, Any]:
        """转为可 JSON 序列化的字典."""
        return {
            "path": str(self.path),
            "status": self.status,
            "duration": round(self.duration, 2),
            "codec": self.codec,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "checks": [{"name": n, "state": s, "detail": d} for n, s, d in self.checks],
        }


def have(command: str) -> bool:
    """外部命令在不在."""
    return shutil.which(command) is not None


def _run(args: list[str]) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=COMMAND_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def iter_audio_files(root: Path) -> Iterator[Path]:
    """递归找出要校验的音频文件, 顺带把 ``.nac`` 和 ``.part`` 挑出来另说."""
    for path in sorted(root.rglob("*")):
        if path.is_file() and (path.suffix.lower() in AUDIO_SUFFIXES
                               or path.suffix.lower() in UNSUPPORTED_SUFFIXES
                               or path.suffix.lower() == ".part"):
            yield path


def verify_file(path: Path) -> FileVerdict:
    """校验一个文件."""
    verdict = FileVerdict(path=path)
    suffix = path.suffix.lower()

    if suffix == ".part":
        verdict.status = "failed"
        verdict.add("残片", "failed", "这是没下完的 .part，不是成品")
        return verdict

    if suffix in UNSUPPORTED_SUFFIXES:
        verdict.status = "skipped"
        verdict.add("容器", "skipped", f"{suffix} 是腾讯自研容器，普通解码器不识别，无法校验")
        return verdict

    # mutagen 时长 —— 无外部依赖, 总是能跑.
    verdict.duration = detect_duration(path)
    if verdict.duration > 0:
        verdict.add("mutagen 时长", "ok", f"{verdict.duration:.2f}s")
    else:
        verdict.add("mutagen 时长", "failed", "读不出时长，容器可能已损坏")

    if have("ffprobe"):
        _probe(path, verdict)
    else:
        verdict.add("ffprobe 解析", "skipped", "没装 ffprobe")

    if have("ffmpeg"):
        _decode(path, verdict)
    else:
        verdict.add("ffmpeg 音频解码", "skipped", "没装 ffmpeg")

    if suffix == ".flac":
        _verify_flac(path, verdict)

    return verdict


#: ffmpeg 的诊断行前缀形如 ``[mjpeg @ 0x...]``. 音频文件里用到 mjpeg 的
#: 只可能是内嵌封面, 所以按这个标签就能把封面的抱怨和音频的问题分开.
_COVER_DECODER_RE = re.compile(r"^\[(?:mjpeg|png|image2)\s*@")

#: 「本机 ffmpeg 不会解这个编码」的特征. 这**不是文件损坏**.
#:
#: 实测: ``ATMOS_DB`` (杜比全景声) 是 ``.mp4`` 容器里的 **AC-4**, 而 ffmpeg 6.1
#: 根本没有 AC-4 解码器, 于是报 ``no decoder found for: ac4`` 并连带打出
#: ``Error initializing a simple filtergraph`` / ``Error opening output file``
#: 这些**后果性**的错误行. 文件本身没问题 —— ffprobe 能正确读出
#: ``ac4 48000Hz 2ch``, mutagen 读得出 325.89s, 字节数与 ``size_dolby`` 精确相等.
#:
#: 把它判成 failed 就是在说「你的杜比全景声文件坏了」, 而实际只是校验工具不会解.
_NO_DECODER_RE = re.compile(r"no decoder found for|[Dd]ecoder .* not found|Unsupported codec")


def _decode(path: Path, verdict: FileVerdict) -> None:
    """全量解码, 并把**内嵌封面的抱怨**与**音频的问题**分开.

    为什么要分: QQ 的封面 JPEG **自带一个损坏的 EXIF TIFF 头**
    (实测直接从 CDN 下下来的原图就报, 与我们的嵌入无关, 字节完全一致).
    不分的话 20 首里有 4 首会因为一张图的 EXIF 被判成「音频解码失败」——
    既是误报, 又会把真正的音频问题淹掉.

    ⚠️ 用 ``-map 0:a`` / ``-vn`` **挡不住**这条: ffmpeg 在打开输入、探测流的
    阶段就已经解过那张附图了, 与后面映射谁无关. 试过 ``-map 0:a``、``-vn``、
    ``-map 0:a:0``、``-vn -sn -dn``, 四种都照报. 所以只能按 stderr 的
    解码器标签分类.
    """
    _code, _out, err = _run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"])
    lines = [line for line in err.strip().splitlines() if line.strip()]
    cover_lines = [line for line in lines if _COVER_DECODER_RE.match(line.strip())]
    audio_lines = [line for line in lines if line not in cover_lines]

    if any(_NO_DECODER_RE.search(line) for line in audio_lines):
        # 本机 ffmpeg 不会解这个编码 —— 无从校验, 不是文件坏了.
        # 后面那几行 filtergraph / output file 的报错都是这一条的后果.
        verdict.add(
            "ffmpeg 音频解码", "skipped",
            f"本机 ffmpeg 没有 {verdict.codec or '该编码'} 的解码器，无从校验"
            f"（不代表文件有问题）",
        )
    elif audio_lines:
        verdict.add("ffmpeg 音频解码", "failed", " / ".join(audio_lines)[:300])
    else:
        verdict.add("ffmpeg 音频解码", "ok", "stderr 干净（封面的抱怨不算）")

    if cover_lines:
        verdict.add(
            "内嵌封面", "note",
            f"解码器对封面有意见（不影响音频）：{cover_lines[0][:120]}",
        )


def _probe(path: Path, verdict: FileVerdict) -> None:
    code, out, err = _run([
        "ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path),
    ])
    if code != 0:
        verdict.add("ffprobe 解析", "failed", err.strip()[:300])
        return
    try:
        info = json.loads(out)
    except json.JSONDecodeError as exc:  # pragma: no cover - ffprobe 输出损坏
        verdict.add("ffprobe 解析", "failed", str(exc))
        return
    streams: list[dict[str, Any]] = info.get("streams", [])
    stream = next((s for s in streams if s.get("codec_type") == "audio"), {})
    if not stream:
        verdict.add("ffprobe 解析", "failed", "没有音频流")
        return
    verdict.codec = str(stream.get("codec_name", ""))
    verdict.sample_rate = int(stream.get("sample_rate") or 0)
    verdict.channels = int(stream.get("channels") or 0)
    verdict.add(
        "ffprobe 解析", "ok",
        f"{verdict.codec} {verdict.sample_rate}Hz {verdict.channels}ch",
    )


def _verify_flac(path: Path, verdict: FileVerdict) -> None:
    """FLAC 的严格校验: 解码 PCM 的 MD5 必须等于 STREAMINFO 内嵌的签名.

    这才是判据. ``flac -t`` 的退出码不是 —— 见模块 docstring.
    """
    if not have("flac"):
        verdict.add("FLAC MD5 签名", "skipped", "没装 flac")
        return

    from mutagen.flac import FLAC

    try:
        info = FLAC(str(path)).info
    except Exception as exc:  # 容器坏了是正常结果之一
        verdict.add("FLAC MD5 签名", "failed", f"读不出 STREAMINFO: {exc}")
        return

    signature = getattr(info, "md5_signature", 0)
    if not signature:
        verdict.add("FLAC MD5 签名", "skipped", "STREAMINFO 里没有签名")
        return

    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "decoded.raw"
        # -F: 撞到 LOST_SYNC 也继续解完. QQ 的普通 FLAC 尾部有冗余字节,
        # 不加这个参数会在最后一帧之后中断, 得不到完整 PCM.
        code, _out, err = _run([
            "flac", "-d", "-s", "-F", "--force-raw-format",
            "--endian=little", "--sign=signed", "-f", "-o", str(raw), str(path),
        ])
        if not raw.exists():
            verdict.add("FLAC MD5 签名", "failed", err.strip()[:300] or f"解码失败，退出码 {code}")
            return
        digest = hashlib.md5(raw.read_bytes(), usedforsecurity=False).hexdigest()
        decoded_bytes = raw.stat().st_size

    expected = info.total_samples * info.channels * (info.bits_per_sample // 8)
    if digest == format(signature, "032x"):
        verdict.add("FLAC MD5 签名", "ok", f"PCM {decoded_bytes} 字节，与 STREAMINFO 签名逐位吻合")
        if err.strip():
            # LOST_SYNC 这类抱怨降级成提示: 音频已经证明是完好的.
            verdict.add(
                "flac -t 提示", "note",
                "解码器报了 LOST_SYNC，但 PCM 与签名一致 —— 是 QQ 普通 FLAC "
                "尾部的冗余字节，不是损坏",
            )
    else:
        verdict.add(
            "FLAC MD5 签名", "failed",
            f"PCM {decoded_bytes} 字节（预期 {expected}），MD5 与 STREAMINFO 签名不符",
        )


def verify_tree(root: Path) -> list[FileVerdict]:
    """校验整个目录."""
    return [verify_file(path) for path in iter_audio_files(root)]
