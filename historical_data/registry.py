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

    def symbol_for_instrument_id(self, instrument_id: str) -> str:
        for symbol, instrument in self.instruments.items():
            if instrument.instrument_id == instrument_id:
                return symbol
        raise KeyError(f"instrument_id is not registered: {instrument_id}")
