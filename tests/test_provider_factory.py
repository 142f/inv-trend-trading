from pathlib import Path

from inv_trend.data.provider_factory import create_default_providers
from inv_trend.data.providers import (
    AlphaVantageProvider,
    BinanceKlineProvider,
    CsvBarsProvider,
    DukascopyBarsProvider,
    YahooChartProvider,
)


def test_default_provider_factory_registers_primary_providers(monkeypatch) -> None:
    monkeypatch.delenv("ALPHAVANTAGE_API_KEY", raising=False)

    providers = create_default_providers()

    assert set(providers) == {"binance", "dukascopy", "yahoo_chart"}
    assert isinstance(providers["binance"], BinanceKlineProvider)
    assert isinstance(providers["dukascopy"], DukascopyBarsProvider)
    assert isinstance(providers["yahoo_chart"], YahooChartProvider)


def test_default_provider_factory_adds_optional_providers(monkeypatch) -> None:
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", "test-key")
    csv_path = Path("licensed-bars.csv")

    providers = create_default_providers(csv_path)

    assert isinstance(providers["alpha_vantage"], AlphaVantageProvider)
    assert isinstance(providers["licensed_csv"], CsvBarsProvider)
    assert providers["licensed_csv"].source == csv_path


def test_default_provider_factory_applies_network_policy(monkeypatch) -> None:
    monkeypatch.delenv("ALPHAVANTAGE_API_KEY", raising=False)
    providers = create_default_providers(timeout=7, retries=2)
    for name in ("binance", "dukascopy", "yahoo_chart"):
        assert providers[name].timeout == 7
        assert providers[name].retries == 2
