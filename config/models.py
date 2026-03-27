from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class ExchangeConfig:
    exchange_id: str = "binanceusdm"
    default_type: str = "future"
    api_key_env: str = "BINANCE_API_KEY"
    api_secret_env: str = "BINANCE_API_SECRET"
    api_passphrase_env: str = "OKX_API_PASSPHRASE"
    enable_rate_limit: bool = True
    recv_window_ms: int = 5000
    timeout_ms: int = 10000


@dataclass
class StrategyConfig:
    symbols: List[str] = field(default_factory=lambda: ["BTCUSDT", "ETHUSDT"])
    raw_timeframes: List[str] = field(
        default_factory=lambda: ["15m", "30m", "1h", "2h", "4h", "1d"]
    )
    aggregate_timeframes: List[str] = field(default_factory=lambda: ["2d", "5d", "7d"])
    weights: Dict[str, int] = field(
        default_factory=lambda: {
            "7d": 24,
            "5d": 20,
            "2d": 16,
            "1d": 14,
            "4h": 10,
            "2h": 7,
            "1h": 5,
            "30m": 3,
            "15m": 1,
        }
    )
    r_gate_long: float = 0.55
    r_gate_short: float = -0.55
    breakout_filter: float = 0.0005
    atr_period: int = 14
    stop_atr_mult: float = 2.0
    min_stop_atr_mult: float = 1.0
    time_stop_bars_15m: int = 48
    time_stop_min_r: float = 0.1
    min_mid_tf_confirm: int = 2
    short_min_mid_tf_confirm: int = 3
    require_daily_align_for_short: bool = True
    adaptive_filter_enabled: bool = True
    adaptive_filter_lookback_15m: int = 192
    adaptive_atr_q_low: float = 0.20
    adaptive_atr_q_high: float = 0.90
    adaptive_score_q: float = 0.60


@dataclass
class RiskConfig:
    risk_tier_a: float = 0.005
    risk_tier_b: float = 0.0035
    risk_tier_c: float = 0.002
    leverage_tier_a: float = 4.0
    leverage_tier_b: float = 3.0
    leverage_tier_c: float = 2.0
    max_leverage_hard: float = 5.0
    portfolio_risk_target: float = 0.015
    portfolio_risk_hard: float = 0.02
    daily_drawdown_limit: float = 0.02
    same_direction_corr_haircut: float = 0.30
    add_on_ratio: float = 0.25
    reduce_step_ratio: float = 0.25
    max_add_ons: int = 1
    dynamic_sizing_enabled: bool = True
    risk_pct_min: float = 0.0008
    risk_pct_max: float = 0.008
    vol_target_annual: float = 0.60
    vol_parity_lookback_15m: int = 192
    vol_parity_scalar_min: float = 0.50
    vol_parity_scalar_max: float = 1.50
    kelly_lookback_15m: int = 288
    kelly_fraction_cap: float = 0.50
    regime_lookback_15m: int = 192
    regime_scale_trend: float = 1.15
    regime_scale_neutral: float = 0.85
    regime_scale_stress: float = 0.55


@dataclass
class TradingCostConfig:
    fee_taker: float = 0.0005
    fee_maker: float = 0.0002
    slippage_bps: float = 5.0
    spread_threshold_bps: float = 8.0
    slippage_buffer_bps: float = 5.0
    funding_settlement_hours: int = 8
    funding_missing_policy: str = "zero_and_warn"
    funding_extreme_threshold: float = 0.0015


@dataclass
class DataConfig:
    fetch_limit: int = 2000
    timezone: str = "UTC"
    require_closed_only: bool = True
    min_required_bars_15m: int = 300
    min_required_bars_1d: int = 220
    max_gap_multiple: int = 2


@dataclass
class RuntimeConfig:
    mode: str = "backtest"
    log_level: str = "INFO"
    log_dir: str = "logs"
    state_dir: str = "state"
    run_once: bool = True


@dataclass
class Settings:
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    cost: TradingCostConfig = field(default_factory=TradingCostConfig)
    data: DataConfig = field(default_factory=DataConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)

    @staticmethod
    def _update_dataclass(obj, values: dict):
        for key, value in values.items():
            if hasattr(obj, key):
                setattr(obj, key, value)
        return obj

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        inst = cls()
        if not data:
            return inst
        if "exchange" in data:
            inst.exchange = cls._update_dataclass(inst.exchange, data["exchange"])
        if "strategy" in data:
            inst.strategy = cls._update_dataclass(inst.strategy, data["strategy"])
        if "risk" in data:
            inst.risk = cls._update_dataclass(inst.risk, data["risk"])
        if "cost" in data:
            inst.cost = cls._update_dataclass(inst.cost, data["cost"])
        if "data" in data:
            inst.data = cls._update_dataclass(inst.data, data["data"])
        if "runtime" in data:
            inst.runtime = cls._update_dataclass(inst.runtime, data["runtime"])
        return inst
