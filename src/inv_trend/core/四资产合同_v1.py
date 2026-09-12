"""Versioned contracts. Unknown historical facts remain null and fail closed."""
from dataclasses import dataclass, fields
from datetime import datetime
import math

CORE = ("BTC", "ETH", "XAU", "XAG")
ASSUMPTION = "RESEARCH_ASSUMPTION"
DISCLAIMER = "这是研究假设，不是正式交易所历史规格。"


def utc(value):
    t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if t.tzinfo is None or t.utcoffset().total_seconds() != 0:
        raise ValueError("timezone mismatch: explicit UTC required")
    return t


@dataclass(frozen=True)
class ContractSpec:
    instrument_id: str
    asset_class: str
    market_type: str
    contract_type: str
    contract_multiplier: float | None
    price_tick: float | None
    quantity_step: float | None
    min_quantity: float | None
    currency: str
    margin_model: str | None
    initial_margin: float | None
    maintenance_margin: float | None
    trading_hours: str | None
    settlement_rule: str | None
    funding_rule: str | None
    fee_rule: str | None
    slippage_rule: str | None
    version: str
    effective_from: str | None
    quality_status: str = "UNVERIFIED"
    evidence: str | None = None
    effective_to: str | None = None

    def validate(self, at, mode="FORMAL"):
        if mode not in ("FORMAL", ASSUMPTION):
            raise ValueError("unknown execution mode")
        permitted = ("VERIFIED_HISTORICAL",) if mode == "FORMAL" else (ASSUMPTION,)
        if self.quality_status not in permitted:
            raise ValueError(f"Execution contract invalid: {self.instrument_id}/{self.quality_status}")
        if any(getattr(self, f.name) is None for f in fields(self) if f.name != "effective_to"):
            raise ValueError("incomplete execution contract")
        for key in ("contract_multiplier", "price_tick", "quantity_step", "min_quantity",
                    "initial_margin", "maintenance_margin"):
            x = getattr(self, key)
            if not math.isfinite(x) or x <= 0:
                raise ValueError(f"invalid {key}")
        if not self.maintenance_margin <= self.initial_margin <= 1:
            raise ValueError("invalid margin hierarchy")
        if not utc(self.effective_from) <= utc(at) or (
                self.effective_to and utc(at) >= utc(self.effective_to)):
            raise ValueError("contract is not effective at execution time")
        if self.margin_model != "fully_funded_long_only":
            raise ValueError("unsupported margin model; no silent derivative emulation")


def unverified_contract(symbol, instrument):
    return ContractSpec(instrument["instrument_id"], instrument["asset_class"],
                        instrument["market"], instrument["instrument_type"],
                        None, None, None, None, instrument["quote_currency"],
                        None, None, None, None, None, None, None, None,
                        "historical-unverified-v1", None)


def research_contract(symbol):
    """Synthetic fully funded units, explicitly NOT the venue's CFD or spot spec."""
    if symbol not in CORE:
        raise ValueError("core universe identity mismatch")
    return ContractSpec(f"{symbol}.USD.RESEARCH.UNIT", "crypto" if symbol in CORE[:2]
                        else "precious_metal", "synthetic_research", "funded_price_unit",
                        1.0, 0.000001, 0.000001, 0.000001, "USD",
                        "fully_funded_long_only", 1.0, 1.0,
                        "observed_provider_labels; not certified sessions",
                        "cash exchange against synthetic units",
                        "predeclared calendar-day carry; no inferred historical funding",
                        "experiment fee_bps", "experiment slippage_bps",
                        "research-unit-v1", "2000-01-01T00:00:00+00:00", ASSUMPTION,
                        DISCLAIMER)
