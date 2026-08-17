from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from inv_trend.application import BacktestService, load_resolved_run_config
from inv_trend.adapters.multi_asset import AssetSpec, TurtleBacktester, TurtleRules
from inv_trend.adapters.multi_asset.config import BacktestConfig


def _bars() -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=80, freq="D", tz="UTC")
    close = np.linspace(100.0, 180.0, len(index))
    return pd.DataFrame(
        {
            "open": np.r_[close[0], close[:-1]],
            "high": np.maximum(np.r_[close[0], close[:-1]], close) + 1.0,
            "low": np.minimum(np.r_[close[0], close[:-1]], close) - 1.0,
            "close": close,
        },
        index=index,
    )


def test_strategy_config_is_immutable_and_does_not_own_instrument_identity() -> None:
    path = Path("src/inv_trend/application/config/strategy.yaml")
    config = load_resolved_run_config(path)
    assert config.profile == "corrected-v2"
    assert "instrument" not in config.contracts
    with pytest.raises(TypeError):
        config.rules["n_period"] = 10  # type: ignore[index]


def test_backtest_service_preserves_backtester_result_and_writes_manifest(tmp_path: Path) -> None:
    bars = _bars()
    rules = TurtleRules(
        n_period=10, fast_entry=10, slow_entry=20, fast_exit=5, slow_exit=10,
        skip_fast_after_win=False,
    )
    specs = {"TEST": AssetSpec("TEST", "etf", "index", qty_step=1)}
    config = BacktestConfig(initial_equity=100_000, code_version="test")
    direct = TurtleBacktester({"TEST": bars}, specs, rules, config=config).run()
    manifest_path = tmp_path / "manifest.json"
    run = BacktestService().run(
        {"TEST": bars}, specs, rules, config=config, manifest_path=manifest_path
    )
    assert run.result.metrics == pytest.approx(direct.metrics, rel=1e-10)
    pd.testing.assert_frame_equal(run.result.orders, direct.orders, check_exact=False)
    assert manifest_path.exists()
    assert run.manifest.mode == "backtest"
    assert run.manifest.config_fingerprint
    assert run.manifest.data_fingerprint
