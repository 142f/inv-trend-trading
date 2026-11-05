"""Portfolio allocation and risk caps for the investment decision desk.

The desk derives one risk-budgeted quantity per symbol.  Real trading cannot take
every triggered symbol at full size: concurrent 1N risk, direction concentration
and gross leverage all have hard caps in the canonical strategy rules.  This
module converts the per-symbol suggestions into one executable plan by spending
the risk budget in a deterministic priority order and trimming (or skipping)
anything that would breach a cap.

It is a pure function over the snapshot rows so the panel and the tests share the
same numbers; no I/O, no global state.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

DEFAULT_CAPS: dict[str, float] = {
    "max_total_1n_risk_pct": 0.12,
    "max_direction_1n_risk_pct": 0.08,
    "max_total_leverage": 2.0,
    "max_direction_leverage": 1.5,
}

_GRADE_RANK = {"A": 0, "B": 1}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number == number and abs(number) != float("inf") else None


def _direction(row: Mapping[str, Any]) -> str | None:
    direction = _mapping(_mapping(row.get("decision")).get("rating")).get("direction")
    return direction if direction in ("long", "short") else None


def _grade(row: Mapping[str, Any]) -> str:
    grade = _mapping(_mapping(row.get("decision")).get("rating")).get("grade")
    return str(grade) if grade else "NONE"


def _triggered_distance(row: Mapping[str, Any], side: str) -> tuple[bool, float | None]:
    """Return (triggered, smallest absolute distance) for the row's own side."""

    entries = [
        item for item in _mapping(row.get("decision")).get("breakouts") or []
        if isinstance(item, Mapping) and item.get("direction") == side
    ]
    triggered = any(bool(item.get("triggered")) for item in entries)
    distances = [
        abs(float(item["distance_pct"]))
        for item in entries
        if _number(item.get("distance_pct")) is not None
    ]
    return triggered, (min(distances) if distances else None)


def _priority(row: Mapping[str, Any], side: str) -> tuple:
    """Sort key: strongest, most liquid, closest setup first, then symbol."""

    decision = _mapping(row.get("decision"))
    rating = _mapping(decision.get("rating"))
    triggered, distance = _triggered_distance(row, side)
    return (
        _GRADE_RANK.get(_grade(row), 2),
        0 if triggered else 1,
        -(_number(rating.get("score")) or 0.0),
        -(_number(row.get("adx")) or 0.0),
        -(_number(row.get("relative_volume")) or 0.0),
        distance if distance is not None else 1e9,
        str(row.get("symbol") or ""),
    )


def _empty_summary(equity: float, risk_pct: float, stop_n: float, caps: Mapping[str, float]) -> dict:
    return {
        "schema_version": 1,
        "equity": equity,
        "risk_pct": risk_pct,
        "stop_n": stop_n,
        "caps": dict(caps),
        "totals": {
            "positions": 0, "notional": 0.0, "risk_amount": 0.0, "risk_pct": 0.0,
            "leverage": 0.0,
            "long": {"positions": 0, "notional": 0.0, "risk_amount": 0.0, "risk_pct": 0.0},
            "short": {"positions": 0, "notional": 0.0, "risk_amount": 0.0, "risk_pct": 0.0},
        },
        "caps_hit": [],
        "allocations": [],
        "skipped": [],
        "excluded": [],
    }


