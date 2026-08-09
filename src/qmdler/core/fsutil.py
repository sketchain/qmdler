"""文件系统检查."""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PathCheck:
    """一次目录检查的结果."""

    path: str
    exists: bool
    writable: bool
    free_bytes: int
    total_bytes: int
    message: str = ""

    @property
    def ok(self) -> bool:
        """是否可以作为下载根目录使用."""
        return self.exists and self.writable

    def as_dict(self) -> dict[str, object]:
        """转为 REST 出参."""
        return {
            "path": self.path,
            "exists": self.exists,
            "writable": self.writable,
            "free_bytes": self.free_bytes,
            "total_bytes": self.total_bytes,
            "ok": self.ok,
            "message": self.message,
        }


def probe_writable(directory: Path) -> tuple[bool, str]:
    """写一个探针文件验证目录真的可写.

    只看 ``os.access`` 不够: 只读挂载、配额用满、容器里的挂载权限都能让
    ``os.access`` 说可写而实际写不进去. 任务开始前必须真写一次, 不能跑到第 200
    首才发现.
    """
    probe = directory / f".qmdler-probe-{uuid.uuid4().hex[:8]}"
    try:
        probe.write_bytes(b"qmdler")
    except OSError as exc:
        return False, f"目录不可写: {exc}"
    finally:
        probe.unlink(missing_ok=True)
    return True, ""


def check_path(raw: str, *, create: bool = False) -> PathCheck:
    """检查一个下载根目录的存在性、可写性与剩余空间."""
    path = Path(raw).expanduser()
    if create:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return PathCheck(str(path), False, False, 0, 0, f"无法创建目录: {exc}")

    if not path.exists():
        return PathCheck(str(path), False, False, 0, 0, "目录不存在")
    if not path.is_dir():
        return PathCheck(str(path), True, False, 0, 0, "路径不是目录")

    writable, message = probe_writable(path)
    try:
        usage = shutil.disk_usage(path)
        free, total = usage.free, usage.total
    except OSError as exc:  # pragma: no cover
        free, total = 0, 0
        message = message or f"无法读取磁盘信息: {exc}"

    return PathCheck(str(path), True, writable, free, total, message)


def check_space(root: str, needed_bytes: int, headroom: int = 0, *, create: bool = True) -> tuple[bool, str]:
    """下载前检查目标目录与磁盘剩余空间.

    用 ``file.size_*`` 预估总大小, 不足时提前警告.

    Args:
        root: 下载根目录.
        needed_bytes: 预估需要的字节数.
        headroom: 额外要求的余量.
        create: 目录不存在时是否创建. 用户配了这个目录就是打算往里写,
            首次下载时它当然还不存在.
    """
    check = check_path(root, create=create)
    if not check.ok:
        return False, check.message or "目标目录不可用"
    required = needed_bytes + headroom
    if check.free_bytes and check.free_bytes < required:
        return False, (
            f"磁盘剩余 {check.free_bytes / 1e9:.2f} GB，"
            f"预计需要 {needed_bytes / 1e9:.2f} GB（含 {headroom / 1e9:.2f} GB 余量）"
        )
    return True, ""


def list_directories(raw: str, limit: int = 200) -> list[str]:
    """列出子目录, 供 TUI 的目录选择器补全."""
    path = Path(raw).expanduser()
    if not path.exists():
        path = path.parent
    if not path.is_dir():
        return []
    try:
        entries = sorted(
            (str(child) for child in path.iterdir() if child.is_dir() and not child.name.startswith(".")),
        )
    except OSError:
        return []
    return entries[:limit]


def human_bytes(size: float) -> str:
    """人类可读的字节数."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"  # pragma: no cover


def expand(raw: str) -> Path:
    """展开 ``~`` 与环境变量."""
    return Path(os.path.expandvars(raw)).expanduser()
