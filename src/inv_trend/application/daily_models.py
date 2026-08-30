"""Single-source-of-truth report bundle shared by JSON, HTML and tests."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from types import MappingProxyType
from typing import Any, Mapping

from inv_trend.core.explanations import AnomalyEvent, RuleEvaluation, StateTransition


def _freeze(value: Any) -> Any:
    """Recursively detach stage results from mutable caller-owned payloads."""

    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    """Return a JSON-safe ordinary container from a frozen stage payload."""

    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        _thaw(payload),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


@dataclass(frozen=True)
class DataUpdateResult:
    """Auditable D1 data-readiness result.

    The model deliberately contains no run ID or wall-clock duration.  Its
    hash therefore identifies the business input available to strategy
    screening, rather than an individual orchestration attempt.
    """

    symbol: str
    instrument_id: str
    timeframe: str
    update_status: str
    dataset_version: str | None
    latest_complete_d1: str | None
    update: Mapping[str, Any] = field(default_factory=dict)
    quality: Mapping[str, Any] = field(default_factory=dict)
    freshness: Mapping[str, Any] = field(default_factory=dict)
    lineage: Mapping[str, Any] = field(default_factory=dict)
    data_readiness: str = "BLOCKED"
    blocking_reasons: tuple[str, ...] = ()
    observation_only: bool = False
    schema_version: str = "1"

    def __post_init__(self) -> None:
        for name in ("update", "quality", "freshness", "lineage"):
            object.__setattr__(self, name, _freeze(getattr(self, name)))
        object.__setattr__(self, "blocking_reasons", tuple(map(str, self.blocking_reasons)))

    @property
    def formal_ready(self) -> bool:
        return self.data_readiness == "READY" and not self.blocking_reasons

    def _business_payload(self) -> dict[str, Any]:
        return {
            "stage": "data_update",
            "schema_version": self.schema_version,
            "symbol": self.symbol,
            "instrument_id": self.instrument_id,
            "timeframe": self.timeframe,
            "update_status": self.update_status,
            "dataset_version": self.dataset_version,
            "latest_complete_d1": self.latest_complete_d1,
            "update": self.update,
            "quality": self.quality,
            "freshness": self.freshness,
            "lineage": self.lineage,
            "data_readiness": self.data_readiness,
            "blocking_reasons": self.blocking_reasons,
            "observation_only": self.observation_only,
        }

    @property
    def result_hash(self) -> str:
        return hashlib.sha256(_canonical_json(self._business_payload())).hexdigest()

    @property
    def result_id(self) -> str:
        return self.result_hash[:24]

    def to_dict(self) -> dict[str, Any]:
        payload = _thaw(self._business_payload())
        payload["result_id"] = self.result_id
        payload["result_hash"] = self.result_hash
        return payload


@dataclass(frozen=True)
class StrategyScreeningResult:
    """One-pass strategy evidence, deliberately without a trading decision."""

    symbol: str
    instrument_id: str
    timeframe: str
    input_data_hash: str
    configuration_hash: str
    dataset_version: str | None
    as_of: str | None
    strategy_checks: Mapping[str, Any] = field(default_factory=dict)
    conditions: tuple[Mapping[str, Any], ...] = ()
    rules: tuple[Mapping[str, Any], ...] = ()
    raw_events: tuple[Mapping[str, Any], ...] = ()
    turtle_breakouts: tuple[Mapping[str, Any], ...] = ()
    eligibility: Mapping[str, Any] = field(default_factory=dict)
    event_snapshots: tuple[Mapping[str, Any], ...] = ()
    # The complete replay/report projection consumed by commit.  It is JSON
    # only (never a DataFrame) and lives in the signed stage result so commit
    # does not depend on a mutable audit-sidecar file.
    commit_evidence: Mapping[str, Any] = field(default_factory=dict)
    commit_evidence_hash: str | None = None
    screening_status: str = "NOT_RUN"
    reason: str | None = None
    observation_only: bool = False
    schema_version: str = "1"

    def __post_init__(self) -> None:
        for name in ("strategy_checks", "eligibility", "commit_evidence"):
            object.__setattr__(self, name, _freeze(getattr(self, name)))
        for name in ("conditions", "rules", "raw_events", "turtle_breakouts", "event_snapshots"):
            object.__setattr__(self, name, tuple(_freeze(item) for item in getattr(self, name)))

    def _business_payload(self) -> dict[str, Any]:
        return {
            "stage": "strategy_screening",
            "schema_version": self.schema_version,
            "symbol": self.symbol,
            "instrument_id": self.instrument_id,
            "timeframe": self.timeframe,
            "input_data_hash": self.input_data_hash,
            "configuration_hash": self.configuration_hash,
            "dataset_version": self.dataset_version,
            "as_of": self.as_of,
            "strategy_checks": self.strategy_checks,
            "conditions": self.conditions,
            "rules": self.rules,
            "raw_events": self.raw_events,
            "turtle_breakouts": self.turtle_breakouts,
            "eligibility": self.eligibility,
            "event_snapshots": self.event_snapshots,
            # Precomputed replay/report evidence is JSON-only and part of the
            # immutable screening contract.  Commit may project it into the
            # legacy SQLite/report shape but never recalculates indicators.
            "commit_evidence": self.commit_evidence,
            "commit_evidence_hash": self.commit_evidence_hash,
            "screening_status": self.screening_status,
            "reason": self.reason,
            "observation_only": self.observation_only,
        }

    @property
    def result_hash(self) -> str:
        return hashlib.sha256(_canonical_json(self._business_payload())).hexdigest()

    @property
    def result_id(self) -> str:
        return self.result_hash[:24]

    def to_dict(self) -> dict[str, Any]:
        payload = _thaw(self._business_payload())
        payload["result_id"] = self.result_id
        payload["result_hash"] = self.result_hash
        return payload


@dataclass(frozen=True)
class TrendDecisionResult:
    """Final decision derived solely from a screening result's evidence."""

    symbol: str
    instrument_id: str
    timeframe: str
    input_data_hash: str
    input_screening_hash: str
    as_of: str | None
    trend_direction: str
    execution_state: str
    decision: str
    reason_code: str = ""
    eligibility_status: str = "UNKNOWN"
    confidence: Mapping[str, Any] = field(default_factory=dict)
    long_evidence: tuple[str, ...] = ()
    short_evidence: tuple[str, ...] = ()
    reverse_evidence: tuple[str, ...] = ()
    risk_blocks: tuple[str, ...] = ()
    event_decisions: tuple[Mapping[str, Any], ...] = ()
    # A run-bound commit plan is intentionally excluded from the business
    # hash: it carries operational fields such as ``detected_at`` and the
    # run-context binding.  ``commit_projection_hash`` is validated
    # separately before persistence, so it remains tamper-evident without
    # making a decision's business identity depend on a run ID/wall clock.
    commit_projection: Mapping[str, Any] = field(default_factory=dict)
    commit_projection_hash: str | None = None
    conclusion: str = ""
    observation_only: bool = False
    schema_version: str = "2"

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason_code", str(self.reason_code))
        object.__setattr__(self, "eligibility_status", str(self.eligibility_status))
        object.__setattr__(self, "confidence", _freeze(self.confidence))
        object.__setattr__(self, "commit_projection", _freeze(self.commit_projection))
        for name in ("long_evidence", "short_evidence", "reverse_evidence", "risk_blocks"):
            object.__setattr__(self, name, tuple(map(str, getattr(self, name))))
        object.__setattr__(
            self,
            "event_decisions",
            tuple(_freeze(item) for item in self.event_decisions),
        )

    def _business_payload(self) -> dict[str, Any]:
        payload = {
            "stage": "trend_decision",
            "schema_version": self.schema_version,
            "symbol": self.symbol,
            "instrument_id": self.instrument_id,
            "timeframe": self.timeframe,
            "input_data_hash": self.input_data_hash,
            "input_screening_hash": self.input_screening_hash,
            "as_of": self.as_of,
            "trend_direction": self.trend_direction,
            "execution_state": self.execution_state,
            "decision": self.decision,
            "confidence": self.confidence,
            "long_evidence": self.long_evidence,
            "short_evidence": self.short_evidence,
            "reverse_evidence": self.reverse_evidence,
            "risk_blocks": self.risk_blocks,
            "event_decisions": self.event_decisions,
            "conclusion": self.conclusion,
            "observation_only": self.observation_only,
        }
        # Schema v1 artifacts predate explicit reason/eligibility fields.  The
        # conditional preserves their historical hashes during staged-run
        # recovery after an application upgrade.
        if self.schema_version != "1":
            payload["reason_code"] = self.reason_code
            payload["eligibility_status"] = self.eligibility_status
        return payload

    @property
    def result_hash(self) -> str:
        return hashlib.sha256(_canonical_json(self._business_payload())).hexdigest()

    @property
    def result_id(self) -> str:
        return self.result_hash[:24]

    def to_dict(self) -> dict[str, Any]:
        payload = _thaw(self._business_payload())
        payload["commit_projection"] = _thaw(self.commit_projection)
        payload["commit_projection_hash"] = self.commit_projection_hash
        payload["result_id"] = self.result_id
        payload["result_hash"] = self.result_hash
        return payload


