"""按资产类别差异化阈值配置。"""

from __future__ import annotations

from .models import (
    BacktestThresholds,
    BreakoutQualityThresholds,
    CorrelationThresholds,
    EligibilityThresholds,
    EventRiskThresholds,
    LiquidityThresholds,
    MarketRegimeThresholds,
    RiskBudgetThresholds,
    VolatilityThresholds,
)

# ── 加密货币（BTC/ETH）─────────────────────────────────────────────────────

CRYPTO_THRESHOLDS = EligibilityThresholds(
    liquidity=LiquidityThresholds(
        min_daily_volume_usd=50_000_000,
        max_spread_bps=20.0,
        min_turnover_ratio=0.02,
        max_estimated_slippage_bps=15.0,
        estimated_spread_bps=5.0,
        max_market_impact_pct=0.005,
    ),
    volatility=VolatilityThresholds(
        min_atr_pct=0.01,
        max_atr_pct=0.12,
        min_atr_percentile=0.15,
        max_atr_percentile=0.85,
        max_gap_atr=2.0,
        max_chase_distance_atr=1.0,
        max_stop_distance_atr=3.0,
    ),
    market_regime=MarketRegimeThresholds(
        trend_ma_short=60,
        trend_ma_long=120,
        adx_threshold=20.0,
        adx_strong_threshold=30.0,
        donchian_width_percentile_low=0.20,
        donchian_width_percentile_high=0.80,
        false_breakout_lookback=20,
    ),
    breakout_quality=BreakoutQualityThresholds(
        min_close_location=0.60,
        max_wick_ratio=0.50,
        min_breakout_magnitude_atr=0.15,
        volume_confirmation_ratio=1.2,
        max_overextended_atr=1.5,
        retest_tolerance_atr=0.25,
    ),
    risk_budget=RiskBudgetThresholds(
        single_trade_risk_pct=0.01,
        max_symbol_risk_pct=0.04,
        max_asset_class_risk_pct=0.12,
        max_portfolio_risk_pct=0.25,
        max_drawdown_pct=0.25,
        max_consecutive_losses=5,
        min_position_size=100.0,
        risk_reduction_after_losses=0.5,
    ),
    correlation=CorrelationThresholds(
        high_correlation=0.70,
        risk_group_max_exposure_pct=0.15,
        max_same_direction_signals=3,
    ),
    event_risk=EventRiskThresholds(
        earnings_blackout_days=0,
        fomc_blackout_hours=24,
        economic_data_blackout_hours=4,
        max_funding_rate=0.001,
        max_open_interest_change_pct=0.20,
    ),
    backtest=BacktestThresholds(
        min_data_years=2.0,
        min_trades=20,
        min_sharpe=0.3,
        max_drawdown_pct=0.40,
        min_calmar=0.25,
        max_false_breakout_rate=0.45,
        max_consecutive_losses=8,
    ),
    data={
        "require_adjustment": False,
        "require_contract_roll_handling": False,
        "session": "24x7",
        "min_history_bars": 250,
        "max_missing_ratio": 0.02,
        "exchange_whitelist": ["binance"],
        "forbidden_mixing": ["spot_perpetual", "cross_exchange"],
    },
)

# ── 贵金属（XAU/XAG）───────────────────────────────────────────────────────

