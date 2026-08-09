"""下载引擎.

硬约束 (实现过程中不为便利妥协):

* **严格串行, 一首一首来.** 全局只允许一个任务在跑, 任务内不做并发下载.
* 流程必须是: 取第 N 首 URL → 立即下载 → 处理元数据 → 落盘 → 等待间隔 →
  取第 N+1 首. **绝不**一次性把整个歌单的 URL 全部取回再慢慢下载 —— vkey 有时效,
  几小时后半段会全部失效.
* 每次 ``get_song_urls`` 只包含**一首**歌 (可含该首的多个音质档位).
* 试听/降级四层校验一层都不能省, 检出即记 ``trial``, 绝不混进「成功」.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from qqmusic_api import Client

from ..auth.manager import AuthManager, NotLoggedInError
from ..config.schema import Settings
from ..events import EventBus, EventKind
from ..fsutil import check_space, expand
from ..metadata.service import MetadataService
from ..metadata.tagger import TagPayload, TagWriteError, detect_duration, supports, write_tags
from ..models import (
    ItemRecord,
    ItemStatus,
    SubStatus,
    TaskRecord,
    TaskStatus,
    quality_extension,
    quality_label,
)
from ..naming.sanitize import dedupe_path
from ..naming.template import RenderContext, TemplateRenderer
from ..quality import build_plan
from ..sources.service import SourceService
from ..storage.repository import Repository
from . import fetcher, verify
from .cdn import CdnManager
from .errors import Action, classify, classify_result_code
from .ratelimit import IntervalWaiter
from .state import ActionOutcome, is_complete, resolve_action, summarize

logger = logging.getLogger(__name__)


@dataclass
class EngineState:
    """引擎当前状态, 推给两个前端."""

    running: bool = False
    task_id: str = ""
    task_name: str = ""
    item_id: int | None = None
    item_title: str = ""
    phase: str = "idle"
    waiting: bool = False
    countdown: float = 0.0
    countdown_total: float = 0.0
    counts: dict[str, int] = field(default_factory=dict)
    pause_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        """转为 WS 帧."""
        return {
            "running": self.running,
            "task_id": self.task_id,
            "task_name": self.task_name,
            "item_id": self.item_id,
            "item_title": self.item_title,
            "phase": self.phase,
            "waiting": self.waiting,
            "countdown": round(self.countdown, 1),
            "countdown_total": round(self.countdown_total, 1),
            "counts": self.counts,
            "pause_reason": self.pause_reason,
        }


class DownloadEngine:
    """串行下载引擎. 全局只有一个实例, 同时只跑一个任务."""

    def __init__(
        self,
        client: Client,
        auth: AuthManager,
        repo: Repository,
        bus: EventBus,
        settings: Settings,
        metadata: MetadataService,
        sources: SourceService,
        http: httpx.AsyncClient,
    ) -> None:
        """初始化."""
        self._client = client
        self._auth = auth
        self._repo = repo
        self._bus = bus
        self._settings = settings
        self._metadata = metadata
        self._sources = sources
        self._http = http

        self._cdn = CdnManager(client)
        self._waiter = IntervalWaiter(settings.download, on_tick=self._on_countdown)
        self._runner: asyncio.Task[None] | None = None
        self._pause_requested = False
        self._cancel_requested = False
        self._state = EngineState()

    # -- 对外控制 ------------------------------------------------------------ #

    @property
    def state(self) -> EngineState:
        """当前状态."""
        return self._state

    @property
    def busy(self) -> bool:
        """是否有任务在跑."""
        return self._runner is not None and not self._runner.done()

    def apply_settings(self, settings: Settings) -> None:
        """热更新配置 (下一首生效)."""
        self._settings = settings
        self._waiter = IntervalWaiter(settings.download, on_tick=self._on_countdown)

    async def start(self, task_id: str) -> None:
        """开始/继续一个任务.

        Raises:
            RuntimeError: 已经有任务在跑.
        """
        if self.busy:
            raise RuntimeError(f"已有任务在运行: {self._state.task_id}")
        task = await self._repo.get_task(task_id)
        if task is None:
            raise KeyError(f"任务不存在: {task_id}")

        self._pause_requested = False
        self._cancel_requested = False
        self._runner = asyncio.create_task(self._run(task), name=f"qmdler-task-{task_id}")

    async def pause(self, reason: str = "用户暂停") -> None:
        """暂停当前任务 (等当前这一首处理完)."""
        if not self.busy:
            return
        self._pause_requested = True
        self._state.pause_reason = reason
        self._waiter.skip()
        self._emit_state()

    async def cancel(self) -> None:
        """取消当前任务."""
        if not self.busy:
            return
        self._cancel_requested = True
        self._waiter.skip()
        if self._runner is not None:
            self._runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._runner
            self._runner = None
        self._emit_state()

    def skip_wait(self) -> None:
        """「立即下载下一首」: 跳过剩余间隔."""
        self._waiter.skip()

    async def wait_idle(self) -> None:
        """等待当前任务结束 (CLI 用)."""
        if self._runner is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._runner

    # -- 主循环 -------------------------------------------------------------- #

    async def _run(self, task: TaskRecord) -> None:
        renderer = TemplateRenderer(self._settings.naming)
        self._state = EngineState(running=True, task_id=task.id, task_name=task.name, phase="starting")
        self._emit_state()

        try:
            # 任务开始前主动校验一次凭证 (服务端).
            await self._auth.ensure_before_task()
        except (NotLoggedInError, Exception) as exc:
            decision = classify(exc) if not isinstance(exc, NotLoggedInError) else None
            reason = decision.message if decision else str(exc)
            await self._finish_task(task.id, TaskStatus.PAUSED_AUTH, reason)
            return

        ok, message = await self._preflight(task)
        if not ok:
            await self._finish_task(task.id, TaskStatus.PAUSED, message)
            return

        await self._repo.set_task_status(task.id, TaskStatus.RUNNING)
        self._bus.emit(EventKind.TASK_STATE, {"status": TaskStatus.RUNNING.value}, task_id=task.id)

        first = True
        try:
            while True:
                if self._cancel_requested:
                    await self._finish_task(task.id, TaskStatus.CANCELLED, "用户取消")
                    return
                if self._pause_requested:
                    await self._finish_task(task.id, TaskStatus.PAUSED, self._state.pause_reason)
                    return

                item = await self._repo.next_pending_item(task.id)
                if item is None:
                    await self._finish_task(task.id, TaskStatus.COMPLETED, "")
                    return

                # 间隔: 第一首不等.
                if not first:
                    self._state.phase = "waiting"
                    self._emit_state()
                    await self._waiter.wait()
                    if self._cancel_requested or self._pause_requested:
                        continue
                first = False

                outcome = await self._process_item(task, item, renderer)
                if outcome is not None and outcome.task_status is not None:
                    await self._finish_task(task.id, outcome.task_status, self._state.pause_reason)
                    return
        except asyncio.CancelledError:
            await self._finish_task(task.id, TaskStatus.CANCELLED, "任务被取消")
            raise
        except Exception as exc:
            logger.exception("任务执行异常")
            await self._finish_task(task.id, TaskStatus.FAILED, f"任务异常: {exc}")

    async def _preflight(self, task: TaskRecord) -> tuple[bool, str]:
        """任务开始前的磁盘检查: 目录可写 + 空间足够."""
        root = str(expand(task.save_root))
        ok, message = check_space(
            root,
            task.est_total_bytes,
            self._settings.paths.disk_headroom_bytes,
        )
        if not ok:
            self._bus.log(f"磁盘检查未通过: {message}", level="error", task_id=task.id)
        return ok, message

    # -- 单首处理 ------------------------------------------------------------ #

    async def _process_item(
        self,
        task: TaskRecord,
        item: ItemRecord,
        renderer: TemplateRenderer,
    ) -> ActionOutcome | None:
        """处理一首歌. 返回非 None 表示要改变任务状态."""
        entry = item.entry
        self._state.item_id = item.id
        self._state.item_title = entry.title
        self._state.phase = "preparing"
        self._emit_state()

        chain = task.quality_chain or self._settings.quality.chain
        plan = build_plan(entry, chain, on_all_unavailable=self._settings.quality.on_all_unavailable)

        if not plan.available:
            await self._set_item(
                item,
                ItemStatus.UNAVAILABLE,
                error_message=plan.unavailable_reason,
                finished_at=int(time.time()),
            )
            return None

        # 判重: songmid + 目标音质. 目标音质 = 链上最高的可用档位, 用列表阶段
        # 已有的 size_* 算出来, 零额外请求.
        existing = await self._repo.find_download(entry.songmid, plan.requested)
        if existing and Path(existing["path"]).exists():
            await self._set_item(
                item,
                ItemStatus.SKIPPED,
                requested_quality=plan.requested,
                actual_quality=plan.requested,
                target_path=existing["path"],
                error_message="已存在（songmid + 目标音质 命中去重索引）",
                finished_at=int(time.time()),
            )
            return None

        await self._repo.update_item(item.id, requested_quality=plan.requested, status=ItemStatus.DOWNLOADING)
        await self._set_item(item, ItemStatus.DOWNLOADING, started_at=int(time.time()), emit_only=True)

        # media_mid 缺失时才懒补全一次详情.
        if not entry.media_mid:
            self._state.phase = "completing"
            self._emit_state()
            try:
                entry = await self._sources.ensure_media_mid(entry)
            except Exception as exc:
                decision = classify(exc)
                self._bus.log(f"补全详情失败: {decision.message}", level="warning", task_id=task.id, item_id=item.id)

        candidates = [candidate.code for candidate in plan.candidates]
        attempt = 0
        max_retries = self._settings.download.max_retries

        while True:
            attempt += 1
            try:
                return await self._attempt_download(task, item, entry, candidates, plan.requested, renderer)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                decision = classify(exc)
                outcome = resolve_action(decision.action)

                if decision.action is Action.REFRESH_AND_RETRY:
                    with contextlib.suppress(Exception):
                        await self._auth.refresh(force=True)

                if outcome.retry and attempt <= max_retries:
                    backoff = self._settings.download.retry_backoff_base ** (attempt - 1)
                    self._bus.log(
                        f"{entry.title}：{decision.message}，{backoff:.0f}s 后重试（{attempt}/{max_retries}）",
                        level="warning",
                        task_id=task.id,
                        item_id=item.id,
                    )
                    await self._repo.update_item(item.id, retry_count=attempt)
                    await asyncio.sleep(backoff)
                    continue

                if outcome.task_status is not None:
                    self._state.pause_reason = decision.message
                    await self._set_item(
                        item,
                        ItemStatus.PENDING,
                        error_type=decision.error_type,
                        error_message=decision.message,
                        api_result_code=decision.result_code,
                    )
                    self._bus.log(decision.message, level="error", task_id=task.id, item_id=item.id)
                    return outcome

                status = outcome.item_status or ItemStatus.FAILED
                await self._set_item(
                    item,
                    status,
                    error_type=decision.error_type,
                    error_message=decision.message,
                    api_result_code=decision.result_code,
                    retry_count=attempt,
                    finished_at=int(time.time()),
                )
                self._bus.log(
                    f"{entry.title}：{decision.message}",
                    level="error",
                    task_id=task.id,
                    item_id=item.id,
                )
                return None

    async def _attempt_download(
        self,
        task: TaskRecord,
        item: ItemRecord,
        entry: Any,
        candidates: list[str],
        requested: str,
        renderer: TemplateRenderer,
    ) -> ActionOutcome | None:
        """取链 → 下载 → 校验 → 元数据 → 落盘."""
        self._state.phase = "fetching_url"
        self._emit_state()

        credential = await self._auth.get_valid_credential()
        # 一次调用只含这一首歌的多个档位.
        urls = await fetcher.fetch_urls(self._client, entry, candidates, credential=credential)
        by_quality = {candidate.quality: candidate for candidate in urls}

        rejected: list[str] = []
        for quality in candidates:
            candidate = by_quality.get(quality)
            if candidate is None:
                continue
            if not candidate.usable:
                decision = classify_result_code(candidate.result)
                if decision.action is Action.PAUSE_TASK:
                    self._state.pause_reason = decision.message
                    await self._set_item(
                        item,
                        ItemStatus.PENDING,
                        error_type=decision.error_type,
                        error_message=decision.message,
                        api_result_code=candidate.result,
                    )
                    return resolve_action(decision.action)
                rejected.append(f"{quality_label(quality)}: {decision.message}")
                continue

            # 第 1 层: filename 前缀比对.
            prefix_result = verify.check_prefix(quality, candidate.purl, candidate.filename)
            if prefix_result.verdict is verify.Verdict.TRIAL and self._settings.quality.reject_trial:
                rejected.append(f"{quality_label(quality)}: {prefix_result.detail}")
                self._bus.log(
                    f"{entry.title}：{prefix_result.detail}，尝试下一档",
                    level="warning",
                    task_id=task.id,
                    item_id=item.id,
                )
                continue
            if (
                prefix_result.verdict is verify.Verdict.DEGRADED
                and self._settings.quality.on_prefix_mismatch == "fail"
            ):
                rejected.append(f"{quality_label(quality)}: {prefix_result.detail}")
                continue

            outcome = await self._download_one(
                task,
                item,
                entry,
                quality,
                requested,
                candidate,
                prefix_result,
                renderer,
            )
            if outcome == "retry_next":
                rejected.append(f"{quality_label(quality)}: 检出试听/不完整，已尝试下一档")
                continue
            return None

        # 所有候选都不可用.
        detail = "；".join(rejected) or "没有任何档位返回可用链接"
        await self._set_item(
            item,
            ItemStatus.UNAVAILABLE,
            error_message=detail,
            finished_at=int(time.time()),
        )
        self._bus.log(f"{entry.title}：{detail}", level="warning", task_id=task.id, item_id=item.id)
        return None

    async def _download_one(
        self,
        task: TaskRecord,
        item: ItemRecord,
        entry: Any,
        quality: str,
        requested: str,
        candidate: fetcher.UrlCandidate,
        prefix_result: verify.VerifyResult,
        renderer: TemplateRenderer,
    ) -> str:
        """下载单个档位. 返回 ``"done"`` 或 ``"retry_next"``."""
        settings = self._settings
        extension = quality_extension(quality)

        # 目标路径.
        ctx = RenderContext(
            entry=entry,
            playlist_name=task.name,
            quality=quality,
            position=item.position + 1 if not entry.track_no else entry.track_no,
        )
        relative = renderer.relative_path(ctx, extension)
        root = expand(task.save_root)
        target = root / relative

        if target.exists():
            policy = settings.download.conflict_policy
            if policy == "skip":
                await self._set_item(
                    item,
                    ItemStatus.SKIPPED,
                    actual_quality=quality,
                    target_path=str(target),
                    error_message="同名文件已存在（冲突策略：跳过）",
                    finished_at=int(time.time()),
                )
                return "done"
            if policy == "suffix":
                target = Path(dedupe_path(str(target), lambda path: Path(path).exists()))
            # overwrite: 直接覆盖

        expected_size = entry.available_qualities.get(quality, 0)

        node = await self._cdn.pick()
        url = self._cdn.join(node, candidate.purl)

        # 第 3 层 (前置): HEAD/Range 探 Content-Length, 不合再下就是浪费 3 分钟.
        self._state.phase = "probing"
        self._emit_state()
        content_length = await fetcher.probe_content_length(self._http, url)

        pre_checks = [
            prefix_result,
            verify.check_trial_size(entry, content_length, tolerance=settings.quality.size_tolerance),
            verify.check_size(expected_size, content_length, tolerance=settings.quality.size_tolerance),
        ]
        pre_result = verify.combine(*pre_checks)
        if pre_result.verdict is verify.Verdict.TRIAL and settings.quality.reject_trial:
            self._bus.log(
                f"{entry.title}：下载前检出试听（{pre_result.detail}），跳过该档位",
                level="warning",
                task_id=task.id,
                item_id=item.id,
            )
            return "retry_next"

        # 下载.
        self._state.phase = "downloading"
        self._emit_state()
        await self._repo.update_item(item.id, bytes_expected=expected_size or content_length, target_path=str(target))

        def on_progress(written: int, total: int) -> None:
            self._bus.emit(
                EventKind.PROGRESS,
                {"written": written, "total": total or expected_size},
                task_id=task.id,
                item_id=item.id,
            )

        try:
            outcome = await fetcher.download_file(
                self._http,
                url,
                target,
                node=node,
                cdn=self._cdn,
                expected_size=expected_size,
                on_progress=on_progress,
                cancelled=lambda: self._cancel_requested,
            )
        except fetcher.LinkExpiredError:
            # 403 / 链接失效: 重新取一次 vkey 续传, 不计入失败次数.
            self._bus.log(
                f"{entry.title}：下载链接失效，重新取 vkey 续传",
                level="info",
                task_id=task.id,
                item_id=item.id,
            )
            credential = await self._auth.get_valid_credential()
            fresh = await fetcher.fetch_urls(self._client, entry, [quality], credential=credential)
            if not fresh or not fresh[0].usable:
                raise
            node = await self._cdn.pick()
            url = self._cdn.join(node, fresh[0].purl)
            outcome = await fetcher.download_file(
                self._http,
                url,
                target,
                node=node,
                cdn=self._cdn,
                expected_size=expected_size,
                on_progress=on_progress,
                cancelled=lambda: self._cancel_requested,
            )

        # 第 3 / 4 层: 落盘后的字节数与时长.
        self._state.phase = "verifying"
        self._emit_state()
        post_result = verify.combine(
            verify.check_trial_size(entry, outcome.bytes_written, tolerance=settings.quality.size_tolerance),
            verify.check_size(expected_size, outcome.bytes_written, tolerance=settings.quality.size_tolerance),
            verify.check_duration(
                entry,
                detect_duration(outcome.path),
                tolerance=settings.quality.duration_tolerance,
            ),
        )

        if post_result.verdict is verify.Verdict.TRIAL:
            return await self._handle_trial(task, item, entry, quality, outcome.path, target, post_result)

        # 元数据.
        degraded = quality != requested or prefix_result.verdict is verify.Verdict.DEGRADED
        final_path = fetcher.finalize(outcome.path, target)
        lyric_status, cover_status, tag_status = await self._apply_metadata(entry, final_path, quality, task)

        await self._repo.record_download(
            entry.songmid,
            requested,
            str(final_path),
            outcome.bytes_written,
            task_id=task.id,
        )
        await self._set_item(
            item,
            ItemStatus.SUCCESS,
            actual_quality=quality,
            degraded=degraded,
            target_path=str(final_path),
            bytes_downloaded=outcome.bytes_written,
            bytes_expected=expected_size or outcome.bytes_written,
            lyric_status=lyric_status,
            cover_status=cover_status,
            tag_status=tag_status,
            error_message="",
            finished_at=int(time.time()),
        )

        note = ""
        if degraded:
            note = f"（已降级：{quality_label(requested)} → {quality_label(quality)}）"
        self._bus.log(
            f"完成 {entry.title} [{quality_label(quality)}]{note}",
            level="info",
            task_id=task.id,
            item_id=item.id,
        )
        return "done"

    async def _handle_trial(
        self,
        task: TaskRecord,
        item: ItemRecord,
        entry: Any,
        quality: str,
        part: Path,
        target: Path,
        result: verify.VerifyResult,
    ) -> str:
        """检出试听片段的处理."""
        settings = self._settings
        keep = settings.quality.keep_trial_file

        if settings.quality.reject_trial:
            if not keep:
                part.unlink(missing_ok=True)
            self._bus.log(
                f"{entry.title}：检出试听片段（{result.detail}），尝试下一档",
                level="warning",
                task_id=task.id,
                item_id=item.id,
            )
            return "retry_next"

        final_path = fetcher.finalize(part, target) if keep else part
        if not keep:
            part.unlink(missing_ok=True)
        await self._set_item(
            item,
            ItemStatus.TRIAL,
            actual_quality=quality,
            trial_reason=result.reason,
            target_path=str(final_path) if keep else "",
            error_message=result.detail,
            finished_at=int(time.time()),
        )
        return "done"

    async def _apply_metadata(
        self,
        entry: Any,
        path: Path,
        quality: str,
        task: TaskRecord,
    ) -> tuple[SubStatus, SubStatus, SubStatus]:
        """歌词 / 封面 / tag. 任何一项失败都不让整首歌算失败."""
        settings = self._settings
        self._state.phase = "metadata"
        self._emit_state()

        lyric_bundle = await self._metadata.fetch_lyric(entry, settings.lyric)
        if lyric_bundle.status is SubStatus.OK:
            with contextlib.suppress(OSError):
                self._metadata.write_lyric_files(lyric_bundle, path, settings.lyric)

        embed_cover = await self._metadata.fetch_cover(entry, settings.cover, for_embed=True)
        cover_status = SubStatus.SKIPPED
        if settings.cover.enabled:
            cover_status = SubStatus.OK if embed_cover.ok else SubStatus.FAILED
            if settings.cover.save_file:
                save_cover = (
                    embed_cover
                    if settings.cover.save_size == settings.cover.embed_size
                    else await self._metadata.fetch_cover(entry, settings.cover, for_embed=False)
                )
                if save_cover.ok:
                    name = settings.cover.file_name or "cover.jpg"
                    with contextlib.suppress(OSError):
                        self._metadata.write_cover_file(save_cover, path.parent / name)

        extra = await self._metadata.fetch_extra_tags(entry, settings.tag)

        tag_status = SubStatus.SKIPPED
        if settings.tag.enabled:
            if not supports(path):
                tag_status = SubStatus.UNSUPPORTED
                self._bus.log(
                    f"{entry.title}：{path.suffix} 是非标准容器，已跳过 tag 写入",
                    level="warning",
                    task_id=task.id,
                )
            else:
                payload = TagPayload(
                    entry=entry,
                    quality_label=quality_label(quality),
                    composer=extra.composer,
                    lyricist=extra.lyricist,
                    genre=extra.genre,
                    lyrics=lyric_bundle.embed_text if settings.lyric.embed_in_tag else "",
                    cover=embed_cover.data if settings.cover.embed else b"",
                    cover_mime=embed_cover.mime,
                )
                try:
                    write_tags(path, payload, settings.tag)
                except TagWriteError as exc:
                    tag_status = SubStatus.FAILED
                    self._bus.log(f"{entry.title}：tag 写入失败 — {exc}", level="warning", task_id=task.id)
                else:
                    tag_status = SubStatus.OK

        return lyric_bundle.status, cover_status, tag_status

    # -- 收尾与状态 ----------------------------------------------------------- #

    async def _finish_task(self, task_id: str, status: TaskStatus, reason: str) -> None:
        await self._repo.set_task_status(task_id, status, reason)
        counts = await self._repo.status_counts(task_id)
        self._state.running = False
        self._state.phase = status.value
        self._state.counts = summarize(counts)
        self._state.pause_reason = reason
        self._emit_state()
        self._bus.emit(
            EventKind.TASK_STATE,
            {"status": status.value, "reason": reason, "counts": summarize(counts)},
            task_id=task_id,
        )
        if status is TaskStatus.COMPLETED or is_complete(counts):
            self._metadata.clear_caches()
            self._bus.emit(EventKind.REPORT, {"task_id": task_id, "counts": summarize(counts)}, task_id=task_id)
        self._runner = None

    async def _set_item(
        self,
        item: ItemRecord,
        status: ItemStatus,
        *,
        emit_only: bool = False,
        **fields: Any,
    ) -> None:
        if not emit_only:
            await self._repo.update_item(item.id, status=status, **fields)
        refreshed = await self._repo.get_item(item.id)
        if refreshed is not None:
            self._bus.emit(
                EventKind.ITEM_STATE,
                refreshed.as_dict(),
                task_id=item.task_id,
                item_id=item.id,
            )
        counts = await self._repo.status_counts(item.task_id)
        self._state.counts = summarize(counts)

    def _on_countdown(self, remaining: float, total: float) -> None:
        self._state.waiting = True
        self._state.countdown = remaining
        self._state.countdown_total = total
        self._bus.emit(
            EventKind.COUNTDOWN,
            {"remaining": round(remaining, 1), "total": round(total, 1)},
            task_id=self._state.task_id,
        )

    def _emit_state(self) -> None:
        self._state.waiting = self._waiter.waiting
        self._state.countdown = self._waiter.remaining
        self._state.countdown_total = self._waiter.total
        self._bus.emit(EventKind.TASK_STATE, {"engine": self._state.as_dict()}, task_id=self._state.task_id or None)


def new_task_id() -> str:
    """生成任务 ID."""
    return uuid.uuid4().hex
