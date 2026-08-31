"""Parsing, canonical validation, and JSON normalization for staged D1 payloads."""

from __future__ import annotations

from datetime import datetime
import math
from typing import Any, Mapping

import pandas as pd

from ..daily_models import DataUpdateResult, StrategyScreeningResult, TrendDecisionResult
from .workspace import canonical_payload_hash


def data_update_result(payload: Mapping[str, Any]) -> DataUpdateResult:
    return DataUpdateResult(
        symbol=str(payload["symbol"]),
        instrument_id=str(payload["instrument_id"]),
        timeframe=str(payload.get("timeframe", "D1")),
        update_status=str(payload["update_status"]),
        dataset_version=optional_text(payload.get("dataset_version")),
        latest_complete_d1=optional_text(payload.get("latest_complete_d1")),
        update=mapping(payload.get("update")),
        quality=mapping(payload.get("quality")),
        freshness=mapping(payload.get("freshness")),
        lineage=mapping(payload.get("lineage")),
        data_readiness=str(payload.get("data_readiness", "BLOCKED")),
        blocking_reasons=tuple(str(item) for item in payload.get("blocking_reasons", ())),
        observation_only=bool(payload.get("observation_only", False)),
        schema_version=str(payload.get("schema_version", "1")),
    )


def strategy_screening_result(payload: Mapping[str, Any]) -> StrategyScreeningResult:
    return StrategyScreeningResult(
        symbol=str(payload["symbol"]),
        instrument_id=str(payload["instrument_id"]),
        timeframe=str(payload.get("timeframe", "D1")),
        input_data_hash=str(payload["input_data_hash"]),
        configuration_hash=str(payload["configuration_hash"]),
        dataset_version=optional_text(payload.get("dataset_version")),
        as_of=optional_text(payload.get("as_of")),
        strategy_checks=mapping(payload.get("strategy_checks")),
        conditions=tuple(rows(payload.get("conditions"))),
        rules=tuple(rows(payload.get("rules"))),
        raw_events=tuple(rows(payload.get("raw_events"))),
        turtle_breakouts=tuple(rows(payload.get("turtle_breakouts"))),
        eligibility=mapping(payload.get("eligibility")),
        event_snapshots=tuple(rows(payload.get("event_snapshots"))),
        indicator_analyses=tuple(rows(payload.get("indicator_analyses"))),
        indicator_signal_episodes=tuple(rows(payload.get("indicator_signal_episodes"))),
        commit_evidence=mapping(payload.get("commit_evidence")),
        commit_evidence_hash=optional_text(payload.get("commit_evidence_hash")),
        screening_status=str(payload.get("screening_status", "NOT_RUN")),
        reason=optional_text(payload.get("reason")),
        observation_only=bool(payload.get("observation_only", False)),
        schema_version=str(payload.get("schema_version", "1")),
    )


def trend_decision_result(payload: Mapping[str, Any]) -> TrendDecisionResult:
    return TrendDecisionResult(
        symbol=str(payload["symbol"]),
        instrument_id=str(payload["instrument_id"]),
        timeframe=str(payload.get("timeframe", "D1")),
        input_data_hash=str(payload["input_data_hash"]),
        input_screening_hash=str(payload["input_screening_hash"]),
        as_of=optional_text(payload.get("as_of")),
        trend_direction=str(payload.get("trend_direction", "NEUTRAL")),
        execution_state=str(payload.get("execution_state", "WAIT")),
        decision=str(payload.get("decision", "WAIT")),
        reason_code=str(payload.get("reason_code", "")),
        eligibility_status=str(payload.get("eligibility_status", "UNKNOWN")),
        confidence=mapping(payload.get("confidence")),
        evidence_chain=tuple(rows(payload.get("evidence_chain"))),
        evidence_summary=mapping(payload.get("evidence_summary")),
        long_evidence=tuple(str(item) for item in payload.get("long_evidence", ())),
        short_evidence=tuple(str(item) for item in payload.get("short_evidence", ())),
        reverse_evidence=tuple(str(item) for item in payload.get("reverse_evidence", ())),
        risk_blocks=tuple(str(item) for item in payload.get("risk_blocks", ())),
        event_decisions=tuple(rows(payload.get("event_decisions"))),
        commit_projection=mapping(payload.get("commit_projection")),
        commit_projection_hash=optional_text(payload.get("commit_projection_hash")),
        conclusion=str(payload.get("conclusion", "")),
        observation_only=bool(payload.get("observation_only", False)),
        schema_version=str(payload.get("schema_version", "1")),
    )


def validate_result_hash(payload: Mapping[str, Any], result: Any, stage: str) -> None:
    expected_stage = {
        "data-update": "data_update",
        "strategy-screen": "strategy_screening",
        "trend-decide": "trend_decision",
    }.get(stage, stage.replace("-", "_"))
    if str(payload.get("stage") or "") != expected_stage:
        raise ValueError(f"{stage} payload has an invalid stage identity")
    if str(payload.get("result_id") or "") != result.result_id:
        raise ValueError(f"{stage} result hash/id is invalid or the JSON was modified")
    provided = str(payload.get("result_hash", ""))
    if not provided or provided != result.result_hash:
        raise ValueError(f"{stage} result hash is invalid or the JSON was modified")


