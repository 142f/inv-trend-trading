"""The three one-way stages used by the D1 daily market scan.

The module is intentionally free of report rendering and persistence.  It
keeps data readiness, strategy evidence and final decisions separate, making
the hash chain useful both for a live daily run and a historical replay.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import hashlib
import json
import math
from typing import Any, Iterable, Mapping

import pandas as pd

from inv_trend.adapters.detector.eligibility.checker import (
    AccountSnapshot,
    TradeEligibilityChecker,
)
from inv_trend.adapters.detector.freshness import FreshnessPolicy
from inv_trend.adapters.detector.models import (
    AssetConfig,
    ConfirmationStatus,
    Direction,
    SignalType,
    TurtleSignal,
)
from inv_trend.data import HistoricalDataService
from inv_trend.data.lineage import load_current_lineage

from .daily_analysis import (
    PreparedDailyAnalysis,
    analyze_prepared_daily_analysis,
    prepare_daily_analysis,
)
from .daily_models import (
    DataUpdateResult,
    StrategyScreeningResult,
    TrendDecisionResult,
)
from .strategy_config import DailyChecksConfig, TrendDecisionConfig


@dataclass(frozen=True)
class ExecutionContext:
    """Explicit opt-in dependencies needed by the optional eligibility gate."""

    checker: TradeEligibilityChecker
    account: AccountSnapshot | None = None
    backtest_validated: bool = False
    execution_ready: bool = False
    require_account: bool = False


@dataclass(frozen=True)
class DataUpdateStage:
    result: DataUpdateResult
    bars: pd.DataFrame | None
    update: Mapping[str, Any]
    refresh_error: Exception | None = None

    @property
    def readable(self) -> bool:
        return self.bars is not None and not self.bars.empty


@dataclass(frozen=True)
class StrategyScreeningStage:
    result: StrategyScreeningResult
    prepared: PreparedDailyAnalysis | None
    analysis: Mapping[str, Any]
    replay_analyses: Mapping[int, Mapping[str, Any]]


class DailyDataUpdateService:
    """Refresh/read D1 data and produce the formal readiness gate."""

    def __init__(
        self,
        historical_service: HistoricalDataService,
        *,
        freshness_policy: FreshnessPolicy | None = None,
        provider_retries: int = 1,
        refresh_data: bool = True,
    ) -> None:
        self.historical_service = historical_service
        self.freshness_policy = freshness_policy or FreshnessPolicy()
        self.provider_retries = provider_retries
        self.refresh_data = refresh_data

    def run(
        self,
        asset: AssetConfig,
        *,
        started_at: datetime,
        bootstrap_days: int,
        research_mode: bool = False,
    ) -> DataUpdateStage:
        instrument = self.historical_service.instrument(asset.symbol)
        before = self.historical_service.lake.current_version(asset.symbol, "D1")
        update: dict[str, Any]
        refresh_error: Exception | None = None
        manifest: Any = None
        try:
            if not self.refresh_data:
                if before is None:
                    raise FileNotFoundError(f"no current dataset for {asset.symbol}/D1")
                update = {
                    "status": "unchanged",
                    "mode": "scan_only",
                    "dataset_version": str(before["version"]),
                    "refresh_failed": False,
                    "skipped": True,
                }
            else:
                manifest = self._refresh(asset.symbol, started_at, bootstrap_days)
                after = self.historical_service.lake.current_version(asset.symbol, "D1")
                if not manifest.quality_passed or after is None:
                    raise RuntimeError(
                        "refresh not published: "
                        f"status={manifest.quality_status}, score={manifest.quality_score}"
                    )
                operation = (
                    "updated"
                    if before is None or before.get("version") != after.get("version")
                    else "unchanged"
                )
                update = {
                    "status": operation,
                    "mode": "bootstrap" if before is None else "incremental",
                    "provider": manifest.data_source,
                    "dataset_version": str(after["version"]),
                    "quality_status": manifest.quality_status,
                    "quality_score": manifest.quality_score,
                    "row_count": manifest.row_count,
                    "refresh_failed": False,
                }
        except Exception as exc:  # a preserved current version may still be usable
            refresh_error = exc
            update = {
                "status": "failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "refresh_failed": True,
                "previous_current_preserved": before is not None,
            }

        bars: pd.DataFrame | None = None
        load_error: Exception | None = None
        try:
            bars = self.historical_service.load_bars(
                asset.symbol, "D1", completed_only=True, allow_research=True
            )
        except Exception as exc:
            load_error = exc

        metadata = _data_metadata(bars)
        freshness: dict[str, Any]
        if bars is not None:
            freshness = self.freshness_policy.evaluate(
                instrument, metadata["latest_complete_bar"], now=started_at
            ).to_dict()
        else:
            freshness = {
                "status": "UNAVAILABLE",
                "expected_date": self.freshness_policy.expected_date(
                    instrument, now=started_at
                ).isoformat(),
                "actual_date": None,
                "reason": "no_readable_completed_d1",
            }

        lineage: dict[str, Any] = {"verified": False}
        lineage_error: Exception | None = None
        if bars is not None:
            try:
                # ``load_bars`` verifies this too.  Calling it here preserves a
                # standalone audit summary for the stage payload.
                current_bars = self.historical_service.lake.read_bars(asset.symbol, "D1")
                verified = load_current_lineage(
                    self.historical_service.lake,
                    asset.symbol,
                    "D1",
                    bars=current_bars,
                )
                manifest_payload = verified.dataset_manifest
                lineage = {
                    "verified": True,
                    "dataset_version": verified.dataset_version,
                    "publication_run_id": manifest_payload.get("publication_run_id"),
                    "curated_sha256": manifest_payload.get("curated_sha256"),
                    "quality_report_sha256": manifest_payload.get("quality_report_sha256"),
                    "quality_status": verified.quality_report.get("quality_status"),
                    "quality_score": verified.quality_report.get("quality_score"),
                }
            except Exception as exc:
                lineage_error = exc
                lineage = {
                    "verified": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }

        quality = {
            "statuses": metadata["quality_statuses"],
            "formal_status": "CURATED",
            "passed": metadata["quality_statuses"] == ["CURATED"],
            "manifest_status": getattr(manifest, "quality_status", None),
            "manifest_score": getattr(manifest, "quality_score", None),
        }
        blocking_reasons: list[str] = []
        if bars is None or bars.empty:
            blocking_reasons.append("no_readable_completed_d1")
        if not quality["passed"]:
            blocking_reasons.append("quality_not_curated")
        if freshness.get("status") != "FRESH":
            blocking_reasons.append("stale_data")
        if not lineage.get("verified"):
            blocking_reasons.append("lineage_verification_failed")
        if load_error is not None and bars is None:
            blocking_reasons.append("data_load_failed")
        # A refresh failure does not invalidate an independently verified,
        # current curated version.  It remains visible in ``update_status`` so
        # the orchestrator can preserve its historical non-zero run status.
        if refresh_error is not None and before is None and bars is None:
            blocking_reasons.append("data_update_failed")

        result = DataUpdateResult(
            symbol=asset.symbol,
            instrument_id=instrument.instrument_id,
            timeframe="D1",
            update_status=str(update["status"]),
            dataset_version=metadata["dataset_version"],
            latest_complete_d1=metadata["latest_complete_bar"],
            quality=quality,
            freshness=freshness,
            lineage=lineage,
            data_readiness="READY" if not blocking_reasons else "BLOCKED",
            blocking_reasons=tuple(dict.fromkeys(blocking_reasons)),
            observation_only=research_mode,
        )
        return DataUpdateStage(result, bars, update, refresh_error or load_error or lineage_error)

    # A semantic alias is convenient for callers that use the stage directly.
    update = run

    def _refresh(self, symbol: str, now: datetime, bootstrap_days: int) -> Any:
        daily_end = _daily_refresh_end(now)
        if self.historical_service.lake.current_version(symbol, "D1") is not None:
            return self.historical_service.update(
                symbol, "D1", end=daily_end, retries=self.provider_retries
            )
        instrument = self.historical_service.instrument(symbol)
        start = daily_end - timedelta(days=bootstrap_days)
        if instrument.earliest_valid_date:
            start = max(start, _earliest_utc(instrument.earliest_valid_date))
        return self.historical_service.ingest(
            symbol, "D1", start, daily_end, retries=self.provider_retries
        )


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
        asset: AssetConfig,
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
        positions = tuple(replay_positions) if replay_positions is not None else (len(prepared.base) - 1,)
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
        asset: AssetConfig,
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


class TrendDecisionService:
    """Derive a compatibility decision from screening evidence only.

    It purposely accepts mappings/results rather than a price frame.  Adding a
    K-line or indicator dependency here would break the architecture boundary.
    """

    def __init__(self, config: TrendDecisionConfig | None = None) -> None:
        self.config = config or TrendDecisionConfig()

    def decide(
        self,
        data: DataUpdateResult,
        screening: StrategyScreeningResult,
        *,
        event_snapshot: Mapping[str, Any] | None = None,
    ) -> TrendDecisionResult:
        evidence = event_snapshot or {
            "as_of": screening.as_of,
            "strategy_checks": screening.strategy_checks,
            "turtle_breakouts": screening.turtle_breakouts,
            "eligibility": screening.eligibility,
        }
        checks = _mapping(evidence.get("strategy_checks"))
        rating = _mapping(checks.get("rating"))
        grade = str(rating.get("grade") or "NONE")
        raw_direction = str(rating.get("direction") or "")
        family_votes = _mapping(rating.get("family_votes"))
        family_conflict = raw_direction == "conflict" or any(
            value == "conflict" for value in family_votes.values()
        )
        has_direction = raw_direction in {"long", "short"} and not family_conflict
        trend_direction = (
            raw_direction.upper() if has_direction and grade in {"A", "B"} else "NEUTRAL"
        )
        candidates = tuple(_mapping(item) for item in evidence.get("turtle_breakouts", ()))
        same_direction = tuple(
            item for item in candidates if str(item.get("direction")) == raw_direction
        )
        eligibility = _mapping(evidence.get("eligibility"))
        risk_blocks = _eligibility_blocks(eligibility, raw_direction)
        long_evidence, short_evidence, reverse_evidence = _directional_evidence(
            checks, candidates, raw_direction
        )
        confidence = _confidence_evidence(rating, checks)

        observation_only = bool(data.observation_only or screening.observation_only)
        if observation_only:
            decision, execution_state = "WAIT", "WAIT"
            conclusion = "研究模式仅生成观察性结论，不可执行、不推进 cursor，也不会进入通知队列。"
        elif not data.formal_ready:
            decision, execution_state = "WAIT", "WAIT"
            conclusion = "数据就绪闸门未通过，停止正式策略筛选与信号持久化，等待数据恢复。"
            trend_direction = "NEUTRAL"
        elif screening.screening_status != "READY":
            decision, execution_state = "WAIT", "WAIT"
            conclusion = "策略筛选结果不可用，等待完整 D1 筛选证据。"
            trend_direction = "NEUTRAL"
        elif not has_direction or grade in {"C", "CONFLICT", "NONE"}:
            decision, execution_state = "NEUTRAL", "NOT_APPLICABLE"
            conclusion = "策略评级没有形成可执行的同向趋势，维持中性观察。"
            trend_direction = "NEUTRAL"
        elif grade == "B":
            decision, execution_state = "WAIT", "WAIT"
            conclusion = "已形成 B 级同向趋势，但 B 级仅用于等待确认，不形成执行信号。"
        elif not same_direction:
            decision, execution_state = "WAIT", "WAIT"
            conclusion = "已形成 A 级同向趋势，但当日没有同向海龟价格突破，继续等待。"
        elif risk_blocks:
            decision, execution_state = "WAIT", "WAIT"
            conclusion = "同向 A 级海龟突破已出现，但资格校验存在阻断，暂不执行。"
        else:
            decision = trend_direction
            execution_state = "ENTER_LONG" if trend_direction == "LONG" else "ENTER_SHORT"
            side = "多头" if trend_direction == "LONG" else "空头"
            conclusion = f"同向 A 级{side}评级与当日海龟价格突破一致，可形成{execution_state}。"

        return TrendDecisionResult(
            symbol=screening.symbol,
            instrument_id=screening.instrument_id,
            timeframe=screening.timeframe,
            input_data_hash=data.result_hash,
            input_screening_hash=screening.result_hash,
            as_of=str(evidence.get("as_of") or screening.as_of or "") or None,
            trend_direction=trend_direction,
            execution_state=execution_state,
            decision=decision,
            confidence=confidence,
            long_evidence=long_evidence,
            short_evidence=short_evidence,
            reverse_evidence=reverse_evidence,
            risk_blocks=risk_blocks,
            conclusion=conclusion,
            observation_only=observation_only,
        )

    run = decide


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
    data: DataUpdateStage | DataUpdateResult, bars: pd.DataFrame | None
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
    return {
        "position": position,
        "as_of": _as_of(analysis),
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
        candidate = {
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
        candidates.append(candidate)
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
    asset: AssetConfig,
) -> TurtleSignal:
    row = prepared.base.iloc[position]
    period = int(candidate["period"])
    direction = Direction.LONG if candidate["direction"] == "long" else Direction.SHORT
    signal_type = SignalType.SYSTEM1_BREAKOUT if period == 20 else SignalType.SYSTEM2_BREAKOUT
    close = _finite(analysis.get("latest_bar", {}).get("close")) or _finite(row.get("close")) or 0.0
    atr = _finite(row.get("atr")) or 0.0
    high = _finite(row.get(f"channel_high_{period}"))
    low = _finite(row.get(f"channel_low_{period}"))
    return TurtleSignal(
        symbol=asset.symbol,
        instrument=asset.instrument,
        market=asset.market.value,
        timeframe="D1",
        signal_type=signal_type,
        raw_signal_type=signal_type,
        direction=direction,
        signal_time=str(candidate["timestamp"]),
        trigger_price=close,
        channel_high=high,
        channel_low=low,
        atr=atr,
        atr_pct=_finite(row.get("atr_pct")) or 0.0,
        stop_price=None,
        next_add_price=None,
        distance_to_breakout_atr=0.0,
        volatility_percentile=_finite(row.get("atr_percentile")) or 0.0,
        suggested_risk_unit=asset.risk_unit_pct,
        trend_status="daily_screening",
        confirmation_status=ConfirmationStatus.CLOSE_CONFIRMED,
        data_source=asset.data_source,
        generated_at=str(candidate["timestamp"]),
        tradeable=True,
    )


def _directional_evidence(
    checks: Mapping[str, Any], candidates: tuple[Mapping[str, Any], ...], direction: str
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    long_items: list[str] = []
    short_items: list[str] = []
    reverse: list[str] = []
    labels = {
        "sma_alignment": "SMA 排列",
        "ema_trend": "EMA 趋势",
        "macd_summary": "MACD 多周期",
        "trend_quality": "ADX/DMI",
    }
    for key, label in labels.items():
        item = _mapping(checks.get(key))
        state = item.get("direction")
        if state == "long":
            long_items.append(f"{label}为多头")
        elif state == "short":
            short_items.append(f"{label}为空头")
        elif state == "conflict":
            reverse.append(f"{label}方向冲突")
        elif state is None:
            reverse.append(f"{label}不可用")
    for candidate in candidates:
        item = f"海龟 {candidate.get('period')} 日{('向上' if candidate.get('direction') == 'long' else '向下')}价格突破"
        if candidate.get("direction") == "long":
            long_items.append(item)
        else:
            short_items.append(item)
    if direction == "long" and short_items:
        reverse.extend(short_items)
    if direction == "short" and long_items:
        reverse.extend(long_items)
    return tuple(long_items), tuple(short_items), tuple(dict.fromkeys(reverse))


def _confidence_evidence(rating: Mapping[str, Any], checks: Mapping[str, Any]) -> dict[str, Any]:
    quality = {
        "adx_dmi": _mapping(checks.get("trend_quality")),
        "atr": _mapping(checks.get("volatility")),
        "relative_volume": _mapping(checks.get("volume")),
    }
    return {
        "rating_grade": rating.get("grade"),
        "rating_score": rating.get("score"),
        "rating_direction": rating.get("direction"),
        "family_votes": _mapping(rating.get("family_votes")),
        "aligned_families": list(rating.get("aligned_families") or []),
        "quality_adjustments": list(rating.get("quality_adjustments") or []),
        "quality_evidence": quality,
        "note": "置信度沿用既有评级等级、评分、指标族对齐及质量调整项；未新增百分比算法。",
    }


def _eligibility_blocks(eligibility: Mapping[str, Any], direction: str) -> tuple[str, ...]:
    if eligibility.get("status") != "BLOCKED":
        return ()
    blocks: list[str] = []
    for item in eligibility.get("results", []):
        result = _mapping(item)
        if result.get("direction") != direction:
            continue
        for block in result.get("hard_blocks", []):
            blocks.append(str(block))
        if not result.get("trade_eligible") and not result.get("hard_blocks"):
            blocks.append(str(result.get("status") or "eligibility_not_passed"))
    return tuple(dict.fromkeys(blocks or ["eligibility_blocked"]))


def _coerce_execution_context(
    value: ExecutionContext | Mapping[str, Any] | None,
) -> ExecutionContext | None:
    if value is None or isinstance(value, ExecutionContext):
        return value
    checker = value.get("checker")
    if not isinstance(checker, TradeEligibilityChecker):
        raise ValueError("execution_context.checker must be a TradeEligibilityChecker")
    account = value.get("account")
    if account is not None and not isinstance(account, AccountSnapshot):
        raise ValueError("execution_context.account must be an AccountSnapshot or None")
    return ExecutionContext(
        checker=checker,
        account=account,
        backtest_validated=bool(value.get("backtest_validated", False)),
        execution_ready=bool(value.get("execution_ready", False)),
        require_account=bool(value.get("require_account", False)),
    )


def _data_metadata(bars: pd.DataFrame | None) -> dict[str, Any]:
    if bars is None or bars.empty:
        return {"dataset_version": None, "quality_statuses": [], "latest_complete_bar": None}
    statuses = sorted({str(item) for item in bars.get("quality_status", pd.Series(dtype=object)).dropna()})
    timestamp = bars["timestamp"].iloc[-1]
    return {
        "dataset_version": str(bars.attrs.get("dataset_version") or bars.get("dataset_version", pd.Series([None])).iloc[-1] or "unknown"),
        "quality_statuses": statuses,
        "latest_complete_bar": _timestamp(timestamp),
    }


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


def _timestamp(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _hash_payload(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _stable_hash(*parts: Any) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode("utf-8")).hexdigest()[:24]


def _daily_refresh_end(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _earliest_utc(value: str) -> datetime:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC").to_pydatetime()


__all__ = [
    "DailyDataUpdateService",
    "DataUpdateStage",
    "ExecutionContext",
    "StrategyScreeningService",
    "StrategyScreeningStage",
    "TrendDecisionService",
]
