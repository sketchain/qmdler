"""命令行入口.

* ``qmdler web``      启 WebUI
* ``qmdler tui``      启 TUI (探不到后端时自动拉起一个)
* ``qmdler download`` 纯 CLI 无人值守
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .core.config import ConfigStore, paths
from .core.config.schema import LogConfig
from .logging_conf import setup_logging

app = typer.Typer(
    name="qmdler",
    help="QQ 音乐歌单批量下载器（WebUI + TUI + CLI）。仅供个人学习与备份自有权益内容使用。",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"qmdler {__version__}")
        raise typer.Exit


@app.callback()
def main_callback(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="显示版本"),
    ] = False,
) -> None:
    """qmdler 命令行."""


@app.command()
def web(
    host: Annotated[str, typer.Option(help="监听地址")] = "",
    port: Annotated[int, typer.Option(help="监听端口")] = 0,
    preset: Annotated[str, typer.Option(help="使用的配置预设")] = "",
    reload: Annotated[bool, typer.Option(help="开发模式自动重载")] = False,
) -> None:
    """启动 WebUI（同时也是 TUI 连接的后端）。"""
    import uvicorn

    settings = ConfigStore().load(preset or None)
    setup_logging(settings.log)

    bind_host = host or settings.server.host
    bind_port = port or settings.server.port

    console.print(f"[green]qmdler[/green] WebUI → http://{bind_host}:{bind_port}")
    console.print("[dim]仅供个人学习与备份自有权益内容使用。[/dim]")

    uvicorn.run(
        "qmdler.server.app:app",
        host=bind_host,
        port=bind_port,
        reload=reload,
        log_level=settings.log.level.lower(),
    )


@app.command()
def tui(
    backend_url: Annotated[str, typer.Option("--backend", help="已有后端地址，留空则自动探测/拉起")] = "",
    no_autostart: Annotated[bool, typer.Option("--no-autostart", help="探不到后端时不自动拉起")] = False,
) -> None:
    """启动 TUI。检测不到后端时会自动拉起一个本地服务。"""
    from .tui.app import run_tui
    from .tui.bootstrap import ensure_backend

    settings = ConfigStore().load()
    # TUI 模式下日志绝不能打到控制台, 否则会打乱界面.
    setup_logging(
        LogConfig(
            level=settings.log.level,
            file=settings.log.file or str(paths.log_file()),
            max_bytes=settings.log.max_bytes,
            backup_count=settings.log.backup_count,
        ),
        console=False,
    )

    async def runner() -> None:
        backend = await ensure_backend(
            backend_url,
            host=settings.server.host,
            port=settings.server.port,
            autostart=not no_autostart,
        )
        await run_tui(backend)

    try:
        asyncio.run(runner())
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


@app.command()
def download(
    playlist: Annotated[str, typer.Option("--playlist", help="歌单 ID 或分享链接")] = "",
    album: Annotated[str, typer.Option("--album", help="专辑 mid")] = "",
    singer: Annotated[str, typer.Option("--singer", help="歌手 mid")] = "",
    fav: Annotated[bool, typer.Option("--fav", help="下载「我喜欢」")] = False,
    mids: Annotated[str, typer.Option("--mids", help="手动 mid 列表，逗号分隔")] = "",
    save_root: Annotated[str, typer.Option("--out", help="保存目录，覆盖配置")] = "",
    limit: Annotated[int, typer.Option(help="最多拉取多少首")] = 2000,
    interval: Annotated[float, typer.Option(help="每首之间的间隔秒数，覆盖配置")] = 0.0,
    preset: Annotated[str, typer.Option(help="使用的配置预设")] = "",
    dry_run: Annotated[bool, typer.Option("--dry-run", help="只列出将要下载的内容，不实际下载")] = False,
) -> None:
    """纯 CLI 无人值守下载。"""
    if not any([playlist, album, singer, fav, mids]):
        console.print("[red]请至少指定一个来源：--playlist / --album / --singer / --fav / --mids[/red]")
        raise typer.Exit(2)

    exit_code = asyncio.run(
        _run_download(
            playlist=playlist,
            album=album,
            singer=singer,
            fav=fav,
            mids=mids,
            save_root=save_root,
            limit=limit,
            interval=interval,
            preset=preset,
            dry_run=dry_run,
        ),
    )
    raise typer.Exit(exit_code)


async def _run_download(
    *,
    playlist: str,
    album: str,
    singer: str,
    fav: bool,
    mids: str,
    save_root: str,
    limit: int,
    interval: float,
    preset: str,
    dry_run: bool,
) -> int:
    import time

    from .core.download.engine import new_task_id
    from .core.events import EventKind
    from .core.models import SongEntry, TaskRecord, TaskStatus
    from .core.quality import build_plan
    from .core.report import build_report
    from .core.sources.resolver import parse_mid_list, resolve
    from .server.context import build_context

    settings_store = ConfigStore()
    base_settings = settings_store.load(preset or None)
    setup_logging(base_settings.log)

    async with build_context(preset or None) as context:
        settings = context.settings
        if interval > 0:
            settings.download.interval_seconds = interval
            context.engine.apply_settings(settings)

        if not context.auth.logged_in:
            console.print("[red]未登录。请先运行 `qmdler web` 或 `qmdler tui` 完成登录。[/red]")
            return 1

        # 把日志转到控制台, 无人值守时才看得见进展.
        def on_event(event: object) -> None:  # pragma: no cover - 事件回调
            pass

        # 1) 拉来源
        try:
            if fav:
                result = await context.sources.from_fav_song(limit=limit)
            elif playlist:
                resolved = resolve(playlist)
                if resolved is None or not resolved.identifier.isdigit():
                    console.print(f"[red]无法解析歌单：{playlist}[/red]")
                    return 2
                result = await context.sources.from_songlist(int(resolved.identifier), limit=limit)
            elif album:
                resolved = resolve(album)
                result = await context.sources.from_album(
                    resolved.identifier if resolved else album,
                    limit=limit,
                )
            elif singer:
                resolved = resolve(singer)
                result = await context.sources.from_singer(
                    resolved.identifier if resolved else singer,
                    limit=limit,
                )
            else:
                parsed = parse_mid_list(mids)
                if not parsed:
                    console.print("[red]没有解析出任何有效的 mid[/red]")
                    return 2
                result = await context.sources.from_mids(parsed)
        except Exception as exc:
            console.print(f"[red]拉取来源失败：{exc}[/red]")
            return 1

        console.print(f"[green]{result.name}[/green]：{len(result.entries)} 首")

        chain = settings.quality.chain
        rows: list[tuple[SongEntry, str, bool]] = []
        est_total = 0
        table = Table("歌名", "歌手", "最高可用", "提示", show_lines=False)
        for entry in result.entries:
            plan = build_plan(entry, chain, on_all_unavailable=settings.quality.on_all_unavailable)
            rows.append((entry, plan.requested, True))
            est_total += entry.available_qualities.get(plan.requested, 0)
            if dry_run:
                table.add_row(
                    entry.title,
                    "、".join(entry.singer_names),
                    plan.requested or "—",
                    "；".join(plan.warnings) or plan.unavailable_reason,
                )

        if dry_run:
            console.print(table)
            console.print(f"预计总大小约 {est_total / 1e9:.2f} GB")
            return 0

        # 2) 建任务
        now = int(time.time())
        task = TaskRecord(
            id=new_task_id(),
            name=result.name,
            source_type=result.source_type.value,
            source_id=result.identifier,
            source_ref=playlist or album or singer or mids,
            save_root=save_root or settings.paths.save_root,
            quality_chain=chain,
            options=settings.as_dict(),
            status=TaskStatus.PENDING,
            pause_reason="",
            total_items=len(rows),
            est_total_bytes=est_total,
            created_at=now,
            updated_at=now,
        )
        await context.repo.create_task(task)
        await context.repo.add_items(task.id, rows)
        await context.repo.update_task_totals(task.id, len(rows), est_total)

        # 3) 把引擎日志打到控制台
        async def drain() -> None:
            async with context.bus.subscribe() as queue:
                while True:
                    event = await queue.get()
                    if event.kind is EventKind.LOG:
                        style = {"error": "red", "warning": "yellow"}.get(event.level, "")
                        message = event.payload.get("message", "")
                        console.print(f"[{style}]{message}[/{style}]" if style else message)

        pump = asyncio.create_task(drain())
        try:
            await context.engine.start(task.id)
            await context.engine.wait_idle()
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump

        # 4) 汇总
        report = await build_report(context.repo, task.id)
        if report is None:
            return 1
        console.print(f"\n[bold]{report.as_dict()['summary']}[/bold]")
        if report.trials:
            console.print(f"[magenta]试听/降级 {len(report.trials)} 首（未计入成功）[/magenta]")
        if report.failures:
            console.print(f"[red]失败 {len(report.failures)} 首[/red]")
        return 0 if not report.failures else 1


def main() -> None:
    """控制台入口."""
    try:
        app()
    except KeyboardInterrupt:  # pragma: no cover
        console.print("\n[dim]已中断[/dim]")
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