def validate_hash_chain(
    data: DataUpdateResult,
    screening: StrategyScreeningResult,
    decision: TrendDecisionResult | None = None,
    *,
    symbol: str,
) -> None:
    identities = {
        (data.symbol, data.instrument_id, data.timeframe),
        (screening.symbol, screening.instrument_id, screening.timeframe),
    }
    if decision is not None:
        identities.add((decision.symbol, decision.instrument_id, decision.timeframe))
    if len(identities) != 1 or data.symbol != symbol:
        raise ValueError(f"staged result identity does not match workspace symbol {symbol}")
    if screening.input_data_hash != data.result_hash:
        raise ValueError(f"strategy-screen input hash does not match data stage for {symbol}")
    if decision is not None and (
        decision.input_data_hash != data.result_hash
        or decision.input_screening_hash != screening.result_hash
    ):
        raise ValueError(f"trend-decision hash chain does not match staged evidence for {symbol}")


def validate_stage_identity(
    *results: Any,
    symbol: str,
    instrument_id: str,
    timeframe: str = "D1",
) -> None:
    """Require staged evidence to match the configured market identity.

    A self-consistent hash chain is not sufficient if an operator copied a
    valid result from a different instrument into the same symbol workspace.
    The configured data-port identity is the final fail-closed boundary before
    feature screening, commit, or publication can consume it.
    """

    for result in results:
        if (
            str(getattr(result, "symbol", "")) != symbol
            or str(getattr(result, "instrument_id", "")) != instrument_id
            or str(getattr(result, "timeframe", "")) != timeframe
        ):
            raise ValueError(
                f"staged result identity does not match configured {symbol}/{instrument_id}/{timeframe}"
            )


def validate_commit_evidence(
    screening: StrategyScreeningResult,
    runtime: Mapping[str, Any],
    symbol: str,
) -> None:
    expected = screening.commit_evidence_hash
    # Frozen Result models expose MappingProxyType values; normalize them
    # back to plain JSON containers before hashing so the digest remains the
    # one created by the screening stage.
    actual = canonical_payload_hash(json_safe(runtime))
    if not expected or expected != actual:
        raise ValueError(
            f"strategy-screen commit evidence hash is invalid for {symbol}; rerun strategy-screen"
        )


def commit_context_payload(
    context: Mapping[str, Any],
    *,
    symbol: str,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the non-temporal context that a screen authorizes for commit.

    The immutable run-context digest protects strategy settings/selection;
    this per-symbol projection additionally binds update/provider metadata
    that affects persisted signal metadata and notification eligibility.
    """

    return {
        "immutable_context_hash": str(context.get("immutable_context_hash") or ""),
        "symbol": symbol,
        "metadata": json_safe(metadata),
    }


def validate_commit_context(
    runtime: Mapping[str, Any],
    context: Mapping[str, Any],
    *,
    symbol: str,
    metadata: Mapping[str, Any],
) -> None:
    expected = commit_context_payload(context, symbol=symbol, metadata=metadata)
    actual = json_safe(mapping(runtime.get("commit_context")))
    if actual != expected:
        raise ValueError(
            f"strategy-screen commit context is invalid for {symbol}; rerun strategy-screen"
        )


def validate_commit_projection(
    decision: TrendDecisionResult,
    context: Mapping[str, Any],
    screening: StrategyScreeningResult,
    *,
    symbol: str,
) -> Mapping[str, Any]:
    """Validate the run-bound plan consumed by the commit-only stage.

    Business result hashes intentionally exclude run ID/time fields.  The
    projection carries those operational values (notably signal
    ``detected_at``), so it has a separate digest and must be bound to both
    this workspace's operational context and the screening evidence it
    materializes.
    """

    projection = mapping(decision.commit_projection)
    expected_hash = str(decision.commit_projection_hash or "")
    actual_hash = canonical_payload_hash(json_safe(projection))
    if not projection or not expected_hash or expected_hash != actual_hash:
        raise ValueError(
            f"trend-decision commit projection hash is invalid for {symbol}; rerun trend-decide"
        )
    if str(projection.get("operational_context_hash") or "") != str(
        context.get("operational_context_hash") or ""
    ):
        raise ValueError(
            f"trend-decision commit projection context is invalid for {symbol}; rerun trend-decide"
        )
    if str(projection.get("input_screening_commit_evidence_hash") or "") != str(
        screening.commit_evidence_hash or ""
    ):
        raise ValueError(
            f"trend-decision commit projection screening evidence is invalid for {symbol}; rerun trend-decide"
        )
    if str(projection.get("decision_result_hash") or "") != decision.result_hash:
        raise ValueError(
            f"trend-decision commit projection decision identity is invalid for {symbol}; rerun trend-decide"
        )
    if not isinstance(projection.get("persistence"), Mapping) or not isinstance(
        projection.get("row_projection"), Mapping
    ):
        raise ValueError(
            f"trend-decision commit projection is incomplete for {symbol}; rerun trend-decide"
        )
    return projection


def mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def rows(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


def json_safe(value: Any) -> Any:
    """Normalize pandas/numpy values before they become staged JSON."""

    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            return json_safe(value.item())
        except (TypeError, ValueError):
            pass
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


__all__ = [
    "data_update_result",
    "commit_context_payload",
    "json_safe",
    "mapping",
    "optional_text",
    "rows",
    "strategy_screening_result",
    "trend_decision_result",
    "validate_commit_evidence",
    "validate_commit_projection",
    "validate_commit_context",
    "validate_hash_chain",
    "validate_result_hash",
    "validate_stage_identity",
]
