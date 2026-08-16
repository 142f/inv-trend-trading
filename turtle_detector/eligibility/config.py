"""前置条件审查配置加载。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import EligibilityThresholds
from .thresholds import DEFAULT_THRESHOLDS


def _read_mapping(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"缺少前置条件配置：{config_path}")
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"前置条件配置必须是映射：{config_path}")
    return payload


def load_eligibility_config(
    path: str | Path | None = None,
) -> EligibilityThresholds:
    """加载前置条件审查配置。

    优先使用指定路径的YAML配置，如果未指定或加载失败则使用代码中定义的默认阈值。
    """
    if path is not None:
        try:
            payload = _read_mapping(path)
            return _build_thresholds(payload)
        except (FileNotFoundError, ValueError, KeyError) as exc:
            import logging

            logging.getLogger(__name__).warning(
                "无法加载前置条件配置 %s，使用默认阈值：%s", path, exc
            )
    return DEFAULT_THRESHOLDS


def _build_thresholds(payload: dict[str, Any]) -> EligibilityThresholds:
    """从YAML字典构建阈值配置。"""
    from .models import (
        BacktestThresholds,
        BreakoutQualityThresholds,
        CorrelationThresholds,
        EventRiskThresholds,
        LiquidityThresholds,
        MarketRegimeThresholds,
        RiskBudgetThresholds,
        VolatilityThresholds,
    )

    return EligibilityThresholds(
        liquidity=LiquidityThresholds(**payload.get("liquidity", {})),
        volatility=VolatilityThresholds(**payload.get("volatility", {})),
        market_regime=MarketRegimeThresholds(
            **payload.get("market_regime", {})
        ),
        breakout_quality=BreakoutQualityThresholds(
            **payload.get("breakout_quality", {})
        ),
        risk_budget=RiskBudgetThresholds(**payload.get("risk_budget", {})),
        correlation=CorrelationThresholds(**payload.get("correlation", {})),
        event_risk=EventRiskThresholds(**payload.get("event_risk", {})),
        backtest=BacktestThresholds(**payload.get("backtest", {})),
        data=payload.get("data", {}),
    )