"""Regressions for the five silent core distortions found in the #17 review.

Each test pins a case where the old code produced a plausible but wrong number
(or a wrong type of failure) instead of failing closed, so the fix cannot be
reverted without turning these red.
"""

from dataclasses import replace
import pathlib
import re

import numpy as np
import pandas as pd
import pytest

from inv_trend.core import 数量步长_v1
from inv_trend.core.四资产合同_v1 import ASSUMPTION, CORE, research_contract
from inv_trend.core.组合账户_v1 import CostModel, PortfolioAccount
from inv_trend.core.数量步长_v1 import floor_to_step
from inv_trend.core.永续风控 import (
    InstrumentSpec,
    PositionTier,
    risk_estimates,
    risk_sized_contracts,
)
from inv_trend.core.rules import UnsupportedOperatorError, evaluate_condition


# --------------------------------------------------------------------------- #
# 1. Lot sizing must never lose a whole step to float representation.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value,step,expected",
    [
        (0.3, 0.1, 0.3),      # 0.3 / 0.1 == 2.9999999999999996 -> used to return 0.2
        (4.3, 0.1, 4.3),      # 4.3 / 0.1 == 42.999999999999993  -> used to return 4.2
        (0.29, 0.01, 0.29),   # 0.29 / 0.01 == 28.999999999999996 -> used to return 0.28
        (2.15, 0.05, 2.15),   # 2.15 / 0.05 == 42.99999999999999 -> used to return 2.1
        (0.35, 0.1, 0.3),     # genuine remainder still rounds down
        (0.0, 0.1, 0.0),
        (5.0, 5.0, 5.0),
    ],
)
def test_floor_to_step_never_loses_a_full_lot(value, step, expected):
    result = floor_to_step(value, step)
    assert result == expected
    assert result <= value + 1e-12


@pytest.mark.parametrize("step", [0.001, 0.01, 0.05, 0.1, 0.2, 0.5, 1.0])
def test_every_exact_lot_multiple_survives_sizing(step):
    """Sweep the whole low range: the naive floor loses 13 of these 413 lots."""

    for lots in range(1, 60):
        value = lots * step
        assert floor_to_step(value, step) == pytest.approx(value, abs=1e-12)


@pytest.mark.parametrize(
    "value,step",
    [(float("nan"), 0.1), (1.0, 0.0), (1.0, -0.1), (float("inf"), 1.0), ("1", 0.1)],
)
def test_floor_to_step_rejects_invalid_inputs(value, step):
    with pytest.raises(ValueError, match="quantity"):
        floor_to_step(value, step)


def test_no_module_divides_by_a_step_on_its_own():
    """Sizing must route through 数量步长, never a bare floor(value / step) * step."""

    root = pathlib.Path(数量步长_v1.__file__).parent.parent
    pattern = re.compile(r"floor\(\s*[A-Za-z_][\w\.]*\s*/\s*[\w\.]+\s*\)\s*\*")
    offenders = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if path.name != "数量步长_v1.py" and pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def _step_contracts(**overrides):
    fields = dict(price_tick=0.01, quantity_step=0.1, min_quantity=0.1, **overrides)
    return {symbol: replace(research_contract(symbol), **fields) for symbol in CORE}


def _step_account(clock=lambda day: f"2020-01-{day:02d}T00:00:00+00:00"):
    return PortfolioAccount(
        _step_contracts(),
        initial_cash=1200.0,
        costs=CostModel(fee_bps=0.0, slippage_bps=0.0, carry_bps_year=0.0),
        mode=ASSUMPTION,
    )


def test_portfolio_fills_the_last_whole_lot_the_budget_allows():
    """25% of 1200 at 1000 is exactly 0.3 lots; the ledger used to buy 0.2."""

    account = _step_account()
    prices = {symbol: 1000.0 for symbol in CORE}
    account.execute_open("2020-01-01T00:00:00+00:00", prices)
    account.mark_close("2020-01-02T00:00:00+00:00", prices)
    account.submit({"BTC": 0.25, "ETH": 0.0, "XAU": 0.0, "XAG": 0.0}, "2020-01-02T00:00:00+00:00")
    account.execute_open("2020-01-03T00:00:00+00:00", prices)

    fill = next(item for item in account.fills if item["symbol"] == "BTC")
    assert fill["quantity"] == 0.3
    assert fill["status"] == "FILLED"
    assert account.positions["BTC"] == 0.3
    assert account.cash == pytest.approx(900.0)


