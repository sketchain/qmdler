"""下载引擎子系统."""

from . import errors, fetcher, state, verify
from .cdn import FALLBACK_DOMAIN, CdnManager
from .engine import DownloadEngine, EngineState, new_task_id
from .ratelimit import IntervalWaiter, MetadataLimiter

__all__ = [
    "FALLBACK_DOMAIN",
    "CdnManager",
    "DownloadEngine",
    "EngineState",
    "IntervalWaiter",
    "MetadataLimiter",
    "errors",
    "fetcher",
    "new_task_id",
    "state",
    "verify",
]
