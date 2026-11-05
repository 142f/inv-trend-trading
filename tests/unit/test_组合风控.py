"""Allocation, portfolio caps and the decision-desk execution block."""

from __future__ import annotations

from inv_trend.application.market_panels.组合风控 import DEFAULT_CAPS, allocate_portfolio
from inv_trend.observability.静态展示.决策面板 import render_decision_desk


def _row(symbol: str, *, grade: str = "A", direction: str = "long", close: float = 100.0,
         stop: float = 96.0, qty: int = 100, triggered: bool = True,
         warmup_ok: bool = True, score: float = 9.0, adx: float = 30.0,
         rel_vol: float = 1.6, funds: str = "QQQ") -> dict:
    return {
        "symbol": symbol,
        "funds": funds,
        "close": close,
        "adx": adx,
        "relative_volume": rel_vol,
        "warmup_ok": warmup_ok,
        "decision": {
            "rating": {"grade": grade, "direction": direction, "score": score},
            "breakouts": [
                {"period": 20, "direction": direction, "level": close - 2.0,
                 "distance_pct": -2.0 if triggered else 1.0, "triggered": triggered},
            ],
            "stops": {"long": stop, "short": close + abs(close - stop)},
            "suggested_qty": {"long": qty, "short": qty},
        },
    }


def test_allocation_respects_total_risk_cap() -> None:
    rows = [_row(f"S{i}", qty=1000) for i in range(40)]
    result = allocate_portfolio(
        rows, equity=100_000.0, risk_pct=0.005, stop_n=2.0,
        # Only the total risk cap may bind in this scenario.
        caps={"max_total_1n_risk_pct": 0.12, "max_direction_1n_risk_pct": 10.0,
              "max_total_leverage": 100.0, "max_direction_leverage": 100.0},
    )
    risk = result["totals"]["risk_amount"]
    assert risk <= 100_000.0 * 0.12 + 1e-6
    assert result["totals"]["risk_pct"] <= 12.0 + 1e-6
    # Budget must actually be spent, not left unused.
    assert risk > 100_000.0 * 0.12 - 100.0


def test_allocation_respects_direction_cap() -> None:
    rows = [_row(f"S{i}", qty=500) for i in range(30)]
    result = allocate_portfolio(rows, equity=100_000.0, risk_pct=0.005, stop_n=2.0,
                                caps={"max_total_1n_risk_pct": 1.0,
                                      "max_direction_1n_risk_pct": 0.08,
                                      "max_total_leverage": 10.0,
                                      "max_direction_leverage": 10.0})
    assert result["totals"]["long"]["risk_amount"] <= 100_000.0 * 0.08 + 1e-6
    assert result["totals"]["short"]["risk_amount"] == 0


def test_allocation_respects_leverage_cap() -> None:
    rows = [_row("CHEAP", close=10.0, stop=9.9, qty=100_000)]
    result = allocate_portfolio(rows, equity=100_000.0, risk_pct=0.005, stop_n=2.0,
                                caps={"max_total_1n_risk_pct": 1.0,
                                      "max_direction_1n_risk_pct": 1.0,
                                      "max_total_leverage": 2.0,
                                      "max_direction_leverage": 2.0})
    assert result["totals"]["notional"] <= 100_000.0 * 2.0 + 1e-6


def test_allocation_prefers_stronger_grade_and_keeps_priority_order() -> None:
    rows = [
        _row("WEAK", grade="B", score=5.0, adx=20.0, rel_vol=1.0, qty=10),
        _row("STRONG", grade="A", score=10.0, adx=35.0, rel_vol=2.0, qty=10),
    ]
    result = allocate_portfolio(rows, equity=100_000.0, risk_pct=0.005, stop_n=2.0)
    assert [item["symbol"] for item in result["allocations"]] == ["STRONG", "WEAK"]


def test_allocation_skips_untriggered_and_warmup_rows() -> None:
    rows = [
        _row("TRIG"),
        _row("NOTRIG", triggered=False),
        _row("WARM", warmup_ok=False),
    ]
    result = allocate_portfolio(rows, equity=100_000.0, risk_pct=0.005, stop_n=2.0)
    assert [item["symbol"] for item in result["allocations"]] == ["TRIG"]
    reasons = {item["symbol"]: item["reason"] for item in result["excluded"]}
    assert reasons["NOTRIG"] == "未触发"
    assert reasons["WARM"] == "预热不足"


def test_allocation_marks_capped_rows_and_keeps_suggestion() -> None:
    rows = [_row("BIG", qty=1000)]
    result = allocate_portfolio(rows, equity=100_000.0, risk_pct=0.005, stop_n=2.0,
                                caps={"max_total_1n_risk_pct": 0.01})
    allocation = result["allocations"][0]
    assert allocation["capped"] is True
    assert allocation["qty"] < allocation["suggested_qty"] == 1000
    assert result["caps_hit"]


def test_allocation_rejects_non_positive_equity() -> None:
    result = allocate_portfolio([_row("A")], equity=0.0, risk_pct=0.005, stop_n=2.0)
    assert result["allocations"] == []
    assert result["totals"]["positions"] == 0


def test_allocation_uses_default_caps_when_absent() -> None:
    result = allocate_portfolio([_row("A")], equity=100_000.0, risk_pct=0.005, stop_n=2.0)
    assert result["caps"] == DEFAULT_CAPS


def test_decision_desk_renders_portfolio_and_tickets() -> None:
    payload = {
        "as_of": "2026-11-05",
        "rows": [_row("AAPL"), _row("MSFT", grade="B", qty=50)],
        "failed": [],
        "incomplete": [],
        "market": {},
        # A tight budget forces a trimmed row so the caps block must render.
        "portfolio": allocate_portfolio(
            [_row("AAPL"), _row("MSFT", grade="B", qty=50)],
            equity=100_000.0, risk_pct=0.005, stop_n=2.0,
            caps={"max_total_1n_risk_pct": 0.0025, "max_direction_1n_risk_pct": 10.0,
                  "max_total_leverage": 100.0, "max_direction_leverage": 100.0},
        ),
    }
    html = render_decision_desk(payload, {"equity": 100_000.0})
    for text in ("组合执行预算", "今日可执行清单", "AAPL", "复制全部票据", "导出 CSV", "预算裁剪"):
        assert text in html, text
    # A capped row keeps both the executable and the suggested size.
    assert "建议 100" in html


def test_decision_desk_without_portfolio_renders_no_execution_block() -> None:
    html = render_decision_desk({"rows": [], "failed": [], "incomplete": [], "market": {}}, {})
    assert "组合执行预算" not in html
