from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from research.d1_suite.scripts.d1_backtest_common import PRESET_RUNS, load_universe, run_backtest
from research.d1_suite.scripts.run_d1_pruned_universe_experiments import build_run_specs
from research.d1_suite.scripts.run_d1_candidate9_audit import build_rules
from turtle_multi_asset import TurtleBacktester


@pytest.fixture(scope="module")
def universe() -> dict:
    data = load_universe(
        core_data_dir=Path("data_external_xau_btc_xag_eth"),
        equity_data_dir=Path("data_external_equities"),
    )
    required = set(PRESET_RUNS["base_9_eth_short_only"]["symbols"])
    required.update(PRESET_RUNS["revised_8_no_eth"]["symbols"])
    missing = sorted(required - set(data))
    if missing:
        pytest.skip(f"external D1 regression data is unavailable: {missing}")
    return data


def _universe_fingerprint(universe: dict, symbols: list[str]) -> str:
    digest = sha256()
    for symbol in sorted(symbols):
        digest.update(symbol.encode("utf-8"))
        digest.update(b"\0")
        payload = universe[symbol].to_csv(
            index=True,
            float_format="%.12g",
            date_format="%Y-%m-%dT%H:%M:%S%z",
            lineterminator="\n",
        )
        digest.update(payload.encode("utf-8"))
    return digest.hexdigest()


def test_run_backtest_fails_closed_for_missing_symbols() -> None:
    with pytest.raises(ValueError, match="MISSING"):
        run_backtest(
            all_data={},
            symbols=["MISSING"],
            initial_equity=10_000.0,
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
    assert _universe_fingerprint(universe, preset["symbols"]) == (
        "7744df2951e0065fc65940072a3ef27ac8cf75dd31c039d19493620923d7f5b0"
    )
    result, _, _, _ = run_backtest(
        all_data=universe,
        symbols=preset["symbols"],
        initial_equity=10_000.0,
        align_start=True,
        align_end=True,
        start="2020-01-01",
        eth_mode="excluded",
    )

    # Baseline v2: indicators warm up on pre-2020 bars and skipped System 1
    # breakouts are advanced as virtual trades.
    assert float(result.equity_curve.iloc[-1]) == pytest.approx(480149.988164, rel=1e-6)
    assert result.metrics["cagr"] == pytest.approx(0.848817, rel=1e-6)
    assert result.metrics["max_drawdown"] == pytest.approx(-0.37326249, rel=1e-6)
