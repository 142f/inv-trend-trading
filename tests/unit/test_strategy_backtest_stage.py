from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from inv_trend.application.strategy_backtest import (
    BacktestPlan,
    BacktestSourceBundle,
    RankingPolicy,
    SourceInstrument,
    StrategyBacktestService,
    ValidationPolicy,
    expand_parameter_grid,
)
from inv_trend.application.strategy_backtest.artifacts import render_backtest_html
from inv_trend.application.strategy_backtest.metrics import performance_metrics
from inv_trend.application.strategy_backtest.projector import StrategyReplayProjector
from inv_trend.application.strategy_backtest.validation import build_walk_forward_windows
from inv_trend.config import load_strategy_mapping


def _bars(count: int = 520) -> pd.DataFrame:
    x = np.arange(count, dtype=float)
    close = 100 + 0.06 * x + 8 * np.sin(x / 17)
    frame = pd.DataFrame(
        {
            "open": close + np.sin(x / 5) * 0.2,
            "high": close + 1.5,
            "low": close - 1.5,
            "close": close,
            "volume": 10_000 + x,
        },
        index=pd.date_range("2020-01-01", periods=count, freq="D", tz="UTC"),
    )
    frame.attrs["dataset_version"] = "version-1"
    return frame


def _source() -> BacktestSourceBundle:
    return BacktestSourceBundle(
        source_run_id="source-1",
        source_run_path="/immutable/source-1",
        strategy_version="corrected-v2",
        instruments=(
            SourceInstrument(
                symbol="TEST", instrument_id="TEST.SPOT", timeframe="D1",
                dataset_version="version-1", data_result_hash="a" * 64,
                screening_result_hash="b" * 64, decision_result_hash="c" * 64,
            ),
        ),
        strategy_config=load_strategy_mapping(),
    )


def _plan(**values: object) -> BacktestPlan:
    defaults: dict[str, object] = {
        "symbols": ("TEST",),
        "parameter_space": {"turtle.rules.fast_entry": (20, 25), "turtle.rules.slow_entry": (55,)},
        "constraints": ("rules.fast_entry < rules.slow_entry",),
        "validation": ValidationPolicy(120, 60, 60, 60, 3),
        "ranking": RankingPolicy(min_oos_trades=0, max_drawdown=1.0),
    }
    defaults.update(values)
    return BacktestPlan(**defaults)


def test_grid_is_deterministic_safe_and_deduplicated() -> None:
    plan = _plan(
        parameter_space={
            "turtle.rules.fast_entry": (20, 20, 25),
            "turtle.rules.slow_entry": (20, 55),
        }
    )
    combinations = expand_parameter_grid(plan)
    assert combinations == (
        {"rules.fast_entry": 20, "rules.slow_entry": 55},
        {"rules.fast_entry": 25, "rules.slow_entry": 55},
    )
    with pytest.raises(ValueError, match="unsupported"):
        expand_parameter_grid(_plan(parameter_space={"os.system": ("bad",)}))


def test_grid_limit_fails_before_execution() -> None:
    with pytest.raises(ValueError, match="max_combinations"):
        expand_parameter_grid(
            _plan(
                parameter_space={
                    "rules.fast_entry": (10, 11, 12),
                    "rules.slow_entry": (30, 31, 32),
                },
                max_combinations=4,
            )
        )


def test_walk_forward_has_independent_holdout_and_fails_closed_when_short() -> None:
    policy = ValidationPolicy(100, 50, 50, 50, 3)
    calendar = pd.date_range("2020-01-01", periods=320, freq="D", tz="UTC")
    windows, holdout, status = build_walk_forward_windows(calendar, policy)
    assert status == "READY"
    assert len(windows) >= 3
    assert pd.Timestamp(windows[-1].validation_end) < pd.Timestamp(holdout[0])
    short = build_walk_forward_windows(calendar[:250], policy)
    assert short == ((), None, "INSUFFICIENT_HISTORY")


def test_undefined_trade_metrics_are_null_with_reason() -> None:
    equity = pd.Series(
        [100.0, 100.0, 100.0],
        index=pd.date_range("2024-01-01", periods=3, freq="D", tz="UTC"),
    )
    metrics, unavailable = performance_metrics(equity, pd.DataFrame())
    assert metrics["trade_count"] == 0
    assert metrics["win_rate"] is None
    assert metrics["sharpe"] is None
    assert unavailable["win_rate"] == "没有已完成交易"


def test_projector_reuses_signal_preparation_for_execution_only_change() -> None:
    projector = StrategyReplayProjector(_source())
    first_bundle, first_store, first_rules = projector.project(
        {"TEST": _bars()}, {"rules.fast_entry": 20, "rules.slow_entry": 55, "rules.stop_n": 1.5}
    )
    second_bundle, second_store, second_rules = projector.project(
        {"TEST": _bars()}, {"rules.fast_entry": 20, "rules.slow_entry": 55, "rules.stop_n": 2.5}
    )
    assert projector.cache_size == 1
    assert first_bundle is second_bundle
    assert first_store is second_store
    assert first_rules.stop_n == 1.5
    assert second_rules.stop_n == 2.5


def test_batch_is_deterministic_and_html_only_embeds_results() -> None:
    plan = _plan(parameter_space={"rules.fast_entry": (20,), "rules.slow_entry": (55,)})
    service = StrategyBacktestService()
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    first = service.run(_source(), plan, {"TEST": _bars()}, run_id="batch-1", now=now)
    second = service.run(_source(), plan, {"TEST": _bars()}, run_id="batch-2", now=now)
    assert first.result_hash == second.result_hash
    assert first.validation_status == "READY"
    assert len(first.combinations) == 1
    assert first.combinations[0].status == "COMPLETED"
    html = render_backtest_html(first)
    assert "策略参数回测与稳定性审计" in html
    assert 'id="batchData"' in html
    assert "compute_turtle_indicators" not in html


def test_insufficient_history_never_claims_best_parameters() -> None:
    plan = _plan(
        parameter_space={"rules.fast_entry": (20,), "rules.slow_entry": (55,)},
        validation=ValidationPolicy(756, 252, 252, 252, 3),
    )
    result = StrategyBacktestService().run(
        _source(), plan, {"TEST": _bars(300)}, run_id="short", now=datetime(2026, 8, 31, tzinfo=timezone.utc)
    )
    assert result.validation_status == "INSUFFICIENT_HISTORY"
    assert result.best_combination_id is None
    assert result.stable_combination_ids == ()
