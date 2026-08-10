# SPDX-License-Identifier: GPL-3.0-or-later
"""歌单来源子系统."""

from .resolver import ResolvedSource, parse_mid_list, resolve
from .service import DEFAULT_LIMIT, QUERY_SONG_MAX_MIDS, SourceResult, SourceService

__all__ = [
    "DEFAULT_LIMIT",
    "QUERY_SONG_MAX_MIDS",
    "ResolvedSource",
    "SourceResult",
    "SourceService",
    "parse_mid_list",
    "resolve",
]
