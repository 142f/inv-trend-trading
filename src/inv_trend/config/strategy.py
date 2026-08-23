"""Canonical strategy-configuration mapping helpers.

The canonical YAML is intentionally model-neutral.  Application, detector,
and backtest boundaries map it to their own validated configuration models so
that no adapter needs to import another adapter or the application layer.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

import yaml


CANONICAL_STRATEGY_SCHEMA_VERSION = "1"

_CANONICAL_TOP_LEVEL_KEYS = {
    "schema_version",
    "turtle",
    "daily_screening",
    "trend_decision",
    "risk",
}
_TURTLE_KEYS = {"profile", "rules", "detector"}
_RISK_KEYS = {
    "initial_equity",
    "cash_model",
    "liquidate_at_end",
    "contracts",
    "runtime",
}
_RUNTIME_KEYS = {
    "strategy_version",
    "html_report",
    "log_level",
    "log_format",
    "log_datefmt",
    "code_version",
    "data_manifest_hash",
    "paths",
}
_APPLICATION_TURTLE_RULE_KEYS = {
    "n_period",
    "fast_entry",
    "slow_entry",
    "fast_exit",
    "slow_exit",
    "stop_n",
    "pyramid_step_n",
}
_BACKTEST_TURTLE_RULE_KEYS = {
    "n_period",
    "fast_entry",
    "slow_entry",
    "fast_exit",
    "slow_exit",
    "stop_n",
    "pyramid_step_n",
    "breakout_buffer_n",
    "slow_entry_score_bonus",
}
_TURTLE_RULE_KEYS = {
    *_BACKTEST_TURTLE_RULE_KEYS,
    "trigger_mode",
    "entry_ma_period",
    "fast_system_enabled",
    "slow_system_enabled",
    "skip_fast_after_win",
    "allow_short",
    "max_total_1n_risk_pct",
    "max_direction_1n_risk_pct",
    "default_cluster_1n_risk_pct",
    "cluster_1n_risk_pct",
    "max_total_leverage",
    "max_direction_leverage",
    "default_cluster_leverage",
    "cluster_leverage",
}
_DETECTOR_KEYS = {
    "atr_period",
    "system1_entry",
    "system2_entry",
    "system1_exit",
    "system2_exit",
    "confirmation_mode",
    "skip_system1_after_win",
    "stop_atr",
    "pyramid_step_atr",
    "max_additions",
    "stop_mode",
    "approaching_atr",
    "overextended_atr",
    "retest_tolerance_atr",
    "false_breakout_bars",
    "max_holding_bars",
    "volatility_lookback",
    "volume_filter",
    "volume_lookback",
    "min_volume_ratio",
    "close_location_filter",
    "min_close_location",
    "trend_ma_period",
    "long_trend_ma_period",
    "trend_filter",
    "max_gap_atr",
}
_DAILY_SCREENING_KEYS = {"sma", "ema", "macd", "dmi", "atr", "volume", "rating"}
_DAILY_SCREENING_SECTION_KEYS = {
    "sma": {"periods"},
    "ema": {"periods"},
    "macd": {"fast", "slow", "signal", "session_periods"},
    "dmi": {"period", "adx_threshold"},
    "atr": {
        "period",
        "percentile_lookback",
        "normal_percentile_low",
        "normal_percentile_high",
    },
    "volume": {"lookback", "confirmation_ratio"},
    "rating": {"a_min_score", "b_min_score", "a_min_families", "b_min_families"},
}
_TREND_DECISION_KEYS = {"execution_grade", "eligibility_gate_enabled"}


def canonical_strategy_config_path() -> Path:
    """Return the packaged canonical strategy YAML path.

    Setuptools installs package data next to this module, so this path remains
    valid both from a source checkout and from a normal wheel installation.
    """

    return Path(__file__).with_name("strategy.yaml")


def load_strategy_mapping(path: str | Path | None = None) -> dict[str, Any]:
    """Read a YAML mapping without binding it to a runtime configuration model."""

    config_path = Path(path) if path is not None else canonical_strategy_config_path()
    if not config_path.exists():
        raise FileNotFoundError(f"missing strategy config: {config_path}")
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"strategy config must contain a mapping: {config_path}")
    return deepcopy(payload)


def is_canonical_strategy_mapping(raw: Mapping[str, Any]) -> bool:
    """Return whether *raw* uses the versioned canonical strategy shape."""

    # The version marker is deliberately the sole discriminator.  Older
    # callers sometimes used words such as ``turtle`` in an otherwise legacy
    # mapping; treating those files as canonical made a harmless extension
    # look like a malformed new configuration.
    return "schema_version" in raw


def canonical_to_application_mapping(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Map canonical strategy YAML to the legacy application configuration shape."""

    canonical = _validated_canonical(raw)
    turtle = _mapping(canonical["turtle"], "turtle")
    risk = _mapping(canonical["risk"], "risk")
    turtle_rules = _mapping(turtle["rules"], "turtle.rules")
    return {
        "profile": turtle["profile"],
        # The legacy application contract owns only these seven Turtle rule
        # overrides.  The wider rules section is consumed by the backtester.
        "rules": {
            key: deepcopy(turtle_rules[key])
            for key in _APPLICATION_TURTLE_RULE_KEYS
            if key in turtle_rules
        },
        "risk": {
            key: deepcopy(risk[key])
            for key in ("initial_equity", "cash_model", "liquidate_at_end")
            if key in risk
        },
        "contracts": _copy_mapping(risk.get("contracts", {}), "risk.contracts"),
        "daily_checks": _copy_mapping(
            canonical["daily_screening"], "daily_screening"
        ),
        "trend_decision": _copy_mapping(
            canonical["trend_decision"], "trend_decision"
        ),
    }


