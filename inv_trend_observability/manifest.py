"""Run-level reproducibility manifest."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class StrategyRunManifest:
    run_id: str
    code_version: str
    config_fingerprint: str
    data_fingerprint: str
    mode: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    data_manifest_refs: tuple[str, ...] = ()
    metadata: Mapping[str, str] = field(default_factory=dict)

    def write(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        return target
