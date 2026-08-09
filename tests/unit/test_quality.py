"""音质选择."""

from __future__ import annotations

from qmdler.core.models import SongEntry
from qmdler.core.quality import annotate, build_plan, pay_warnings


def test_zero_size_qualities_are_excluded(entry: SongEntry) -> None:
    """size_* == 0 表示该音质根本不存在, 直接从候选里排除."""
    entry.sizes = {"MASTER": 0, "FLAC": 100, "MP3_320": 200}
    plan = build_plan(entry, ["MASTER", "FLAC", "MP3_320"])
    assert [candidate.code for candidate in plan.candidates] == ["FLAC", "MP3_320"]


def test_candidates_follow_user_chain_order(entry: SongEntry) -> None:
    """候选顺序完全跟随用户的优先级链, 不是音质高低."""
    entry.sizes = {"FLAC": 1, "MP3_128": 2, "MP3_320": 3}
    plan = build_plan(entry, ["MP3_128", "FLAC", "MP3_320"])
    assert [candidate.code for candidate in plan.candidates] == ["MP3_128", "FLAC", "MP3_320"]
    assert plan.requested == "MP3_128"


def test_pay_flags_do_not_filter_candidates(entry: SongEntry) -> None:
    """pay 只作提示, 不参与过滤 —— 走的是播放链路, pay_down=1 仍可能拿到完整文件."""
    entry.pay = {"pay_month": 1, "pay_play": 1, "pay_down": 1, "price_track": 500}
    entry.sizes = {"FLAC": 100}
    plan = build_plan(entry, ["FLAC"])
    assert plan.available
    assert [candidate.code for candidate in plan.candidates] == ["FLAC"]
    assert plan.warnings, "受限情况应该有 UI 提示"


def test_pay_warnings_text(entry: SongEntry) -> None:
    """付费提示文案覆盖各字段."""
    entry.pay = {"pay_month": 1, "pay_play": 1, "pay_down": 1, "price_track": 300, "time_free": 30}
    warnings = pay_warnings(entry)
    joined = " ".join(warnings)
    assert "单曲付费" in joined
    assert "需付费播放" in joined
    assert "需绿钻" in joined
    assert "限免" in joined


def test_offline_song_is_unavailable_without_any_request(entry: SongEntry) -> None:
    """status=1 已下架: 列表阶段就判定, 不浪费一次取链."""
    entry.status = 1
    plan = build_plan(entry, ["FLAC"])
    assert not plan.available
    assert plan.unavailable_reason == "已下架"
    assert plan.candidates == []


def test_pending_review_is_unavailable(entry: SongEntry) -> None:
    """status=2 待审核同样直接判定."""
    entry.status = 2
    assert build_plan(entry, ["FLAC"]).unavailable_reason == "待审核"


def test_region_limited_still_attempts(entry: SongEntry) -> None:
    """status=3 仅限部分区域: 只标注, 仍然尝试."""
    entry.status = 3
    entry.sizes = {"FLAC": 100}
    plan = build_plan(entry, ["FLAC"])
    assert plan.available
    assert any("区域" in warning for warning in plan.warnings)


def test_skip_when_nothing_available(entry: SongEntry) -> None:
    """链上全不可用且策略为 skip: 判为不可用."""
    entry.sizes = {"MP3_128": 100}
    plan = build_plan(entry, ["MASTER", "FLAC"], on_all_unavailable="skip")
    assert not plan.available
    assert "没有任何可用音质" in plan.unavailable_reason


def test_lowest_fallback_picks_worst_available(entry: SongEntry) -> None:
    """兜底策略挑最低可用档位."""
    entry.sizes = {"MP3_320": 100, "MP3_128": 50}
    plan = build_plan(entry, ["MASTER"], on_all_unavailable="lowest")
    assert plan.available
    assert plan.requested == "MP3_128"
    assert any("兜底" in warning for warning in plan.warnings)


def test_lowest_fallback_still_unavailable_when_nothing_exists(entry: SongEntry) -> None:
    """一个档位都不存在时, 兜底也救不回来."""
    entry.sizes = {}
    plan = build_plan(entry, ["FLAC"], on_all_unavailable="lowest")
    assert not plan.available


def test_annotation_reports_best_available(entry: SongEntry) -> None:
    """列表阶段预先标注可用的最高音质."""
    entry.sizes = {"MP3_128": 1, "FLAC": 2, "MASTER": 3}
    result = annotate(entry, ["MP3_128"])
    # 最高可用按档位表排序算, 与用户的链无关
    assert result["best_available"] == "MASTER"
    # 但请求的仍是链头
    assert result["plan"]["requested"] == "MP3_128"


def test_annotation_flags_payment(entry: SongEntry) -> None:
    """预先标注是否需要付费."""
    entry.pay = {"pay_play": 1}
    assert annotate(entry, ["FLAC"])["needs_payment"] is True
    entry.pay = {}
    assert annotate(entry, ["FLAC"])["needs_payment"] is False


def test_annotation_passes_sa_through_without_interpreting(entry: SongEntry) -> None:
    """sa 的位布局未文档化, 只透传原值, 不参与判断."""
    entry.sa = 0xDEADBEEF
    entry.sizes = {"FLAC": 100}
    result = annotate(entry, ["FLAC"])
    assert result["sa"] == 0xDEADBEEF
    # sa 不影响候选
    assert result["plan"]["candidates"]


def test_estimated_size_matches_requested_quality(entry: SongEntry) -> None:
    """预估大小取的是判重键对应档位的大小."""
    entry.sizes = {"FLAC": 38412345, "MP3_320": 9812345}
    result = annotate(entry, ["FLAC", "MP3_320"])
    assert result["estimated_size"] == 38412345
