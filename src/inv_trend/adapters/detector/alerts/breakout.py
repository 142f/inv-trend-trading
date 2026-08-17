"""D1 Turtle breakout alerts over the canonical historical-data repository."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Iterable, Mapping

import pandas as pd

from inv_trend.core.features import FeatureRequest, PreparedBars

from ..alerts.notifier import Notifier
from ..data.normalizer import normalize_bars
from ..models import (
    AssetConfig,
    ConfirmationStatus,
    Direction,
    SignalType,
    TurtleSignal,
)
from ..storage.signal_repository import SignalRepository


BarLoader = Callable[[str, str], pd.DataFrame]


@dataclass(frozen=True)
class AlertScanResult:
    scanned_symbols: int
    alerts: tuple[TurtleSignal, ...]
    failures: tuple[tuple[str, str], ...] = ()
    statuses: tuple["ScanStatus", ...] = ()


class ScanState(str, Enum):
    BREAKOUT_UP = "BREAKOUT_UP"
    BREAKOUT_DOWN = "BREAKOUT_DOWN"
    NO_BREAKOUT = "NO_BREAKOUT"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    QUALITY_REJECTED = "QUALITY_REJECTED"


@dataclass(frozen=True)
class ScanStatus:
    symbol: str
    state: ScanState
    bar_time: str | None = None
    current_price: float | None = None
    high_20: float | None = None
    low_20: float | None = None
    high_55: float | None = None
    low_55: float | None = None
    signals: tuple[str, ...] = ()
    breakout_levels: tuple[float, ...] = ()
    data_version: str = "unknown"
    funds: str = ""
    ranks: str = ""
    weights: str = ""
    snapshot_dates: str = ""
    alert_emitted: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = dict(self.__dict__)
        payload["state"] = self.state.value
        payload["signals"] = list(self.signals)
        payload["breakout_levels"] = list(self.breakout_levels)
        return payload


class TurtleBreakoutAlertScanner:
    """Detect completed-bar 20/55-day breakouts without portfolio state changes."""

    def __init__(self, repository: SignalRepository, notifier: Notifier | None = None) -> None:
        self.repository = repository
        self.notifier = notifier

    def scan_bars(
        self, bars: pd.DataFrame, asset: AssetConfig, *, data_version: str | None = None
    ) -> tuple[TurtleSignal, ...]:
        return self.scan_bars_with_status(
            bars, asset, data_version=data_version
        )[0]

    def scan_bars_with_status(
        self, bars: pd.DataFrame, asset: AssetConfig, *, data_version: str | None = None,
        universe: Mapping[str, str] | None = None,
    ) -> tuple[tuple[TurtleSignal, ...], ScanStatus]:
        version = data_version or str(
            bars.attrs.get("dataset_version")
            or (bars["dataset_version"].iloc[-1] if "dataset_version" in bars and len(bars) else "unknown")
        )
        complete = bars
        if "is_complete" in complete.columns:
            complete = complete.loc[complete["is_complete"].fillna(False).astype(bool)]
        if len(complete) < 56:
            last_time = None
            if len(complete):
                column = next((c for c in ("timestamp", "time", "date") if c in complete), None)
                last_time = pd.Timestamp(complete[column].iloc[-1]).isoformat() if column else None
            return (), self._status(
                asset.symbol, ScanState.INSUFFICIENT_HISTORY, version, universe,
                bar_time=last_time, error=f"requires 56 completed D1 bars, got {len(complete)}",
            )

        normalized = normalize_bars(complete, asset, "D1")
        prepared = PreparedBars.build(
            normalized, FeatureRequest(donchian_periods=(20, 55))
        ).frame
        row = prepared.iloc[-1]
        emitted: list[TurtleSignal] = []
        detected: list[str] = []
        levels: list[float] = []
        directions: list[Direction] = []
        for period, signal_type in (
            (20, SignalType.SYSTEM1_BREAKOUT),
            (55, SignalType.SYSTEM2_BREAKOUT),
        ):
            high = float(row[f"channel_high_{period}"])
            low = float(row[f"channel_low_{period}"])
            close = float(row["close"])
            candidates = []
            if close > high:
                candidates.append((Direction.LONG, high))
            if close < low:
                candidates.append((Direction.SHORT, low))
            for direction, level in candidates:
                detected.append(f"{period}日{'向上' if direction is Direction.LONG else '向下'}")
                levels.append(level)
                directions.append(direction)
                signal = self._signal(row, asset, signal_type, direction, level, version)
                if not self.repository.is_new(signal):
                    continue
                self.repository.save_signal(signal)
                if self.notifier is not None:
                    self.notifier.notify(signal)
                emitted.append(signal)
        state = (
            ScanState.BREAKOUT_UP if directions and directions[0] is Direction.LONG
            else ScanState.BREAKOUT_DOWN if directions else ScanState.NO_BREAKOUT
        )
        status = self._status(
            asset.symbol, state, version, universe, bar_time=row.name.isoformat(),
            current_price=float(row["close"]), high_20=float(row["channel_high_20"]),
            low_20=float(row["channel_low_20"]), high_55=float(row["channel_high_55"]),
            low_55=float(row["channel_low_55"]), signals=tuple(detected),
            breakout_levels=tuple(levels), alert_emitted=bool(emitted),
        )
        return tuple(emitted), status

    @staticmethod
    def _status(
        symbol: str, state: ScanState, version: str, universe: Mapping[str, str] | None,
        **values: Any,
    ) -> ScanStatus:
        membership = universe or {}
        return ScanStatus(
            symbol=symbol, state=state, data_version=version,
            funds=str(membership.get("funds", "")), ranks=str(membership.get("ranks", "")),
            weights=str(membership.get("weights", "")),
            snapshot_dates=str(membership.get("snapshot_dates", "")), **values,
        )

    @staticmethod
    def _signal(
        row: pd.Series,
        asset: AssetConfig,
        signal_type: SignalType,
        direction: Direction,
        level: float,
        data_version: str,
    ) -> TurtleSignal:
        close = float(row["close"])
        period = 20 if signal_type is SignalType.SYSTEM1_BREAKOUT else 55
        return TurtleSignal(
            symbol=asset.symbol, instrument=asset.instrument, market=asset.market.value,
            timeframe="D1", signal_type=signal_type, raw_signal_type=signal_type,
            direction=direction, signal_time=row.name.isoformat(), trigger_price=close,
            channel_high=level if direction is Direction.LONG else None,
            channel_low=level if direction is Direction.SHORT else None,
            atr=0.0, atr_pct=0.0, stop_price=None, next_add_price=None,
            distance_to_breakout_atr=0.0, volatility_percentile=0.0,
            suggested_risk_unit=asset.risk_unit_pct, trend_status="not_evaluated",
            confirmation_status=ConfirmationStatus.CLOSE_CONFIRMED,
            data_source=asset.data_source, generated_at=TurtleSignal.now_iso(),
            tradeable=False, confidence_note="completed D1 breakout alert",
            metadata={
                "breakout_period": period,
                "breakout_direction": "up" if direction is Direction.LONG else "down",
                "breakout_level": level,
                "data_version": data_version,
            },
        )


def scan_configured_d1(
    repository: SignalRepository,
    notifier: Notifier | None = None,
    *,
    loader: BarLoader | None = None,
    assets: Iterable[AssetConfig] | None = None,
    universe_metadata: Mapping[str, Mapping[str, str]] | None = None,
) -> AlertScanResult:
    """Scan every configured D1 asset through the existing Repository loader."""
    if loader is None:
        from inv_trend.data import load_bars

        def repository_loader(symbol: str, timeframe: str) -> pd.DataFrame:
            return load_bars(symbol, timeframe, completed_only=True)

        loader = repository_loader
    if assets is None:
        raise ValueError("assets must be supplied by the application or CLI boundary")
    configured = tuple(assets)
    scanner = TurtleBreakoutAlertScanner(repository, notifier)
    alerts: list[TurtleSignal] = []
    failures: list[tuple[str, str]] = []
    scanned = 0
    statuses: list[ScanStatus] = []
    for asset in configured:
        if "D1" not in asset.timeframes:
            continue
        try:
            bars = loader(asset.symbol, "D1")
            emitted, status = scanner.scan_bars_with_status(
                bars, asset, universe=(universe_metadata or {}).get(asset.symbol)
            )
            alerts.extend(emitted)
            statuses.append(status)
            scanned += 1
        except (FileNotFoundError, KeyError, ValueError) as exc:
            failures.append((asset.symbol, str(exc)))
            message = str(exc)
            state = (
                ScanState.QUALITY_REJECTED
                if type(exc).__name__ == "DataQualityError" and not message.startswith("no published ")
                else ScanState.DATA_UNAVAILABLE
            )
            statuses.append(scanner._status(
                asset.symbol, state, "unknown", (universe_metadata or {}).get(asset.symbol),
                error=str(exc),
            ))
    return AlertScanResult(scanned, tuple(alerts), tuple(failures), tuple(statuses))