@dataclass(frozen=True)
class BreakoutAssessment:
    """Display-only decision projection for one already-detected Turtle breakout."""

    assessment_id: str
    signal_id: str | None
    timestamp: str
    timeframe: str
    current_price: float | None
    breakout_type: str
    breakout_object: str
    breakout_level: float | None
    previous_state: str
    post_state: str
    trend: str
    trend_basis: tuple[str, ...] = ()
    entry_direction: str = "不入场"
    signal_strength: str = "不可用"
    triggered_conditions: tuple[str, ...] = ()
    unmet_conditions: tuple[str, ...] = ()
    quality_notes: tuple[str, ...] = ()
    conclusion: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "assessment_id": self.assessment_id,
            "signal_id": self.signal_id,
            "timestamp": self.timestamp,
            "timeframe": self.timeframe,
            "current_price": self.current_price,
            "breakout_type": self.breakout_type,
            "breakout_object": self.breakout_object,
            "breakout_level": self.breakout_level,
            "previous_state": self.previous_state,
            "post_state": self.post_state,
            "trend": self.trend,
            "trend_basis": list(self.trend_basis),
            "entry_direction": self.entry_direction,
            "signal_strength": self.signal_strength,
            "triggered_conditions": list(self.triggered_conditions),
            "unmet_conditions": list(self.unmet_conditions),
            "quality_notes": list(self.quality_notes),
            "conclusion": self.conclusion,
        }


