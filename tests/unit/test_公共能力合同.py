from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timedelta, timezone
import hashlib

import numpy as np
import pandas as pd
import pytest

from inv_trend.core.identity import stable_id
from inv_trend.core.math_utils import finite_float
from inv_trend.core.signals import SignalEvent, is_execution_notification_event
from inv_trend.data.integrity import sha256_file, stable_hash
from inv_trend.data.文件操作 import atomic_write
from inv_trend.core.时间 import utc_datetime, utc_timestamp


@pytest.mark.parametrize(
    "value",
    [
        datetime(2024, 1, 1, 0, 0, 0, 123456),
        datetime(2024, 1, 1, 0, 0, 0, 123456, tzinfo=timezone.utc),
        datetime(2024, 1, 1, 8, 0, 0, 123456, tzinfo=timezone(timedelta(hours=8))),
    ],
)
def test_shared_utc_normalization_preserves_instant_and_precision(value):
    expected = datetime(2024, 1, 1, 0, 0, 0, 123456, tzinfo=timezone.utc)
    assert utc_datetime(value) == expected
    assert utc_timestamp(value) == pd.Timestamp(expected)
    assert utc_timestamp(pd.Timestamp(value) + pd.Timedelta(7, unit="ns")) == (
        pd.Timestamp(expected) + pd.Timedelta(7, unit="ns")
    )


@pytest.mark.parametrize("symbols", [None, [], (), "BTC"])
def test_staging_symbols_rejects_missing_or_non_list_selection(symbols):
    from inv_trend.application.daily.stage_payloads import context_symbols

    with pytest.raises(ValueError, match="staging context has no selected symbols"):
        context_symbols({"symbols": symbols})


def test_staging_symbols_preserves_order_duplicates_and_scalar_conversion():
    from inv_trend.application.daily.stage_payloads import context_symbols

    values = ["ETH", "BTC", "ETH", 42]
    assert context_symbols({"symbols": values}) == ["ETH", "BTC", "ETH", "42"]
    assert values == ["ETH", "BTC", "ETH", 42]


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, None),
        (pd.NA, None),
        ("bad", None),
        (float("nan"), None),
        (float("inf"), None),
        (-float("inf"), None),
        ("1.25", 1.25),
        (Decimal("2.5"), 2.5),
        (np.float32(3.5), 3.5),
        (True, 1.0),
    ],
)
def test_finite_float_preserves_report_and_strategy_scalar_contract(value, expected):
    assert finite_float(value) == expected


def test_stable_id_and_payload_hash_stay_order_sensitive_and_canonical() -> None:
    assert stable_id("turtle", "20", "2024-01-01") != stable_id("turtle", "55", "2024-01-01")
    assert stable_id("a", 1) == stable_id("a", "1")
    assert len(stable_id("a", "b")) == 24
    payload = {"b": 1, "a": ["x", 2]}
    assert stable_hash(payload) == stable_hash({"a": ["x", 2], "b": 1})
    assert stable_hash(payload) == hashlib.sha256(
        b'{"a":["x",2],"b":1}'
    ).hexdigest()


def test_execution_notification_predicate_accepts_only_confirmed_entries() -> None:
    def event(signal_type: str, indicator: str = "execution_decision") -> SignalEvent:
        return SignalEvent(
            signal_id="1",
            signal_key="k",
            instrument_id="i",
            symbol="BTC",
            timeframe="D1",
            signal_type=signal_type,
            direction="long",
            signal_time="t",
            detected_at="t",
            trigger_price=1.0,
            reference_value=None,
            dataset_version="v",
            indicator_name=indicator,
        )

    assert is_execution_notification_event(event("ENTRY_DECISION_LONG"))
    assert is_execution_notification_event(event("ENTRY_DECISION_SHORT"))
    assert not is_execution_notification_event(event("EXIT_DECISION_LONG"))
    assert not is_execution_notification_event(event("ENTRY_DECISION_LONG", "turtle_20"))


