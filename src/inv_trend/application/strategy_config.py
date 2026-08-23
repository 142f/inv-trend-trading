"""Resolved strategy configuration independent of data-source identity."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from inv_trend.config import (
    canonical_to_application_mapping,
    is_canonical_strategy_mapping,
    load_strategy_mapping,
)


@dataclass(frozen=True)
class DailyChecksConfig:
    """Validated settings for the report-only daily strategy dimensions."""

    sma_periods: tuple[int, ...] = (5, 10, 20, 55, 120)
    ema_periods: tuple[int, int] = (144, 169)
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    macd_session_periods: tuple[int, ...] = (1, 2, 5, 7)
    dmi_period: int = 14
    adx_threshold: float = 25.0
    atr_period: int = 14
    atr_percentile_lookback: int = 120
    atr_normal_percentile_low: float = 0.10
    atr_normal_percentile_high: float = 0.90
    volume_lookback: int = 20
    volume_confirmation_ratio: float = 1.5
    rating_a_min_score: float = 6.0
    rating_b_min_score: float = 4.0
    rating_a_min_families: int = 3
    rating_b_min_families: int = 2

    def __post_init__(self) -> None:
        if len(self.sma_periods) < 2 or any(period < 2 for period in self.sma_periods):
            raise ValueError("daily_checks.sma.periods must contain periods >= 2")
        if tuple(sorted(set(self.sma_periods))) != self.sma_periods:
            raise ValueError("daily_checks.sma.periods must be unique and increasing")
        if len(self.ema_periods) != 2 or not 1 < self.ema_periods[0] < self.ema_periods[1]:
            raise ValueError("daily_checks.ema.periods must be two increasing periods >= 2")
        if not 1 < self.macd_fast < self.macd_slow or self.macd_signal < 2:
            raise ValueError("daily_checks.macd periods are invalid")
        if not self.macd_session_periods or self.macd_session_periods[0] != 1:
            raise ValueError("daily_checks.macd.session_periods must start with D1")
        if tuple(sorted(set(self.macd_session_periods))) != self.macd_session_periods or any(
            period < 1 for period in self.macd_session_periods
        ):
            raise ValueError("daily_checks.macd.session_periods must be unique positive integers")
        if self.dmi_period < 2 or self.atr_period < 2 or self.volume_lookback < 2:
            raise ValueError("daily_checks periods must be >= 2")
        if self.atr_percentile_lookback < 2:
            raise ValueError("daily_checks.atr.percentile_lookback must be >= 2")
        if not 0.0 <= self.atr_normal_percentile_low < self.atr_normal_percentile_high <= 1.0:
            raise ValueError("daily_checks.atr normal percentiles must be within [0, 1]")
        if self.adx_threshold <= 0.0 or self.volume_confirmation_ratio <= 0.0:
            raise ValueError("daily_checks thresholds must be positive")
        if self.rating_b_min_score > self.rating_a_min_score:
            raise ValueError("daily_checks.rating B score cannot exceed A score")
        if not 1 <= self.rating_b_min_families <= self.rating_a_min_families <= 4:
            raise ValueError("daily_checks.rating family counts must be between 1 and 4")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "DailyChecksConfig":
        if not isinstance(raw, Mapping):
            raise ValueError("daily_checks must be a mapping")
        allowed = {"sma", "ema", "macd", "dmi", "atr", "volume", "rating"}
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(f"unsupported daily_checks keys: {sorted(unknown)}")

        def section(name: str, allowed_keys: set[str]) -> Mapping[str, Any]:
            value = raw.get(name, {})
            if not isinstance(value, Mapping):
                raise ValueError(f"daily_checks.{name} must be a mapping")
            unknown_keys = set(value) - allowed_keys
            if unknown_keys:
                raise ValueError(
                    f"unsupported daily_checks.{name} keys: {sorted(unknown_keys)}"
                )
            return value

        sma = section("sma", {"periods"})
        ema = section("ema", {"periods"})
        macd = section("macd", {"fast", "slow", "signal", "session_periods"})
        dmi = section("dmi", {"period", "adx_threshold"})
        atr = section("atr", {"period", "percentile_lookback", "normal_percentile_low", "normal_percentile_high"})
        volume = section("volume", {"lookback", "confirmation_ratio"})
        rating = section("rating", {"a_min_score", "b_min_score", "a_min_families", "b_min_families"})
        defaults = cls()
        return cls(
            sma_periods=tuple(int(value) for value in sma.get("periods", defaults.sma_periods)),
            ema_periods=tuple(int(value) for value in ema.get("periods", defaults.ema_periods)),
            macd_fast=int(macd.get("fast", defaults.macd_fast)),
            macd_slow=int(macd.get("slow", defaults.macd_slow)),
            macd_signal=int(macd.get("signal", defaults.macd_signal)),
            macd_session_periods=tuple(int(value) for value in macd.get("session_periods", defaults.macd_session_periods)),
            dmi_period=int(dmi.get("period", defaults.dmi_period)),
            adx_threshold=float(dmi.get("adx_threshold", defaults.adx_threshold)),
            atr_period=int(atr.get("period", defaults.atr_period)),
            atr_percentile_lookback=int(atr.get("percentile_lookback", defaults.atr_percentile_lookback)),
            atr_normal_percentile_low=float(atr.get("normal_percentile_low", defaults.atr_normal_percentile_low)),
            atr_normal_percentile_high=float(atr.get("normal_percentile_high", defaults.atr_normal_percentile_high)),
            volume_lookback=int(volume.get("lookback", defaults.volume_lookback)),
            volume_confirmation_ratio=float(volume.get("confirmation_ratio", defaults.volume_confirmation_ratio)),
            rating_a_min_score=float(rating.get("a_min_score", defaults.rating_a_min_score)),
            rating_b_min_score=float(rating.get("b_min_score", defaults.rating_b_min_score)),
            rating_a_min_families=int(rating.get("a_min_families", defaults.rating_a_min_families)),
            rating_b_min_families=int(rating.get("b_min_families", defaults.rating_b_min_families)),
        )


@dataclass(frozen=True)
class TrendDecisionConfig:
    """Execution-only controls for the final trend-decision stage.

    Scoring remains entirely in :class:`DailyChecksConfig`.  This separate
    section is intentionally small so a deployment cannot accidentally turn a
    report-score tweak into a new execution rule.  When the eligibility gate
    is disabled or no execution context is supplied, A-grade breakouts remain
    ``ENTRY_CANDIDATE_*`` rather than being overstated as executable entries.
    """

    execution_grade: str = "A"
    eligibility_gate_enabled: bool = False

    def __post_init__(self) -> None:
        if self.execution_grade != "A":
            raise ValueError("trend_decision.execution_grade must be 'A'")
        if not isinstance(self.eligibility_gate_enabled, bool):
            raise ValueError("trend_decision.eligibility_gate_enabled must be boolean")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "TrendDecisionConfig":
        if not isinstance(raw, Mapping):
            raise ValueError("trend_decision must be a mapping")
        allowed = {"execution_grade", "eligibility_gate_enabled"}
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(
                f"unsupported trend_decision keys: {sorted(unknown)}"
            )
        return cls(
            execution_grade=str(raw.get("execution_grade", "A")),
            eligibility_gate_enabled=raw.get("eligibility_gate_enabled", False),
        )


@dataclass(frozen=True)
class ResolvedRunConfig:
    """Immutable execution contract after profile/default resolution.

    Instrument identity remains owned by ``inv_trend.data``; this model only
    carries strategy, risk, and contract overrides keyed by instrument ID.
    """

    profile: str = "corrected-v2"
    rules: Mapping[str, Any] = field(default_factory=dict)
    risk: Mapping[str, Any] = field(default_factory=dict)
    contracts: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    daily_checks: DailyChecksConfig = field(default_factory=DailyChecksConfig)
    trend_decision: TrendDecisionConfig = field(default_factory=TrendDecisionConfig)

    def __post_init__(self) -> None:
        for field_name in ("rules", "risk", "contracts"):
            value = getattr(self, field_name)
            if not isinstance(value, Mapping):
                raise ValueError(f"{field_name} must be a mapping")
            object.__setattr__(self, field_name, MappingProxyType(dict(value)))
        if isinstance(self.daily_checks, Mapping):
            object.__setattr__(
                self, "daily_checks", DailyChecksConfig.from_mapping(self.daily_checks)
            )
        elif not isinstance(self.daily_checks, DailyChecksConfig):
            raise ValueError("daily_checks must be a DailyChecksConfig or mapping")
        if isinstance(self.trend_decision, Mapping):
            object.__setattr__(
                self,
                "trend_decision",
                TrendDecisionConfig.from_mapping(self.trend_decision),
            )
        elif not isinstance(self.trend_decision, TrendDecisionConfig):
            raise ValueError("trend_decision must be a TrendDecisionConfig or mapping")


def load_resolved_run_config(path: str | Path | None = None) -> ResolvedRunConfig:
    """Load canonical strategy YAML or the legacy application configuration shape.

    Omitting ``path`` resolves the package-owned, versioned canonical file.
    Explicit legacy ``application/config/strategy.yaml`` paths continue to be
    accepted so existing callers can migrate without changing behavior.
    """

    raw = load_strategy_mapping(path)
    if is_canonical_strategy_mapping(raw):
        raw = canonical_to_application_mapping(raw)
    allowed = set(ResolvedRunConfig.__dataclass_fields__)
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"unsupported strategy configuration keys: {sorted(unknown)}")
    return ResolvedRunConfig(**raw)