PRECIOUS_METAL_THRESHOLDS = EligibilityThresholds(
    liquidity=LiquidityThresholds(
        min_daily_volume_usd=10_000_000_000,
        max_spread_bps=10.0,
        min_turnover_ratio=0.01,
        max_estimated_slippage_bps=10.0,
        estimated_spread_bps=3.0,
        max_market_impact_pct=0.002,
    ),
    volatility=VolatilityThresholds(
        min_atr_pct=0.005,
        max_atr_pct=0.06,
        min_atr_percentile=0.10,
        max_atr_percentile=0.90,
        max_gap_atr=1.5,
        max_chase_distance_atr=0.75,
        max_stop_distance_atr=2.5,
    ),
    market_regime=MarketRegimeThresholds(
        trend_ma_short=60,
        trend_ma_long=120,
        adx_threshold=20.0,
        adx_strong_threshold=30.0,
        donchian_width_percentile_low=0.20,
        donchian_width_percentile_high=0.80,
        false_breakout_lookback=20,
    ),
    breakout_quality=BreakoutQualityThresholds(
        min_close_location=0.65,
        max_wick_ratio=0.50,
        min_breakout_magnitude_atr=0.10,
        volume_confirmation_ratio=1.0,
        max_overextended_atr=1.5,
        retest_tolerance_atr=0.25,
    ),
    risk_budget=RiskBudgetThresholds(
        single_trade_risk_pct=0.01,
        max_symbol_risk_pct=0.04,
        max_asset_class_risk_pct=0.12,
        max_portfolio_risk_pct=0.25,
        max_drawdown_pct=0.25,
        max_consecutive_losses=5,
        min_position_size=500.0,
        risk_reduction_after_losses=0.5,
    ),
    correlation=CorrelationThresholds(
        high_correlation=0.70,
        risk_group_max_exposure_pct=0.15,
        max_same_direction_signals=3,
    ),
    event_risk=EventRiskThresholds(
        earnings_blackout_days=0,
        fomc_blackout_hours=24,
        economic_data_blackout_hours=4,
        max_funding_rate=0.0,
        max_open_interest_change_pct=0.20,
    ),
    backtest=BacktestThresholds(
        min_data_years=3.0,
        min_trades=30,
        min_sharpe=0.3,
        max_drawdown_pct=0.35,
        min_calmar=0.3,
        max_false_breakout_rate=0.40,
        max_consecutive_losses=8,
    ),
    data={
        "require_adjustment": False,
        "require_contract_roll_handling": True,
        "session": "spot_market_hours",
        "min_history_bars": 250,
        "max_missing_ratio": 0.02,
        "exchange_whitelist": ["dukascopy", "oanda"],
        "forbidden_mixing": ["cfd_spot", "cross_provider"],
    },
)

# ── 美股科技股（QQQ及其持仓）────────────────────────────────────────────────

US_EQUITY_THRESHOLDS = EligibilityThresholds(
    liquidity=LiquidityThresholds(
        min_daily_volume_usd=100_000_000,
        max_spread_bps=10.0,
        min_turnover_ratio=0.005,
        max_estimated_slippage_bps=10.0,
        estimated_spread_bps=2.0,
        max_market_impact_pct=0.002,
    ),
    volatility=VolatilityThresholds(
        min_atr_pct=0.008,
        max_atr_pct=0.08,
        min_atr_percentile=0.10,
        max_atr_percentile=0.90,
        max_gap_atr=1.5,
        max_chase_distance_atr=0.75,
        max_stop_distance_atr=2.5,
    ),
    market_regime=MarketRegimeThresholds(
        trend_ma_short=60,
        trend_ma_long=120,
        adx_threshold=20.0,
        adx_strong_threshold=30.0,
        donchian_width_percentile_low=0.20,
        donchian_width_percentile_high=0.80,
        false_breakout_lookback=20,
    ),
    breakout_quality=BreakoutQualityThresholds(
        min_close_location=0.65,
        max_wick_ratio=0.50,
        min_breakout_magnitude_atr=0.10,
        volume_confirmation_ratio=1.2,
        max_overextended_atr=1.5,
        retest_tolerance_atr=0.25,
    ),
    risk_budget=RiskBudgetThresholds(
        single_trade_risk_pct=0.01,
        max_symbol_risk_pct=0.04,
        max_asset_class_risk_pct=0.12,
        max_portfolio_risk_pct=0.25,
        max_drawdown_pct=0.25,
        max_consecutive_losses=5,
        min_position_size=500.0,
        risk_reduction_after_losses=0.5,
    ),
    correlation=CorrelationThresholds(
        high_correlation=0.70,
        risk_group_max_exposure_pct=0.15,
        max_same_direction_signals=3,
    ),
    event_risk=EventRiskThresholds(
        earnings_blackout_days=3,
        fomc_blackout_hours=24,
        economic_data_blackout_hours=4,
        max_funding_rate=0.0,
        max_open_interest_change_pct=0.20,
    ),
    backtest=BacktestThresholds(
        min_data_years=3.0,
        min_trades=30,
        min_sharpe=0.3,
        max_drawdown_pct=0.35,
        min_calmar=0.3,
        max_false_breakout_rate=0.40,
        max_consecutive_losses=8,
    ),
    data={
        "require_adjustment": True,
        "require_contract_roll_handling": False,
        "session": "nasdaq_hours",
        "min_history_bars": 250,
        "max_missing_ratio": 0.01,
        "exchange_whitelist": ["nasdaq", "nyse"],
        "forbidden_mixing": ["pre_post_market", "cross_exchange"],
        "adjustment_policy": "back_adjusted",
    },
)

# ── 默认阈值（保守配置）─────────────────────────────────────────────────────

