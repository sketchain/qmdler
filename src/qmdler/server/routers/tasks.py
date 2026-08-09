"""任务端点: 创建、勾选、启动、暂停、恢复、取消、报告."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field

from ...core.download.engine import new_task_id
from ...core.models import (
    ItemStatus,
    SongEntry,
    TaskRecord,
    TaskStatus,
)
from ...core.quality import build_plan
from ...core.report import build_report, export_csv
from ..deps import Ctx

router = APIRouter(prefix="/tasks", tags=["tasks"])


class CreateTaskRequest(BaseModel):
    """创建任务."""

    name: str = Field(min_length=1, max_length=200)
    source_type: str
    source_id: str = ""
    source_ref: str = ""
    #: 允许每个歌单单独设置保存位置; 留空用全局根目录.
    save_root: str = ""
    quality_chain: list[str] = Field(default_factory=list)
    #: 歌曲快照, 由 ``/sources/fetch`` 的结果直接回传.
    items: list[dict[str, Any]] = Field(default_factory=list)
    #: 只有勾选的才会下载.
    selected_mids: list[str] | None = None


class SelectionRequest(BaseModel):
    """勾选 / 取消勾选."""

    item_ids: list[int] | None = None
    selected: bool = True


class FilterRequest(BaseModel):
    """按关键词过滤勾选."""

    keyword: str = ""
    #: 只勾选匹配项 (True) 还是取消勾选匹配项 (False).
    select_matched: bool = True


@router.get("")
async def list_tasks(context: Ctx, limit: int = 50) -> list[dict[str, Any]]:
    """任务列表."""
    tasks = await context.repo.list_tasks(limit=limit)
    result = []
    for task in tasks:
        counts = await context.repo.status_counts(task.id)
        result.append({**task.as_dict(), "counts": counts})
    return result


@router.post("")
async def create_task(payload: CreateTaskRequest, context: Ctx) -> dict[str, Any]:
    """创建一个下载任务并把曲目入队."""
    settings = context.settings
    chain = payload.quality_chain or settings.quality.chain
    save_root = payload.save_root or settings.paths.per_source_roots.get(
        f"{payload.source_type}:{payload.source_id}",
        settings.paths.save_root,
    )

    now = int(time.time())
    task = TaskRecord(
        id=new_task_id(),
        name=payload.name,
        source_type=payload.source_type,
        source_id=payload.source_id,
        source_ref=payload.source_ref,
        save_root=save_root,
        quality_chain=chain,
        options=settings.as_dict(),
        status=TaskStatus.PENDING,
        pause_reason="",
        total_items=0,
        est_total_bytes=0,
        created_at=now,
        updated_at=now,
    )
    await context.repo.create_task(task)

    selected_set = set(payload.selected_mids) if payload.selected_mids is not None else None
    rows: list[tuple[SongEntry, str, bool]] = []
    est_total = 0
    for raw in payload.items:
        entry = SongEntry.from_dict(raw)
        plan = build_plan(entry, chain, on_all_unavailable=settings.quality.on_all_unavailable)
        selected = selected_set is None or entry.songmid in selected_set
        rows.append((entry, plan.requested, selected))
        if selected and plan.requested:
            est_total += entry.available_qualities.get(plan.requested, 0)

    if rows:
        await context.repo.add_items(task.id, rows)
    await context.repo.update_task_totals(task.id, len(rows), est_total)

    created = await context.repo.get_task(task.id)
    assert created is not None
    return created.as_dict()


@router.get("/{task_id}")
async def get_task(task_id: str, context: Ctx) -> dict[str, Any]:
    """任务详情."""
    task = await context.repo.get_task(task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    counts = await context.repo.status_counts(task_id)
    return {**task.as_dict(), "counts": counts}


@router.get("/{task_id}/items")
async def list_items(
    task_id: str,
    context: Ctx,
    limit: int = 500,
    offset: int = 0,
    status_filter: str = "",
) -> list[dict[str, Any]]:
    """任务的曲目列表."""
    statuses = None
    if status_filter:
        try:
            statuses = [ItemStatus(value) for value in status_filter.split(",") if value]
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"未知状态: {exc}") from exc
    items = await context.repo.list_items(task_id, statuses=statuses, limit=limit, offset=offset)
    return [item.as_dict() for item in items]


@router.post("/{task_id}/selection")
async def set_selection(task_id: str, payload: SelectionRequest, context: Ctx) -> dict[str, Any]:
    """全选 / 反选 / 手动勾选."""
    await context.repo.set_item_selection(task_id, payload.item_ids, payload.selected)
    return await context.repo.status_counts(task_id)


@router.post("/{task_id}/selection/invert")
async def invert_selection(task_id: str, context: Ctx) -> dict[str, Any]:
    """反选."""
    await context.repo.invert_selection(task_id)
    return await context.repo.status_counts(task_id)


@router.post("/{task_id}/selection/filter")
async def filter_selection(task_id: str, payload: FilterRequest, context: Ctx) -> dict[str, int]:
    """按关键词过滤勾选 (歌名 / 歌手 / 专辑)."""
    keyword = payload.keyword.strip().lower()
    items = await context.repo.list_items(task_id)
    matched: list[int] = []
    for item in items:
        haystack = " ".join(
            [item.entry.title, item.entry.album_name, *item.entry.singer_names],
        ).lower()
        if keyword and keyword in haystack:
            matched.append(item.id)
    await context.repo.set_item_selection(task_id, matched, payload.select_matched)
    return {"matched": len(matched)}


@router.post("/{task_id}/start")
async def start_task(task_id: str, context: Ctx) -> dict[str, Any]:
    """开始 / 继续任务. 全局只允许一个任务在跑."""
    try:
        await context.engine.start(task_id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return context.engine.state.as_dict()


@router.post("/{task_id}/pause")
async def pause_task(task_id: str, context: Ctx) -> dict[str, Any]:
    """暂停 (等当前这一首处理完)."""
    await context.engine.pause()
    return context.engine.state.as_dict()


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str, context: Ctx) -> dict[str, Any]:
    """取消任务."""
    if context.engine.state.task_id == task_id:
        await context.engine.cancel()
    await context.repo.set_task_status(task_id, TaskStatus.CANCELLED, "用户取消")
    return context.engine.state.as_dict()


@router.post("/{task_id}/skip-wait")
async def skip_wait(task_id: str, context: Ctx) -> dict[str, Any]:
    """「立即下载下一首」, 跳过剩余间隔."""
    context.engine.skip_wait()
    return context.engine.state.as_dict()


@router.post("/{task_id}/retry-failed")
async def retry_failed(task_id: str, context: Ctx) -> dict[str, int]:
    """只重试失败项 (含试听与受限)."""
    count = await context.repo.reset_failed(task_id)
    return {"reset": count}


@router.get("/{task_id}/report")
async def report(task_id: str, context: Ctx) -> dict[str, Any]:
    """汇总报告: 成功 N / 失败 M / 跳过 K / 受限 J / 试听 T."""
    result = await build_report(context.repo, task_id)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    return result.as_dict()


@router.get("/{task_id}/report.csv")
async def report_csv(task_id: str, context: Ctx) -> Response:
    """导出 CSV."""
    text = await export_csv(context.repo, task_id)
    return Response(
        content="﻿" + text,  # BOM, 让 Excel 正确识别 UTF-8
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="qmdler-{task_id}.csv"'},
    )


@router.delete("/{task_id}")
async def delete_task(task_id: str, context: Ctx) -> dict[str, bool]:
    """删除任务."""
    if context.engine.state.task_id == task_id and context.engine.busy:
        raise HTTPException(status.HTTP_409_CONFLICT, "任务正在运行，请先取消")
    await context.repo.delete_task(task_id)
    return {"ok": True}
