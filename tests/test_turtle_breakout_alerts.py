from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from io import StringIO

import pandas as pd
import pytest

from inv_trend.adapters.detector.alerts import StructuredLoggingNotifier
from inv_trend.adapters.detector.alerts.breakout import TurtleBreakoutAlertScanner
from inv_trend.adapters.detector.alerts.breakout import AlertScanResult, ScanState
from inv_trend.adapters.detector.alerts.report import render_and_write_report, status_table
from inv_trend.adapters.detector.models import AssetConfig, Direction, Market, SignalType
from inv_trend.adapters.detector.storage import InMemorySignalRepository


def _asset() -> AssetConfig:
    return AssetConfig(
        symbol="TEST", instrument="TEST_SPOT", market=Market.CRYPTO,
        data_source="repository", timeframes=("D1",), adjustment="none",
    )


def _bars(last_close: float, *, complete: bool = True) -> pd.DataFrame:
    # 55 prior bars span [90, 110]; the final bar is evaluated against them.
    closes = [100.0] * 55 + [last_close]
    frame = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01", periods=56, tz="UTC", freq="D"),
        "open": closes,
        "high": [110.0] * 55 + [max(last_close, 100.0)],
        "low": [90.0] * 55 + [min(last_close, 100.0)],
        "close": closes,
        "volume": [1_000.0] * 56,
        "is_complete": [True] * 55 + [complete],
        "dataset_version": ["v-real"] * 56,
    })
    frame.attrs["dataset_version"] = "v-real"
    return frame


@pytest.mark.parametrize(
    ("price", "direction"), [(111.0, Direction.LONG), (89.0, Direction.SHORT)]
)
def test_20_and_55_up_and_down_breakouts(price: float, direction: Direction) -> None:
    repository = InMemorySignalRepository()
    signals = TurtleBreakoutAlertScanner(repository).scan_bars(_bars(price), _asset())
    assert [(s.signal_type, s.direction) for s in signals] == [
        (SignalType.SYSTEM1_BREAKOUT, direction),
        (SignalType.SYSTEM2_BREAKOUT, direction),
    ]
    assert {s.metadata["breakout_level"] for s in signals} == ({110.0} if price > 100 else {90.0})
    assert all(s.metadata["data_version"] == "v-real" for s in signals)


def test_no_breakout_emits_nothing() -> None:
    assert TurtleBreakoutAlertScanner(InMemorySignalRepository()).scan_bars(
        _bars(100.0), _asset()
    ) == ()


def test_repeat_scan_is_deduplicated_by_business_signal() -> None:
    repository = InMemorySignalRepository()
    scanner = TurtleBreakoutAlertScanner(repository)
    assert len(scanner.scan_bars(_bars(111.0), _asset())) == 2
    revised = _bars(112.0)
    revised.attrs["dataset_version"] = "v-revised"
    assert scanner.scan_bars(revised, _asset()) == ()
    assert len(repository.signals) == 2


def test_incomplete_latest_bar_cannot_trigger() -> None:
    signals = TurtleBreakoutAlertScanner(InMemorySignalRepository()).scan_bars(
        _bars(111.0, complete=False), _asset()
    )
    assert signals == ()


def test_channel_excludes_current_bar() -> None:
    signals = TurtleBreakoutAlertScanner(InMemorySignalRepository()).scan_bars(
        _bars(111.0), _asset()
    )
    assert [s.metadata["breakout_level"] for s in signals] == [110.0, 110.0]


def test_structured_log_contains_required_alert_fields(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="test.alert")
    logger = logging.getLogger("test.alert")
    scanner = TurtleBreakoutAlertScanner(
        InMemorySignalRepository(), StructuredLoggingNotifier(logger)
    )
    scanner.scan_bars(_bars(111.0), _asset())
    payload = json.loads(caplog.records[0].message)
    assert payload["symbol"] == "TEST"
    assert payload["signal_time"]
    assert payload["trigger_price"] == 111.0
    assert payload["signal_type"] == "SYSTEM1_BREAKOUT"
    assert payload["metadata"]["breakout_level"] == 110.0
    assert payload["metadata"]["data_version"] == "v-real"


def test_status_contains_all_channel_values_and_membership() -> None:
    _, status = TurtleBreakoutAlertScanner(InMemorySignalRepository()).scan_bars_with_status(
        _bars(111.0), _asset(), universe={"funds": "QQQ,SPY", "ranks": "QQQ:1,SPY:2"}
    )
    assert status.state is ScanState.BREAKOUT_UP
    assert status.signals == ("20日向上", "55日向上")
    assert status.funds == "QQQ,SPY"
    assert status.ranks == "QQQ:1,SPY:2"
    assert status.alert_emitted is True


def test_incomplete_history_has_explicit_status() -> None:
    _, status = TurtleBreakoutAlertScanner(InMemorySignalRepository()).scan_bars_with_status(
        _bars(100.0).iloc[:20], _asset()
    )
    assert status.state is ScanState.INSUFFICIENT_HISTORY
    assert "got 20" in status.error


def test_table_color_and_daily_logs(tmp_path) -> None:
    _, status = TurtleBreakoutAlertScanner(InMemorySignalRepository()).scan_bars_with_status(
        _bars(111.0), _asset()
    )
    table = status_table([status])
    from rich.console import Console
    colored = StringIO()
    Console(file=colored, force_terminal=True, color_system="standard").print(table)
    assert "\x1b[" in colored.getvalue()
    result = AlertScanResult(1, (), (), (status,))
    plain = StringIO()
    text_path, jsonl_path = render_and_write_report(
        result, tmp_path, no_color=True,
        now=datetime(2024, 1, 2, tzinfo=timezone.utc),
        console=Console(file=plain, no_color=True),
    )
    assert text_path.name == "2024-01-02.log"
    assert "\x1b[" not in text_path.read_text(encoding="utf-8")
    row = json.loads(jsonl_path.read_text(encoding="utf-8").splitlines()[0])
    assert row["symbol"] == "TEST"
    assert row["alert_emitted"] is True
