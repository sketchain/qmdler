"""试听片段与静默降级检测.

``result == 0`` 且 ``purl`` 非空, **不代表**拿到的是完整源文件. 服务端可能返回
30 秒试听片段 (``SpecialSongFileType.TRY`` = ``RS02``, 或 ``TRY_OGG_640`` =
``O802``), 也可能悄悄降级到更低档位.

这是本项目最危险的静默错误: 文件下下来了、tag 也写了、状态是 success, 用户一年
后才发现半个库是残的. 所以四层校验一层都不能省:

下载前 (看 URL 就能判断)
    1. 比对 ``purl`` 里 filename 的四字符前缀与请求档位的编码;
    2. 对照 ``file.size_try`` / ``try_begin`` / ``try_end`` (以及 ``vi[4]`` /
       ``vi[5]``) 交叉验证.

下载中/后 (看文件本身)
    3. Content-Length / 实际字节数 vs ``file.size_*``;
    4. 实际时长 vs ``track.interval``.

注: ``SpecialSongFileType.ACCOM`` 的编码同样是 ``O801`` (与 ``SongFileType.OGG_640``
相同), 但伴奏要传 ``Song.vs[9]`` 的 media mid, 我们从不请求它, 所以前缀比对不会
把正常的 OGG_640 误判成伴奏.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from ..models import TRIAL_PREFIXES, SongEntry, TrialReason, quality_start_code

#: purl 形如 ``M500003w2xz20QlUZt.mp3?guid=...&vkey=...``, 前 4 个字符是档位编码.
#: 只在**最后一个路径段**上匹配 —— 域名里也有 ``xxx.yyy`` 的形状, 从头匹配会命中它.
_FILENAME_RE = re.compile(r"^([0-9A-Za-z]{4})[0-9A-Za-z]+\.[0-9A-Za-z]+$")


class Verdict(str, Enum):
    """校验结论."""

    OK = "ok"
    TRIAL = "trial"
    DEGRADED = "degraded"


@dataclass(slots=True)
class VerifyResult:
    """一次校验的结论."""

    verdict: Verdict
    reason: str = ""
    detail: str = ""
    #: 前缀比对推断出的实际档位编码 (四字符), 无法推断时为空.
    actual_prefix: str = ""

    @property
    def ok(self) -> bool:
        """是否通过."""
        return self.verdict is Verdict.OK


OK = VerifyResult(Verdict.OK)


def extract_prefix(purl_or_filename: str) -> str:
    """从 purl 或 filename 里取出四字符档位编码前缀."""
    if not purl_or_filename:
        return ""
    head = purl_or_filename.split("?", 1)[0]
    tail = head.rsplit("/", 1)[-1]
    match = _FILENAME_RE.match(tail)
    if match:
        return match.group(1)
    # 退化处理: 直接取文件名头 4 个字符.
    return tail[:4] if len(tail) >= 4 else ""


def check_prefix(requested_quality: str, purl: str, filename: str = "") -> VerifyResult:
    """第 1 层: 比对 filename 前缀与请求档位.

    请求 ``F000`` 结果回来的前缀是 ``RS02`` / ``O802`` → 试听片段;
    是 ``M500`` 等其他编码 → 被静默降级.
    """
    expected = quality_start_code(requested_quality)
    actual = extract_prefix(purl) or extract_prefix(filename)
    if not expected or not actual:
        return OK
    if actual == expected:
        return OK
    if actual in TRIAL_PREFIXES:
        return VerifyResult(
            Verdict.TRIAL,
            TrialReason.FILENAME_PREFIX.value,
            f"请求 {expected}，服务端返回试听片段 {actual}",
            actual,
        )
    return VerifyResult(
        Verdict.DEGRADED,
        TrialReason.FILENAME_PREFIX.value,
        f"请求 {expected}，服务端返回 {actual}",
        actual,
    )


def check_trial_size(entry: SongEntry, content_length: int, *, tolerance: float = 0.02) -> VerifyResult:
    """第 2 层: 与 ``size_try`` 交叉验证.

    拿到的字节数正好等于试听片段大小, 基本可以坐实是试听.
    """
    if entry.size_try <= 0 or content_length <= 0:
        return OK
    delta = abs(content_length - entry.size_try) / entry.size_try
    if delta <= tolerance:
        windows = entry.trial_windows
        window = (
            "，试听区间 " + "、".join(f"{begin}~{end}ms（{source}）" for begin, end, source in windows)
            if windows
            else ""
        )
        return VerifyResult(
            Verdict.TRIAL,
            TrialReason.SIZE_TRY_MATCH.value,
            f"文件大小 {content_length} 与 size_try {entry.size_try} 吻合{window}",
        )
    return OK


def check_size(expected_size: int, actual_size: int, *, tolerance: float = 0.02) -> VerifyResult:
    """第 3 层: 实际字节数 vs ``size_*``.

    ``query_song`` 给的 ``size_*`` 是完整文件的准确字节数. 差异超过阈值直接判为
    不完整 —— 下载前先发 HEAD 拿 Content-Length 比对, 不用浪费 3 分钟下完再说.
    """
    if expected_size <= 0 or actual_size <= 0:
        return OK
    delta = abs(actual_size - expected_size) / expected_size
    if delta <= tolerance:
        return OK
    if actual_size < expected_size:
        return VerifyResult(
            Verdict.TRIAL,
            TrialReason.SIZE_MISMATCH.value,
            f"实际 {actual_size} 字节，预期 {expected_size} 字节，缺失 {delta:.1%}",
        )
    # 比预期大不算残缺, 但值得记一笔.
    return VerifyResult(
        Verdict.DEGRADED,
        TrialReason.SIZE_MISMATCH.value,
        f"实际 {actual_size} 字节大于预期 {expected_size} 字节",
    )


def check_duration(entry: SongEntry, actual_seconds: float, *, tolerance: float = 0.9) -> VerifyResult:
    """第 4 层: 实际时长 vs ``track.interval``.

    比 ``interval`` 短一大截即为试听片段.

    与试听窗口比对时**逐个比对全部已知窗口** (``file.try_begin/try_end`` 与
    ``vi[4]/vi[5]`` 是并列候选, 不是主备), 命中任意一个都要点出来 —— 只比对
    被选中的那一个会漏判.
    """
    expected = entry.interval
    if expected <= 0 or actual_seconds <= 0:
        return OK

    # 只要与任意一个试听窗口的时长吻合, 就算 interval 比对还没到阈值也判 trial:
    # 漏判 trial 的代价远高于误判.
    matched = _matching_trial_window(entry, actual_seconds)
    if matched is not None:
        begin, end, source = matched
        return VerifyResult(
            Verdict.TRIAL,
            TrialReason.DURATION_SHORT.value,
            f"实际时长 {actual_seconds:.0f}s 与试听窗口 {begin}~{end}ms"
            f"（{(end - begin) / 1000:.0f}s，取自 {source}）吻合，应为 {expected}s",
        )

    if actual_seconds >= expected * tolerance:
        return OK

    return VerifyResult(
        Verdict.TRIAL,
        TrialReason.DURATION_SHORT.value,
        f"实际时长 {actual_seconds:.0f}s，应为 {expected}s",
    )


def _matching_trial_window(entry: SongEntry, actual_seconds: float) -> tuple[int, int, str] | None:
    """实际时长命中了哪个试听窗口 (逐个比对, 不只看被选中的那个)."""
    if actual_seconds <= 0:
        return None
    # 完整曲目的时长本身就短于任何窗口时不作数.
    for begin, end, source in entry.trial_windows:
        trial_seconds = (end - begin) / 1000
        if trial_seconds <= 0:
            continue
        if entry.interval > 0 and trial_seconds >= entry.interval * 0.95:
            # 「试听窗口」几乎等于整首歌, 比对没有意义.
            continue
        if abs(actual_seconds - trial_seconds) <= max(3.0, trial_seconds * 0.1):
            return begin, end, source
    return None


def combine(*results: VerifyResult) -> VerifyResult:
    """合并多层结论: 试听优先于降级, 降级优先于通过."""
    trial = next((result for result in results if result.verdict is Verdict.TRIAL), None)
    if trial is not None:
        return trial
    degraded = next((result for result in results if result.verdict is Verdict.DEGRADED), None)
    if degraded is not None:
        return degraded
    return OK
