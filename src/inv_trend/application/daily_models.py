"""Single-source-of-truth report bundle shared by JSON, HTML and regression tests."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Mapping

from inv_trend.core.explanations import AnomalyEvent, RuleEvaluation, StateTransition


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


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
    anomalies: tuple[AnomalyEvent, ...] = ()
    state_transitions: tuple[StateTransition, ...] = ()
    summary: Mapping[str, Any] = field(default_factory=dict)
    strategy_snapshot: Mapping[str, Any] = field(default_factory=dict)
    change_log: tuple[Mapping[str, Any], ...] = ()
    schema_version: str = "1"

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
        "change": "FeatureRequest 合并与单次 D1 特征准备",
        "reason": "消除日报基础指标与策略检查对 Donchian/SMA/MACD 的重复计算",
        "effect": "同一标的同一快照只构建一份 D1 特征框架，回放直接复用",
    },
    {
        "module": "core/explanations.py + core/rules.py",
        "change": "统一条件、规则、异常和状态切换解释模型",
        "reason": "原实现只输出最终事件，未记录未命中条件和相对阈值位置",
        "effect": "每个条件输出实际值、阈值、运算、true/false、上下方、权重与时间价格",
    },
    {
        "module": "application/daily_analysis.py",
        "change": "新增单一日报分析结果",
        "reason": "计算、回放、图表和 JSON 之前分散拼装，存在重复计算和字段漂移",
        "effect": "策略结果、条件树、异常、状态和图表数据来自同一 PreparedDailyAnalysis",
    },
    {
        "module": "observability/report_renderer.py",
        "change": "自包含交互式 HTML",
        "reason": "旧版仅静态 SVG/表格，无法联动查看触发条件、异常和状态",
        "effect": "支持缩放区间、联动悬停、信号/异常标记、条件展开及修改效果审计",
    },
    {
        "module": "eligibility/checker.py",
        "change": "修复显式阈值注入与标的身份不匹配",
        "reason": "自定义 thresholds 被市场默认配置覆盖；身份不匹配仍返回 verified",
        "effect": "显式配置优先，source mismatch 作为硬阻断并保留证据",
    },
)


__all__ = ["DEFAULT_CHANGE_LOG", "InstrumentReportBundle"]
