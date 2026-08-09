"""文件名与目录模板子系统."""

from .sanitize import (
    DEFAULT_MAX_BYTES,
    ILLEGAL_MAP,
    RESERVED_NAMES,
    dedupe_path,
    sanitize_relative_path,
    sanitize_segment,
    truncate_bytes,
)
from .template import (
    PLACEHOLDERS,
    PRESET_COMBOS,
    PRESET_DIR_TEMPLATES,
    PRESET_FILE_TEMPLATES,
    RenderContext,
    TemplateRenderer,
    unknown_placeholders,
)

__all__ = [
    "DEFAULT_MAX_BYTES",
    "ILLEGAL_MAP",
    "PLACEHOLDERS",
    "PRESET_COMBOS",
    "PRESET_DIR_TEMPLATES",
    "PRESET_FILE_TEMPLATES",
    "RESERVED_NAMES",
    "RenderContext",
    "TemplateRenderer",
    "dedupe_path",
    "sanitize_relative_path",
    "sanitize_segment",
    "truncate_bytes",
    "unknown_placeholders",
]