@dataclass(frozen=True)
class TurtleObservation:
    """Report-only intraday/close relationship to one prepared Donchian channel."""

    observation_id: str
    timestamp: str
    period: int
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    channel_high: float | None
    channel_low: float | None
    intraday_directions: tuple[str, ...] = ()
    close_confirmation: str = "none"
    status: str = "unavailable"
    upper_excess_pct: float | None = None
    lower_excess_pct: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "timestamp": self.timestamp,
            "period": self.period,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "channel_high": self.channel_high,
            "channel_low": self.channel_low,
            "intraday_directions": list(self.intraday_directions),
            "close_confirmation": self.close_confirmation,
            "status": self.status,
            "upper_excess_pct": self.upper_excess_pct,
            "lower_excess_pct": self.lower_excess_pct,
        }


@dataclass(frozen=True)
class AnomalyEpisode:
    """Report projection grouping consecutive raw anomaly evidence."""

    episode_id: str
    anomaly_type: str
    side: str
    severity: str
    start_timestamp: str
    end_timestamp: str
    duration_bars: int
    status: str
    extreme_actual: float | None
    reference_value: Any
    member_anomaly_ids: tuple[str, ...] = ()
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "episode_id": self.episode_id,
            "anomaly_type": self.anomaly_type,
            "side": self.side,
            "severity": self.severity,
            "start_timestamp": self.start_timestamp,
            "end_timestamp": self.end_timestamp,
            "duration_bars": self.duration_bars,
            "status": self.status,
            "extreme_actual": self.extreme_actual,
            "reference_value": _thaw(_freeze(self.reference_value)),
            "member_anomaly_ids": list(self.member_anomaly_ids),
            "summary": self.summary,
        }


@dataclass(frozen=True)
class MarketAssessment:
    """Latest-bar report conclusion; it neither creates an order nor a signal."""

    as_of: str | None
    market_status: str
    trend: str
    trend_basis: tuple[str, ...] = ()
    entry_direction: str = "不入场"
    entry_reason: str = ""
    rating_grade: str | None = None
    rating_score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "market_status": self.market_status,
            "trend": self.trend,
            "trend_basis": list(self.trend_basis),
            "entry_direction": self.entry_direction,
            "entry_reason": self.entry_reason,
            "rating_grade": self.rating_grade,
            "rating_score": self.rating_score,
        }


