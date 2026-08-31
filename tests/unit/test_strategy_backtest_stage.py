from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json

import numpy as np
import pandas as pd
import pytest

from inv_trend.application.backtest import (
    BacktestPlan,
    BacktestArtifactWriter,
    BacktestBatchService,
    BacktestSourceBundle,
    RankingPolicy,
    SourceInstrument,
    ValidationPolicy,
    expand_parameter_grid,
)
from inv_trend.application.backtest.artifacts import render_backtest_html
from inv_trend.application.backtest.metrics import performance_metrics
from inv_trend.application.backtest.projector import StrategyReplayProjector
from inv_trend.application.backtest.reporting import build_report_model
from inv_trend.application.backtest.validation import build_walk_forward_windows
from inv_trend.config import load_strategy_mapping
from inv_trend.cli.backtest import load_plan


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


def test_v1_plan_is_normalized_to_v2() -> None:
    plan = BacktestPlan.from_mapping({"schema_version": "1", "symbols": ["test"]})
    assert plan.schema_version == "2"
    assert plan.strategy_id == "turtle"


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


def test_legacy_batch_service_name_warns() -> None:
    from inv_trend.application.backtest import StrategyBacktestService

    with pytest.warns(DeprecationWarning, match="BacktestBatchService"):
        service = StrategyBacktestService()
    assert isinstance(service, BacktestBatchService)


def test_walk_forward_has_independent_holdout_and_fails_closed_when_short() -> None:
    policy = ValidationPolicy(100, 50, 50, 50, 3)
    calendar = pd.date_range("2020-01-01", periods=320, freq="D", tz="UTC")
    windows, holdout, status = build_walk_forward_windows(calendar, policy)
    assert status == "READY"
    assert len(windows) >= 3
    assert pd.Timestamp(windows[-1].validation_end) < pd.Timestamp(holdout[0])
    short = build_walk_forward_windows(calendar[:250], policy)
    assert short == ((), None, "INSUFFICIENT_HISTORY")


def test_long_horizon_defaults_produce_four_yearly_folds_and_one_year_holdout() -> None:
    policy = ValidationPolicy()
    assert (
        policy.train_bars,
        policy.validation_bars,
        policy.step_bars,
        policy.holdout_bars,
    ) == (1460, 365, 365, 365)
    calendar = pd.date_range("2017-08-17", periods=3297, freq="D", tz="UTC")
    windows, holdout, status = build_walk_forward_windows(calendar, policy)
    assert status == "READY"
    assert len(windows) == 4
    assert holdout is not None
    holdout_days = (pd.Timestamp(holdout[1]) - pd.Timestamp(holdout[0])).days + 1
    assert holdout_days == 365


def test_btc_eth_plan_locks_20_55_and_enables_both_directions() -> None:
    plan = load_plan("config/策略回测计划_BTC_ETH_v2.yaml")
    combinations = expand_parameter_grid(plan)
    assert len(combinations) == 2
    assert {item["rules.fast_entry"] for item in combinations} == {20}
    assert {item["rules.slow_entry"] for item in combinations} == {55}
    assert {item["rules.allow_short"] for item in combinations} == {True}
    for symbol in ("BTC", "ETH"):
        assert {item[f"assets.{symbol}.can_long"] for item in combinations} == {True}
        assert {item[f"assets.{symbol}.can_short"] for item in combinations} == {True}


def test_undefined_trade_metrics_are_null_with_reason() -> None:
    equity = pd.Series(
        [100.0, 100.0, 100.0],
        index=pd.date_range("2024-01-01", periods=3, freq="D", tz="UTC"),
    )
    metrics, unavailable = performance_metrics(equity, pd.DataFrame())
    assert metrics["trade_count"] == 0
    assert metrics["win_rate"] is None
    assert metrics["sharpe_ratio"] is None
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
    service = BacktestBatchService()
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    first = service.run(_source(), plan, {"TEST": _bars()}, run_id="batch-1", now=now)
    second = service.run(_source(), plan, {"TEST": _bars()}, run_id="batch-2", now=now)
    assert first.result_hash == second.result_hash
    assert first.validation_status == "READY"
    assert len(first.combinations) == 1
    assert first.combinations[0].status == "COMPLETED"
    assert first.combinations[0].orders == second.combinations[0].orders
    assert (
        first.combinations[0].execution_result_hash
        == second.combinations[0].execution_result_hash
    )
    html = render_backtest_html(first)
    assert "统一策略回测与参数比较报告" in html
    assert 'id="reportData"' in html
    assert "compute_turtle_indicators" not in html
    assert first.schema_version == "2"
    assert len(first.combinations[0].execution_result_hash) == 64
    report = build_report_model(first)
    assert report.schema_version == "1"
    assert report.basic_information["strategy_id"] == "turtle"
    assert report.charts["trade_distribution"]["trade_count"] >= 0


