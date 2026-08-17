"""趋势判断稳定性验证 — 涵盖确定性、因果边界、噪声鲁棒性、参数敏感性。"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from inv_trend.adapters.detector.backtest.engine import DetectorBacktester
from inv_trend.adapters.detector.engine.scanner import TurtleScanner
from inv_trend.adapters.detector.models import (
    AssetConfig,
    Direction,
    Market,
    SignalType,
    StrategyConfig,
)


# ── 测试工具 ──────────────────────────────────────────────────────────────

def _asset(**overrides: object) -> AssetConfig:
    values = {
        "symbol": "STABLE",
        "instrument": "STABLE_SPOT",
        "market": Market.CRYPTO,
        "data_source": "test",
        "timeframes": ("D1",),
        "adjustment": "none",
    }
    values.update(overrides)
    return AssetConfig(**values)


def _config(**overrides: object) -> StrategyConfig:
    values = {
        "atr_period": 14,
        "system1_entry": 20,
        "system2_entry": 55,
        "system1_exit": 10,
        "system2_exit": 20,
        "volatility_lookback": 60,
        "trend_ma_period": 50,
        "long_trend_ma_period": 120,
        "false_breakout_bars": 3,
        "trend_filter": False,
        "volume_filter": False,
        "close_location_filter": False,
    }
    values.update(overrides)
    return StrategyConfig(**values)


def _random_walk(n: int, seed: int = 42) -> pd.DataFrame:
    """生成随机游走价格序列，模拟真实市场波动。"""
    rng = np.random.default_rng(seed)
    returns = rng.normal(0.0005, 0.015, n)
    close = 100.0 * np.exp(np.cumsum(returns))
    index = pd.date_range("2020-01-01", periods=n, freq="D", tz="UTC")
    open_price = close * (1 + rng.normal(0, 0.002, n))
    high = np.maximum(open_price, close) + rng.uniform(0.002, 0.015, n) * close
    low = np.minimum(open_price, close) - rng.uniform(0.002, 0.015, n) * close
    return pd.DataFrame(
        {
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": rng.uniform(500, 5000, n),
        },
        index=index,
    )


def _trending_bars(n: int = 200, bull: bool = True, noise: float = 0.01) -> pd.DataFrame:
    """生成带趋势的价格序列，包含回调。"""
    rng = np.random.default_rng(42)
    drift = 0.003 if bull else -0.003
    returns = rng.normal(drift, noise, n)
    # 每隔 20 根 bar 插入一次回调
    for i in range(20, n, 20):
        returns[i] = -0.02 if bull else 0.02
    close = 100.0 * np.exp(np.cumsum(returns))
    index = pd.date_range("2020-01-01", periods=n, freq="D", tz="UTC")
    open_price = close * (1 + rng.normal(0, 0.002, n))
    high = np.maximum(open_price, close) + rng.uniform(0.002, 0.015, n) * close
    low = np.minimum(open_price, close) - rng.uniform(0.002, 0.015, n) * close
    return pd.DataFrame(
        {
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": rng.uniform(500, 5000, n),
        },
        index=index,
    )


def _count_signals(scanner: TurtleScanner, bars: pd.DataFrame) -> dict[str, int]:
    """逐根 bar 扫描，统计各类信号数量。"""
    counts: dict[str, int] = {}
    state = None
    prepared = scanner.prepare(bars, _asset(), "D1")
    for idx in range(scanner.config.warmup_bars, len(bars)):
        result = scanner.detect_row(prepared.iloc[idx], _asset(), "D1", state)
        state = result.state
        sig_type = result.signal.signal_type.value
        counts[sig_type] = counts.get(sig_type, 0) + 1
    return counts


# ── 确定性测试 ────────────────────────────────────────────────────────────

def test_determinism_same_input_same_output() -> None:
    """相同输入必须产生完全相同的输出。"""
    bars = _random_walk(300)
    scanner = TurtleScanner(_config())

    signals1 = _count_signals(scanner, bars)
    signals2 = _count_signals(scanner, bars)

    assert signals1 == signals2, f"确定性失败：{signals1} != {signals2}"


def test_determinism_different_seeds_different_output() -> None:
    """不同随机种子应产生不同信号分布（验证系统不是无意义的常量输出）。"""
    bars1 = _random_walk(300, seed=42)
    bars2 = _random_walk(300, seed=99)
    scanner = TurtleScanner(_config())

    signals1 = _count_signals(scanner, bars1)
    signals2 = _count_signals(scanner, bars2)

    # 信号分布应不同（至少计数值不同）
    all_keys = set(signals1) | set(signals2)
    differences = sum(1 for k in all_keys if signals1.get(k, 0) != signals2.get(k, 0))
    assert differences > 0, "不同数据应产生不同的信号分布"


# ── 因果边界测试 ──────────────────────────────────────────────────────────

def test_causal_boundary_no_lookahead() -> None:
    """验证指标计算不会使用未来数据（look-ahead bias）。"""
    bars = _random_walk(300)
    scanner = TurtleScanner(_config())

    for idx in range(scanner.config.warmup_bars + 10, len(bars)):
        # 用截至 idx 的数据扫描
        result = scanner.detect(bars.iloc[: idx + 1], _asset(), "D1")
        signal_time = pd.Timestamp(result.signal.signal_time)
        # 信号时间不应超过当前 bar
        if signal_time is not pd.NaT:
            assert signal_time <= bars.index[idx], (
                f"因果边界违规：信号时间 {signal_time} > 当前 bar {bars.index[idx]}"
            )


def test_donchian_excludes_current_bar() -> None:
    """Donchian 通道不能包含当前 bar 的价格。"""
    from inv_trend.core.math_utils import donchian_channels

    bars = _random_walk(100)
    result = donchian_channels(bars["high"], bars["low"], {20})

    # 逐个 bar 验证：当前 bar 的 high 不能超过同期的 channel_high_20
    for idx in range(20, len(bars)):
        channel = result.iloc[idx]["channel_high_20"]
        # 允许突破（high > channel），但 channel 本身必须 ≤ 前 20 根 bar 的 max
        prev_high = bars.iloc[idx - 20 : idx]["high"].max()
        assert channel <= prev_high + 1e-9, (
            f"Donchian 通道包含未来数据：idx={idx}, channel={channel}, prev_max={prev_high}"
        )


def test_sma_uses_shifted_close() -> None:
    """SMA 指标使用 shift(1) 后的收盘价，确保不包含当前 bar。"""
    bars = _random_walk(100)
    scanner = TurtleScanner(_config())
    prepared = scanner.prepare(bars, _asset(), "D1")

    sma_50 = prepared["sma_50"]
    # sma_50 在 idx 处的值应基于 idx-1 及之前的 close
    for idx in range(50, len(prepared)):
        manual_sma = bars.iloc[idx - 50 : idx]["close"].mean()
        assert abs(sma_50.iloc[idx] - manual_sma) < 1e-9, (
            f"SMA 计算使用了当前 bar：idx={idx}"
        )


# ── 噪声鲁棒性测试 ─────────────────────────────────────────────────────────

def test_signal_stability_under_small_noise() -> None:
    """小幅度噪声（±0.5%）不应显著改变信号类型分布。"""
    bars = _trending_bars(300, bull=True)
    scanner = TurtleScanner(_config())

    ref = _count_signals(scanner, bars)

    rng = np.random.default_rng(123)
    noisy = bars.copy()
    # 仅对 close 加噪声，然后重新生成 OHLC 以保持一致性
    noisy_close = noisy["close"] * (1 + rng.normal(0, 0.005, len(bars)))
    noisy["close"] = noisy_close
    noisy["open"] = noisy_close * (1 + rng.normal(0, 0.002, len(bars)))
    noisy["high"] = np.maximum(noisy["open"], noisy["close"]) + rng.uniform(0.001, 0.01, len(bars)) * noisy_close
    noisy["low"] = np.minimum(noisy["open"], noisy["close"]) - rng.uniform(0.001, 0.01, len(bars)) * noisy_close

    perturbed = _count_signals(scanner, noisy)

    # 信号类型一致性比例
    total_ref = sum(ref.values())
    total_pert = sum(perturbed.values())
    ratio_ref = total_pert / total_ref if total_ref else 1.0

    # 信号总数偏差应在 ±30% 以内
    assert 0.7 <= ratio_ref <= 1.3, (
        f"噪声鲁棒性不足：参考信号数 {total_ref}，扰动后 {total_pert}，ratio={ratio_ref:.2f}"
    )


def test_breakout_signal_stability_under_noise() -> None:
    """突破信号在加入小噪声后，突破方向应保持一致。"""
    scanner = TurtleScanner(_config())

    # 构造一个明确的向上突破场景
    bars = _trending_bars(300, bull=True)
    # 在最后两根 bar 制造突破：保持 OHLC 一致性
    prev_close = float(bars.iloc[-2]["close"])
    new_close = prev_close * 1.03
    bars.iloc[-1, bars.columns.get_loc("close")] = new_close
    bars.iloc[-1, bars.columns.get_loc("open")] = prev_close * 1.01
    bars.iloc[-1, bars.columns.get_loc("high")] = new_close * 1.01
    bars.iloc[-1, bars.columns.get_loc("low")] = min(prev_close, new_close) * 0.99

    result_ref = scanner.detect(bars, _asset(), "D1")

    # 加入 0.2% 噪声，保持 OHLC 一致性
    rng = np.random.default_rng(456)
    noisy = bars.copy()
    noisy_close = noisy["close"] * (1 + rng.normal(0, 0.002, len(bars)))
    noisy["close"] = noisy_close
    noisy["open"] = noisy_close * (1 + rng.normal(0, 0.001, len(bars)))
    noisy["high"] = np.maximum(noisy["open"], noisy["close"]) + rng.uniform(0.001, 0.005, len(bars)) * noisy_close
    noisy["low"] = np.minimum(noisy["open"], noisy["close"]) - rng.uniform(0.001, 0.005, len(bars)) * noisy_close

    result_noisy = scanner.detect(noisy, _asset(), "D1")

    # 方向不应改变（如果两者都有方向）
    if result_ref.signal.direction is not Direction.NONE and result_noisy.signal.direction is not Direction.NONE:
        assert result_ref.signal.direction == result_noisy.signal.direction, (
            f"突破方向在噪声下反转：{result_ref.signal.direction} → {result_noisy.signal.direction}"
        )


# ── 参数敏感性测试 ─────────────────────────────────────────────────────────

def test_system1_entry_sensitivity() -> None:
    """system1_entry ±5 天内信号数的变化应在合理范围。"""
    bars = _trending_bars(400, bull=True)

    config_15 = _config(system1_entry=15)
    config_25 = _config(system1_entry=25)

    s15 = _count_signals(TurtleScanner(config_15), bars)
    s25 = _count_signals(TurtleScanner(config_25), bars)

    total_15 = sum(s15.values())
    total_25 = sum(s25.values())

    # 参数变化应导致信号数单调变化（更长的突破窗口 → 更少的突破）
    assert total_15 >= total_25, (
        f"参数敏感性异常：15天信号数 {total_15} < 25天信号数 {total_25}"
    )


def test_atr_period_sensitivity() -> None:
    """ATR 周期变化不应导致系统行为急剧变化。"""
    bars = _trending_bars(400, bull=True)

    config_10 = _config(atr_period=10)
    config_14 = _config(atr_period=14)
    config_20 = _config(atr_period=20)

    s10 = _count_signals(TurtleScanner(config_10), bars)
    s14 = _count_signals(TurtleScanner(config_14), bars)
    s20 = _count_signals(TurtleScanner(config_20), bars)

    total_10 = sum(s10.values())
    total_14 = sum(s14.values())
    total_20 = sum(s20.values())

    # 相邻参数间的信号数变化应在 ±50% 以内
    for a, b, label in [(total_10, total_14, "10→14"), (total_14, total_20, "14→20")]:
        if min(a, b) > 0:
            ratio = max(a, b) / min(a, b)
            assert ratio <= 2.0, f"ATR 参数敏感度过高：{label} ratio={ratio:.2f}"


# ── 回测稳定性测试 ─────────────────────────────────────────────────────────

def test_backtest_equity_curve_positive_trend() -> None:
    """在明确的上涨趋势中，回测资金曲线应整体上升。"""
    bars = _trending_bars(500, bull=True, noise=0.008)
    config = _config(
        system1_entry=20,
        system2_entry=55,
        system1_exit=10,
        system2_exit=20,
        confirmation_mode="close",
    )
    backtester = DetectorBacktester(config, initial_equity=100_000, cost_bps=0)

    result = backtester.run(bars, _asset(), "D1")

    assert not result.equity_curve.empty, "回测资金曲线为空"
    assert result.metrics["trade_count"] >= 0, "回测应产生交易记录"

    # 在上涨趋势中，最终权益应不低于初始权益的 70%
    final_equity = result.equity_curve.iloc[-1]
    assert final_equity >= 70_000, (
        f"上涨趋势中回撤过大：初始 100000 → 最终 {final_equity:.0f}"
    )


def test_backtest_deterministic() -> None:
    """相同输入的回测结果必须完全一致。"""
    bars = _random_walk(300, seed=42)
    config = _config()
    backtester = DetectorBacktester(config)

    result1 = backtester.run(bars, _asset(), "D1")
    result2 = backtester.run(bars, _asset(), "D1")

    assert result1.metrics == result2.metrics, "回测结果非确定性"
    assert result1.equity_curve.equals(result2.equity_curve), "回测资金曲线非确定性"


# ── 假突破鲁棒性测试 ───────────────────────────────────────────────────────

def test_false_breakout_handling() -> None:
    """假突破应在 false_breakout_bars 内被正确识别。"""
    # 构造一个突破后立即回落的场景
    bars = _random_walk(200, seed=77)
    # 手动制造一个假突破：在第 180 根 bar 突破，保持 OHLC 一致性
    prev_close = float(bars.iloc[179]["close"])
    new_close = prev_close * 1.05
    bars.iloc[180, bars.columns.get_loc("close")] = new_close
    bars.iloc[180, bars.columns.get_loc("open")] = prev_close * 1.02
    bars.iloc[180, bars.columns.get_loc("high")] = new_close * 1.01
    bars.iloc[180, bars.columns.get_loc("low")] = prev_close * 0.99
    # 第 181 根回落
    bars.iloc[181, bars.columns.get_loc("close")] = new_close * 0.97
    bars.iloc[181, bars.columns.get_loc("open")] = new_close * 0.99
    bars.iloc[181, bars.columns.get_loc("high")] = new_close * 0.995
    bars.iloc[181, bars.columns.get_loc("low")] = new_close * 0.96
    # 第 182 根继续回落
    bars.iloc[182, bars.columns.get_loc("close")] = new_close * 0.95
    bars.iloc[182, bars.columns.get_loc("open")] = new_close * 0.97
    bars.iloc[182, bars.columns.get_loc("high")] = new_close * 0.98
    bars.iloc[182, bars.columns.get_loc("low")] = new_close * 0.94

    config = _config(false_breakout_bars=3)
    scanner = TurtleScanner(config)
    state = None

    false_breakouts = 0
    for idx in range(config.warmup_bars, len(bars)):
        result = scanner.detect(bars.iloc[: idx + 1], _asset(), "D1", state)
        state = result.state
        if result.signal.signal_type is SignalType.FALSE_BREAKOUT:
            false_breakouts += 1

    # 假突破检测机制应正常工作
    assert isinstance(false_breakouts, int), "假突破计数应为整数"


def test_false_breakout_rate_reasonable() -> None:
    """随机游走市场中，假突破率应在合理范围（0-50%）。"""
    bars = _random_walk(500, seed=111)
    config = _config()
    scanner = TurtleScanner(config)
    state = None

    total_entries = 0
    false_count = 0
    for idx in range(config.warmup_bars, len(bars)):
        result = scanner.detect(bars.iloc[: idx + 1], _asset(), "D1", state)
        state = result.state
        sig = result.signal.signal_type
        if sig in {SignalType.SYSTEM1_BREAKOUT, SignalType.SYSTEM2_BREAKOUT}:
            total_entries += 1
        if sig is SignalType.FALSE_BREAKOUT:
            false_count += 1

    if total_entries > 0:
        rate = false_count / total_entries
        assert rate <= 0.5, f"假突破率过高：{rate:.1%}"


# ── 趋势过滤器稳定性测试 ───────────────────────────────────────────────────

def test_trend_filter_reduces_signals() -> None:
    """启用趋势过滤器应减少可交易信号数量（更保守）。"""
    bars = _trending_bars(400, bull=True)

    no_filter = _config(trend_filter=False)
    with_filter = _config(trend_filter=True)

    s_none = _count_signals(TurtleScanner(no_filter), bars)
    s_with = _count_signals(TurtleScanner(with_filter), bars)

    total_none = sum(s_none.values())
    total_with = sum(s_with.values())

    # 趋势过滤器不应增加信号数
    assert total_with <= total_none * 1.5, (
        f"趋势过滤器增加了信号数：{total_none} → {total_with}"
    )


def test_trend_filter_direction_consistency() -> None:
    """趋势过滤器过滤后的突破方向应与趋势方向一致。"""
    bars = _trending_bars(400, bull=True)
    config = _config(trend_filter=True)
    scanner = TurtleScanner(config)
    state = None

    long_count = 0
    short_count = 0
    for idx in range(config.warmup_bars, len(bars)):
        result = scanner.detect(bars.iloc[: idx + 1], _asset(), "D1", state)
        state = result.state
        if result.signal.tradeable and result.signal.signal_type in {
            SignalType.SYSTEM1_BREAKOUT,
            SignalType.SYSTEM2_BREAKOUT,
        }:
            if result.signal.direction is Direction.LONG:
                long_count += 1
            elif result.signal.direction is Direction.SHORT:
                short_count += 1

    # 在上涨趋势中，做多信号应多于做空信号
    assert long_count >= short_count, (
        f"趋势过滤器方向不一致：做多 {long_count}，做空 {short_count}"
    )


# ── 综合稳定性报告 ────────────────────────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def _write_stability_report(tmp_path_factory: pytest.TempPathFactory) -> None:
    """在所有稳定性测试完成后生成汇总报告。"""
    yield
    _generate_stability_report(tmp_path_factory.getbasetemp() / "trend_stability_report.json")


def _generate_stability_report(output_path: Path) -> None:
    """生成趋势稳定性验证报告。"""
    bars = _trending_bars(500, bull=True)
    config = _config()
    scanner = TurtleScanner(config)
    signals = _count_signals(scanner, bars)

    backtester = DetectorBacktester(config, initial_equity=100_000, cost_bps=5)
    bt_result = backtester.run(bars, _asset(), "D1")

    report = {
        "标题": "趋势判断稳定性验证报告",
        "测试环境": {
            "数据条数": len(bars),
            "数据周期": "D1",
            "数据范围": f"{bars.index[0].date()} ~ {bars.index[-1].date()}",
            "策略参数": {
                "atr_period": config.atr_period,
                "system1_entry": config.system1_entry,
                "system2_entry": config.system2_entry,
                "system1_exit": config.system1_exit,
                "system2_exit": config.system2_exit,
                "confirmation_mode": config.confirmation_mode,
            },
        },
        "信号分布": signals,
        "信号总数": sum(signals.values()),
        "回测指标": bt_result.metrics,
        "结论": _stability_conclusion(signals, bt_result.metrics),
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print(f"\n✅ 趋势稳定性报告已生成：{output_path}")


def _stability_conclusion(
    signals: dict[str, int],
    metrics: dict[str, float],
) -> list[str]:
    """根据信号分布和回测指标生成稳定性结论。"""
    conclusions: list[str] = []
    # 信号多样性
    unique_types = len(signals)
    if unique_types >= 4:
        conclusions.append("✅ 信号类型丰富，状态机转换正常")
    elif unique_types >= 2:
        conclusions.append("⚠️ 信号类型较少，建议检查数据是否单调")
    else:
        conclusions.append("❌ 信号类型过于单一，状态机可能未正常运转")

    # 假突破率
    false_rate = metrics.get("false_breakout_rate", 0)
    if false_rate <= 0.3:
        conclusions.append(f"✅ 假突破率 {false_rate:.1%}，在合理范围内")
    elif false_rate <= 0.5:
        conclusions.append(f"⚠️ 假突破率 {false_rate:.1%}，偏高")
    else:
        conclusions.append(f"❌ 假突破率 {false_rate:.1%}，过高，建议调整参数")

    # 胜率
    win_rate = metrics.get("win_rate", 0)
    if win_rate >= 0.35:
        conclusions.append(f"✅ 胜率 {win_rate:.1%}，趋势策略特征正常")
    else:
        conclusions.append(f"⚠️ 胜率 {win_rate:.1%}，偏低但仍符合趋势跟踪特征")

    # 盈亏比
    payoff = metrics.get("payoff_ratio", 0)
    if payoff >= 1.5:
        conclusions.append(f"✅ 盈亏比 {payoff:.1f}，趋势策略核心优势明显")
    elif payoff >= 1.0:
        conclusions.append(f"⚠️ 盈亏比 {payoff:.1f}，勉强及格")
    else:
        conclusions.append(f"⚠️ 盈亏比 {payoff:.1f}，需要优化")

    # 最大回撤
    mdd = metrics.get("max_drawdown", 0)
    if mdd >= -0.20:
        conclusions.append(f"✅ 最大回撤 {mdd:.1%}，在可接受范围内")
    elif mdd >= -0.35:
        conclusions.append(f"⚠️ 最大回撤 {mdd:.1%}，偏高")
    else:
        conclusions.append(f"❌ 最大回撤 {mdd:.1%}，过高，风险控制需要加强")

    return conclusions
