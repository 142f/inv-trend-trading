from __future__ import annotations

import ast
from pathlib import Path

from inv_trend.adapters.detector.indicators.daily_signals import (
    analyze_daily_signals as legacy_analyze_daily_signals,
    analyze_prepared_daily_signals as legacy_analyze_prepared_daily_signals,
    daily_signal_feature_request as legacy_daily_signal_feature_request,
    normalize_completed_daily_bars as legacy_normalize_completed_daily_bars,
    prepare_daily_signal_frame as legacy_prepare_daily_signal_frame,
)
from inv_trend.adapters.detector.indicators.daily_strategy_checks import (
    analyze_prepared_strategy_checks as legacy_analyze_prepared_strategy_checks,
    daily_strategy_feature_request as legacy_daily_strategy_feature_request,
    prepare_daily_strategy_checks as legacy_prepare_daily_strategy_checks,
)
from inv_trend.application.daily_analysis import (
    PreparedDailyAnalysis as legacy_prepared_daily_analysis,
    analyze_prepared_daily_analysis as legacy_analyze_prepared_daily_analysis,
    build_rule_evaluations as legacy_build_rule_evaluations,
    detect_anomalies as legacy_detect_anomalies,
    prepare_daily_analysis as legacy_prepare_daily_analysis,
    state_transitions as legacy_state_transitions,
)
from inv_trend.core.strategy.daily.analysis import (
    PreparedDailyAnalysis,
    analyze_prepared_daily_analysis,
    build_rule_evaluations,
    detect_anomalies,
    prepare_daily_analysis,
    state_transitions,
)
from inv_trend.core.strategy.daily.signals import (
    analyze_daily_signals,
    analyze_prepared_daily_signals,
    daily_signal_feature_request,
    normalize_completed_daily_bars,
    prepare_daily_signal_frame,
)
from inv_trend.core.strategy.daily.strategy_checks import (
    analyze_prepared_strategy_checks,
    daily_strategy_feature_request,
    prepare_daily_strategy_checks,
)


def test_legacy_d1_calculation_paths_reexport_core_implementations() -> None:
    assert legacy_analyze_daily_signals is analyze_daily_signals
    assert legacy_analyze_prepared_daily_signals is analyze_prepared_daily_signals
    assert legacy_daily_signal_feature_request is daily_signal_feature_request
    assert legacy_normalize_completed_daily_bars is normalize_completed_daily_bars
    assert legacy_prepare_daily_signal_frame is prepare_daily_signal_frame

    assert legacy_analyze_prepared_strategy_checks is analyze_prepared_strategy_checks
    assert legacy_daily_strategy_feature_request is daily_strategy_feature_request
    assert legacy_prepare_daily_strategy_checks is prepare_daily_strategy_checks

    assert legacy_prepared_daily_analysis is PreparedDailyAnalysis
    assert legacy_analyze_prepared_daily_analysis is analyze_prepared_daily_analysis
    assert legacy_build_rule_evaluations is build_rule_evaluations
    assert legacy_detect_anomalies is detect_anomalies
    assert legacy_prepare_daily_analysis is prepare_daily_analysis
    assert legacy_state_transitions is state_transitions


def test_core_d1_calculations_do_not_import_application_or_adapter_layers() -> None:
    root = Path(__file__).parents[2] / "src" / "inv_trend" / "core" / "strategy" / "daily"
    for path in (root / "signals.py", root / "strategy_checks.py", root / "analysis.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported_modules = [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        ]
        imported_modules.extend(
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
        assert all(
            not module.startswith(("inv_trend.application", "inv_trend.adapters"))
            and module.split(".")[0] not in {"application", "adapters"}
            for module in imported_modules
        ), path
        dynamic_targets = [
            node.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "__import__"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ]
        dynamic_targets.extend(
            node.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "import_module"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        )
        assert all(
            not target.startswith(("inv_trend.application", "inv_trend.adapters"))
            for target in dynamic_targets
        ), path
