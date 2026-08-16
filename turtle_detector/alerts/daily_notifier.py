"""Notification boundary for persisted daily SignalEvent records."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from inv_trend_core.signals import SignalEvent


class SignalNotifier(Protocol):
    def notify(self, signal: SignalEvent) -> None: ...


class LogNotifier:
    """Append one structured record per signal ID, safely retrying pending delivery."""

    def __init__(self, root: str | Path = "logs/signals") -> None:
        self.root = Path(root)

    def notify(self, signal: SignalEvent) -> None:
        day = signal.detected_at[:10]
        path = self.root / f"{day}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    if json.loads(line).get("signal_id") == signal.signal_id:
                        return
                except json.JSONDecodeError:
                    continue
        payload = {"timestamp": signal.detected_at, "status": "NEW", **signal.to_dict()}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n")


__all__ = ["LogNotifier", "SignalNotifier"]
