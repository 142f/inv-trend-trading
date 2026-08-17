from __future__ import annotations

import json
from pathlib import Path

from inv_trend.core.events import DecisionEvent
from inv_trend.core.serializers import serialize_event


class JsonlAuditSink:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def emit(self, event: DecisionEvent) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(serialize_event(event), ensure_ascii=False, allow_nan=False) + "\n")
