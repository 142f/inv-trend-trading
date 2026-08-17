from __future__ import annotations

import pandas as pd
import pytest

from inv_trend.data.providers import HoldingsCsvProvider


def test_holdings_provider_ranks_and_takes_top_50(monkeypatch: pytest.MonkeyPatch) -> None:
    source = pd.DataFrame({
        "ticker": [f"S{i:02d}" for i in range(55)],
        "weight": list(range(55, 0, -1)),
        "as of": ["2026-08-08"] * 55,
        "fund": ["QQQ"] * 55,
    })
    monkeypatch.setattr(pd, "read_csv", lambda _: source)
    result = HoldingsCsvProvider("holdings.csv").fetch(top=50)
    assert len(result) == 50
    assert result["rank"].tolist() == list(range(1, 51))
    assert result.iloc[0]["symbol"] == "S00"


def test_holdings_provider_rejects_mixed_dates(monkeypatch: pytest.MonkeyPatch) -> None:
    source = pd.DataFrame({
        "symbol": ["A", "B"], "weight": [0.6, 0.4],
        "snapshot_date": ["2026-08-07", "2026-08-08"], "fund": ["SPY", "SPY"],
    })
    monkeypatch.setattr(pd, "read_csv", lambda _: source)
    with pytest.raises(ValueError, match="exactly one snapshot_date"):
        HoldingsCsvProvider("holdings.csv").fetch(top=50)


def test_holdings_provider_rejects_duplicate_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    source = pd.DataFrame({
        "symbol": ["A", "A"], "weight": [0.6, 0.4],
        "snapshot_date": ["2026-08-08"] * 2, "fund": ["SPY"] * 2,
    })
    monkeypatch.setattr(pd, "read_csv", lambda _: source)
    with pytest.raises(ValueError, match="unique"):
        HoldingsCsvProvider("holdings.csv").fetch(top=50)
