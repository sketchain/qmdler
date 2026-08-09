"""试听 / 静默降级检测 —— 本项目最危险的静默错误."""

from __future__ import annotations

from qmdler.core.download import verify
from qmdler.core.models import SongEntry, TrialReason

# --------------------------------------------------------------------------- #
# 第 1 层: filename 前缀
# --------------------------------------------------------------------------- #


def test_extract_prefix_from_purl() -> None:
    """从带 query 的 purl 里抠出四字符编码前缀."""
    assert verify.extract_prefix("F000003w2xz20QlUZt.flac?guid=1&vkey=ABC") == "F000"
    assert verify.extract_prefix("/amobile.music.tc.qq.com/M500abc.mp3?vkey=x") == "M500"
    assert verify.extract_prefix("") == ""


def test_matching_prefix_passes() -> None:
    """前缀一致就通过."""
    assert verify.check_prefix("FLAC", "F000abc.flac?vkey=1").ok


def test_trial_prefix_detected() -> None:
    """请求 F000 却回 RS02 → 试听片段."""
    result = verify.check_prefix("FLAC", "RS02abc.mp3?vkey=1")
    assert result.verdict is verify.Verdict.TRIAL
    assert result.reason == TrialReason.FILENAME_PREFIX.value
    assert result.actual_prefix == "RS02"


def test_ogg_trial_prefix_detected() -> None:
    """O802 是 SQ 无损试听."""
    assert verify.check_prefix("OGG_640", "O802abc.ogg").verdict is verify.Verdict.TRIAL


def test_silent_downgrade_detected() -> None:
    """请求 F000 却回 M500 → 被静默降级, 不是试听."""
    result = verify.check_prefix("FLAC", "M500abc.mp3")
    assert result.verdict is verify.Verdict.DEGRADED
    assert result.actual_prefix == "M500"


def test_ogg640_not_confused_with_accompaniment() -> None:
    """SpecialSongFileType.ACCOM 的编码同为 O801, 但我们从不请求它,
    所以正常的 OGG_640 (O801) 不该被误判."""
    assert verify.check_prefix("OGG_640", "O801abc.ogg").ok


# --------------------------------------------------------------------------- #
# 第 2 层: size_try 交叉验证
# --------------------------------------------------------------------------- #


def test_size_matching_size_try_is_trial(entry: SongEntry) -> None:
    """字节数正好等于试听片段大小."""
    result = verify.check_trial_size(entry, entry.size_try)
    assert result.verdict is verify.Verdict.TRIAL
    assert result.reason == TrialReason.SIZE_TRY_MATCH.value
    assert "60000~90000ms" in result.detail


def test_size_within_tolerance_of_size_try(entry: SongEntry) -> None:
    """2% 容差内也算命中."""
    assert verify.check_trial_size(entry, int(entry.size_try * 1.015)).verdict is verify.Verdict.TRIAL


def test_full_size_is_not_trial(entry: SongEntry) -> None:
    """完整文件大小与 size_try 差得远, 不该误判."""
    assert verify.check_trial_size(entry, entry.sizes["FLAC"]).ok


def test_trial_window_falls_back_to_vi(entry: SongEntry) -> None:
    """file.try_begin/try_end 缺失时回退到 vi[4]/vi[5]."""
    entry.try_begin_ms = 0
    entry.try_end_ms = 0
    assert entry.trial_window_ms == (60000, 90000)


# --------------------------------------------------------------------------- #
# 第 3 层: 字节数 vs size_*
# --------------------------------------------------------------------------- #


def test_short_file_is_trial() -> None:
    """明显小于预期 → 不完整."""
    result = verify.check_size(1_000_000, 300_000)
    assert result.verdict is verify.Verdict.TRIAL
    assert result.reason == TrialReason.SIZE_MISMATCH.value


def test_size_within_two_percent_passes() -> None:
    """2% 阈值内通过."""
    assert verify.check_size(1_000_000, 990_000).ok


def test_size_just_outside_tolerance_fails() -> None:
    """刚好超出阈值就要报."""
    assert not verify.check_size(1_000_000, 970_000).ok


def test_larger_than_expected_is_only_degraded() -> None:
    """比预期大不算残缺, 记一笔降级即可."""
    assert verify.check_size(1_000_000, 1_200_000).verdict is verify.Verdict.DEGRADED


def test_unknown_expected_size_passes() -> None:
    """拿不到预期大小时不做判断, 交给后面的时长校验."""
    assert verify.check_size(0, 500).ok


# --------------------------------------------------------------------------- #
# 第 4 层: 时长 vs interval
# --------------------------------------------------------------------------- #


def test_short_duration_is_trial(entry: SongEntry) -> None:
    """30 秒 vs 240 秒 → 试听."""
    result = verify.check_duration(entry, 30.0)
    assert result.verdict is verify.Verdict.TRIAL
    assert result.reason == TrialReason.DURATION_SHORT.value
    # 落在 try_end - try_begin 附近时要点出来
    assert "试听时长" in result.detail


def test_full_duration_passes(entry: SongEntry) -> None:
    """完整时长通过."""
    assert verify.check_duration(entry, 240.0).ok
    assert verify.check_duration(entry, 236.0).ok


def test_slightly_short_within_tolerance(entry: SongEntry) -> None:
    """容差内的小偏差不报 (编码器差异)."""
    assert verify.check_duration(entry, 240 * 0.95).ok


def test_unknown_duration_passes(entry: SongEntry) -> None:
    """读不出时长时不误判."""
    assert verify.check_duration(entry, 0.0).ok


# --------------------------------------------------------------------------- #
# 合并
# --------------------------------------------------------------------------- #


def test_combine_prefers_trial_over_degraded() -> None:
    """试听优先于降级."""
    trial = verify.VerifyResult(verify.Verdict.TRIAL, "a")
    degraded = verify.VerifyResult(verify.Verdict.DEGRADED, "b")
    assert verify.combine(degraded, trial, verify.OK) is trial


def test_combine_returns_degraded_when_no_trial() -> None:
    """没有试听就返回降级."""
    degraded = verify.VerifyResult(verify.Verdict.DEGRADED, "b")
    assert verify.combine(verify.OK, degraded) is degraded


def test_combine_all_ok() -> None:
    """全通过."""
    assert verify.combine(verify.OK, verify.OK).ok
