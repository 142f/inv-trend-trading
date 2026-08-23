"""Filesystem workspace for independently executed D1 daily stages.

The public run directory is immutable once published.  Stage commands therefore
exchange canonical JSON through a private ``.staging`` sibling and only the
publication stage promotes a completed run through :class:`DailyRunArtifactWriter`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping


_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+$")
_STAGE_FILES = {
    "data_update": "data_update_result.json",
    "strategy_screening": "strategy_screening_result.json",
    "trend_decision": "trend_decision_result.json",
}


@dataclass(frozen=True)
class DailyStagingWorkspace:
    """One mutable staging workspace identified by report date and run ID."""

    artifact_root: Path
    report_date: str
    run_id: str

    def __post_init__(self) -> None:
        _validate_segment(self.report_date, "report_date")
        _validate_segment(self.run_id, "run_id")
        try:
            if date.fromisoformat(self.report_date).isoformat() != self.report_date:
                raise ValueError
        except ValueError as exc:
            raise ValueError("report_date must use YYYY-MM-DD") from exc

    @property
    def root(self) -> Path:
        return self.artifact_root / "runs" / self.report_date / ".staging" / self.run_id

    @property
    def context_path(self) -> Path:
        return self.root / "run_context.json"

    @property
    def commit_receipt_path(self) -> Path:
        return self.root / "commit_receipt.json"

    @property
    def delivery_receipt_path(self) -> Path:
        return self.root / "delivery_receipt.json"

    def initialize(self, context: Mapping[str, Any]) -> dict[str, Any]:
        """Create a workspace or verify that a resumed invocation matches it."""

        self.root.mkdir(parents=True, exist_ok=True)
        if self.context_path.exists():
            existing = self.read_context()
            # A run ID names one immutable business context.  Retrying a
            # stage may complete missing files, but must never silently swap
            # symbols, configuration, or its evaluation instant underneath
            # previously hashed evidence.
            for key in (
                "run_id",
                "report_date",
                "started_at",
                "timezone",
                "timeframe",
                "symbols",
                "configuration",
            ):
                if _canonical_json(existing.get(key)) != _canonical_json(context.get(key)):
                    raise ValueError(
                        f"staging workspace {key} does not match the existing immutable run context"
                    )
            return existing
        payload = dict(context)
        payload["run_id"] = self.run_id
        payload["report_date"] = self.report_date
        payload["immutable_context_hash"] = _immutable_context_hash(payload)
        payload["operational_context_hash"] = _operational_context_hash(payload)
        _atomic_write_json(self.context_path, payload)
        return payload

    def read_context(self) -> dict[str, Any]:
        context = _read_json(self.context_path)
        if str(context.get("run_id") or "") != self.run_id:
            raise ValueError("staging run_context run_id does not match its workspace")
        if str(context.get("report_date") or "") != self.report_date:
            raise ValueError("staging run_context report_date does not match its workspace")
        expected = _immutable_context_hash(context)
        provided = str(context.get("immutable_context_hash") or "")
        if not provided or provided != expected:
            raise ValueError(
                "staging run_context immutable hash is invalid; rerun data-update with a new run_id"
            )
        expected_operational = _operational_context_hash(context)
        provided_operational = str(context.get("operational_context_hash") or "")
        if not provided_operational or provided_operational != expected_operational:
            raise ValueError(
                "staging run_context operational hash is invalid; rerun data-update with a new run_id"
            )
        return context

    def write_context(self, context: Mapping[str, Any]) -> Path:
        payload = dict(context)
        payload["run_id"] = self.run_id
        payload["report_date"] = self.report_date
        payload["immutable_context_hash"] = _immutable_context_hash(payload)
        payload["operational_context_hash"] = _operational_context_hash(payload)
        _atomic_write_json(self.context_path, payload)
        return self.context_path

    def canonical_dir(self, symbol: str) -> Path:
        return self.symbol_root(symbol) / "01_canonical"

    def symbol_root(self, symbol: str) -> Path:
        _validate_segment(symbol, "symbol")
        return self.root / symbol

    def stage_path(self, symbol: str, stage: str) -> Path:
        try:
            filename = _STAGE_FILES[stage]
        except KeyError as exc:
            raise ValueError(f"unsupported daily stage: {stage}") from exc
        return self.canonical_dir(symbol) / filename

    def write_stage(self, symbol: str, stage: str, payload: Mapping[str, Any]) -> Path:
        path = self.stage_path(symbol, stage)
        _atomic_write_json(path, payload)
        return path

    def read_stage(self, symbol: str, stage: str) -> dict[str, Any]:
        return _read_json(self.stage_path(symbol, stage))

    def write_runtime(self, symbol: str, name: str, payload: Mapping[str, Any]) -> Path:
        _validate_segment(name, "runtime name")
        path = self.symbol_root(symbol) / "04_audit" / f"{name}.json"
        _atomic_write_json(path, payload)
        return path

    def read_runtime(self, symbol: str, name: str) -> dict[str, Any]:
        _validate_segment(name, "runtime name")
        return _read_json(self.symbol_root(symbol) / "04_audit" / f"{name}.json")

    def write_commit_receipt(self, payload: Mapping[str, Any]) -> Path:
        _atomic_write_json(self.commit_receipt_path, payload)
        return self.commit_receipt_path

    def read_commit_receipt(self) -> dict[str, Any]:
        return _read_json(self.commit_receipt_path)

    def write_delivery_receipt(self, payload: Mapping[str, Any]) -> Path:
        _atomic_write_json(self.delivery_receipt_path, payload)
        return self.delivery_receipt_path

    def read_delivery_receipt(self) -> dict[str, Any]:
        return _read_json(self.delivery_receipt_path)

    def summary(self, *, stage: str, symbols: list[str]) -> dict[str, Any]:
        return {
            "stage": stage,
            "run_id": self.run_id,
            "report_date": self.report_date,
            "staging_directory": str(self.root),
            "symbols": symbols,
        }


def _validate_segment(value: str, label: str) -> None:
    if not value or not _SAFE_SEGMENT.fullmatch(value):
        raise ValueError(f"{label} must be a non-empty ASCII path segment")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"required staged artifact is missing: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"staged artifact must contain a JSON object: {path}")
    return value


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False, default=str)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def canonical_payload_hash(payload: Mapping[str, Any]) -> str:
    """Hash an auxiliary staged payload with the same stable JSON semantics.

    Runtime evidence is intentionally stored outside a ``*Result`` object so
    DataFrames never leak into the authority files.  A result binds this digest
    before commit consumes the evidence, which keeps the JSON hand-off
    tamper-evident without making the on-disk layout opaque.
    """

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_json(value: Any) -> str:
    """Compare JSON-shaped context values without tuple/list false positives."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )


def _immutable_context_hash(context: Mapping[str, Any]) -> str:
    """Digest stage-affecting run context while excluding run timing/identity.

    This is not a secret signature; it is an audit guard that makes accidental
    or unaudited edits to pinned configuration and selection fail before any
    later stage consumes them.  Per-symbol mutable update metadata is bound to
    the screened runtime evidence separately.
    """

    payload = {
        "schema_version": context.get("schema_version"),
        "timezone": context.get("timezone"),
        "timeframe": context.get("timeframe"),
        "symbols": context.get("symbols"),
        "configuration": context.get("configuration"),
    }
    return canonical_payload_hash(payload)


def _operational_context_hash(context: Mapping[str, Any]) -> str:
    """Bind staging location and evaluation time without changing result hashes.

    Business stage hashes intentionally exclude run identity and wall-clock
    timing.  The staging workspace must nevertheless reject a copied or
    edited context before commit can use those fields for audit timestamps.
    """

    return canonical_payload_hash(
        {
            "run_id": context.get("run_id"),
            "report_date": context.get("report_date"),
            "started_at": context.get("started_at"),
            "immutable_context_hash": context.get("immutable_context_hash"),
        }
    )


__all__ = ["DailyStagingWorkspace", "canonical_payload_hash"]
