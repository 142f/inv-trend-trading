from __future__ import annotations

from datetime import datetime, timezone

from turtle_multi_asset.mt5_data import _infer_asset_fields, _to_utc_datetime


def test_infer_asset_fields_for_core_assets() -> None:
    assert _infer_asset_fields("XAUUSD")["cluster"] == "precious_metals"
    assert _infer_asset_fields("XAGUSD")["max_units"] == 2
    assert _infer_asset_fields("BTCUSD")["cluster"] == "crypto"
    assert _infer_asset_fields("SPY.US")["cluster"] == "us_equity"


def test_to_utc_datetime_from_string() -> None:
    dt = _to_utc_datetime("2024-01-01")
    assert dt.tzinfo is not None
    assert dt.year == 2024


def test_to_utc_datetime_from_aware_datetime() -> None:
    source = datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert _to_utc_datetime(source) == source
