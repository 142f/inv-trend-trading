"""D1 strategy-screening stage and its canonical evidence."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from typing import Any, Iterable, Mapping

import pandas as pd

from inv_trend.core.strategy.daily import (
    PreparedDailyAnalysis,
    analyze_prepared_daily_analysis,
    prepare_daily_analysis,
)

from ..daily_analysis import (
    build_breakout_assessments,
    build_instrument_report_bundle,
)
from ..daily_models import DataUpdateResult, StrategyScreeningResult
from ..strategy_config import DailyChecksConfig, TrendDecisionConfig
from .data_update import DataUpdateStage
from .ports import EligibilityPort
from .stage_payloads import json_safe


@dataclass(frozen=True)
class ExecutionContext:
    """Explicit opt-in dependencies needed by the optional eligibility gate."""

    checker: EligibilityPort
    account: Any = None
    backtest_validated: bool = False
    execution_ready: bool = False
    require_account: bool = False

    def __post_init__(self) -> None:
        if not callable(getattr(self.checker, "evaluate", None)):
            raise ValueError("execution_context.checker must implement EligibilityPort")


@dataclass(frozen=True)
class StrategyScreeningStage:
    """Canonical screening result plus short-lived in-process replay evidence."""

    result: StrategyScreeningResult
    prepared: PreparedDailyAnalysis | None
    analysis: Mapping[str, Any]
    replay_analyses: Mapping[int, Mapping[str, Any]]


class StrategyScreeningService:
    """Prepare D1 features once and expose complete strategy evidence."""

    def __init__(
        self,
        daily_checks_config: DailyChecksConfig,
        trend_decision_config: TrendDecisionConfig | None = None,
    ) -> None:
        self.daily_checks_config = daily_checks_config
        self.trend_decision_config = trend_decision_config or TrendDecisionConfig()
        self.configuration_hash = _hash_payload(
            {
                "daily_checks": asdict(self.daily_checks_config),
                "trend_decision": asdict(self.trend_decision_config),
            }
        )

    def screen(
        self,
        data: DataUpdateStage | DataUpdateResult,
        asset: Any,
        *,
        bars: pd.DataFrame | None = None,
        session_anchor: pd.Timestamp | str | None = None,
        replay_positions: Iterable[int] | None = None,
        execution_context: ExecutionContext | Mapping[str, Any] | None = None,
    ) -> StrategyScreeningStage:
        data_result, stage_bars = _unpack_data_stage(data, bars)
        if stage_bars is None or stage_bars.empty:
            result = _not_run_screening(
                data_result,
                self.configuration_hash,
                reason="没有可读取的完整 D1 K 线，策略筛选未执行。",
            )
            return StrategyScreeningStage(result, None, {}, {})

        anchor = session_anchor or pd.Timestamp(stage_bars["timestamp"].iloc[0])
        prepared = prepare_daily_analysis(
            stage_bars, self.daily_checks_config, session_anchor=anchor
        )
        latest = analyze_prepared_daily_analysis(prepared)
        positions = (
            tuple(replay_positions)
            if replay_positions is not None
            else (len(prepared.base) - 1,)
        )
        event_snapshots: list[Mapping[str, Any]] = []
        replay_analyses: dict[int, Mapping[str, Any]] = {}
        context = _coerce_execution_context(execution_context)
        for position in positions:
            if position < 0 or position >= len(prepared.base):
                raise IndexError("replay position is outside the prepared D1 frame")
            replay = analyze_prepared_daily_analysis(prepared, position=position)
            replay_analyses[position] = replay
            candidates = _turtle_candidates(replay)
            if not candidates:
                continue
            eligibility = self._evaluate_eligibility(
                candidates,
                replay,
                prepared,
                position,
                asset,
                context,
            )
            event_snapshots.append(
                _screening_snapshot(replay, candidates, eligibility, position)
            )

        latest_candidates = _turtle_candidates(latest)
        latest_eligibility = self._evaluate_eligibility(
            latest_candidates,
            latest,
            prepared,
            len(prepared.base) - 1,
            asset,
            context,
        )
        result = StrategyScreeningResult(
            symbol=asset.symbol,
            instrument_id=data_result.instrument_id,
            timeframe="D1",
            input_data_hash=data_result.result_hash,
            configuration_hash=self.configuration_hash,
            dataset_version=data_result.dataset_version,
            as_of=_as_of(latest),
            strategy_checks=_mapping(latest.get("strategy_checks")),
            conditions=_conditions(latest),
            rules=tuple(_mapping(item) for item in latest.get("rule_evaluations", [])),
            raw_events=tuple(_mapping(item) for item in latest.get("signals", [])),
            turtle_breakouts=latest_candidates,
            eligibility=latest_eligibility,
            event_snapshots=tuple(event_snapshots),
            screening_status="OBSERVATION_ONLY" if data_result.observation_only else "READY",
            observation_only=data_result.observation_only,
        )
        return StrategyScreeningStage(result, prepared, latest, replay_analyses)

    # Explicit name for a stage's public operation.
    run = screen

    def _evaluate_eligibility(
        self,
        candidates: tuple[Mapping[str, Any], ...],
        analysis: Mapping[str, Any],
        prepared: PreparedDailyAnalysis,
        position: int,
        asset: Any,
        context: ExecutionContext | None,
    ) -> Mapping[str, Any]:
        if not candidates:
            return {
                "status": "NOT_APPLICABLE",
                "evaluated": False,
                "passed": None,
                "reason": "本时点没有海龟价格突破。",
                "results": [],
            }
        if not self.trend_decision_config.eligibility_gate_enabled:
            return {
                "status": "NOT_EVALUATED",
                "evaluated": False,
                "passed": None,
                "reason": "资格闸门未在 trend_decision 配置中启用。",
                "results": [],
            }
        if context is None:
            return {
                "status": "NOT_EVALUATED",
                "evaluated": False,
                "passed": None,
                "reason": "未注入执行上下文，日报不运行 TradeEligibilityChecker。",
                "results": [],
            }
        results: list[dict[str, Any]] = []
        for candidate in candidates:
            signal = _eligibility_signal(candidate, analysis, prepared, position, asset)
            verdict = context.checker.evaluate(
                signal=signal,
                bars=prepared.base.iloc[: position + 1].copy(),
                asset=asset,
                account=context.account,
                backtest_validated=context.backtest_validated,
                execution_ready=context.execution_ready,
                require_account=context.require_account,
            )
            item = verdict.to_dict()
            item["candidate_id"] = candidate["candidate_id"]
            item["direction"] = candidate["direction"]
            results.append(item)
        passed = bool(results) and all(bool(item.get("trade_eligible")) for item in results)
        return {
            "status": "PASSED" if passed else "BLOCKED",
            "evaluated": True,
            "passed": passed,
            "reason": "资格校验已执行。",
            "results": results,
        }


class StrategyScreeningEvidenceBuilder:
    """Serialize precomputed screen evidence without recalculating features.

    The builder belongs to the screening stage because it only consumes the
    single prepared D1 analysis created there.  Commit later verifies its
    digest before using replay evidence for legacy signal persistence.
    """

    @staticmethod
    def build(
        *,
        asset: Any,
        data_result: DataUpdateResult,
        stage: StrategyScreeningStage,
        positions: tuple[int, ...],
        cursor_before: str | None,
        chart_bars: int,
        session_anchor: str,
    ) -> dict[str, Any]:
        prepared = stage.prepared
        if prepared is None:
            return {
                "analysis": {},
                "positions": [],
                "replay_analyses": {},
                "report_bundle_seed": {},
            }
        base_assessments: dict[str, list[dict[str, Any]]] = {}
        for position in positions:
            replay = _mapping(stage.replay_analyses.get(position))
            base_assessments[str(position)] = [
                item.to_dict()
                for item in build_breakout_assessments(prepared, replay, position=position)
            ]
        bundle = build_instrument_report_bundle(
            prepared,
            symbol=asset.symbol,
            instrument_id=data_result.instrument_id,
            # A stage hash must identify business evidence, not the wall clock
            # at which an operator invoked the stage.  The D1 as-of timestamp
            # is stable for this pinned dataset; actual run timing lives in
            # the manifest/receipt instead.
            generated_at=data_result.latest_complete_d1 or "",
            analysis=stage.analysis,
            signals=(),
            chart_bars=chart_bars,
        )
        return json_safe(
            {
                "analysis": stage.analysis,
                "positions": list(positions),
                "replay_analyses": {
                    str(key): value for key, value in stage.replay_analyses.items()
                },
                "base_breakout_assessments": base_assessments,
                "report_bundle_seed": bundle.to_dict(),
                "cursor_before": cursor_before,
                "latest_time": prepared.base.index[-1].isoformat(),
                # Do not replace a persisted anchor with the first current
                # bar: higher-session aggregation must use the value that was
                # read before the screen stage began.
                "session_anchor": session_anchor,
            }
        )


def _not_run_screening(
    data: DataUpdateResult, configuration_hash: str, *, reason: str
) -> StrategyScreeningResult:
    return StrategyScreeningResult(
        symbol=data.symbol,
        instrument_id=data.instrument_id,
        timeframe=data.timeframe,
        input_data_hash=data.result_hash,
        configuration_hash=configuration_hash,
        dataset_version=data.dataset_version,
        as_of=data.latest_complete_d1,
        screening_status="NOT_RUN",
        reason=reason,
        observation_only=data.observation_only,
    )


def _unpack_data_stage(
    data: DataUpdateStage | DataUpdateResult,
    bars: pd.DataFrame | None,
) -> tuple[DataUpdateResult, pd.DataFrame | None]:
    if isinstance(data, DataUpdateStage):
        return data.result, data.bars if bars is None else bars
    return data, bars


def _screening_snapshot(
    analysis: Mapping[str, Any],
    candidates: tuple[Mapping[str, Any], ...],
    eligibility: Mapping[str, Any],
    position: int,
) -> dict[str, Any]:
    latest_bar = _mapping(analysis.get("latest_bar"))
    # This small latest-bar projection is canonical evidence, not runtime
    # convenience data: a later commit can construct event signals without
    # trusting the mutable replay-analysis sidecar.
    return {
        "position": position,
        "as_of": _as_of(analysis),
        "latest_bar": {
            "timestamp": latest_bar.get("timestamp"),
            "close": _finite(latest_bar.get("close")),
        },
        "strategy_checks": _mapping(analysis.get("strategy_checks")),
        "conditions": list(_conditions(analysis)),
        "rules": [_mapping(item) for item in analysis.get("rule_evaluations", [])],
        "raw_events": [_mapping(item) for item in analysis.get("signals", [])],
        "turtle_breakouts": [_mapping(item) for item in candidates],
        "eligibility": _mapping(eligibility),
    }


def _turtle_candidates(analysis: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    latest = _mapping(analysis.get("latest_bar"))
    candidates: list[dict[str, Any]] = []
    for raw in analysis.get("signals", []):
        event = _mapping(raw)
        indicator = str(event.get("indicator") or "")
        direction = str(event.get("direction") or "")
        if indicator not in {"turtle_20", "turtle_55"} or direction not in {"long", "short"}:
            continue
        if str(event.get("event") or "") not in {"breakout_up", "breakout_down"}:
            continue
        timestamp = str(event.get("signal_time") or latest.get("timestamp") or "")
        period = int(indicator.removeprefix("turtle_"))
        candidates.append(
            {
                "candidate_id": _stable_hash("turtle", indicator, timestamp, direction),
                "indicator": indicator,
                "period": period,
                "event": event.get("event"),
                "direction": direction,
                "timestamp": timestamp,
                "trigger_price": _finite(event.get("trigger_price", latest.get("close"))),
                "breakout_level": _finite(event.get("breakout_level")),
                "parameters": _mapping(event.get("parameters")),
            }
        )
    return tuple(candidates)


def _conditions(analysis: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    values: list[Mapping[str, Any]] = []
    for rule in analysis.get("rule_evaluations", []):
        payload = _mapping(rule)
        for condition in payload.get("conditions", []):
            values.append(_mapping(condition))
    return tuple(values)


def _eligibility_signal(
    candidate: Mapping[str, Any],
    analysis: Mapping[str, Any],
    prepared: PreparedDailyAnalysis,
    position: int,
    asset: Any,
) -> Mapping[str, Any]:
    """Build a port-level, JSON-shaped eligibility request.

    The application stage deliberately does not construct detector adapter
    models.  Production ``TradeEligibilityChecker`` accepts this normalized
    request through its adapter boundary, while test/alternative ports can
    consume the same simple payload without importing detector classes.
    """

    row = prepared.base.iloc[position]
    period = int(candidate["period"])
    close = _finite(analysis.get("latest_bar", {}).get("close")) or _finite(row.get("close")) or 0.0
    atr = _finite(row.get("atr")) or 0.0
    high = _finite(row.get(f"channel_high_{period}"))
    low = _finite(row.get(f"channel_low_{period}"))
    market = getattr(getattr(asset, "market", None), "value", getattr(asset, "market", ""))
    return {
        "symbol": str(asset.symbol),
        "instrument": str(asset.instrument),
        "market": str(market),
        "timeframe": "D1",
        "signal_type": "SYSTEM1_BREAKOUT" if period == 20 else "SYSTEM2_BREAKOUT",
        "raw_signal_type": "SYSTEM1_BREAKOUT" if period == 20 else "SYSTEM2_BREAKOUT",
        "direction": "long" if candidate["direction"] == "long" else "short",
        "signal_time": str(candidate["timestamp"]),
        "trigger_price": close,
        "channel_high": high,
        "channel_low": low,
        "atr": atr,
        "atr_pct": _finite(row.get("atr_pct")) or 0.0,
        "stop_price": None,
        "next_add_price": None,
        "distance_to_breakout_atr": 0.0,
        "volatility_percentile": _finite(row.get("atr_percentile")) or 0.0,
        "suggested_risk_unit": float(getattr(asset, "risk_unit_pct", 0.0)),
        "trend_status": "daily_screening",
        "confirmation_status": "close_confirmed",
        "data_source": str(getattr(asset, "data_source", "")),
        "generated_at": str(candidate["timestamp"]),
        "tradeable": True,
    }


def _coerce_execution_context(
    value: ExecutionContext | Mapping[str, Any] | None,
) -> ExecutionContext | None:
    if value is None or isinstance(value, ExecutionContext):
        return value
    checker = value.get("checker")
    if not callable(getattr(checker, "evaluate", None)):
        raise ValueError("execution_context.checker must implement EligibilityPort")
    return ExecutionContext(
        checker=checker,
        account=value.get("account"),
        backtest_validated=bool(value.get("backtest_validated", False)),
        execution_ready=bool(value.get("execution_ready", False)),
        require_account=bool(value.get("require_account", False)),
    )


def _as_of(analysis: Mapping[str, Any]) -> str | None:
    latest = _mapping(analysis.get("latest_bar"))
    return str(latest.get("timestamp") or "") or None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _hash_payload(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _stable_hash(*parts: Any) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode("utf-8")).hexdigest()[:24]


__all__ = [
    "ExecutionContext",
    "StrategyScreeningResult",
    "StrategyScreeningService",
    "StrategyScreeningStage",
    "StrategyScreeningEvidenceBuilder",
]
