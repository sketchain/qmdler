# SPDX-License-Identifier: GPL-3.0-or-later
"""附加内容 (歌词 / 封面 / tag) 子系统."""

from . import lyric
from .cover import COVER_SIZES, EMPTY_COVER, CoverFetcher, CoverResult, candidate_sizes
from .service import ExtraTags, LyricBundle, MetadataService
from .tagger import UNSUPPORTED_SUFFIXES, TagPayload, TagWriteError, detect_duration, supports, write_tags

__all__ = [
    "COVER_SIZES",
    "EMPTY_COVER",
    "UNSUPPORTED_SUFFIXES",
    "CoverFetcher",
    "CoverResult",
    "ExtraTags",
    "LyricBundle",
    "MetadataService",
    "TagPayload",
    "TagWriteError",
    "candidate_sizes",
    "detect_duration",
    "lyric",
    "supports",
    "write_tags",
]
