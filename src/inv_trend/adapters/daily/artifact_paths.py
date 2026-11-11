"""Filename and path sanitisers for artifact directory names.

Single responsibility: turn arbitrary labels into legal, length-bounded path
segments (Windows reserved names included).
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

def _filename_segment(
    value: Any,
    *,
    fallback: str,
    max_length: int = 64,
) -> str:
    raw = str(value or "").strip()
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", raw)
    cleaned = re.sub(r"\s+", "-", cleaned).strip(" .-_") or fallback
    if len(cleaned) <= max_length:
        return cleaned
    suffix = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:10]
    return f"{cleaned[: max_length - len(suffix) - 1]}-{suffix}"


def _path_segment(value: Any, *, fallback: str) -> str:
    segment = _filename_segment(value, fallback=fallback)
    if segment.upper() in {
        "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5",
        "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5",
        "LPT6", "LPT7", "LPT8", "LPT9",
    }:
        return f"_{segment}"
    return segment

__all__ = ["_filename_segment", "_path_segment"]
