from __future__ import annotations

from pathlib import Path

import pytest

from inv_trend.adapters.detector.config import load_strategy_config
from inv_trend.adapters.multi_asset.config import load_config
from inv_trend.adapters.multi_asset.models import TurtleRules
from inv_trend.application.strategy_config import load_resolved_run_config
from inv_trend.config import (
    canonical_strategy_config_path,
    canonical_to_application_mapping,
    canonical_to_backtest_mapping,
    canonical_to_detector_mapping,
    is_canonical_strategy_mapping,
    load_strategy_mapping,
)


ROOT = Path("src/inv_trend")
LEGACY_APPLICATION = ROOT / "application" / "config" / "strategy.yaml"
LEGACY_DETECTOR = ROOT / "adapters" / "detector" / "config" / "strategy.yaml"
LEGACY_BACKTEST = ROOT / "adapters" / "multi_asset" / "config" / "defaults.yaml"


def test_canonical_strategy_config_is_the_versioned_default() -> None:
    payload = load_strategy_mapping()

    assert canonical_strategy_config_path().is_file()
    assert payload["schema_version"] == "1"
    assert set(payload) == {
        "schema_version",
        "turtle",
        "daily_screening",
        "trend_decision",
        "risk",
    }


def test_application_legacy_strategy_yaml_maps_to_the_canonical_result() -> None:
    canonical = load_resolved_run_config()
    legacy = load_resolved_run_config(LEGACY_APPLICATION)

    assert canonical == legacy


def test_detector_legacy_strategy_yaml_maps_to_the_canonical_result() -> None:
    canonical = load_strategy_config()
    legacy = load_strategy_config(LEGACY_DETECTOR)

    assert canonical == legacy


def test_backtest_legacy_yaml_and_canonical_yaml_resolve_to_same_rules() -> None:
    canonical = load_config()
    legacy = load_config(LEGACY_BACKTEST)

    assert (
        canonical.initial_equity,
        canonical.cash_model,
        canonical.liquidate_at_end,
        canonical.strategy_version,
        canonical.html_report,
        canonical.log_level,
        canonical.log_format,
        canonical.log_datefmt,
        canonical.code_version,
        canonical.data_manifest_hash,
        dict(canonical.paths),
    ) == (
        legacy.initial_equity,
        legacy.cash_model,
        legacy.liquidate_at_end,
        legacy.strategy_version,
        legacy.html_report,
        legacy.log_level,
        legacy.log_format,
        legacy.log_datefmt,
        legacy.code_version,
        legacy.data_manifest_hash,
        dict(legacy.paths),
    )
    assert TurtleRules(**dict(canonical.rules)) == TurtleRules(**dict(legacy.rules))
    assert dict(canonical.rules) == dict(legacy.rules)


def test_canonical_mapping_rejects_unknown_top_level_keys() -> None:
    payload = load_strategy_mapping()
    payload["historical_alias"] = True

    with pytest.raises(ValueError, match="unsupported canonical strategy keys"):
        canonical_to_application_mapping(payload)


@pytest.mark.parametrize(
    ("section", "value", "message"),
    (
        ("daily_screening", {"typo": True}, "unsupported daily_screening keys"),
        ("trend_decision", {"typo": True}, "unsupported trend_decision keys"),
    ),
)
def test_canonical_mapping_rejects_unknown_nested_sections(
    section: str, value: object, message: str
) -> None:
    payload = load_strategy_mapping()
    payload[section] = value

    with pytest.raises(ValueError, match=message):
        canonical_to_application_mapping(payload)


@pytest.mark.parametrize(
    "mapper",
    (
        canonical_to_application_mapping,
        canonical_to_detector_mapping,
        canonical_to_backtest_mapping,
    ),
    ids=("application", "detector", "backtest"),
)
@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("execution_grade", "B", "execution_grade must be the string 'A'"),
        ("execution_grade", 1, "execution_grade must be the string 'A'"),
        (
            "eligibility_gate_enabled",
            "false",
            "eligibility_gate_enabled must be boolean",
        ),
        ("eligibility_gate_enabled", 1, "eligibility_gate_enabled must be boolean"),
    ),
)
def test_canonical_trend_decision_values_are_validated_by_every_mapper(
    mapper: object,
    field: str,
    value: object,
    message: str,
) -> None:
    payload = load_strategy_mapping()
    payload["trend_decision"][field] = value

    with pytest.raises(ValueError, match=message):
        mapper(payload)  # type: ignore[operator]


def test_canonical_mapping_rejects_unknown_rule_without_silent_drop() -> None:
    payload = load_strategy_mapping()
    payload["turtle"]["rules"]["fast_entyr"] = 20

    with pytest.raises(ValueError, match="unsupported turtle.rules keys"):
        canonical_to_application_mapping(payload)


def test_legacy_mapping_with_turtle_name_is_not_misclassified_as_canonical() -> None:
    assert not is_canonical_strategy_mapping({"turtle": {}})
