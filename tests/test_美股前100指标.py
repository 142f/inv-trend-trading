"""Fixture checks for the QQQ/SPY daily snapshot口径. No network."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from importlib.machinery import SourceFileLoader
import json
from pathlib import Path

import pandas as pd

from inv_trend.data.calendar import us_session_bounds
from inv_trend.data.models import InstrumentConfig

ROOT = Path(__file__).resolve().parents[1]
board = SourceFileLoader("us_top100_board", str(ROOT / "scripts" / "美股前100指标.py")).load_module()


def _instrument() -> InstrumentConfig:
    return InstrumentConfig(
        symbol="TEST", source_symbol="TEST", asset_class="equity", market="xnas",
        instrument_type="equity", quote_currency="USD", timezone="America/New_York",
        session="regular", primary_source="yahoo_chart",
        price_basis="provider_adjusted_close", adjustment_method="provider_adjusted_close",
        session_timezone="America/New_York",
    )


def _sessions(count: int, end: str = "2026-10-26") -> list[pd.Timestamp]:
    day = pd.Timestamp(end).date()
    found: list[pd.Timestamp] = []
    while len(found) < count:
        if us_session_bounds(day, market="xnas") is not None:
            found.append(pd.Timestamp(datetime(day.year, day.month, day.day, 14, 30, tzinfo=timezone.utc)))
        day -= timedelta(days=1)
    return list(reversed(found))


def _frame(stamps: list[pd.Timestamp], *, volume: list[float] | None = None, factor: float = 1.0) -> pd.DataFrame:
    count = len(stamps)
    close = [100.0 + index for index in range(count)]
    volumes = volume if volume is not None else [1000.0] * count
    return pd.DataFrame({
        "timestamp": stamps,
        "open": close,
        "high": [item + 1 for item in close],
        "low": [item - 1 for item in close],
        "close": close,
        "volume": volumes,
        "adjusted_close": [item * factor for item in close],
    })


def _empty_page(partial: bool) -> str:
    return board._page({
        "partial": partial, "rows": [], "failed": [], "incomplete": [],
        "universe": "QQQ+SPY", "price_basis": "provider_adjusted_close",
        "data_cutoff": "2026-10-26T20:00:00Z", "as_of": "2026-10-26",
    })


def test_open_bar_is_excluded_unless_partial_mode():
    now = datetime(2026, 10, 27, 18, 0, tzinfo=timezone.utc)
    closed = board.resolve_window(now, None, completed_only=True)
    assert closed["partial_mode"] is False
    assert closed["fetch_end"] <= now
    assert closed["fetch_end"] == closed["completeness_now"]
    try:
        board.resolve_window(now, "2026-10-27", completed_only=True)
        raise AssertionError("an unclosed session date must not be treated as complete")
    except ValueError:
        pass
    still = board.resolve_window(now, "2026-10-26", completed_only=True)
    assert still["fetch_end"] == board.parse_as_of("2026-10-26", now)
    assert still["fetch_end"] <= now

    opened = board.resolve_window(now, None, completed_only=False)
    assert opened["partial_mode"] is True
    assert opened["fetch_end"] <= now
    assert opened["completeness_now"] < board._session_end(datetime(2026, 10, 27).date())
    dated = board.resolve_window(now, "2026-10-27", completed_only=False)
    assert dated == opened
    instant = board.resolve_window(now, "2026-10-27T18:00:00Z", completed_only=False)
    assert instant["partial_mode"] is True
    assert instant["fetch_end"] == now
    assert instant["completeness_now"] == now

    raw = _frame(_sessions(8, "2026-10-27"))
    used, _counts = board.select_completed_bars(
        board.clip_fetch(raw, closed["fetch_end"]), _instrument(), "D1", "yahoo_chart",
        now=closed["completeness_now"], completed_only=True,
    )
    last = pd.Timestamp(used["timestamp"].iloc[-1]).tz_convert("America/New_York").date().isoformat()
    assert last < "2026-10-27"
    kept, counts = board.select_completed_bars(
        board.clip_fetch(raw, opened["fetch_end"]), _instrument(), "D1", "yahoo_chart",
        now=opened["completeness_now"], completed_only=False,
    )
    kept_day = pd.Timestamp(kept["timestamp"].iloc[-1]).tz_convert("America/New_York").date().isoformat()
    assert kept_day == "2026-10-27"
    assert counts["partial_rows"] >= 1
    quiet = _empty_page(False)
    loud = _empty_page(True)
    assert "盘中快照" not in quiet
    assert 'class="banner show"' in loud and "盘中快照" in loud and "hidden" not in loud


def test_missing_volume_is_not_zero_and_relative_volume_is_empty():
    stamps = _sessions(30)
    frame = _frame(stamps)
    frame.loc[frame.index[-1], "volume"] = None
    now = board.session_end_cutoff(datetime(2026, 10, 26, 22, tzinfo=timezone.utc))
    used, counts = board.select_completed_bars(frame, _instrument(), "D1", "yahoo_chart", now=now)
    assert counts["volume_missing_count"] >= 1
    assert used["volume"].isna().any()
    assert not (used["volume"].fillna(-1) == 0).all()
    features = board._feature_frame(used)
    prepared = board.PreparedBars.build(features, board.REQUEST).frame
    assert board.relative_volume_value(prepared.iloc[-1]["volume"], prepared.iloc[-1]["volume_sma_20_lag_1"]) is None


def test_adjustment_scales_indicator_ohlc_and_keeps_raw_close():
    stamps = _sessions(5)
    used, _counts = board.select_completed_bars(
        _frame(stamps, factor=0.5), _instrument(), "D1", "yahoo_chart",
        now=board.parse_as_of("2026-10-26"),
    )
    assert used["unadjusted_close"].iloc[-1] == 104
    assert abs(used["close"].iloc[-1] - 52) < 1e-9
    assert abs(used["adjustment_factor"].iloc[-1] - 0.5) < 1e-9


def test_short_history_is_incomplete_and_hides_long_indicators():
    stamps = _sessions(94)
    now = board.parse_as_of("2026-10-26")
    used, _counts = board.select_completed_bars(
        _frame(stamps), _instrument(), "D1", "yahoo_chart", now=now,
    )
    prepared = board.PreparedBars.build(board._feature_frame(used), board.REQUEST).frame
    row = board.indicator_snapshot("HONA", prepared, {"funds": "QQQ", "qqq_rank": 76, "spy_rank": None}, partial=False)
    assert row["warmup_ok"] is False
    assert row["bar_count"] < 169
    assert row["sma"]["120"] is None
    assert row["ema"]["144"] is None
    assert row["ema"]["169"] is None
    assert board.incomplete_entry(row)["reason"] == "warmup<169"


def test_lag1_relative_volume_excludes_the_current_bar():
    stamps = _sessions(40)
    volumes = [10.0] * 39 + [100.0]
    now = board.parse_as_of("2026-10-26")
    used, _counts = board.select_completed_bars(
        _frame(stamps, volume=volumes), _instrument(), "D1", "yahoo_chart", now=now,
    )
    prepared = board.PreparedBars.build(board._feature_frame(used), board.REQUEST).frame
    lag1 = board.relative_volume_value(prepared.iloc[-1]["volume"], prepared.iloc[-1]["volume_sma_20_lag_1"])
    same_bar = board.FeatureRequest(
        volume_sma_lags=((20, 0),),
    )
    included = board.PreparedBars.build(board._feature_frame(used), same_bar).frame
    lag0 = board.relative_volume_value(included.iloc[-1]["volume"], included.iloc[-1]["volume_sma_20_lag_0"])
    assert lag1 == 10
    assert lag0 != lag1


def test_receipt_records_the_phase1_fields():
    members = board.universe_members()
    assert len(members) == 163
    assert all(item["symbol"] != "SPCX" for item in members)
    wmt = next(item for item in members if item["symbol"] == "WMT")
    assert wmt["qqq_rank"] == 12
    receipt = board.build_receipt(
        generated_at="2026-10-28T00:00:00Z",
        data_cutoff="2026-10-26T20:00:00Z",
        provider="yahoo_chart",
        completed_only=True,
        universe={"id": "qqq_spy_top100_20261023", "hash": "abc", "count": 163},
        config_hash="def",
        script_git_commit="abc123",
        counts={"rows": 163, "failed": 0, "incomplete": 1, "partial": 0},
        failed=[],
        incomplete=[{"symbol": "HONA", "bar_count": 93, "reason": "warmup<169"}],
        content_hash="fff",
        bar_count={"total": 15000, "by_symbol": {"HONA": 93}},
        raw_hash="aaa",
    )
    for key in (
        "provider", "research_only", "data_cutoff", "completed_only", "feature_request",
        "price_basis", "universe", "config_hash", "counts", "content_hash",
        "bar_count", "raw_hash",
    ):
        assert key in receipt
    assert receipt["bar_count"]["by_symbol"]["HONA"] == 93
    assert receipt["raw_hash"] == "aaa"
    assert receipt["price_basis"] == "provider_adjusted_close"
    assert receipt["research_only"] is True
    assert receipt["counts"]["partial"] == 0
    assert receipt["feature_request"]["relative_volume"] == "volume / volume_sma_20_lag_1"


def test_snapshot_exposes_the_full_display_indicator_set():
    stamps = _sessions(200)
    now = board.parse_as_of("2026-10-26")
    used, _counts = board.select_completed_bars(
        _frame(stamps), _instrument(), "D1", "yahoo_chart", now=now,
    )
    prepared = board.PreparedBars.build(board._feature_frame(used), board.REQUEST).frame
    row = board.indicator_snapshot("TEST", prepared, {"funds": "QQQ", "qqq_rank": 1, "spy_rank": None}, partial=False)
    assert row["warmup_ok"] is True
    for key in ("plus_di", "minus_di", "histogram", "macd_bar", "volume", "volume_sma_20", "atr"):
        assert key in row
    assert set(row["channel"]) == {"high_20", "low_20", "high_55", "low_55"}
    assert row["channel"]["high_20"] is not None


def test_universe_is_loaded_from_the_registered_file():
    assert board.UNIVERSE_PATH.is_file()
    payload = json.loads(board.UNIVERSE_PATH.read_text(encoding="utf-8"))
    assert payload["id"] == board.UNIVERSE_ID
    assert payload["count"] == len(payload["members"]) == 163
    assert payload["exclusions"] == [{"symbol": "SPCX", "qqq_rank": 11,
                                      "reason": "not registered in assets.yaml"}]
    assert board.universe_members() == payload["members"]


def test_page_carries_meta_header_and_prefix_search_contract():
    page = board._page(
        {"partial": False, "rows": [], "failed": [], "incomplete": [], "universe": "QQQ+SPY",
         "price_basis": "provider_adjusted_close", "data_cutoff": "2026-10-26T20:00:00Z",
         "as_of": "2026-10-26"},
        {"provider": "yahoo_chart", "data_cutoff": "2026-10-26T20:00:00Z",
         "price_basis": "provider_adjusted_close", "config_hash": "cfg-abc",
         "content_hash": "hash-fff", "generated_at": "2026-10-28T00:00:00Z",
         "script_git_commit": "abc123"},
    )
    for text in ("数据来源", "yahoo_chart", "内容哈希", "hash-fff", "价格口径", "脚本提交", "abc123"):
        assert text in page
    assert "indexOf(state.q) !== 0" in page
    assert "清空筛选" in page
    assert "通道 20 日" in page and "MACD 柱" in page and "−DI" in page
