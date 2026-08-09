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
# 第 2 层: size_try 作试听片段的**绝对大小基准**
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


def test_size_try_is_never_hardcoded(entry: SongEntry) -> None:
    """必须用响应里的 ``size_try``, 不能写死实测到的 960887.

    实测该值在四首不同的歌上都是 960887, 极易被后来者当成常量写进代码.
    但高码率试听 (``O802``) 的片段大小未必相同 —— 写死就会漏判.
    """
    entry.size_try = 2_500_000  # 假想的高码率试听片段
    assert verify.check_trial_size(entry, 2_500_000).verdict is verify.Verdict.TRIAL
    # 旧的恒定值此时不该再命中任何东西.
    assert verify.check_trial_size(entry, 960_887).ok


def test_size_try_match_ignores_interval_and_quality(entry: SongEntry) -> None:
    """绝对值比对与 interval / 档位无关: 长曲目照样能判出试听片段."""
    entry.interval = 3600  # 一小时的长音频
    assert verify.check_trial_size(entry, entry.size_try).verdict is verify.Verdict.TRIAL


def test_trial_window_falls_back_to_vi(entry: SongEntry) -> None:
    """file.try_begin/try_end 缺失时展示 vi[4]/vi[5]."""
    entry.try_begin_ms = 0
    entry.try_end_ms = 0
    assert entry.trial_window_ms == (60000, 90000)


# --------------------------------------------------------------------------- #
# 试听窗口是「并列候选」而非「主备」
#
# 实测「晴天」: file 给 84346~142843ms (58.5s), vi 给 84346~114346ms (30s)。
# 差距太大不像取整误差, 更像是不同档位各有各的试听区间 —— 所以两个都要比对,
# 只比对被选中的那个会漏判 trial。
# --------------------------------------------------------------------------- #


def test_both_windows_are_kept_when_they_differ(entry: SongEntry) -> None:
    """两个来源给的区间不一致时, 两个都要留着."""
    entry.try_begin_ms, entry.try_end_ms = 84346, 142843
    entry.vi = [0, 0, 0, 0, 84346, 114346]
    windows = entry.trial_windows
    assert len(windows) == 2
    assert (84346, 142843, "file.try_begin/try_end") in windows
    assert (84346, 114346, "vi[4]/vi[5]") in windows


def test_identical_windows_are_deduped(entry: SongEntry) -> None:
    """两个来源一致时不重复记录."""
    entry.try_begin_ms, entry.try_end_ms = 60000, 90000
    entry.vi = [0, 0, 0, 0, 60000, 90000]
    assert len(entry.trial_windows) == 1


def test_duration_matching_the_non_preferred_window_is_still_trial(entry: SongEntry) -> None:
    """实际时长命中 vi 那个区间 —— 即使 file 是首选, 也必须判 trial.

    这是「并列候选」的核心: 只比对 file 会把 30s 的试听片段放行。
    """
    entry.interval = 269
    entry.try_begin_ms, entry.try_end_ms = 84346, 142843   # 58.5s（首选）
    entry.vi = [0, 0, 0, 0, 84346, 114346]                 # 30s（次选）

    result = verify.check_duration(entry, 30.0)
    assert result.verdict is verify.Verdict.TRIAL
    assert "vi[4]/vi[5]" in result.detail


def test_duration_matching_the_preferred_window_is_trial(entry: SongEntry) -> None:
    """命中 file 那个区间同样判 trial."""
    entry.interval = 269
    entry.try_begin_ms, entry.try_end_ms = 84346, 142843
    entry.vi = [0, 0, 0, 0, 84346, 114346]

    result = verify.check_duration(entry, 58.5)
    assert result.verdict is verify.Verdict.TRIAL
    assert "file.try_begin/try_end" in result.detail


def test_full_length_not_flagged_even_with_two_windows(entry: SongEntry) -> None:
    """完整时长不能因为存在两个窗口就被误判."""
    entry.interval = 269
    entry.try_begin_ms, entry.try_end_ms = 84346, 142843
    entry.vi = [0, 0, 0, 0, 84346, 114346]
    assert verify.check_duration(entry, 269.0).ok


def test_window_covering_whole_song_is_ignored(entry: SongEntry) -> None:
    """「试听窗口」几乎等于整首歌时, 比对没有意义, 不能拿它判 trial."""
    entry.interval = 60
    entry.try_begin_ms, entry.try_end_ms = 0, 60000
    entry.vi = []
    assert verify.check_duration(entry, 60.0).ok


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
    # 命中试听窗口时要把是哪个窗口点出来
    assert "试听窗口" in result.detail


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
