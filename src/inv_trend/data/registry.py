from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .models import InstrumentConfig, InstrumentNotImplementedError


@dataclass(frozen=True)
class InstrumentRegistry:
    """The single symbol-to-instrument mapping boundary for the data layer."""

    instruments: Mapping[str, InstrumentConfig]

    def resolve(self, symbol: str) -> InstrumentConfig:
        key = symbol.upper()
        if key not in self.instruments:
            raise KeyError(f"instrument is not registered: {symbol}")
        instrument = self.instruments[key]
        if instrument.status == "RESERVED":
            raise InstrumentNotImplementedError(f"instrument is reserved for a future provider: {symbol}")
        return instrument

