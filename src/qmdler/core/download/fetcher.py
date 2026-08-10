# SPDX-License-Identifier: GPL-3.0-or-later
"""取链与落盘.

**本项目一律走明文播放链路, 这是有意为之.**

``get_song_urls`` 传普通 ``SongFileType`` 时内部调 ``music.vkey.GetVkey`` /
``UrlGetVkey``, 即播放取流接口 (编码 ``M500`` / ``F000`` / ``Q000`` 这类).
播放链路必须返回播放器能直接解码的流, 所以拿到的是**明文音频**, 可直接写 tag、
直接播放.

传 ``EncryptedSongFileType`` 才走 ``GetEVkey`` / ``CgiGetEVkey`` (编码 ``F0M0`` /
``O8M0`` 这类 M 系), 产物是 ``.mflac`` / ``.mgg`` 加密文件, ``UrlinfoItem.ekey``
也只在那条路上才有值. **本项目不实现任何解密, 不使用 ekey.**

由此推出: 权限判定按**播放**权益而非下载权益, ``pay`` 字段只作提示,
实际能不能拿到以 ``result`` 为准.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
from qqmusic_api import Client, Credential
from qqmusic_api.modules.song import SongFileInfo, SongFileType

from ..models import SongEntry
from .cdn import CdnManager, CdnNode

logger = logging.getLogger(__name__)

#: 链接确实失效 (资源没了), 需要重新取 vkey.
#:
#: ⚠️ **403 不在这里, 而且不要把它加回来.** 详见下面 ``REJECT_STATUSES`` 的说明
#: 与 ``cdn.py`` 的模块 docstring —— 那是一条实测得出、无法从代码推导的结论,
#: 加错一次就会让每首歌多打好几次 ``get_song_urls``.
STALE_LINK_STATUSES = frozenset({404, 410})

#: 节点拒绝服务这条 purl. **不代表链接失效, 也不代表节点故障.**
#:
#: 实测 (2026-08, 12 首不同的歌):
#:
#: * dispatch 的 6 个节点用 keepalive 探针逐个探, 6/6 返回 200 → 排除节点故障;
#: * 同一条 purl 在放行节点上可以稳定下完并逐字节校验通过 → 排除 vkey 过期;
#: * 放行分布在 12 条不同 purl 上高度一致 (sjy6 12/12, 其余 4 个 0/12).
#:
#: 所以遇到 403 应当**换节点重试同一条 purl** (零额外 API 请求),
#: 而不是当成链接失效去重新取 vkey.
REJECT_STATUSES = frozenset({401, 403})

CHUNK_SIZE = 256 * 1024


class LinkExpiredError(RuntimeError):
    """下载途中链接失效, 需要重新取 vkey."""


class CdnRejectedError(RuntimeError):
    """该 CDN 节点不服务这条 purl (403), 或临时不可用 (5xx).

    换一个节点用**同一条** purl 重试即可, 不需要重新取 vkey.
    """

    def __init__(self, message: str, status_code: int = 0) -> None:
        """记录状态码, 便于诊断输出."""
        super().__init__(message)
        self.status_code = status_code


@dataclass(slots=True)
class UrlCandidate:
    """一个档位的取链结果."""

    quality: str
    result: int
    purl: str
    filename: str
    vkey: str

    @property
    def usable(self) -> bool:
        """``result == 0`` 且 ``purl`` 非空."""
        return self.result == 0 and bool(self.purl)


@dataclass(slots=True)
class DownloadOutcome:
    """一次文件下载的结果."""

    path: Path
    bytes_written: int
    content_length: int
    resumed_from: int
    cdn: str


async def fetch_urls(
    client: Client,
    entry: SongEntry,
    qualities: list[str],
    credential: Credential | None = None,
) -> list[UrlCandidate]:
    """为**一首**歌取多个档位的链接.

    这里的「多」指同一首歌的多个音质档位, **不是多首歌**. 任何一次
    ``get_song_urls`` 调用都只包含一首歌 —— 即便接口支持单次 100 个 mid 也不用:
    批量取链既会让 vkey 提前失效, 也是最典型的风控特征.
    """
    if not qualities:
        return []

    file_info = [
        SongFileInfo(
            mid=entry.songmid,
            file_type=SongFileType[quality],
            song_type=entry.song_type,
            media_mid=entry.media_mid or None,
        )
        for quality in qualities
    ]
    response = await client.song.get_song_urls(file_info, credential=credential)

    # 返回顺序与请求顺序一致, 但保险起见按 filename 前缀回填档位.
    candidates: list[UrlCandidate] = []
    for index, info in enumerate(response.data):
        quality = qualities[index] if index < len(qualities) else ""
        candidates.append(
            UrlCandidate(
                quality=quality,
                result=info.result,
                purl=info.purl,
                filename=info.filename,
                vkey=info.vkey,
            ),
        )
    return candidates


@dataclass(slots=True)
class ProbeResult:
    """下载前探测的结果. ``method`` 直接喂给 ``--single`` 的诊断输出."""

    length: int = 0
    #: ``head`` / ``range`` / ``rejected`` / ``failed``
    method: str = "failed"
    status_code: int = 0
    #: 该节点拒绝服务这条 purl, 应当换节点.
    rejected: bool = False

    @property
    def ok(self) -> bool:
        """是否探到了长度."""
        return self.length > 0


async def probe(http: httpx.AsyncClient, url: str) -> ProbeResult:
    """下载前探一下 Content-Length, 并报告走的是哪条分支.

    先试 HEAD; CDN 不支持 HEAD 时退化成 ``Range: bytes=0-0`` 读 ``Content-Range``.

    实测 (2026-08): 放行的节点 HEAD 直接 200 并给出准确 Content-Length,
    走的是 ``head`` 分支; 不放行的节点 HEAD 与 Range 都是 403, 此时返回
    ``rejected=True``, 由调用方换节点 —— 不要当成链接失效去重新取 vkey.
    """
    try:
        response = await http.head(url, follow_redirects=True)
        if response.status_code in REJECT_STATUSES:
            return ProbeResult(method="rejected", status_code=response.status_code, rejected=True)
        if response.status_code < 400:
            length = response.headers.get("content-length")
            if length and length.isdigit() and int(length) > 0:
                return ProbeResult(length=int(length), method="head", status_code=response.status_code)
    except httpx.HTTPError as exc:
        logger.debug("HEAD 探测失败, 改用 Range 探测: %s", exc)

    try:
        response = await http.get(url, headers={"Range": "bytes=0-0"}, follow_redirects=True)
        if response.status_code in REJECT_STATUSES:
            return ProbeResult(method="rejected", status_code=response.status_code, rejected=True)
        content_range = response.headers.get("content-range", "")
        if "/" in content_range:
            total = content_range.rsplit("/", 1)[-1]
            if total.isdigit():
                return ProbeResult(length=int(total), method="range", status_code=response.status_code)
        length = response.headers.get("content-length")
        if response.status_code == 200 and length and length.isdigit():
            return ProbeResult(length=int(length), method="range", status_code=response.status_code)
        return ProbeResult(method="failed", status_code=response.status_code)
    except httpx.HTTPError as exc:
        logger.debug("Range 探测失败: %s", exc)
    return ProbeResult(method="failed")


async def probe_content_length(http: httpx.AsyncClient, url: str) -> int:
    """只要长度的简化入口."""
    return (await probe(http, url)).length


async def download_file(
    http: httpx.AsyncClient,
    url: str,
    target: Path,
    *,
    node: CdnNode,
    cdn: CdnManager,
    expected_size: int = 0,
    on_progress: Callable[[int, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> DownloadOutcome:
    """流式下载到 ``.part`` 临时文件, 支持 HTTP Range 断点续传.

    ``.part`` 与目标文件同目录同名加后缀 —— 跨设备 rename 不是原子操作,
    放到别的临时目录反而不安全.

    Raises:
        LinkExpiredError: 链接失效 (403/404 等), 需要重新取 vkey.
        asyncio.CancelledError: 被取消.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")

    resume_from = part.stat().st_size if part.exists() else 0
    if expected_size and resume_from >= expected_size:
        # 已经下满了, 直接进入校验环节.
        resume_from = 0
        part.unlink(missing_ok=True)

    headers: dict[str, str] = {}
    if resume_from > 0:
        headers["Range"] = f"bytes={resume_from}-"

    written = resume_from
    content_length = 0

    try:
        async with http.stream("GET", url, headers=headers, follow_redirects=True) as response:
            if response.status_code in REJECT_STATUSES:
                # 节点不给这条 purl。换节点即可，别去重新取 vkey。
                cdn.report_rejection(node)
                raise CdnRejectedError(f"CDN 拒绝服务 HTTP {response.status_code}", response.status_code)
            if response.status_code in STALE_LINK_STATUSES:
                raise LinkExpiredError(f"链接失效 HTTP {response.status_code}")
            if response.status_code >= 500:
                # 临时故障，同样换节点重试。
                cdn.report_failure(node)
                raise CdnRejectedError(f"CDN 暂时不可用 HTTP {response.status_code}", response.status_code)
            if response.status_code >= 400:
                cdn.report_failure(node)
                raise httpx.HTTPStatusError(
                    f"HTTP {response.status_code}",
                    request=response.request,
                    response=response,
                )

            if response.status_code == 200 and resume_from > 0:
                # 服务端忽略了 Range, 只能从头来.
                logger.debug("CDN 未接受 Range, 从头下载: %s", url)
                resume_from = 0
                written = 0
                part.unlink(missing_ok=True)

            header_length = response.headers.get("content-length")
            if header_length and header_length.isdigit():
                content_length = int(header_length) + resume_from

            mode = "ab" if resume_from > 0 else "wb"
            with part.open(mode) as handle:
                async for chunk in response.aiter_bytes(CHUNK_SIZE):
                    if cancelled is not None and cancelled():
                        raise asyncio.CancelledError
                    handle.write(chunk)
                    written += len(chunk)
                    if on_progress is not None:
                        on_progress(written, content_length or expected_size)
    except httpx.HTTPError:
        cdn.report_failure(node)
        raise
    else:
        cdn.report_success(node)

    return DownloadOutcome(
        path=part,
        bytes_written=written,
        content_length=content_length,
        resumed_from=resume_from,
        cdn=node.base,
    )


def finalize(part: Path, target: Path) -> Path:
    """校验通过后把 ``.part`` 改名成正式文件."""
    target.parent.mkdir(parents=True, exist_ok=True)
    part.replace(target)
    return target


def cleanup_orphan_parts(roots: list[Path], keep: set[Path] | None = None) -> list[Path]:
    """清理无主的 ``.part`` 文件.

    启动时扫描各任务的目标目录: 数据库里没有对应 ``downloading`` 条目的 ``.part``
    直接删掉, 免得下次续传接到一个上上次的残片上.

    Args:
        roots: 要扫描的根目录.
        keep: 要保留的 ``.part`` 路径 (仍在进行中的条目).

    Returns:
        被删除的文件路径.
    """
    keep = keep or set()
    removed: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.part"):
            if path in keep:
                continue
            try:
                path.unlink()
            except OSError as exc:  # pragma: no cover - 权限问题
                logger.warning("清理孤儿 .part 失败 %s: %s", path, exc)
            else:
                removed.append(path)
    return removed
