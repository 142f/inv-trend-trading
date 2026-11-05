"""Contract tests for the public surfaces that have no in-repo caller.

Three names were flagged as "zero references, but looks like public capability":
``OKXClient.place_market_order`` / ``place_limit_order`` / ``system_time``,
``notifications.渠道.NotificationChannel`` and ``core.math_utils.macd``.
Deleting them would be a product decision, so instead these tests pin the
behaviour they currently promise.  If someone removes or changes them, CI says
so explicitly instead of the capability quietly disappearing.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from inv_trend.core.math_utils import macd
from inv_trend.integrations.okx import OKXClient, OKXConfig
from inv_trend.notifications.渠道 import NotificationChannel, WebhookNotifier


class _TradeStub:
    """Records order submissions instead of talking to the exchange."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def place_order(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return {"code": "0", "data": [{"ordId": "42"}]}


@pytest.fixture(scope="module")
def _okx() -> OKXClient:
    """One client per module: building the SDK APIs costs ~3.5s each."""

    return OKXClient(OKXConfig())


@pytest.fixture()
def okx(_okx: OKXClient) -> tuple[OKXClient, _TradeStub]:
    stub = _TradeStub()
    _okx.trade = stub  # type: ignore[assignment]
    return _okx, stub


def test_dry_run_order_never_reaches_the_exchange(okx) -> None:
    client, stub = okx

    result = client.place_market_order("BTC-USDT-SWAP", "BUY", Decimal("0.5"))

    assert result["dry_run"] is True
    assert result["simulated"] is True
    assert result["would_submit"] == {
        "instId": "BTC-USDT-SWAP",
        "tdMode": "isolated",
        "side": "buy",
        "ordType": "market",
        "sz": "0.5",
    }
    assert stub.calls == []


def test_limit_order_carries_price_and_optional_fields(okx) -> None:
    client, stub = okx

    dry = client.place_limit_order(
        "BTC-USDT-SWAP", "sell", 1, 25_000.0, reduce_only=True, client_order_id="abc"
    )

    assert dry["would_submit"] == {
        "instId": "BTC-USDT-SWAP",
        "tdMode": "isolated",
        "side": "sell",
        "ordType": "limit",
        "sz": "1",
        "px": "25000.0",
        "clOrdId": "abc",
        "reduceOnly": "true",
    }
    assert stub.calls == []


def test_real_submission_is_blocked_until_the_switch_is_set(okx) -> None:
    client, stub = okx
    client.config = OKXConfig(api_key="k", secret_key="s", passphrase="p")

    with pytest.raises(RuntimeError, match="OKX_ENABLE_TRADING"):
        client.place_market_order("BTC-USDT-SWAP", "buy", 1, dry_run=False)
    assert stub.calls == []


def test_missing_credentials_are_named_in_the_error(okx) -> None:
    client, stub = okx
    client.config = OKXConfig(enable_trading=True)

    with pytest.raises(RuntimeError) as excinfo:
        client.place_market_order("BTC-USDT-SWAP", "buy", 1, dry_run=False)

    message = str(excinfo.value)
    for name in ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE"):
        assert name in message
    assert stub.calls == []


def test_enabled_submission_forwards_the_cleaned_parameters(okx) -> None:
    client, stub = okx
    client.config = OKXConfig(
        api_key="k", secret_key="s", passphrase="p", enable_trading=True
    )

    result = client.place_market_order(
        "BTC-USDT-SWAP", "buy", Decimal("2.5"), position_side="long", dry_run=False
    )

    assert result == {"code": "0", "data": [{"ordId": "42"}]}
    assert stub.calls == [
        {
            "instId": "BTC-USDT-SWAP",
            "tdMode": "isolated",
            "side": "buy",
            "ordType": "market",
            "sz": "2.5",
            "px": "",
            "clOrdId": "",
            "posSide": "long",
            "reduceOnly": "",
        }
    ]


def test_system_time_returns_the_public_payload(okx) -> None:
    client, _ = okx

    class _Public:
        def get_system_time(self) -> dict[str, object]:
            return {"code": "0", "data": [{"ts": "1700000000000"}]}

    client.public = _Public()  # type: ignore[assignment]

    assert client.system_time() == {"code": "0", "data": [{"ts": "1700000000000"}]}


def test_notification_channel_protocol_shape_is_stable() -> None:
    """The Protocol is the structural contract every notifier must satisfy."""

    assert set(NotificationChannel.__annotations__) == {"channel_id"}
    assert callable(NotificationChannel.send)

    notifier = WebhookNotifier("https://example.test/hook")
    assert notifier.channel_id.startswith("webhook:")
    assert callable(notifier.send)


def test_macd_is_the_shared_ema_definition() -> None:
    values = pd.Series([100.0 + value * 0.5 for value in range(80)])

    frame = macd(values)

    assert list(frame.columns) == [
        "ema_12",
        "ema_26",
        "dif",
        "dea",
        "histogram",
        "macd_bar",
    ]
    pd.testing.assert_series_equal(
        frame["dif"], frame["ema_12"] - frame["ema_26"], check_names=False
    )
    pd.testing.assert_series_equal(
        frame["histogram"], frame["dif"] - frame["dea"], check_names=False
    )
    pd.testing.assert_series_equal(
        frame["macd_bar"], frame["histogram"] * 2.0, check_names=False
    )


@pytest.mark.parametrize(
    "kwargs", [{"fast": 1}, {"fast": 26, "slow": 12}, {"slow": 26, "signal": 1}]
)
def test_macd_rejects_impossible_periods(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        macd(pd.Series([1.0, 2.0, 3.0]), **kwargs)
