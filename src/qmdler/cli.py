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
def login(
    method: Annotated[str, typer.Option("--method", help="qq / wx / mobile")] = "qq",
    diagnose: Annotated[
        bool,
        typer.Option("--diagnose", help="额外打印二维码载荷文本（需 zxing-cpp，仅诊断用）"),
    ] = False,
    timeout: Annotated[float, typer.Option(help="等待扫码的秒数")] = 180.0,
) -> None:
    """终端扫码登录。

    同时给出三条出口：图片 HTTP 地址（首选）、落盘文件路径、字符画。
    字符画没通过自检就不画，只给地址。
    """
    exit_code = asyncio.run(_run_login(method=method, diagnose=diagnose, timeout=timeout))
    raise typer.Exit(exit_code)


async def _run_login(*, method: str, diagnose: bool, timeout: float) -> int:
    from .core.auth.qrimage import art_width
    from .core.events import EventKind
    from .core.netutil import public_bases
    from .server.context import build_context

    settings = ConfigStore().load()
    setup_logging(settings.log)

    async with build_context() as context:
        context.login.configure_endpoint(
            public_bases(settings.server.host, settings.server.port),
            paths.state_dir() / "qrcode",
        )
        try:
            state = await context.login.start_qrcode(method)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            return 2

        if state.status == "error":
            console.print(f"[red]{state.message}[/red]")
            return 1

        # 出口 1：HTTP 图片地址（首选）。服务没跑起来时地址打不开，明确说明。
        console.print("\n[bold]扫码登录[/bold]")
        console.print("[dim]提示：图片地址需要后端在跑（另开一个终端执行 `qmdler web`）。[/dim]")
        for url in state.image_urls:
            console.print(f"  图片地址: [cyan]{url}[/cyan]")
        # 出口 2：落盘文件。
        if state.saved_path:
            console.print(f"  已保存到: [cyan]{state.saved_path}[/cyan]")
        # 出口 3（仅诊断）：载荷文本。
        if diagnose:
            payload, error = context.login.decode_payload(state.session_id)
            if payload:
                console.print(f"  二维码载荷: [cyan]{payload}[/cyan]")
            else:
                console.print(f"  [yellow]载荷解码不可用：{error}[/yellow]")

        # 字符画：通过自检才画。
        if state.ascii_error:
            console.print(f"\n[yellow]终端二维码不可用：{state.ascii_error}[/yellow]")
        elif state.ascii_art:
            needed = art_width(state.ascii_art)
            console.print()
            console.print(state.ascii_art, highlight=False, markup=False)
            console.print(f"[dim]（字符画宽 {needed} 列；终端窄于此请用上面的地址）[/dim]")

        console.print(f"\n{state.message}\n")

        done = asyncio.Event()
        final: dict[str, object] = {}

        async def watch() -> None:
            async with context.bus.subscribe() as queue:
                while True:
                    event = await queue.get()
                    if event.kind is not EventKind.LOGIN_EVENT:
                        continue
                    payload = event.payload
                    if payload.get("session_id") != state.session_id:
                        continue
                    console.print(f"  · {payload.get('message', '')}")
                    if payload.get("status") in ("done", "refused", "timeout", "error"):
                        final.update(payload)
                        done.set()
                        return

        watcher = asyncio.create_task(watch())
        try:
            await asyncio.wait_for(done.wait(), timeout=timeout)
        except TimeoutError:
            console.print("[yellow]等待超时[/yellow]")
            return 1
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher

        if final.get("status") == "done":
            profile = await context.auth.load_profile()
            console.print(f"[green]登录成功[/green]：{profile.get('nickname') or context.auth.credential.musicid}")
            return 0
        console.print(f"[red]登录未完成：{final.get('message', '')}[/red]")
        return 1



@app.command()
def single(
    mid: Annotated[str, typer.Argument(help="歌曲 songmid，或含 songmid 的分享链接")],
    save_root: Annotated[str, typer.Option("--out", help="保存目录，覆盖配置")] = "",
    quality: Annotated[str, typer.Option("--quality", help="只试这一个档位，留空用配置里的优先级链")] = "",
    keep: Annotated[bool, typer.Option("--keep/--no-keep", help="保留下载到的文件")] = True,
    as_json: Annotated[bool, typer.Option("--json", help="输出 JSON，便于粘给我")] = False,
) -> None:
    """诊断模式：只下一首，跳过间隔，把四层校验每一层的实际判定值全部打印出来。

    输出里不会出现凭证 / musickey / vkey / 完整 purl —— purl 只打印最后一个路径段。
    """
    raise typer.Exit(
        asyncio.run(
            _run_single(mid=mid, save_root=save_root, quality=quality, keep=keep, as_json=as_json),
        ),
    )


