"""汇总报告与 CSV 导出."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import Any

from .models import ItemRecord, ItemStatus, TaskRecord, quality_label
from .storage.repository import Repository

CSV_COLUMNS = [
    "序号",
    "歌名",
    "歌手",
    "专辑",
    "songmid",
    "状态",
    "请求音质",
    "实际音质",
    "是否降级",
    "试听原因",
    "文件路径",
    "文件大小",
    "错误码",
    "错误信息",
    "重试次数",
    "歌词",
    "封面",
    "标签",
]

STATUS_LABELS: dict[str, str] = {
    ItemStatus.PENDING.value: "待下载",
    ItemStatus.DOWNLOADING.value: "下载中",
    ItemStatus.SUCCESS.value: "成功",
    ItemStatus.FAILED.value: "失败",
    ItemStatus.SKIPPED.value: "已跳过",
    ItemStatus.UNAVAILABLE.value: "受限/无版权",
    ItemStatus.TRIAL.value: "试听片段",
}


@dataclass(slots=True)
class Report:
    """一个任务的汇总报告."""

    task: TaskRecord
    counts: dict[str, int]
    failures: list[dict[str, Any]]
    trials: list[dict[str, Any]]
    unavailable: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        """转为 REST 出参."""
        return {
            "task": self.task.as_dict(),
            "counts": self.counts,
            "summary": (
                f"成功 {self.counts.get('success', 0)} / "
                f"失败 {self.counts.get('failed', 0)} / "
                f"跳过 {self.counts.get('skipped', 0)} / "
                f"受限 {self.counts.get('unavailable', 0)} / "
                f"试听 {self.counts.get('trial', 0)}"
            ),
            "failures": self.failures,
            # 试听/降级单独成栏, 绝不混进「成功」.
            "trials": self.trials,
            "unavailable": self.unavailable,
        }


def _brief(item: ItemRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "title": item.entry.title,
        "singers": "、".join(item.entry.singer_names),
        "songmid": item.entry.songmid,
        "status": item.status.value,
        "requested_quality": quality_label(item.requested_quality) if item.requested_quality else "",
        "actual_quality": quality_label(item.actual_quality) if item.actual_quality else "",
        "reason": item.error_message or item.trial_reason,
        "api_result_code": item.api_result_code,
    }


async def build_report(repo: Repository, task_id: str) -> Report | None:
    """生成汇总报告."""
    task = await repo.get_task(task_id)
    if task is None:
        return None
    counts = await repo.status_counts(task_id)
    from .download.state import summarize

    items = await repo.list_items(task_id)
    return Report(
        task=task,
        counts=summarize(counts),
        failures=[_brief(item) for item in items if item.status is ItemStatus.FAILED],
        trials=[_brief(item) for item in items if item.status is ItemStatus.TRIAL],
        unavailable=[_brief(item) for item in items if item.status is ItemStatus.UNAVAILABLE],
    )


async def export_csv(repo: Repository, task_id: str) -> str:
    """把任务明细导出成 CSV 文本."""
    items = await repo.list_items(task_id)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    for index, item in enumerate(items, start=1):
        writer.writerow(
            [
                index,
                item.entry.title,
                "、".join(item.entry.singer_names),
                item.entry.album_name,
                item.entry.songmid,
                STATUS_LABELS.get(item.status.value, item.status.value),
                quality_label(item.requested_quality) if item.requested_quality else "",
                quality_label(item.actual_quality) if item.actual_quality else "",
                "是" if item.degraded else "",
                item.trial_reason,
                item.target_path,
                item.bytes_downloaded,
                item.api_result_code if item.api_result_code is not None else "",
                item.error_message,
                item.retry_count,
                item.lyric_status.value,
                item.cover_status.value,
                item.tag_status.value,
            ],
        )
    return buffer.getvalue()