def test_insufficient_history_never_claims_best_parameters() -> None:
    plan = _plan(
        parameter_space={"rules.fast_entry": (20,), "rules.slow_entry": (55,)},
        validation=ValidationPolicy(756, 252, 252, 252, 3),
    )
    result = BacktestBatchService().run(
        _source(), plan, {"TEST": _bars(300)}, run_id="short", now=datetime(2026, 8, 31, tzinfo=timezone.utc)
    )
    assert result.validation_status == "INSUFFICIENT_HISTORY"
    assert result.best_combination_id is None
    assert result.stable_combination_ids == ()


def test_execution_hash_is_independent_from_ranking_and_report_fields() -> None:
    plan = _plan(parameter_space={"rules.fast_entry": (20,), "rules.slow_entry": (55,)})
    batch = BacktestBatchService().run(
        _source(), plan, {"TEST": _bars()}, run_id="hash", now=datetime(2026, 8, 31, tzinfo=timezone.utc)
    )
    original = batch.combinations[0]
    reranked = replace(original, score=99.0, rank=9, overfit_risk={"level": "HIGH"})
    assert original.execution_result_hash == reranked.execution_result_hash
    assert original.result_hash != reranked.result_hash


def test_v1_report_is_read_only_normalized_to_the_v2_view() -> None:
    legacy = {
        "schema_version": "1", "run_id": "old", "report_date": "2026-08-31",
        "source": {"strategy_version": "corrected-v2", "instruments": []},
        "plan": {"parameter_space": {}}, "validation_status": "READY",
        "best_combination_id": "组合-old", "stable_combination_ids": [],
        "combinations": [{
            "combination_id": "组合-old", "status": "COMPLETED", "rank": 1,
            "score": 1.0, "qualified": True, "parameters": {},
            "metrics": {"cagr": 0.2, "sharpe": 1.1, "trade_count": 3},
            "validation_metrics": {"cagr": 0.1, "sharpe": 0.8},
            "holdout_metrics": {}, "folds": [], "equity_curve": [],
            "drawdown_curve": [], "trades": [], "long_metrics": {},
            "short_metrics": {}, "unavailable": {}, "overfit_risk": {"level": "LOW"},
        }],
    }
    model = build_report_model(legacy)
    assert model.batch_schema_version == "1"
    assert model.ranking[0]["metrics"]["annualized_return"] == 0.2
    assert model.ranking[0]["metrics"]["sharpe_ratio"] == 1.1
    assert model.conclusion["key_issues"][0]["code"] == "LEGACY_RESULT"


def test_artifacts_use_v2_layout_hashes_and_regenerable_report(tmp_path) -> None:
    plan = _plan(parameter_space={"rules.fast_entry": (20,), "rules.slow_entry": (55,)})
    batch = BacktestBatchService().run(
        _source(), plan, {"TEST": _bars()}, run_id="artifact-v2",
        now=datetime(2026, 8, 31, tzinfo=timezone.utc),
    )
    writer = BacktestArtifactWriter(tmp_path / "backtest")
    paths = writer.write(batch, lineage={"TEST": {"dataset_version": "version-1"}})
    assert "04_可视化报告" in paths["report"]
    assert paths["report"].endswith("参数比较报告_v2.html")
    index = json.loads(open(paths["index"], encoding="utf-8").read())
    assert index["schema_version"] == "2"
    assert index["execution_result_hashes"][batch.combinations[0].combination_id]
    assert (tmp_path / "backtest" / "历史回测归档索引_v1.json").is_file()
    regenerated = writer.regenerate_report(paths["batch_result"], tmp_path / "重新生成报告_v2.html")
    assert "统一策略回测与参数比较报告" in regenerated.read_text(encoding="utf-8")