def canonical_to_detector_mapping(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Map canonical strategy YAML to :class:`detector.models.StrategyConfig`."""

    canonical = _validated_canonical(raw)
    turtle = _mapping(canonical["turtle"], "turtle")
    return _copy_mapping(turtle["detector"], "turtle.detector")


def canonical_to_backtest_mapping(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Map canonical strategy YAML to the legacy backtest configuration shape."""

    canonical = _validated_canonical(raw)
    turtle = _mapping(canonical["turtle"], "turtle")
    risk = _mapping(canonical["risk"], "risk")
    runtime = _mapping(risk.get("runtime", {}), "risk.runtime")
    result = {
        key: deepcopy(risk[key])
        for key in ("initial_equity", "cash_model", "liquidate_at_end")
        if key in risk
    }
    # Keep the serialized BacktestConfig (and therefore its manifest/config
    # fingerprint) equivalent to the historical defaults.  The full rule
    # section remains canonical authority, but values that were previously
    # implicit dataclass defaults must not suddenly appear in a legacy run
    # manifest merely because the source config was upgraded.
    turtle_rules = _mapping(turtle["rules"], "turtle.rules")
    result["rules"] = {
        key: deepcopy(turtle_rules[key])
        for key in _BACKTEST_TURTLE_RULE_KEYS
        if key in turtle_rules
    }
    result.update(deepcopy(runtime))
    return result


def _validated_canonical(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("canonical strategy config must be a mapping")
    unknown = set(raw) - _CANONICAL_TOP_LEVEL_KEYS
    if unknown:
        raise ValueError(f"unsupported canonical strategy keys: {sorted(unknown)}")
    missing = _CANONICAL_TOP_LEVEL_KEYS - set(raw)
    if missing:
        raise ValueError(f"missing canonical strategy keys: {sorted(missing)}")

    schema_version = str(raw["schema_version"])
    if schema_version != CANONICAL_STRATEGY_SCHEMA_VERSION:
        raise ValueError(
            "unsupported strategy schema_version: "
            f"{raw['schema_version']!r}; expected {CANONICAL_STRATEGY_SCHEMA_VERSION!r}"
        )

    turtle = _mapping(raw["turtle"], "turtle")
    turtle_unknown = set(turtle) - _TURTLE_KEYS
    if turtle_unknown:
        raise ValueError(f"unsupported turtle keys: {sorted(turtle_unknown)}")
    turtle_missing = _TURTLE_KEYS - set(turtle)
    if turtle_missing:
        raise ValueError(f"missing turtle keys: {sorted(turtle_missing)}")
    if not isinstance(turtle["profile"], str) or not turtle["profile"]:
        raise ValueError("turtle.profile must be a non-empty string")
    _validate_keys(_mapping(turtle["rules"], "turtle.rules"), _TURTLE_RULE_KEYS, "turtle.rules")
    _validate_keys(_mapping(turtle["detector"], "turtle.detector"), _DETECTOR_KEYS, "turtle.detector")

    risk = _mapping(raw["risk"], "risk")
    risk_unknown = set(risk) - _RISK_KEYS
    if risk_unknown:
        raise ValueError(f"unsupported risk keys: {sorted(risk_unknown)}")
    _mapping(risk.get("contracts", {}), "risk.contracts")
    runtime = _mapping(risk.get("runtime", {}), "risk.runtime")
    runtime_unknown = set(runtime) - _RUNTIME_KEYS
    if runtime_unknown:
        raise ValueError(f"unsupported risk.runtime keys: {sorted(runtime_unknown)}")
    daily_screening = _mapping(raw["daily_screening"], "daily_screening")
    _validate_keys(daily_screening, _DAILY_SCREENING_KEYS, "daily_screening")
    for name, allowed in _DAILY_SCREENING_SECTION_KEYS.items():
        _validate_keys(_mapping(daily_screening.get(name, {}), f"daily_screening.{name}"), allowed, f"daily_screening.{name}")
    _validate_trend_decision(
        _mapping(raw["trend_decision"], "trend_decision")
    )
    return deepcopy(dict(raw))


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    return value


def _copy_mapping(value: Any, name: str) -> dict[str, Any]:
    return deepcopy(dict(_mapping(value, name)))


def _validate_keys(value: Mapping[str, Any], allowed: set[str], name: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"unsupported {name} keys: {sorted(unknown)}")


def _validate_trend_decision(value: Mapping[str, Any]) -> None:
    """Validate execution controls before any consumer-specific mapping.

    Detector and backtest consumers do not otherwise use this section.  Its
    values nevertheless belong to the single canonical strategy contract, so
    accepting an invalid value in either mapper would make the same YAML
    valid or invalid depending on the command that happened to load it.
    """

    _validate_keys(value, _TREND_DECISION_KEYS, "trend_decision")

    if "execution_grade" in value:
        execution_grade = value["execution_grade"]
        if not isinstance(execution_grade, str) or execution_grade != "A":
            raise ValueError(
                "trend_decision.execution_grade must be the string 'A'"
            )

    if "eligibility_gate_enabled" in value and not isinstance(
        value["eligibility_gate_enabled"], bool
    ):
        raise ValueError("trend_decision.eligibility_gate_enabled must be boolean")