def allocate_portfolio(
    rows: Iterable[Mapping[str, Any]],
    *,
    equity: float,
    risk_pct: float,
    stop_n: float,
    caps: Mapping[str, float] | None = None,
) -> dict:
    """Spend the risk budget across triggered rows and return the executable plan."""

    resolved_caps = {**DEFAULT_CAPS}
    for key in DEFAULT_CAPS:
        value = _number((caps or {}).get(key))
        if value is not None and value > 0:
            resolved_caps[key] = value
    if equity is None or equity <= 0:
        return _empty_summary(float(equity or 0.0), risk_pct, stop_n, resolved_caps)

    max_total_risk = equity * resolved_caps["max_total_1n_risk_pct"]
    max_side_risk = {
        side: equity * resolved_caps["max_direction_1n_risk_pct"] for side in ("long", "short")
    }
    max_total_notional = equity * resolved_caps["max_total_leverage"]
    max_side_notional = {
        side: equity * resolved_caps["max_direction_leverage"] for side in ("long", "short")
    }

    summary = _empty_summary(float(equity), float(risk_pct), float(stop_n), resolved_caps)
    totals = summary["totals"]
    caps_hit: list[dict[str, str]] = summary["caps_hit"]

    ranked: list[tuple[tuple, Mapping[str, Any], str]] = []
    for row in rows:
        side = _direction(row)
        if side is None:
            summary["excluded"].append({"symbol": str(row.get("symbol") or ""), "reason": "无方向"})
            continue
        triggered, _ = _triggered_distance(row, side)
        if not triggered:
            summary["excluded"].append({"symbol": str(row.get("symbol") or ""), "reason": "未触发"})
            continue
        if not bool(row.get("warmup_ok", True)):
            summary["excluded"].append({"symbol": str(row.get("symbol") or ""), "reason": "预热不足"})
            continue
        ranked.append((_priority(row, side), row, side))
    ranked.sort(key=lambda item: item[0])

    for _, row, side in ranked:
        symbol = str(row.get("symbol") or "")
        decision = _mapping(row.get("decision"))
        price = _number(row.get("close"))
        stop = _number(_mapping(decision.get("stops")).get(side))
        desired = _number(_mapping(decision.get("suggested_qty")).get(side))
        if price is None or price <= 0 or stop is None or desired is None or desired <= 0:
            summary["skipped"].append({"symbol": symbol, "side": side, "reason": "缺少价格/止损/股数"})
            continue
        risk_per_share = abs(price - stop)
        if risk_per_share <= 0:
            summary["skipped"].append({"symbol": symbol, "side": side, "reason": "止损等于入场价"})
            continue

        side_totals = totals[side]
        remaining_risk = min(
            max_total_risk - totals["risk_amount"],
            max_side_risk[side] - side_totals["risk_amount"],
        )
        remaining_notional = min(
            max_total_notional - totals["notional"],
            max_side_notional[side] - side_totals["notional"],
        )
        if remaining_risk <= 0 or remaining_notional <= 0:
            summary["skipped"].append({"symbol": symbol, "side": side, "reason": "风险预算已用尽"})
            caps_hit.append({"cap": "risk_or_leverage", "detail": f"{symbol} {side} 无剩余额度"})
            continue

        risk_cap_qty = int(remaining_risk // risk_per_share)
        notional_cap_qty = int(remaining_notional // price)
        allocated = int(min(desired, risk_cap_qty, notional_cap_qty))
        capped = allocated < int(desired)
        if allocated <= 0:
            summary["skipped"].append({"symbol": symbol, "side": side, "reason": "额度不足 1 股"})
            continue

        risk_amount = allocated * risk_per_share
        notional = allocated * price
        totals["positions"] += 1
        totals["notional"] += notional
        totals["risk_amount"] += risk_amount
        side_totals["positions"] += 1
        side_totals["notional"] += notional
        side_totals["risk_amount"] += risk_amount
        if capped:
            caps_hit.append({
                "cap": "position",
                "detail": f"{symbol} {side} 建议 {int(desired)} 股，按上限裁剪为 {allocated} 股",
            })
        summary["allocations"].append({
            "symbol": symbol,
            "side": side,
            "grade": _grade(row),
            "score": _number(_mapping(decision.get("rating")).get("score")),
            "price": round(price, 4),
            "stop": round(stop, 4),
            "risk_per_share": round(risk_per_share, 4),
            "qty": allocated,
            "suggested_qty": int(desired),
            "capped": capped,
            "risk_amount": round(risk_amount, 2),
            "notional": round(notional, 2),
            "funds": str(row.get("funds") or ""),
            "adx": _number(row.get("adx")),
            "relative_volume": _number(row.get("relative_volume")),
        })

    for side in ("long", "short"):
        side_totals = totals[side]
        side_totals["risk_pct"] = round(side_totals["risk_amount"] / equity * 100.0, 3)
        side_totals["leverage"] = round(side_totals["notional"] / equity, 3)
    totals["risk_pct"] = round(totals["risk_amount"] / equity * 100.0, 3)
    totals["leverage"] = round(totals["notional"] / equity, 3)
    totals["notional"] = round(totals["notional"], 2)
    totals["risk_amount"] = round(totals["risk_amount"], 2)
    totals["long"]["notional"] = round(totals["long"]["notional"], 2)
    totals["short"]["notional"] = round(totals["short"]["notional"], 2)
    totals["long"]["risk_amount"] = round(totals["long"]["risk_amount"], 2)
    totals["short"]["risk_amount"] = round(totals["short"]["risk_amount"], 2)
    return summary