DEFAULT_THRESHOLDS = EligibilityThresholds(
    liquidity=LiquidityThresholds(
        min_daily_volume_usd=50_000_000,
        max_spread_bps=50.0,
        min_turnover_ratio=0.01,
        max_estimated_slippage_bps=30.0,
        estimated_spread_bps=10.0,
        max_market_impact_pct=0.01,
    ),
    volatility=VolatilityThresholds(
        min_atr_pct=0.005,
        max_atr_pct=0.10,
        min_atr_percentile=0.10,
        max_atr_percentile=0.90,
        max_gap_atr=1.5,
        max_chase_distance_atr=1.0,
        max_stop_distance_atr=3.0,
    ),
    market_regime=MarketRegimeThresholds(
        trend_ma_short=60,
        trend_ma_long=120,
        adx_threshold=20.0,
        adx_strong_threshold=30.0,
        donchian_width_percentile_low=0.20,
        donchian_width_percentile_high=0.80,
        false_breakout_lookback=20,
    ),
    breakout_quality=BreakoutQualityThresholds(
        min_close_location=0.60,
        max_wick_ratio=0.50,
        min_breakout_magnitude_atr=0.10,
        volume_confirmation_ratio=1.0,
        max_overextended_atr=1.5,
        retest_tolerance_atr=0.25,
    ),
    risk_budget=RiskBudgetThresholds(
        single_trade_risk_pct=0.01,
        max_symbol_risk_pct=0.04,
        max_asset_class_risk_pct=0.12,
        max_portfolio_risk_pct=0.25,
        max_drawdown_pct=0.25,
        max_consecutive_losses=5,
        min_position_size=0.0,
        risk_reduction_after_losses=0.5,
    ),
    correlation=CorrelationThresholds(
        high_correlation=0.70,
        risk_group_max_exposure_pct=0.15,
        max_same_direction_signals=3,
    ),
    event_risk=EventRiskThresholds(
        earnings_blackout_days=3,
        fomc_blackout_hours=24,
        economic_data_blackout_hours=4,
        max_funding_rate=0.001,
        max_open_interest_change_pct=0.20,
    ),
    backtest=BacktestThresholds(
        min_data_years=3.0,
        min_trades=30,
        min_sharpe=0.3,
        max_drawdown_pct=0.35,
        min_calmar=0.3,
        max_false_breakout_rate=0.40,
        max_consecutive_losses=8,
    ),
    data={
        "require_adjustment": False,
        "require_contract_roll_handling": False,
        "session": "24x7",
        "min_history_bars": 250,
        "max_missing_ratio": 0.02,
        "exchange_whitelist": [],
        "forbidden_mixing": [],
    },
)

# ── 风险组映射 ─────────────────────────────────────────────────────────────

RISK_GROUPS: dict[str, str] = {
    "BTC": "CRYPTO_RISK_GROUP",
    "ETH": "CRYPTO_RISK_GROUP",
    "XAU": "PRECIOUS_METALS_RISK_GROUP",
    "XAG": "PRECIOUS_METALS_RISK_GROUP",
    "NVDA": "US_TECH_RISK_GROUP",
    "MSFT": "US_TECH_RISK_GROUP",
    "GOOGL": "US_TECH_RISK_GROUP",
    "AMZN": "US_TECH_RISK_GROUP",
    "META": "US_TECH_RISK_GROUP",
    "AVGO": "US_TECH_RISK_GROUP",
    "TSM": "US_TECH_RISK_GROUP",
    "AMD": "US_TECH_RISK_GROUP",
    "AAPL": "US_TECH_RISK_GROUP",
    "QQQ": "US_TECH_RISK_GROUP",
    "SPY": "US_TECH_RISK_GROUP",
    "TSLA": "US_TECH_RISK_GROUP",
    "NFLX": "US_TECH_RISK_GROUP",
    "ORCL": "US_TECH_RISK_GROUP",
    "MU": "US_TECH_RISK_GROUP",
    "PLTR": "US_TECH_RISK_GROUP",
    "SNDK": "US_TECH_RISK_GROUP",
    "XLY": "US_TECH_RISK_GROUP",
}

# ── 市场到阈值映射 ─────────────────────────────────────────────────────────


def get_thresholds_for_market(market: str) -> EligibilityThresholds:
    """根据市场类型返回对应的阈值配置。"""
    market_map = {
        "crypto": CRYPTO_THRESHOLDS,
        "precious_metal": PRECIOUS_METAL_THRESHOLDS,
        "us_equity": US_EQUITY_THRESHOLDS,
    }
    return market_map.get(market, DEFAULT_THRESHOLDS)


def get_risk_group(symbol: str) -> str:
    """获取品种所属的风险组。"""
    return RISK_GROUPS.get(symbol.upper(), "OTHER_RISK_GROUP")