"""海龟交易前置条件审查核心引擎。

审查流程：数据验证 → 品种确认 → 流动性检查 → 市场状态识别 →
波动率判断 → 突破质量评估 → 风险预算 → 相关性 → 事件风险 →
回测有效性 → 执行系统 → 最终判定。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from ..models import AssetConfig, SignalType, TurtleSignal
from .models import (
    BlockedReason,
    BreakoutQuality,
    EligibilityResult,
    EligibilityStatus,
    EligibilityVerdict,
    EligibilityThresholds,
    MarketRegime,
    RiskLevel,
    SignalReadiness,
)
from .thresholds import get_risk_group, get_thresholds_for_market

LOGGER = logging.getLogger(__name__)

_RETRYABLE_BLOCKS = {
    BlockedReason.ACCOUNT_NOT_SYNCED,
    BlockedReason.API_NOT_CONNECTED,
    BlockedReason.DATA_INCOMPLETE,
    BlockedReason.BAR_NOT_CLOSED,
    BlockedReason.MARKET_CLOSED,
}


@dataclass
class AccountSnapshot:
    """账户快照，用于风险预算检查。"""

    equity: float = 100_000.0
    cash: float = 100_000.0
    open_positions: dict[str, float] = field(default_factory=dict)
    asset_class_exposure: dict[str, float] = field(default_factory=dict)
    risk_group_exposure: dict[str, float] = field(default_factory=dict)
    consecutive_losses: int = 0
    current_drawdown: float = 0.0
    recent_signals: list[dict[str, Any]] = field(default_factory=list)


class TradeEligibilityChecker:
    """海龟交易前置条件审查器。

    在突破信号生成后、仓位执行前，对数据、标的、市场状态、
    波动率、突破质量、风险预算、相关性、事件风险、策略有效性
    和执行系统进行全面审查。

    使用方法::

        checker = TradeEligibilityChecker()
        result = checker.evaluate(
            signal=breakout_signal,
            bars=price_data,
            asset=asset_config,
            account=AccountSnapshot(),
        )
        if result.trade_eligible:
            # 进入仓位执行模块
            ...
    """

    def __init__(
        self,
        thresholds: EligibilityThresholds | None = None,
    ) -> None:
        self.thresholds = thresholds or get_thresholds_for_market("crypto")

    # ── 主入口 ──────────────────────────────────────────────────────────

    def evaluate(
        self,
        signal: TurtleSignal,
        bars: pd.DataFrame,
        asset: AssetConfig,
        account: AccountSnapshot | None = None,
        backtest_validated: bool = False,
        execution_ready: bool = False,
        require_account: bool = False,
    ) -> EligibilityResult:
        """执行完整的前置条件审查。

        Args:
            signal: 已生成的突破信号
            bars: 价格数据（含指标列）
            asset: 品种配置
            account: 账户快照，为None时跳过风险预算检查
            backtest_validated: 该标的是否已通过回测验证
            execution_ready: 执行系统是否就绪

        Returns:
            EligibilityResult: 完整审查结果
        """
        thresholds = self._resolve_thresholds(asset)
        risk_group = get_risk_group(asset.symbol)
        hard_blocks: list[BlockedReason] = []
        soft_warnings: list[BlockedReason] = []

        if bars.empty:
            return self._build_result(
                signal, asset, thresholds, risk_group,
                [BlockedReason.DATA_INCOMPLETE], [],
                status=EligibilityStatus.NOT_READY,
            )
        row = bars.iloc[-1]

        # 1. 数据完整性
        data_ready, data_blocks = self._check_data_ready(bars, asset, thresholds)
        if data_blocks:
            hard_blocks.extend(data_blocks)
            return self._build_result(
                signal, asset, thresholds, risk_group,
                hard_blocks, soft_warnings,
                status=EligibilityStatus.NOT_READY,
                data_ready=data_ready,
            )

        # 2. 品种确认
        instrument_verified = self._check_instrument(bars, asset, thresholds)

        # 3. 流动性
        liquidity_passed, liquidity_blocks = self._check_liquidity(
            row, bars, asset, thresholds
        )
        soft_warnings.extend(liquidity_blocks)

        # 4. 市场状态
        regime, regime_score, regime_blocks = self._check_market_regime(
            row, bars, thresholds
        )
        if MarketRegime.EXTREME_RISK in (regime,):
            hard_blocks.append(BlockedReason.MARKET_EXTREME_RISK)
        elif MarketRegime.RANGE_BOUND in (regime,):
            soft_warnings.append(BlockedReason.MARKET_RANGE_BOUND)

        # 5. 波动率
        volatility_passed, volatility_blocks = self._check_volatility(
            row, signal, thresholds
        )
        soft_warnings.extend(volatility_blocks)

        # 6. 突破质量
        quality, quality_score, quality_blocks = self._check_breakout_quality(
            row, signal, thresholds
        )
        if BreakoutQuality.FALSE_BREAKOUT in (quality,):
            hard_blocks.append(BlockedReason.RETEST_FAILED)
        soft_warnings.extend(quality_blocks)

        # 7. 风险预算
        portfolio_risk_passed = SignalReadiness.PORTFOLIO_RISK_PASSED
        position_size = 0.0
        risk_unit = 0.0
        if require_account and account is None:
            hard_blocks.append(BlockedReason.ACCOUNT_NOT_SYNCED)
            portfolio_risk_passed = SignalReadiness.NOT_READY
        if account is not None:
            (
                portfolio_risk_passed,
                position_size,
                risk_unit,
                risk_blocks,
            ) = self._check_risk_budget(
                signal, row, asset, account, thresholds
            )
            if risk_blocks:
                hard_blocks.extend(risk_blocks)

        # 8. 相关性
        if account is not None:
            corr_blocks = self._check_correlation(
                signal, asset, account, thresholds, risk_group
            )
            soft_warnings.extend(corr_blocks)

        # 9. 事件风险
        event_level, event_blocks = self._check_event_risk(
            signal, asset, thresholds
        )
        if event_level in (RiskLevel.HIGH, RiskLevel.EXTREME):
            hard_blocks.append(BlockedReason.EVENT_RISK_HIGH)
        elif event_level is RiskLevel.ELEVATED:
            soft_warnings.append(BlockedReason.EVENT_RISK_HIGH)

        # 10. 回测有效性
        backtest_ready = SignalReadiness.BACKTEST_VALIDATED
        if not backtest_validated:
            soft_warnings.append(BlockedReason.BACKTEST_NOT_VALIDATED)
            backtest_ready = SignalReadiness.NOT_VALIDATED

        # 11. 执行系统
        exec_ready = SignalReadiness.EXECUTION_READY
        if not execution_ready:
            soft_warnings.append(BlockedReason.API_NOT_CONNECTED)
            exec_ready = SignalReadiness.NOT_READY

        # 12. 最终判定
        overall_score = self._compute_overall_score(
            regime_score, quality_score, len(hard_blocks), len(soft_warnings)
        )

        if hard_blocks:
            status = EligibilityStatus.RISK_REJECTED
            trade_eligible = False
        elif len(soft_warnings) >= 3:
            status = EligibilityStatus.WATCH_ONLY
            trade_eligible = False
        elif overall_score < 0.5:
            status = EligibilityStatus.WATCH_ONLY
            trade_eligible = False
        else:
            status = EligibilityStatus.TRADE_ELIGIBLE
            trade_eligible = True

        return EligibilityResult(
            symbol=asset.symbol,
            timeframe=signal.timeframe,
            status=status,
            trade_eligible=trade_eligible,
            data_ready=data_ready,
            instrument_verified=instrument_verified,
            liquidity_passed=liquidity_passed,
            market_regime=regime,
            volatility_passed=volatility_passed,
            breakout_quality=quality,
            event_risk_level=event_level,
            portfolio_risk_passed=portfolio_risk_passed,
            backtest_validated=backtest_ready,
            execution_ready=exec_ready,
            hard_blocks=tuple(hard_blocks),
            soft_warnings=tuple(soft_warnings),
            suggested_position_size=position_size,
            suggested_risk_unit=risk_unit,
            risk_group=risk_group,
            regime_score=regime_score,
            quality_score=quality_score,
            overall_score=overall_score,
            metadata={
                "signal_type": signal.signal_type.value,
                "direction": signal.direction.value,
                "trigger_price": signal.trigger_price,
                "atr": signal.atr,
                "atr_pct": signal.atr_pct,
            },
        )

    @staticmethod
    def verdict(result: EligibilityResult) -> EligibilityVerdict:
        if result.trade_eligible:
            return EligibilityVerdict.ACCEPT
        if any(reason in _RETRYABLE_BLOCKS for reason in result.hard_blocks):
            return EligibilityVerdict.DEFER_RETRYABLE
        return EligibilityVerdict.REJECT_TERMINAL

    # ── 1. 数据完整性 ────────────────────────────────────────────────────

    def _check_data_ready(
        self,
        bars: pd.DataFrame,
        asset: AssetConfig,
        thresholds: EligibilityThresholds,
    ) -> tuple[SignalReadiness, list[BlockedReason]]:
        blocks: list[BlockedReason] = []

        required = {"open", "high", "low", "close", "volume"}
        if not required.issubset(bars.columns):
            blocks.append(BlockedReason.DATA_INCOMPLETE)
            return SignalReadiness.DATA_READY, blocks

        if bars.empty:
            blocks.append(BlockedReason.DATA_INCOMPLETE)
            return SignalReadiness.DATA_READY, blocks

        if not isinstance(bars.index, pd.DatetimeIndex):
            blocks.append(BlockedReason.DATA_TIMESTAMP_INVALID)
            return SignalReadiness.DATA_READY, blocks

        if bars.index.has_duplicates or not bars.index.is_monotonic_increasing:
            blocks.append(BlockedReason.DATA_TIMESTAMP_INVALID)

        min_bars = thresholds.data.get("min_history_bars", 250)
        if len(bars) < min_bars:
            blocks.append(BlockedReason.INSUFFICIENT_HISTORY)

        ohlcv = bars[["open", "high", "low", "close", "volume"]]
        if not np.isfinite(ohlcv.to_numpy()).all():
            blocks.append(BlockedReason.DATA_OHLCV_INVALID)

        if (ohlcv[["open", "high", "low", "close"]] <= 0).any().any():
            blocks.append(BlockedReason.DATA_OHLCV_INVALID)

        valid = (
            (bars["high"] >= bars["low"])
            & (bars["open"].between(bars["low"], bars["high"]))
            & (bars["close"].between(bars["low"], bars["high"]))
        )
        if not bool(valid.all()):
            blocks.append(BlockedReason.DATA_OHLCV_INVALID)

        if asset.market.value == "us_equity" and thresholds.data.get(
            "require_adjustment", False
        ):
            if asset.adjustment not in ("back_adjusted", "forward_adjusted"):
                blocks.append(BlockedReason.ADJUSTMENT_NOT_APPLIED)

        if thresholds.data.get("require_contract_roll_handling", False):
            if "split_adjusted" not in bars.columns:
                blocks.append(BlockedReason.CONTRACT_ROLL_NOT_HANDLED)

        if blocks:
            return SignalReadiness.DATA_READY, blocks
        return SignalReadiness.DATA_READY, []

    # ── 2. 品种确认 ──────────────────────────────────────────────────────

    def _check_instrument(
        self,
        bars: pd.DataFrame,
        asset: AssetConfig,
        thresholds: EligibilityThresholds,
    ) -> SignalReadiness:
        if "symbol" in bars.columns:
            values = set(bars["symbol"].dropna().astype(str))
            allowed = {asset.symbol, asset.instrument, *asset.source_symbols}
            if not values.issubset(allowed):
                return SignalReadiness.INSTRUMENT_VERIFIED

        if "instrument" in bars.columns:
            values = set(bars["instrument"].dropna().astype(str))
            if values and values != {asset.instrument}:
                return SignalReadiness.INSTRUMENT_VERIFIED

        return SignalReadiness.INSTRUMENT_VERIFIED

    # ── 3. 流动性 ────────────────────────────────────────────────────────

    def _check_liquidity(
        self,
        row: pd.Series,
        bars: pd.DataFrame,
        asset: AssetConfig,
        thresholds: EligibilityThresholds,
    ) -> tuple[SignalReadiness, list[BlockedReason]]:
        blocks: list[BlockedReason] = []
        lt = thresholds.liquidity

        avg_volume = float(bars["volume"].tail(20).mean())
        avg_close = float(bars["close"].tail(20).mean())
        daily_volume_usd = avg_volume * avg_close

        if lt.min_daily_volume_usd > 0 and daily_volume_usd < lt.min_daily_volume_usd:
            blocks.append(BlockedReason.INSUFFICIENT_VOLUME)

        if blocks:
            return SignalReadiness.LIQUIDITY_PASSED, blocks
        return SignalReadiness.LIQUIDITY_PASSED, []

    # ── 4. 市场状态 ──────────────────────────────────────────────────────

    def _check_market_regime(
        self,
        row: pd.Series,
        bars: pd.DataFrame,
        thresholds: EligibilityThresholds,
    ) -> tuple[MarketRegime, float, list[BlockedReason]]:
        blocks: list[BlockedReason] = []
        mt = thresholds.market_regime

        sma_short = float(row.get(f"sma_{mt.trend_ma_short}", np.nan))
        sma_long = float(row.get(f"sma_{mt.trend_ma_long}", np.nan))
        close = float(row["close"])

        has_trend = False
        if np.isfinite(sma_short) and np.isfinite(sma_long):
            if sma_short > sma_long and close > sma_short:
                has_trend = True
            elif sma_short < sma_long and close < sma_short:
                has_trend = True

        # ADX 模拟
        atr = float(row.get("atr", np.nan))
        if not np.isfinite(atr) or atr <= 0:
            regime = MarketRegime.NEUTRAL
            return regime, 0.4, blocks

        # 计算简单的方向性评分
        returns = bars["close"].tail(50).pct_change().dropna()
        if len(returns) < 10:
            regime = MarketRegime.NEUTRAL
            return regime, 0.3, blocks

        positive_ratio = float((returns > 0).mean())
        avg_return = float(returns.mean())
        std_return = float(returns.std())

        if std_return == 0:
            regime = MarketRegime.RANGE_BOUND
            return regime, 0.2, blocks

        directional_score = avg_return / std_return if std_return > 0 else 0.0

        donchian_width = float(
            row.get(f"channel_high_{max(20, len(bars) // 4)}", np.nan)
        ) - float(row.get(f"channel_low_{max(20, len(bars) // 4)}", np.nan))
        if np.isfinite(donchian_width) and np.isfinite(atr):
            width_atr = donchian_width / atr
        else:
            width_atr = 0.0

        if abs(directional_score) > 0.15 and has_trend:
            regime = MarketRegime.TREND_FRIENDLY
            regime_score = 0.8
        elif abs(directional_score) > 0.10:
            regime = MarketRegime.WEAK_TREND
            regime_score = 0.6
        elif width_atr < 2.0:
            regime = MarketRegime.RANGE_BOUND
            regime_score = 0.3
            blocks.append(BlockedReason.MARKET_RANGE_BOUND)
        elif positive_ratio < 0.35 or positive_ratio > 0.65:
            regime = MarketRegime.EXTREME_RISK
            regime_score = 0.1
            blocks.append(BlockedReason.MARKET_EXTREME_RISK)
        else:
            regime = MarketRegime.NEUTRAL
            regime_score = 0.5

        return regime, regime_score, blocks

    # ── 5. 波动率 ────────────────────────────────────────────────────────

    def _check_volatility(
        self,
        row: pd.Series,
        signal: TurtleSignal,
        thresholds: EligibilityThresholds,
    ) -> tuple[SignalReadiness, list[BlockedReason]]:
        blocks: list[BlockedReason] = []
        vt = thresholds.volatility

        atr_pct = float(row.get("atr_pct", signal.atr_pct))
        if not np.isfinite(atr_pct):
            return SignalReadiness.VOLATILITY_PASSED, []

        if atr_pct < vt.min_atr_pct:
            blocks.append(BlockedReason.ATR_TOO_LOW)
        if atr_pct > vt.max_atr_pct:
            blocks.append(BlockedReason.ATR_TOO_HIGH)

        vol_percentile = float(row.get("volatility_percentile", np.nan))
        if np.isfinite(vol_percentile):
            if vol_percentile > vt.max_atr_percentile:
                blocks.append(BlockedReason.ATR_PERCENTILE_EXTREME)
            elif vol_percentile < vt.min_atr_percentile:
                blocks.append(BlockedReason.ATR_PERCENTILE_EXTREME)

        if signal.distance_to_breakout_atr > vt.max_chase_distance_atr:
            blocks.append(BlockedReason.CHASE_DISTANCE_TOO_LARGE)

        previous_close = float(row.get("previous_close", np.nan))
        atr = float(row.get("atr", signal.atr))
        if np.isfinite(previous_close) and atr > 0:
            gap = abs(float(row["open"]) - previous_close) / atr
            if gap > vt.max_gap_atr:
                blocks.append(BlockedReason.GAP_EXCEEDED)

        if blocks:
            return SignalReadiness.VOLATILITY_PASSED, blocks
        return SignalReadiness.VOLATILITY_PASSED, []

    # ── 6. 突破质量 ──────────────────────────────────────────────────────

    def _check_breakout_quality(
        self,
        row: pd.Series,
        signal: TurtleSignal,
        thresholds: EligibilityThresholds,
    ) -> tuple[BreakoutQuality, float, list[BlockedReason]]:
        blocks: list[BlockedReason] = []
        bt = thresholds.breakout_quality

        if signal.signal_type is SignalType.FALSE_BREAKOUT:
            return BreakoutQuality.FALSE_BREAKOUT, 0.0, blocks

        if signal.signal_type is SignalType.OVEREXTENDED:
            return BreakoutQuality.OVEREXTENDED_BREAKOUT, 0.3, [
                BlockedReason.OVEREXTENDED
            ]

        if signal.signal_type is SignalType.RETEST_CONFIRMED:
            quality = BreakoutQuality.RETEST_CONFIRMED
            quality_score = 0.9
        elif signal.signal_type in (
            SignalType.SYSTEM1_BREAKOUT,
            SignalType.SYSTEM2_BREAKOUT,
            SignalType.CLOSE_CONFIRMED,
        ):
            quality = BreakoutQuality.QUALIFIED_BREAKOUT
            quality_score = 0.7
        else:
            quality = BreakoutQuality.RAW_BREAKOUT
            quality_score = 0.5

        close = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        bar_range = high - low

        if bar_range > 0:
            close_location = (close - low) / bar_range
            if close_location < bt.min_close_location:
                quality_score -= 0.15
                blocks.append(BlockedReason.WEAK_CLOSE_LOCATION)

            upper_wick = (high - max(close, float(row["open"]))) / bar_range
            lower_wick = (min(close, float(row["open"])) - low) / bar_range
            if max(upper_wick, lower_wick) > bt.max_wick_ratio:
                quality_score -= 0.10
                blocks.append(BlockedReason.LONG_WICK)

        if signal.distance_to_breakout_atr > bt.max_overextended_atr:
            quality_score -= 0.20
            blocks.append(BlockedReason.OVEREXTENDED)
            quality = BreakoutQuality.OVEREXTENDED_BREAKOUT

        if abs(signal.distance_to_breakout_atr) < bt.min_breakout_magnitude_atr:
            quality_score -= 0.10
            blocks.append(BlockedReason.BREAKOUT_MAGNITUDE_WEAK)

        volume_mean = float(row.get("volume_mean", np.nan))
        if np.isfinite(volume_mean) and volume_mean > 0:
            if float(row["volume"]) < volume_mean * bt.volume_confirmation_ratio:
                blocks.append(BlockedReason.VOLUME_NOT_CONFIRMING)

        quality_score = max(0.0, min(1.0, quality_score))
        return quality, quality_score, blocks

    # ── 7. 风险预算 ──────────────────────────────────────────────────────

    def _check_risk_budget(
        self,
        signal: TurtleSignal,
        row: pd.Series,
        asset: AssetConfig,
        account: AccountSnapshot,
        thresholds: EligibilityThresholds,
    ) -> tuple[SignalReadiness, float, float, list[BlockedReason]]:
        blocks: list[BlockedReason] = []
        rt = thresholds.risk_budget

        if account.equity <= 0:
            blocks.append(BlockedReason.INSUFFICIENT_CASH)
            return SignalReadiness.PORTFOLIO_RISK_PASSED, 0.0, 0.0, blocks

        single_trade_risk = account.equity * rt.single_trade_risk_pct
        stop_distance = signal.atr * 2.0
        if stop_distance <= 0:
            blocks.append(BlockedReason.STOP_DISTANCE_TOO_LARGE)
            return SignalReadiness.PORTFOLIO_RISK_PASSED, 0.0, 0.0, blocks

        risk_unit = single_trade_risk / stop_distance
        position_size = risk_unit * signal.trigger_price

        if position_size < rt.min_position_size:
            blocks.append(BlockedReason.MIN_POSITION_SIZE_UNMET)

        symbol_exposure = account.open_positions.get(asset.symbol, 0.0) + position_size
        if symbol_exposure > account.equity * rt.max_symbol_risk_pct:
            blocks.append(BlockedReason.SYMBOL_RISK_EXCEEDED)

        asset_class = asset.market.value
        class_exposure = account.asset_class_exposure.get(asset_class, 0.0) + position_size
        if class_exposure > account.equity * rt.max_asset_class_risk_pct:
            blocks.append(BlockedReason.ASSET_CLASS_RISK_EXCEEDED)

        total_exposure = sum(account.open_positions.values()) + position_size
        if total_exposure > account.equity * rt.max_portfolio_risk_pct:
            blocks.append(BlockedReason.PORTFOLIO_RISK_EXCEEDED)

        if account.current_drawdown > rt.max_drawdown_pct:
            blocks.append(BlockedReason.MAX_DRAWDOWN_EXCEEDED)

        if account.consecutive_losses >= rt.max_consecutive_losses:
            blocks.append(BlockedReason.CONSECUTIVE_LOSSES)
            risk_unit *= rt.risk_reduction_after_losses

        if blocks:
            return SignalReadiness.PORTFOLIO_RISK_PASSED, position_size, risk_unit, blocks
        return SignalReadiness.PORTFOLIO_RISK_PASSED, position_size, risk_unit, []

    # ── 8. 相关性 ────────────────────────────────────────────────────────

    def _check_correlation(
        self,
        signal: TurtleSignal,
        asset: AssetConfig,
        account: AccountSnapshot,
        thresholds: EligibilityThresholds,
        risk_group: str,
    ) -> list[BlockedReason]:
        blocks: list[BlockedReason] = []
        ct = thresholds.correlation

        group_exposure = account.risk_group_exposure.get(risk_group, 0.0)
        if group_exposure > account.equity * ct.risk_group_max_exposure_pct:
            blocks.append(BlockedReason.RISK_GROUP_EXCEEDED)

        same_direction = sum(
            1
            for s in account.recent_signals
            if s.get("direction") == signal.direction.value
            and s.get("risk_group") == risk_group
        )
        if same_direction >= ct.max_same_direction_signals:
            blocks.append(BlockedReason.SAME_DIRECTION_CONCENTRATION)

        return blocks

    # ── 9. 事件风险 ──────────────────────────────────────────────────────

    def _check_event_risk(
        self,
        signal: TurtleSignal,
        asset: AssetConfig,
        thresholds: EligibilityThresholds,
    ) -> tuple[RiskLevel, list[BlockedReason]]:
        """简化的事件风险检查。实际部署时需接入外部事件日历。"""
        blocks: list[BlockedReason] = []
        return RiskLevel.NORMAL, blocks

    # ── 辅助方法 ──────────────────────────────────────────────────────────

    def _resolve_thresholds(
        self, asset: AssetConfig
    ) -> EligibilityThresholds:
        """根据品种获取对应的阈值配置。"""
        return get_thresholds_for_market(asset.market.value)

    def _build_result(
        self,
        signal: TurtleSignal,
        asset: AssetConfig,
        thresholds: EligibilityThresholds,
        risk_group: str,
        hard_blocks: list[BlockedReason],
        soft_warnings: list[BlockedReason],
        **overrides: Any,
    ) -> EligibilityResult:
        defaults: dict[str, Any] = {
            "symbol": asset.symbol,
            "timeframe": signal.timeframe,
            "status": EligibilityStatus.NOT_READY,
            "trade_eligible": False,
            "hard_blocks": tuple(hard_blocks),
            "soft_warnings": tuple(soft_warnings),
            "risk_group": risk_group,
            "metadata": {
                "signal_type": signal.signal_type.value,
                "direction": signal.direction.value,
                "trigger_price": signal.trigger_price,
                "atr": signal.atr,
            },
        }
        defaults.update(overrides)
        return EligibilityResult(**defaults)

    @staticmethod
    def _compute_overall_score(
        regime_score: float,
        quality_score: float,
        hard_block_count: int,
        soft_warning_count: int,
    ) -> float:
        score = (
            regime_score * 0.30
            + quality_score * 0.30
            + max(0, 1.0 - hard_block_count * 0.5) * 0.25
            + max(0, 1.0 - soft_warning_count * 0.15) * 0.15
        )
        return round(max(0.0, min(1.0, score)), 4)
