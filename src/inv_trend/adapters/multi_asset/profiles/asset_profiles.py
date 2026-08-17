"""Centralized asset profile inference.

The compatibility name ``_infer_asset_fields`` remains available from
``integrations.mt5`` and ``mt5_data``; new code should use ``infer_asset_fields``.
"""

from __future__ import annotations

from typing import Mapping

from ..models.domain import AssetSpec


def infer_asset_fields(symbol: str) -> dict[str, object]:
    upper = symbol.upper()
    if "XAU" in upper or "GOLD" in upper:
        return _asset_fields("metal", "precious_metals", 3, 0.004, 0.016, 1.0, 1.0, 3.0)
    if "XAG" in upper or "SILVER" in upper:
        return _asset_fields("metal", "precious_metals", 2, 0.003, 0.012, 0.7, 1.5, 4.0)
    if "BTC" in upper or "ETH" in upper:
        return _asset_fields("crypto", "crypto", 2, 0.003, 0.012, 0.5, 3.0, 8.0)
    if upper in {"SPY", "QQQ"} or upper.endswith(".US"):
        return _asset_fields("equity", "us_equity", 3, 0.004, 0.016, 1.0, 1.0, 3.0)
    return _asset_fields("other", "other", 2, 0.003, 0.012, 0.5, 2.0, 5.0)


def build_asset_specs(
    symbols: list[str],
    overrides: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[str, AssetSpec]:
    overrides = overrides or {}
    specs: dict[str, AssetSpec] = {}
    for symbol in symbols:
        inferred = infer_asset_fields(symbol)
        params = {
            "symbol": symbol,
            "asset_class": inferred["asset_class"],
            "cluster": inferred["cluster"],
            "max_units": inferred["max_units"],
            "unit_1n_risk_pct": inferred["unit_1n_risk_pct"],
            "max_symbol_1n_risk_pct": inferred["max_symbol_1n_risk_pct"],
            "max_symbol_leverage": inferred["max_symbol_leverage"],
            "cost_bps": inferred["cost_bps"],
            "slippage_bps": inferred["slippage_bps"],
        }
        params.update(dict(overrides.get(symbol, {})))
        specs[symbol] = AssetSpec(**params)
    return specs


def _asset_fields(
    asset_class: str,
    cluster: str,
    max_units: int,
    unit_1n_risk_pct: float,
    max_symbol_1n_risk_pct: float,
    max_symbol_leverage: float,
    cost_bps: float,
    slippage_bps: float,
) -> dict[str, object]:
    return {
        "asset_class": asset_class,
        "cluster": cluster,
        "max_units": max_units,
        "unit_1n_risk_pct": unit_1n_risk_pct,
        "max_symbol_1n_risk_pct": max_symbol_1n_risk_pct,
        "max_symbol_leverage": max_symbol_leverage,
        "cost_bps": cost_bps,
        "slippage_bps": slippage_bps,
    }