# --------------------------------------------------------------------------- #
# 2. A checkpoint must accept every UTC clock the API itself accepts.
# --------------------------------------------------------------------------- #
def _run_ledger(clock):
    account = _step_account(clock)
    prices = {symbol: 1000.0 for symbol in CORE}
    account.execute_open(clock(1), prices)
    account.mark_close(clock(2), prices)
    account.submit({symbol: 0.25 for symbol in CORE}, clock(2))
    account.execute_open(clock(3), prices)
    account.mark_close(clock(4), prices)
    return account


def test_checkpoint_accepts_timestamp_clocks_and_matches_iso_string_runs():
    iso = _run_ledger(lambda day: f"2020-01-{day:02d}T00:00:00+00:00").checkpoint()
    stamped = _run_ledger(lambda day: pd.Timestamp(f"2020-01-{day:02d}", tz="UTC")).checkpoint()

    assert stamped == iso
    assert stamped["state"]["last_close"] == "2020-01-04T00:00:00+00:00"

    restored = _step_account()
    restored.restore(stamped)
    assert restored.positions == iso["state"]["positions"]
    assert restored.cash == pytest.approx(iso["state"]["cash"])
    assert restored.checkpoint()["hash"] == iso["hash"]


# --------------------------------------------------------------------------- #
# 3. Perpetual sizing shares the same lot rounding.
# --------------------------------------------------------------------------- #
def _perp_spec(**kwargs):
    tiers = (
        PositionTier(10000, 0.01, 100),
        PositionTier(20000, 0.02, 50),
        PositionTier(1000000, 0.03, 20),
    )
    base = InstrumentSpec(
        "BTC-USDT-SWAP",
        "v1",
        "t1",
        "2020-01-01T00:00:00Z",
        None,
        "fixture-only",
        "SIMULATED_SPEC",
        1.0,
        0.001,
        0.001,
        100.0,
        0.001,
        0.002,
        tiers,
    )
    return replace(base, **kwargs)


def test_risk_sized_contracts_keeps_the_last_full_lot():
    """Budget 0.6 / unit loss 2.0 is exactly 0.3 lots with a 0.1 lot size."""

    sized = risk_sized_contracts(
        equity=120.0,
        side=1,
        price=100.0,
        stop=98.0,
        spec=_perp_spec(lot_size=0.1, minimum_size=0.1, fee_rate=0.0),
        trade_risk=0.005,
    )
    assert sized == 0.3
    assert sized / 0.1 == pytest.approx(3.0, abs=1e-12)


# --------------------------------------------------------------------------- #
# 4. Risk estimates must accept a single instrument.
# --------------------------------------------------------------------------- #
def _single_close_series(periods=520):
    rng = np.random.default_rng(7)
    prices = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, periods)))
    index = pd.date_range("2020-01-01", periods=periods, tz="UTC")
    return pd.Series(prices, index=index, name="BTC")


def test_risk_estimates_accepts_a_single_series_like_a_one_column_frame():
    closes = _single_close_series()
    cov, stress = risk_estimates(closes)
    frame_cov, frame_stress = risk_estimates(closes.to_frame())

    assert cov.shape == (1, 1) and stress.shape == (1,)
    assert np.allclose(cov, frame_cov) and np.allclose(stress, frame_stress)


def test_risk_estimates_still_fails_closed_on_short_history():
    with pytest.raises(ValueError, match="insufficient causal volatility history"):
        risk_estimates(_single_close_series(periods=200))


# --------------------------------------------------------------------------- #
# 5. An unknown operator is a schema error, not "unavailable evidence".
# --------------------------------------------------------------------------- #
def test_unsupported_operator_raises_instead_of_degrading_to_unavailable():
    with pytest.raises(UnsupportedOperatorError, match="unsupported comparison operator"):
        evaluate_condition(
            condition_id="c1",
            name="bad operator",
            actual=1.0,
            operator="~=",
            reference=2.0,
        )


def test_missing_numeric_input_still_degrades_to_unavailable():
    condition = evaluate_condition(
        condition_id="c2",
        name="missing actual",
        actual=None,
        operator=">",
        reference=1.0,
    )
    assert condition.passed is None
    assert condition.relative_position == "unavailable"
    assert condition.status == "unavailable"
