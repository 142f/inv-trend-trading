from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import yaml

from ..models import InstrumentConfig


def load_instruments(path: str | Path | None = None) -> dict[str, InstrumentConfig]:
    source = Path(path) if path else Path(str(files("inv_trend.data.config").joinpath("assets.yaml")))
    payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    result: dict[str, InstrumentConfig] = {}
    for symbol, values in payload["instruments"].items():
        symbol = str(symbol).upper()
        values = dict(values)
        for key in ("fallback_sources", "cross_validation_sources"):
            values[key] = tuple(values.get(key, ()))
        result[symbol] = InstrumentConfig(symbol=symbol, **values)
    return result