async def _run_single(*, mid: str, save_root: str, quality: str, keep: bool, as_json: bool) -> int:
    import json as jsonlib
    import time

    from .core.download.diagnose import contains_secret
    from .core.download.engine import new_task_id
    from .core.models import TaskRecord, TaskStatus
    from .core.quality import build_plan
    from .core.sources.resolver import parse_mid_list
    from .server.context import build_context

    settings_store = ConfigStore()
    setup_logging(settings_store.load().log, console=not as_json)

    mids = parse_mid_list(mid)
    if not mids:
        console.print(f"[red]解析不出 songmid：{mid}[/red]")
        return 2

    async with build_context() as context:
        settings = context.settings
        if quality:
            settings.quality.chain = [quality]
        if save_root:
            settings.paths.save_root = save_root
        settings.quality.keep_trial_file = keep
        context.engine.apply_settings(settings)

        if not context.auth.logged_in:
            console.print("[red]未登录。先跑 `qmdler login` 或 `qmdler web`。[/red]")
            return 1

        diagnosis = context.engine.enable_diagnosis()

        result = await context.sources.from_mids(mids[:1], name="诊断")
        if not result.entries:
            console.print("[red]查不到这首歌的详情[/red]")
            return 1
        entry = result.entries[0]

        now = int(time.time())
        task = TaskRecord(
            id=new_task_id(),
            name="诊断",
            source_type="manual",
            source_id=entry.songmid,
            source_ref="",
            save_root=settings.paths.save_root,
            quality_chain=settings.quality.chain,
            options={},
            status=TaskStatus.PENDING,
            pause_reason="",
            total_items=1,
            est_total_bytes=0,
            created_at=now,
            updated_at=now,
        )
        await context.repo.create_task(task)
        plan = build_plan(entry, settings.quality.chain, on_all_unavailable=settings.quality.on_all_unavailable)
        await context.repo.add_items(task.id, [(entry, plan.requested, True)])

        await context.engine.start(task.id)
        await context.engine.wait_idle()

        payload = diagnosis.as_dict()
        text = jsonlib.dumps(payload, ensure_ascii=False, indent=2)

        # 脱敏是硬约束：真漏了就宁可不打印。
        leaked = contains_secret(text)
        if leaked:
            console.print(f"[red]诊断输出里检测到敏感字样 {leaked!r}，已拒绝打印。这是 bug，请报告。[/red]")
            return 1

        if as_json:
            print(text)
            return 0

        _print_diagnosis(payload)
        return 0


def _print_diagnosis(payload: dict) -> None:
    """人读版诊断输出."""
    console.print()
    console.print(f"[bold]{payload['title']}[/bold] — {payload['singers']}")
    console.print(f"  songmid={payload['songmid']}  时长={payload['interval']}s  上下架状态={payload['status']}")
    console.print(f"  [dim]sa={payload['sa']}（权益位掩码原值；位布局无文档，仅记录不解读）[/dim]")
    console.print(f"  pay={payload['pay']}")
    console.print(f"  可用档位={payload['available_qualities']}")
    console.print(f"  优先级链={payload['quality_chain']}  →  请求 {payload['requested_quality']}"
                  f"（编码 {payload['requested_start_code']}）")
    console.print()

    console.print("[bold]取链[/bold]")
    console.print(f"  实际尝试档位: {payload['attempted_quality']}   result={payload['result_code']}")
    console.print(f"  purl 文件名段: {payload['purl_filename']}  [dim](完整 purl 与 vkey 不打印)[/dim]")
    console.print()

    console.print("[bold]CDN[/bold]")
    if not payload["cdn_attempts"]:
        console.print("  [dim](没有拿到可用链接，未触及 CDN)[/dim]")
    for attempt in payload["cdn_attempts"]:
        console.print(f"  · {attempt}")
    if payload["cdn_used"]:
        console.print(f"  选用: {payload['cdn_used']}")
    if payload["probe_method"]:
        console.print(f"  探测分支: [cyan]{payload['probe_method']}[/cyan]（HTTP {payload['probe_status']}）"
                      f"  Content-Length={payload['content_length']}")
    console.print()

    console.print("[bold]四层校验[/bold]")
    if not payload["layers"]:
        console.print("  [dim](没有拿到可用链接，四层校验未执行)[/dim]")
    colors = {"ok": "green", "trial": "magenta", "degraded": "yellow"}
    for layer in payload["layers"]:
        color = colors.get(layer["verdict"], "white")
        console.print(f"  [{color}]{layer['verdict']:<8}[/{color}] {layer['name']}")
        console.print(f"           实测: {layer['observed']}")
        if layer["expected"]:
            console.print(f"           期望: {layer['expected']}")
        if layer["detail"]:
            console.print(f"           说明: {layer['detail']}")
    console.print()

    console.print(f"  试听窗口（全部候选，第 4 层逐个比对）  size_try={payload['size_try']}")
    for window in payload["trial_windows"]:
        console.print(f"    · {window['begin_ms']}~{window['end_ms']}ms "
                      f"（{window['seconds']}s，取自 [cyan]{window['source']}[/cyan]）")
    if not payload["trial_windows"]:
        console.print("    [dim](该曲目没有试听窗口信息)[/dim]")
    console.print(f"  落盘: {payload['downloaded_bytes']} 字节  时长 {payload['actual_duration']}s")
    console.print()

    verdict = payload["verdict"]
    color = {"success": "green", "trial": "magenta", "failed": "red", "unavailable": "yellow"}.get(verdict, "white")
    console.print(f"[bold]最终判定[/bold]: [{color}]{verdict}[/{color}]  {payload['reason']}")


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