def test_failed_atomic_replace_preserves_previous_artifact_and_cleans_stage(tmp_path, monkeypatch):
    from inv_trend.data import 文件操作

    path = tmp_path / "snapshot.json"
    path.write_bytes(b"previous")

    def fail(*args):
        raise PermissionError("target is in use")

    monkeypatch.setattr(文件操作.os, "replace", fail)
    with pytest.raises(PermissionError, match="in use"):
        atomic_write(path, b"new")
    assert path.read_bytes() == b"previous"
    assert list(tmp_path.iterdir()) == [path]


def test_immutable_write_rechecks_target_created_during_staging(tmp_path, monkeypatch):
    from inv_trend.data import 文件操作

    path = tmp_path / "immutable.json"
    original = 文件操作.os.fsync

    def competing_writer(descriptor):
        original(descriptor)
        path.write_bytes(b"other writer")

    monkeypatch.setattr(文件操作.os, "fsync", competing_writer)
    with pytest.raises(FileExistsError):
        atomic_write(path, b"ours", replace=False)
    assert path.read_bytes() == b"other writer"
    assert list(tmp_path.iterdir()) == [path]


def test_streaming_hash_handles_empty_and_multi_block_files(tmp_path):
    path = tmp_path / "data.bin"
    for payload in (b"", b"a" * (2 * 1024 * 1024 + 17)):
        path.write_bytes(payload)
        assert sha256_file(path) == hashlib.sha256(payload).hexdigest()
        assert sha256_file(str(path)) == sha256_file(path)


def test_atomic_workspace_json_preserves_utf8_and_lf(tmp_path):
    from inv_trend.application.daily.workspace import _atomic_write_json

    path = tmp_path / "context.json"
    _atomic_write_json(path, {"label": "中文", "count": 1})
    assert path.read_bytes() == '{\n  "label": "中文",\n  "count": 1\n}\n'.encode("utf-8")
    with pytest.raises(ValueError):
        _atomic_write_json(path, {"count": float("nan")})
    assert b'"count": 1' in path.read_bytes()


def test_coverage_uses_the_same_bar_snapshot_for_status_and_lineage(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from inv_trend.data import api

    bars = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=2, tz="UTC"),
            "is_complete": [True, False],
        }
    )
    service = object.__new__(api.HistoricalDataService)
    service.lake = SimpleNamespace(
        current_version=Mock(return_value={"version": "v1"}),
        read_bars=Mock(return_value=bars),
    )

    def verify(lake, symbol, timeframe, *, bars):
        assert bars is service.lake.read_bars.return_value
        return SimpleNamespace(
            quality_report_path="quality.json", quality_report={"missing_intervals": []}
        )

    monkeypatch.setattr(api, "load_current_lineage", verify)
    result = service.coverage("btc", "d1")
    service.lake.read_bars.assert_called_once_with("BTC", "D1")
    assert result["version"] == "v1"
    assert result["latest_complete_bar"] == "2024-01-01T00:00:00+00:00"
    assert result["actual_end"] == "2024-01-02T00:00:00+00:00"


def test_chart_payload_preserves_missing_values_timezones_and_input():
    from inv_trend.application.daily_analysis import _series_payload

    frame = pd.DataFrame(
        {"close": ["1.25", pd.NA, float("inf")], "volume": [1, 2, 3]},
        index=pd.date_range("2024-01-01", periods=3, tz="Asia/Shanghai"),
    )
    original = frame.copy(deep=True)
    payload = _series_payload(frame)
    assert [row["close"] for row in payload] == [1.25, None, None]
    assert [row["volume"] for row in payload] == [1.0, 2.0, 3.0]
    assert all(row["open"] is None for row in payload)
    assert payload[0]["timestamp"] == "2024-01-01T00:00:00+08:00"
    assert _series_payload(frame.iloc[:0]) == []
    pd.testing.assert_frame_equal(frame, original)
