from __future__ import annotations

from pathlib import Path

import pytest

from research.d1_suite.scripts.d1_backtest_common import PRESET_RUNS, load_universe, run_backtest
from research.d1_suite.scripts.run_d1_pruned_universe_experiments import build_run_specs
from research.d1_suite.scripts.run_d1_candidate9_audit import build_rules
from turtle_multi_asset import TurtleBacktester


@pytest.fixture(scope="module")
def universe() -> dict:
    return load_universe(
        core_data_dir=Path("data_external_xau_btc_xag_eth"),
        equity_data_dir=Path("data_external_equities"),
    )


def test_unified_runner_matches_pruned_experiment_for_candidate9(universe: dict) -> None:
    preset = PRESET_RUNS["base_9_eth_short_only"]
    new_result, new_data, _, _ = run_backtest(
        all_data=universe,
        symbols=preset["symbols"],
        initial_equity=10_000.0,
        align_start=True,
        align_end=True,
        eth_mode="short_only",
    )

    old_specs = build_run_specs(list(new_data), eth_mode="short_only")
    old_rules = build_rules(list(new_data), cluster_leverage={}, rule_overrides={})
    old_result = TurtleBacktester(
        data=new_data,
        specs=old_specs,
        rules=old_rules,
        initial_equity=10_000.0,
    ).run()

    assert float(new_result.equity_curve.iloc[-1]) == pytest.approx(float(old_result.equity_curve.iloc[-1]))
    assert new_result.metrics["cagr"] == pytest.approx(old_result.metrics["cagr"])
    assert new_result.metrics["max_drawdown"] == pytest.approx(old_result.metrics["max_drawdown"])
    assert len(new_result.trades) == len(old_result.trades)


def test_unified_runner_matches_revised8_2020_window(universe: dict) -> None:
    preset = PRESET_RUNS["revised_8_no_eth"]
    result, _, _, _ = run_backtest(
        all_data=universe,
        symbols=preset["symbols"],
        initial_equity=10_000.0,
        align_start=True,
        align_end=True,
        start="2020-01-01",
        eth_mode="excluded",
    )

    assert float(result.equity_curve.iloc[-1]) == pytest.approx(301571.449408, rel=1e-6)
    assert result.metrics["cagr"] == pytest.approx(0.717646, rel=1e-6)
    assert result.metrics["max_drawdown"] == pytest.approx(-0.471054, rel=1e-6)
