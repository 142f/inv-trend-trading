"""Read published JSON artifacts back into the compatibility shape.

Both readers used to branch on a "storage_version": 3 marker and rebuild frames
from the v3 store.  Nothing in the repository ever writes that marker (the v3
publishers write SQLite documents instead of these JSON files), so the branches
were unreachable, and the daily one would have raised FileNotFoundError because
the referenced paths never exist on disk.
"""
from __future__ import annotations

import json
from pathlib import Path


def read_backtest(path):
    """Return the published backtest report exactly as written."""

    return json.loads(Path(path).resolve().read_text(encoding="utf-8"))


def read_daily(path):
    """Return the published daily compatibility snapshot as written."""

    return json.loads(Path(path).read_text(encoding="utf-8"))
