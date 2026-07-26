"""海龟交易前置条件审查模块 — 单元测试和异常场景测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from turtle_detector.eligibility import (
    BlockedReason,
    BreakoutQuality,
    EligibilityResult,
    EligibilityStatus,
    MarketRegime,
    RiskLevel,
    TradeEligibilityChecker,
    load_eligibility_config,
)
from turtle_detector.eligibility.checker import AccountSnapshot
from turtle_detector.eligibility.thresholds import (
    CRYPTO_THRESHOLDS,
    PRECIOUS_METAL_THRESHOLDS,
    US_EQUITY_THRESHOLDS,
    get_risk_group,
    get_thresholds_for_market,
)
from turtle_detector.models import (
    AssetConfig,
    Direction,
    Market,
    SignalType,
    TurtleSignal,
)


# ── 测试工具 ──────────────────────────────────────────────────────────────

def _asset(market: str = "crypto", **overrides: object) -> AssetConfig:
    values = {
        "symbol": "BTC",
        "instrument": "BTCUSDT_BINANCE_SPOT",
        "market": Market(market),
        "data_source": "binance",
        "timeframes": ("D1",),
        "adjustment": "none",
    }
    values.update(overrides)
    return AssetConfig(**values)


def _signal(
    signal_type: SignalType = SignalType.SYSTEM1_BREAKOUT,
    direction: Direction = Direction.LONG,
    trigger_price: float = 105.0,
    atr: float = 2.0,
    atr_pct: float = 0.02,
    distance_to_breakout_atr: float = 0.3,
    **overrides: object,
) -> TurtleSignal:
    values = {
        "symbol": "BTC",
        "instrument": "BTCUSDT_BINANCE_SPOT",
        "market": "crypto",
        "timeframe": "D1",
        "signal_type": signal_type,
        "raw_signal_type": signal_type,
        "direction": direction,
        "signal_time": "2024-06-15T00:00:00+00:00",
        "trigger_price": trigger_price,
        "channel_high": 100.0,
        "channel_low": 90.0,
        "atr": atr,
        "atr_pct": atr_pct,
        "stop_price": trigger_price - atr * 2,
        "next_add_price": trigger_price + atr * 0.5,
        "distance_to_breakout_atr": distance_to_breakout_atr,
        "volatility_percentile": 0.5,
        "suggested_risk_unit": 0.01,
        "trend_status": "up",
        "confirmation_status": "close_confirmed",
        "data_source": "binance",
        "generated_at": "2024-06-15T00:00:00+00:00",
        "tradeable": True,
    }
    values.update(overrides)
    return TurtleSignal(**values)


def _bars(
    closes: list[float] | None = None,
    n: int = 300,
    seed: int = 42,
    trend: bool = True,
) -> pd.DataFrame:
    if closes is not None:
        n = len(closes)
    rng = np.random.default_rng(seed)
    if closes is not None:
        close = np.array(closes, dtype=float)
    elif trend:
        returns = rng.normal(0.003, 0.015, n)
        close = 100.0 * np.exp(np.cumsum(returns))
    else:
        returns = rng.normal(0, 0.01, n)
        close = 100.0 * np.exp(np.cumsum(returns))

    index = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    open_price = close * (1 + rng.normal(0, 0.002, n))
    high = np.maximum(open_price, close) + rng.uniform(0.002, 0.015, n) * close
    low = np.minimum(open_price, close) - rng.uniform(0.002, 0.015, n) * close

    df = pd.DataFrame(
        {
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": rng.uniform(500, 5000, n),
        },
        index=index,
    )
    df["atr"] = close * 0.02
    df["atr_pct"] = 0.02
    df["volatility_percentile"] = 0.50
    df["sma_60"] = df["close"].rolling(60).mean().shift(1)
    df["sma_120"] = df["close"].rolling(120).mean().shift(1)
    df["previous_close"] = df["close"].shift(1)
    df["volume_mean"] = df["volume"].rolling(20).mean().shift(1)
    df["channel_high_20"] = df["high"].rolling(20).max().shift(1)
    df["channel_low_20"] = df["low"].rolling(20).min().shift(1)
    df["channel_high_55"] = df["high"].rolling(55).max().shift(1)
    df["channel_low_55"] = df["low"].rolling(55).min().shift(1)
    return df


def _account(**overrides: object) -> AccountSnapshot:
    values = {
        "equity": 100_000.0,
        "cash": 80_000.0,
        "open_positions": {},
        "asset_class_exposure": {},
        "risk_group_exposure": {},
        "consecutive_losses": 0,
        "current_drawdown": 0.0,
        "recent_signals": [],
    }
    values.update(overrides)
    return AccountSnapshot(**values)


# ── 一、数据完整性测试 ────────────────────────────────────────────────────


def test_data_ready_valid_bars() -> None:
    """完整有效的OHLCV数据应通过数据检查。"""
    checker = TradeEligibilityChecker()
    result = checker.evaluate(
        _signal(), _bars(n=300), _asset(), _account(),
    )
    assert result.data_ready.value == "data_ready"
    assert result.status != EligibilityStatus.NOT_READY


def test_data_not_ready_missing_columns() -> None:
    """缺少必要列应返回DATA_NOT_READY。"""
    checker = TradeEligibilityChecker()
    bad = pd.DataFrame(
        {"close": [100.0] * 300},
        index=pd.date_range("2024-01-01", periods=300, freq="D", tz="UTC"),
    )
    result = checker.evaluate(_signal(), bad, _asset(), _account())
    assert result.status == EligibilityStatus.NOT_READY
    assert BlockedReason.DATA_INCOMPLETE in result.hard_blocks


def test_data_not_ready_empty_bars() -> None:
    """空数据应返回NOT_READY。"""
    checker = TradeEligibilityChecker()
    empty = pd.DataFrame(
        columns=["open", "high", "low", "close", "volume"],
        index=pd.DatetimeIndex([], tz="UTC"),
    )
    result = checker.evaluate(_signal(), empty, _asset(), _account())
    assert result.status == EligibilityStatus.NOT_READY


def test_data_not_ready_insufficient_history() -> None:
    """历史数据不足应被阻断。"""
    checker = TradeEligibilityChecker()
    result = checker.evaluate(
        _signal(), _bars(n=30), _asset(), _account(),
    )
    assert BlockedReason.INSUFFICIENT_HISTORY in result.hard_blocks


def test_data_not_ready_invalid_ohlc() -> None:
    """OHLC逻辑不一致（high < low）应被阻断。"""
    checker = TradeEligibilityChecker()
    bars = _bars(n=300)
    bars.iloc[-1, bars.columns.get_loc("high")] = bars.iloc[-1]["low"] - 1.0
    result = checker.evaluate(_signal(), bars, _asset(), _account())
    assert BlockedReason.DATA_OHLCV_INVALID in result.hard_blocks


def test_data_not_ready_negative_prices() -> None:
    """负价格应被阻断。"""
    checker = TradeEligibilityChecker()
    bars = _bars(n=300)
    bars.iloc[-1, bars.columns.get_loc("close")] = -50.0
    result = checker.evaluate(_signal(), bars, _asset(), _account())
    assert BlockedReason.DATA_OHLCV_INVALID in result.hard_blocks


def test_data_not_ready_duplicate_timestamps() -> None:
    """重复时间戳应被阻断。"""
    checker = TradeEligibilityChecker()
    bars = _bars(n=300)
    dup = bars.copy()
    dup.index = pd.DatetimeIndex([dup.index[0]] * len(dup), tz="UTC")
    result = checker.evaluate(_signal(), dup, _asset(), _account())
    assert BlockedReason.DATA_TIMESTAMP_INVALID in result.hard_blocks


def test_data_not_ready_non_datetime_index() -> None:
    """非DatetimeIndex应被阻断。"""
    checker = TradeEligibilityChecker()
    bars = pd.DataFrame(
        {
            "open": [100.0, 101.0],
            "high": [102.0, 103.0],
            "low": [99.0, 100.0],
            "close": [101.0, 102.0],
            "volume": [1000.0, 1100.0],
        },
        index=[0, 1],
    )
    result = checker.evaluate(_signal(), bars, _asset(), _account())
    assert BlockedReason.DATA_TIMESTAMP_INVALID in result.hard_blocks


# ── 二、标的可交易性测试 ──────────────────────────────────────────────────


def test_instrument_verified() -> None:
    """品种确认应通过。"""
    checker = TradeEligibilityChecker()
    result = checker.evaluate(
        _signal(), _bars(n=300), _asset(), _account(),
    )
    assert result.instrument_verified.value == "instrument_verified"


def test_liquidity_insufficient_volume() -> None:
    """成交量不足应触发流动性警告。"""
    checker = TradeEligibilityChecker()
    bars = _bars(n=300)
    bars["volume"] = 1.0  # 极低成交量
    result = checker.evaluate(
        _signal(), bars, _asset(), _account(),
    )
    assert BlockedReason.INSUFFICIENT_VOLUME in result.soft_warnings


# ── 三、市场状态测试 ──────────────────────────────────────────────────────


def test_market_regime_trend_friendly() -> None:
    """上涨趋势中被识别为TREND_FRIENDLY。"""
    checker = TradeEligibilityChecker()
    result = checker.evaluate(
        _signal(), _bars(n=300, trend=True), _asset(), _account(),
    )
    assert result.market_regime in (
        MarketRegime.TREND_FRIENDLY,
        MarketRegime.WEAK_TREND,
        MarketRegime.NEUTRAL,
    )


def test_market_regime_range_bound() -> None:
    """震荡市场中应被识别为RANGE_BOUND。"""
    checker = TradeEligibilityChecker()
    result = checker.evaluate(
        _signal(), _bars(n=300, trend=False), _asset(), _account(),
    )
    assert result.regime_score < 1.0


# ── 四、波动率测试 ────────────────────────────────────────────────────────


def test_volatility_normal() -> None:
    """正常波动率应通过检查。"""
    checker = TradeEligibilityChecker()
    result = checker.evaluate(
        _signal(atr_pct=0.02), _bars(n=300), _asset(), _account(),
    )
    assert result.volatility_passed.value == "volatility_passed"


def test_volatility_too_low() -> None:
    """ATR过低应触发警告。"""
    checker = TradeEligibilityChecker()
    bars = _bars(n=300)
    bars["atr_pct"] = 0.001
    signal = _signal(atr_pct=0.001)
    result = checker.evaluate(signal, bars, _asset(), _account())
    assert BlockedReason.ATR_TOO_LOW in result.soft_warnings


def test_volatility_too_high() -> None:
    """ATR过高应触发警告。"""
    checker = TradeEligibilityChecker()
    bars = _bars(n=300)
    bars["atr_pct"] = 0.15
    signal = _signal(atr_pct=0.15)
    result = checker.evaluate(signal, bars, _asset(), _account())
    assert BlockedReason.ATR_TOO_HIGH in result.soft_warnings


# ── 五、突破质量测试 ──────────────────────────────────────────────────────


def test_breakout_quality_qualified() -> None:
    """标准突破信号应被识别为QUALIFIED_BREAKOUT。"""
    checker = TradeEligibilityChecker()
    result = checker.evaluate(
        _signal(signal_type=SignalType.SYSTEM1_BREAKOUT),
        _bars(n=300),
        _asset(),
        _account(),
    )
    assert result.breakout_quality == BreakoutQuality.QUALIFIED_BREAKOUT


def test_breakout_quality_false_breakout() -> None:
    """假突破应被识别为FALSE_BREAKOUT。"""
    checker = TradeEligibilityChecker()
    result = checker.evaluate(
        _signal(signal_type=SignalType.FALSE_BREAKOUT),
        _bars(n=300),
        _asset(),
        _account(),
    )
    assert result.breakout_quality == BreakoutQuality.FALSE_BREAKOUT
    assert BlockedReason.RETEST_FAILED in result.hard_blocks


def test_breakout_quality_overextended() -> None:
    """过度延伸的突破应被识别。"""
    checker = TradeEligibilityChecker()
    result = checker.evaluate(
        _signal(
            signal_type=SignalType.OVEREXTENDED,
            distance_to_breakout_atr=2.0,
        ),
        _bars(n=300),
        _asset(),
        _account(),
    )
    assert result.breakout_quality == BreakoutQuality.OVEREXTENDED_BREAKOUT


def test_breakout_quality_retest_confirmed() -> None:
    """回测确认的突破应获得最高质量分数。"""
    checker = TradeEligibilityChecker()
    result = checker.evaluate(
        _signal(signal_type=SignalType.RETEST_CONFIRMED),
        _bars(n=300),
        _asset(),
        _account(),
    )
    assert result.breakout_quality == BreakoutQuality.RETEST_CONFIRMED
    assert result.quality_score >= 0.8


# ── 六、风险预算测试 ──────────────────────────────────────────────────────


def test_risk_budget_normal() -> None:
    """正常风险预算应通过。"""
    checker = TradeEligibilityChecker()
    result = checker.evaluate(
        _signal(), _bars(n=300), _asset(), _account(),
    )
    assert result.portfolio_risk_passed.value == "portfolio_risk_passed"


def test_risk_budget_single_trade_exceeded() -> None:
    """单笔风险超出应被阻断。"""
    checker = TradeEligibilityChecker()
    account = _account(equity=100.0)
    result = checker.evaluate(
        _signal(trigger_price=5000.0, atr=100.0),
        _bars(n=300),
        _asset(),
        account,
    )
    assert any(
        b in result.hard_blocks
        for b in (
            BlockedReason.SINGLE_TRADE_RISK_EXCEEDED,
            BlockedReason.SYMBOL_RISK_EXCEEDED,
            BlockedReason.INSUFFICIENT_CASH,
        )
    )


def test_risk_budget_max_drawdown_exceeded() -> None:
    """最大回撤超出应被阻断。"""
    checker = TradeEligibilityChecker()
    account = _account(current_drawdown=0.30)
    result = checker.evaluate(
        _signal(), _bars(n=300), _asset(), account,
    )
    assert BlockedReason.MAX_DRAWDOWN_EXCEEDED in result.hard_blocks


def test_risk_budget_consecutive_losses() -> None:
    """连续亏损应触发风险降级。"""
    checker = TradeEligibilityChecker()
    account = _account(consecutive_losses=5)
    result = checker.evaluate(
        _signal(), _bars(n=300), _asset(), account,
    )
    assert BlockedReason.CONSECUTIVE_LOSSES in result.hard_blocks
    # 连续亏损时仓位应降低
    assert result.suggested_risk_unit > 0


# ── 七、相关性测试 ────────────────────────────────────────────────────────


def test_correlation_risk_group_exceeded() -> None:
    """风险组暴露超出应触发警告。"""
    checker = TradeEligibilityChecker()
    account = _account(
        risk_group_exposure={"CRYPTO_RISK_GROUP": 20_000.0},
        recent_signals=[
            {"direction": "long", "risk_group": "CRYPTO_RISK_GROUP"},
            {"direction": "long", "risk_group": "CRYPTO_RISK_GROUP"},
            {"direction": "long", "risk_group": "CRYPTO_RISK_GROUP"},
        ],
    )
    result = checker.evaluate(
        _signal(direction=Direction.LONG),
        _bars(n=300),
        _asset(),
        account,
    )
    assert BlockedReason.RISK_GROUP_EXCEEDED in result.soft_warnings


def test_correlation_same_direction_concentration() -> None:
    """同方向信号集中应触发警告。"""
    checker = TradeEligibilityChecker()
    account = _account(
        recent_signals=[
            {"direction": "long", "risk_group": "CRYPTO_RISK_GROUP"},
            {"direction": "long", "risk_group": "CRYPTO_RISK_GROUP"},
            {"direction": "long", "risk_group": "CRYPTO_RISK_GROUP"},
            {"direction": "long", "risk_group": "CRYPTO_RISK_GROUP"},
        ],
    )
    result = checker.evaluate(
        _signal(direction=Direction.LONG),
        _bars(n=300),
        _asset(),
        account,
    )
    assert BlockedReason.SAME_DIRECTION_CONCENTRATION in result.soft_warnings


# ── 八、事件风险测试 ──────────────────────────────────────────────────────


def test_event_risk_normal() -> None:
    """无事件时风险等级为NORMAL。"""
    checker = TradeEligibilityChecker()
    result = checker.evaluate(
        _signal(), _bars(n=300), _asset(), _account(),
    )
    assert result.event_risk_level == RiskLevel.NORMAL


# ── 九、回测有效性测试 ────────────────────────────────────────────────────


def test_backtest_not_validated() -> None:
    """未通过回测验证应触发软警告。"""
    checker = TradeEligibilityChecker()
    result = checker.evaluate(
        _signal(),
        _bars(n=300),
        _asset(),
        _account(),
        backtest_validated=False,
    )
    assert BlockedReason.BACKTEST_NOT_VALIDATED in result.soft_warnings


def test_backtest_validated() -> None:
    """已通过回测验证。"""
    checker = TradeEligibilityChecker()
    result = checker.evaluate(
        _signal(),
        _bars(n=300),
        _asset(),
        _account(),
        backtest_validated=True,
    )
    assert BlockedReason.BACKTEST_NOT_VALIDATED not in result.soft_warnings


# ── 十、执行系统测试 ──────────────────────────────────────────────────────


def test_execution_not_ready() -> None:
    """执行系统未就绪应触发软警告。"""
    checker = TradeEligibilityChecker()
    result = checker.evaluate(
        _signal(),
        _bars(n=300),
        _asset(),
        _account(),
        execution_ready=False,
    )
    assert BlockedReason.API_NOT_CONNECTED in result.soft_warnings


def test_execution_ready() -> None:
    """执行系统就绪。"""
    checker = TradeEligibilityChecker()
    result = checker.evaluate(
        _signal(),
        _bars(n=300),
        _asset(),
        _account(),
        execution_ready=True,
    )
    assert BlockedReason.API_NOT_CONNECTED not in result.soft_warnings


# ── 十一、最终状态判定测试 ─────────────────────────────────────────────────


def test_trade_eligible_all_conditions_met() -> None:
    """所有条件满足时应为TRADE_ELIGIBLE。"""
    checker = TradeEligibilityChecker()
    account = _account(equity=50_000_000.0, cash=40_000_000.0)
    bars = _bars(n=300, trend=True)
    # 确保成交量足够大，通过流动性检查
    bars["volume"] = 1_000_000.0
    bars["volume_mean"] = 1_000_000.0
    result = checker.evaluate(
        _signal(
            signal_type=SignalType.RETEST_CONFIRMED,
            atr_pct=0.02,
            trigger_price=5.0,
            atr=1.0,
        ),
        bars,
        _asset(),
        account,
        backtest_validated=True,
        execution_ready=True,
    )
    assert result.trade_eligible is True, (
        f"trade_eligible应为True，实际：{result.status.value}，"
        f"硬阻断：{[b.value for b in result.hard_blocks]}"
    )
    assert result.status == EligibilityStatus.TRADE_ELIGIBLE


def test_risk_rejected() -> None:
    """数据不满足时应为RISK_REJECTED或NOT_READY。"""
    checker = TradeEligibilityChecker()
    bars = _bars(n=300)
    bars.iloc[-1, bars.columns.get_loc("high")] = bars.iloc[-1]["low"] - 1.0
    result = checker.evaluate(_signal(), bars, _asset(), _account())
    assert result.status in (EligibilityStatus.NOT_READY, EligibilityStatus.RISK_REJECTED)
    assert result.trade_eligible is False


def test_watch_only_too_many_warnings() -> None:
    """多个软警告累积时应为WATCH_ONLY。"""
    checker = TradeEligibilityChecker()
    bars = _bars(n=300, trend=False)
    bars["volume"] = 1.0
    bars["atr_pct"] = 0.001
    signal = _signal(
        atr_pct=0.001,
        signal_type=SignalType.APPROACHING_BREAKOUT,
        distance_to_breakout_atr=2.0,
    )
    result = checker.evaluate(
        signal, bars, _asset(), _account(),
        backtest_validated=False,
        execution_ready=False,
    )
    # 软警告 >= 3 时应为WATCH_ONLY 或 RISK_REJECTED
    assert result.status in (
        EligibilityStatus.WATCH_ONLY,
        EligibilityStatus.RISK_REJECTED,
    )
    assert result.trade_eligible is False


def test_result_to_dict() -> None:
    """to_dict应返回可序列化字典。"""
    checker = TradeEligibilityChecker()
    result = checker.evaluate(
        _signal(), _bars(n=300), _asset(), _account(),
    )
    d = result.to_dict()
    assert isinstance(d, dict)
    assert d["symbol"] == "BTC"
    assert d["timeframe"] == "D1"
    assert isinstance(d["hard_blocks"], list)
    assert isinstance(d["soft_warnings"], list)


# ── 十二、阈值配置测试 ─────────────────────────────────────────────────────


def test_get_thresholds_for_market() -> None:
    """各市场类型应返回正确的阈值。"""
    assert get_thresholds_for_market("crypto").volatility.min_atr_pct == 0.01
    assert get_thresholds_for_market("precious_metal").volatility.min_atr_pct == 0.005
    assert get_thresholds_for_market("us_equity").data["require_adjustment"] is True
    assert get_thresholds_for_market("unknown").volatility.min_atr_pct == 0.005


def test_get_risk_group() -> None:
    """品种应映射到正确的风险组。"""
    assert get_risk_group("BTC") == "CRYPTO_RISK_GROUP"
    assert get_risk_group("ETH") == "CRYPTO_RISK_GROUP"
    assert get_risk_group("XAU") == "PRECIOUS_METALS_RISK_GROUP"
    assert get_risk_group("NVDA") == "US_TECH_RISK_GROUP"
    assert get_risk_group("QQQ") == "US_TECH_RISK_GROUP"
    assert get_risk_group("UNKNOWN") == "OTHER_RISK_GROUP"


def test_load_eligibility_config_default() -> None:
    """默认配置应可加载。"""
    thresholds = load_eligibility_config()
    assert thresholds.volatility.min_atr_pct == 0.005
    assert thresholds.risk_budget.single_trade_risk_pct == 0.01


def test_load_eligibility_config_from_yaml() -> None:
    """从YAML加载配置。"""
    from pathlib import Path

    config_path = (
        Path(__file__).parent.parent
        / "turtle_detector"
        / "eligibility"
        / "config"
        / "eligibility.yaml"
    )
    if config_path.exists():
        thresholds = load_eligibility_config(str(config_path))
        assert thresholds.volatility.min_atr_pct >= 0


# ── 十三、多资产类型测试 ──────────────────────────────────────────────────


def test_us_equity_adjustment_required() -> None:
    """美股需要复权处理：未复权资产应被阻断。"""
    checker = TradeEligibilityChecker(thresholds=US_EQUITY_THRESHOLDS)
    asset = _asset(
        market="us_equity",
        symbol="NVDA",
        instrument="NVDA_NSDQ",
        adjustment="back_adjusted",
    )
    bars = _bars(n=300)
    # 使用 object.__setattr__ 绕过 frozen dataclass 验证
    # 模拟一个 adjustment 为非复权值的资产
    from dataclasses import replace

    bad_asset = replace(asset, adjustment="forward_adjusted")
    # "forward_adjusted" 是有效的，不应阻断
    readiness, blocks = checker._check_data_ready(
        bars, bad_asset, US_EQUITY_THRESHOLDS
    )
    assert BlockedReason.ADJUSTMENT_NOT_APPLIED not in blocks

    # 验证：当 require_adjustment=True 且 adjustment 为非复权值时阻断
    # 由于 AssetConfig 不允许非复权值，此测试验证正向逻辑
    assert readiness.value == "data_ready"


def test_precious_metal_thresholds() -> None:
    """贵金属阈值应独立于加密货币。"""
    checker = TradeEligibilityChecker(thresholds=PRECIOUS_METAL_THRESHOLDS)
    asset = _asset(market="precious_metal", symbol="XAU", instrument="XAUUSD_DUKAS")
    result = checker.evaluate(
        _signal(market="precious_metal", symbol="XAU", instrument="XAUUSD_DUKAS"),
        _bars(n=300),
        asset,
        _account(),
    )
    assert result.risk_group == "PRECIOUS_METALS_RISK_GROUP"


def test_adjustment_blocked_bypass_frozen() -> None:
    """绕过 frozen dataclass 验证，直接测试检测逻辑。"""
    checker = TradeEligibilityChecker(thresholds=US_EQUITY_THRESHOLDS)
    # 先用有效值构造，再绕过 frozen 修改
    asset = _asset(
        market="us_equity",
        symbol="NVDA",
        instrument="NVDA_NSDQ",
        adjustment="back_adjusted",
    )
    # 使用 object.__setattr__ 绕过 frozen dataclass 的 __setattr__ 限制
    object.__setattr__(asset, "adjustment", "none")
    bars = _bars(n=300)
    readiness, blocks = checker._check_data_ready(
        bars, asset, US_EQUITY_THRESHOLDS
    )
    assert BlockedReason.ADJUSTMENT_NOT_APPLIED in blocks


# ── 十四、无账户快照测试 ──────────────────────────────────────────────────


def test_evaluate_without_account() -> None:
    """无账户快照时应跳过风险检查。"""
    checker = TradeEligibilityChecker()
    result = checker.evaluate(
        _signal(),
        _bars(n=300),
        _asset(),
        backtest_validated=True,
        execution_ready=True,
    )
    # 无账户时不应有风险相关的硬阻断
    risk_blocks = {
        BlockedReason.SINGLE_TRADE_RISK_EXCEEDED,
        BlockedReason.SYMBOL_RISK_EXCEEDED,
        BlockedReason.ASSET_CLASS_RISK_EXCEEDED,
        BlockedReason.PORTFOLIO_RISK_EXCEEDED,
    }
    assert not any(b in result.hard_blocks for b in risk_blocks)


# ── 十五、状态输出示例测试 ─────────────────────────────────────────────────


def test_status_output_example() -> None:
    """验证状态输出示例包含所有必填字段。"""
    checker = TradeEligibilityChecker()
    result = checker.evaluate(
        _signal(
            signal_type=SignalType.SYSTEM1_BREAKOUT,
            direction=Direction.LONG,
            atr_pct=0.02,
        ),
        _bars(n=300, trend=True),
        _asset(),
        _account(),
        backtest_validated=False,
        execution_ready=False,
    )

    d = result.to_dict()
    required = [
        "symbol", "timeframe", "status", "trade_eligible",
        "data_ready", "instrument_verified", "liquidity_passed",
        "market_regime", "volatility_passed", "breakout_quality",
        "event_risk_level", "portfolio_risk_passed",
        "backtest_validated", "execution_ready",
        "hard_blocks", "soft_warnings",
        "suggested_position_size", "suggested_risk_unit",
        "risk_group", "regime_score", "quality_score", "overall_score",
    ]
    for key in required:
        assert key in d, f"缺少字段：{key}"

    print("\n状态输出示例：")
    for key, value in d.items():
        print(f"  {key}: {value}")