@dataclass(frozen=True)
class InstrumentReportBundle:
    """Immutable report payload calculated once and rendered many ways."""

    symbol: str
    instrument_id: str
    timeframe: str
    dataset_version: str
    generated_at: str
    latest_bar: Mapping[str, Any] | None
    series: tuple[Mapping[str, Any], ...]
    signals: tuple[Mapping[str, Any], ...]
    rule_evaluations: tuple[RuleEvaluation, ...]
    market_assessment: MarketAssessment | None = None
    breakout_assessments: tuple[BreakoutAssessment, ...] = ()
    turtle_observations: tuple[TurtleObservation, ...] = ()
    data_update_result: Mapping[str, Any] = field(default_factory=dict)
    strategy_screening_result: Mapping[str, Any] = field(default_factory=dict)
    trend_decision_result: Mapping[str, Any] = field(default_factory=dict)
    event_decisions: tuple[Mapping[str, Any], ...] = ()
    anomalies: tuple[AnomalyEvent, ...] = ()
    anomaly_episodes: tuple[AnomalyEpisode, ...] = ()
    state_transitions: tuple[StateTransition, ...] = ()
    summary: Mapping[str, Any] = field(default_factory=dict)
    strategy_snapshot: Mapping[str, Any] = field(default_factory=dict)
    change_log: tuple[Mapping[str, Any], ...] = ()
    schema_version: str = "4"

    def _payload_without_identity(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "symbol": self.symbol,
            "instrument_id": self.instrument_id,
            "timeframe": self.timeframe,
            "dataset_version": self.dataset_version,
            "generated_at": self.generated_at,
            "latest_bar": dict(self.latest_bar) if self.latest_bar is not None else None,
            "series": [dict(row) for row in self.series],
            "signals": [dict(item) for item in self.signals],
            "rule_evaluations": [item.to_dict() for item in self.rule_evaluations],
            "market_assessment": self.market_assessment.to_dict() if self.market_assessment else None,
            "breakout_assessments": [item.to_dict() for item in self.breakout_assessments],
            "turtle_observations": [item.to_dict() for item in self.turtle_observations],
            "data_update_result": _thaw(self.data_update_result),
            "strategy_screening_result": _thaw(self.strategy_screening_result),
            "trend_decision_result": _thaw(self.trend_decision_result),
            "event_decisions": [_thaw(item) for item in self.event_decisions],
            "anomalies": [item.to_dict() for item in self.anomalies],
            "anomaly_episodes": [item.to_dict() for item in self.anomaly_episodes],
            "state_transitions": [item.to_dict() for item in self.state_transitions],
            "summary": dict(self.summary),
            "strategy_snapshot": dict(self.strategy_snapshot),
            "change_log": [dict(item) for item in self.change_log],
        }

    @property
    def result_hash(self) -> str:
        return hashlib.sha256(_canonical_json(self._payload_without_identity())).hexdigest()

    @property
    def result_id(self) -> str:
        return self.result_hash[:24]

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload_without_identity()
        payload["result_id"] = self.result_id
        payload["result_hash"] = self.result_hash
        return payload


DEFAULT_CHANGE_LOG: tuple[Mapping[str, str], ...] = (
    {
        "module": "application/daily_analysis.py + observability/report_renderer.py",
        "change": "区分海龟正式收盘突破与盘中越轨观察，并聚合异常阶段",
        "reason": "K 线影线越过通道但收盘回落时，旧报告无法解释为何没有正式信号；逐日异常也会形成重复噪声。",
        "effect": "正式策略语义不变；报告增加盘中未确认证据、异常阶段和可展开的逐日明细。",
    },
    {
        "module": "core/decision_events.py + application/daily/trend_decision.py",
        "change": "正式通知改由确认后的执行决策事件驱动",
        "reason": "A 级评级事件与 A 级、同向突破、资格通过的最终入场条件不等价。",
        "effect": "无突破不误报；评级已为 A 后的新突破不漏报；资格未确认仅输出入场候选。",
    },
    {
        "module": "core/features.py",
        "change": "合并特征请求，并一次性准备 D1 特征",
        "reason": "消除日报基础指标与策略检查对唐奇安通道、SMA、MACD 的重复计算。",
        "effect": "同一标的的同一快照只构建一份 D1 特征框架，回放直接复用。",
    },
    {
        "module": "core/explanations.py + core/rules.py",
        "change": "统一条件、规则、异常与状态变更解释模型",
        "reason": "原实现只输出最终事件，未记录未命中条件和相对阈值位置。",
        "effect": "每个条件均输出实际值、阈值、运算、满足状态、相对位置、权重、时间和价格。",
    },
    {
        "module": "application/daily_analysis.py",
        "change": "新增单一日报分析结果",
        "reason": "计算、回放、图表和 JSON 之前分散拼装，存在重复计算和字段漂移。",
        "effect": "策略结果、条件树、异常、状态和图表数据均来自同一份 PreparedDailyAnalysis。",
    },
    {
        "module": "observability/report_renderer.py",
        "change": "自包含交互式 HTML 报告",
        "reason": "旧版仅有静态 SVG 和表格，无法联动查看触发条件、异常和状态。",
        "effect": "支持区间缩放、联动悬停、突破/异常标记、条件展开和报告审查。",
    },
    {
        "module": "eligibility/checker.py",
        "change": "修复显式阈值注入与标的身份不匹配问题",
        "reason": "自定义阈值曾被市场默认配置覆盖；身份不匹配仍可能返回已验证状态。",
        "effect": "显式配置优先；数据源身份不匹配作为硬阻断并保留证据。",
    },
)


__all__ = [
    "AnomalyEpisode",
    "BreakoutAssessment",
    "DataUpdateResult",
    "DEFAULT_CHANGE_LOG",
    "InstrumentReportBundle",
    "MarketAssessment",
    "StrategyScreeningResult",
    "TurtleObservation",
    "TrendDecisionResult",
]
