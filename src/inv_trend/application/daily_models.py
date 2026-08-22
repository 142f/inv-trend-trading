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
    quality: Mapping[str, Any] = field(default_factory=dict)
    freshness: Mapping[str, Any] = field(default_factory=dict)
    lineage: Mapping[str, Any] = field(default_factory=dict)
    data_readiness: str = "BLOCKED"
    blocking_reasons: tuple[str, ...] = ()
    observation_only: bool = False
    schema_version: str = "1"

    def __post_init__(self) -> None:
        for name in ("quality", "freshness", "lineage"):
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
    screening_status: str = "NOT_RUN"
    reason: str | None = None
    observation_only: bool = False
    schema_version: str = "1"

    def __post_init__(self) -> None:
        for name in ("strategy_checks", "eligibility"):
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
    confidence: Mapping[str, Any] = field(default_factory=dict)
    long_evidence: tuple[str, ...] = ()
    short_evidence: tuple[str, ...] = ()
    reverse_evidence: tuple[str, ...] = ()
    risk_blocks: tuple[str, ...] = ()
    conclusion: str = ""
    observation_only: bool = False
    schema_version: str = "1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "confidence", _freeze(self.confidence))
        for name in ("long_evidence", "short_evidence", "reverse_evidence", "risk_blocks"):
            object.__setattr__(self, name, tuple(map(str, getattr(self, name))))

    def _business_payload(self) -> dict[str, Any]:
        return {
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
            "conclusion": self.conclusion,
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
    data_update_result: Mapping[str, Any] = field(default_factory=dict)
    strategy_screening_result: Mapping[str, Any] = field(default_factory=dict)
    trend_decision_result: Mapping[str, Any] = field(default_factory=dict)
    event_decisions: tuple[Mapping[str, Any], ...] = ()
    anomalies: tuple[AnomalyEvent, ...] = ()
    state_transitions: tuple[StateTransition, ...] = ()
    summary: Mapping[str, Any] = field(default_factory=dict)
    strategy_snapshot: Mapping[str, Any] = field(default_factory=dict)
    change_log: tuple[Mapping[str, Any], ...] = ()
    schema_version: str = "3"

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
            "data_update_result": _thaw(self.data_update_result),
            "strategy_screening_result": _thaw(self.strategy_screening_result),
            "trend_decision_result": _thaw(self.trend_decision_result),
            "event_decisions": [_thaw(item) for item in self.event_decisions],
            "anomalies": [item.to_dict() for item in self.anomalies],
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
    "BreakoutAssessment",
    "DataUpdateResult",
    "DEFAULT_CHANGE_LOG",
    "InstrumentReportBundle",
    "MarketAssessment",
    "StrategyScreeningResult",
    "TrendDecisionResult",
]
