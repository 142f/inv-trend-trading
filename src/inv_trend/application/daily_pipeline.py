"""Application orchestration for the D1 daily market scan.

This module owns the use case and returns a result object.  Report rendering is
deliberately left to ``inv_trend.observability`` at the CLI boundary.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from uuid import uuid4
from zoneinfo import ZoneInfo

from inv_trend.data import HistoricalDataService, create_default_providers
from inv_trend.core.signals import CORRECTED_STRATEGY_VERSION, SignalEvent

from inv_trend.adapters.detector.alerts.daily_notifier import LogNotifier, SignalNotifier
from inv_trend.adapters.detector.freshness import FreshnessPolicy
from inv_trend.adapters.detector.indicators.daily_signals import (
    analyze_prepared_daily_signals,
    prepare_daily_signal_frame,
)
from inv_trend.adapters.detector.models import AssetConfig
from inv_trend.adapters.detector.storage.daily_signal_repository import SQLiteDailySignalRepository


BEIJING = ZoneInfo("Asia/Shanghai")
DEFAULT_BOOTSTRAP_DAYS = 400


@dataclass(frozen=True)
class DailyMarketScanResult:
    snapshot_path: Path
    database_path: Path
    signal_log_root: Path
    snapshot: Mapping[str, Any]

    @property
    def failed_symbols(self) -> tuple[str, ...]:
        bad = {"failed", "blocked", "stale"}
        return tuple(str(row["symbol"]) for row in self.snapshot["symbols"] if row["run_status"] in bad)

    @property
    def exit_code(self) -> int:
        return 1 if self.failed_symbols else 0


class DailyMarketScanService:
    def __init__(
        self,
        *,
        data_root: str | Path = "data",
        output_dir: str | Path = "outputs/daily_market_scan",
        database_path: str | Path | None = None,
        signal_log_dir: str | Path = "logs/signals",
        assets: Mapping[str, AssetConfig] | None = None,
        historical_service: HistoricalDataService | None = None,
        signal_repository: SQLiteDailySignalRepository | None = None,
        notifier: SignalNotifier | None = None,
        freshness_policy: FreshnessPolicy | None = None,
        provider_timeout: int = 10,
        provider_retries: int = 1,
        refresh_data: bool = True,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.data_root = Path(data_root)
        self.output_dir = Path(output_dir)
        self.database_path = Path(database_path or self.output_dir / "signals.sqlite3")
        self.signal_log_dir = Path(signal_log_dir)
        from .asset_config import load_detector_asset_configs

        self.assets = dict(assets or load_detector_asset_configs())
        self.historical_service = historical_service or HistoricalDataService(
            self.data_root,
            providers=create_default_providers(timeout=provider_timeout, retries=provider_retries),
        )
        self.signal_repository = signal_repository or SQLiteDailySignalRepository(self.database_path)
        self.notifier = notifier or LogNotifier(self.signal_log_dir)
        self.freshness_policy = freshness_policy or FreshnessPolicy()
        self.provider_retries = provider_retries
        self.refresh_data = refresh_data
        self._now = now or (lambda: datetime.now(timezone.utc))

    def run(
        self,
        *,
        symbols: Iterable[str] | None = None,
        bootstrap_days: int = DEFAULT_BOOTSTRAP_DAYS,
        research_mode: bool = False,
        strategy_version: str = CORRECTED_STRATEGY_VERSION,
        backfill_signals: bool = False,
        chart_bars: int = 180,
    ) -> DailyMarketScanResult:
        if bootstrap_days < 1:
            raise ValueError("bootstrap_days must be positive")
        if strategy_version != CORRECTED_STRATEGY_VERSION:
            raise ValueError("only the executable strategy version 'corrected-v2' is supported")
        if chart_bars < 1:
            raise ValueError("chart_bars must be positive")
        selected = self._selected_assets(symbols)
        started_at = _aware_utc(self._now())
        run_id = uuid4().hex
        self.signal_repository.start_run(run_id, started_at.isoformat())
        rows: list[dict[str, Any]] = []
        all_new: list[SignalEvent] = []
        delivery = {"notified": 0, "errors": 0, "recovered": 0}
        try:
            for asset in selected:
                row, new_events = self._run_symbol(
                    asset, run_id=run_id, started_at=started_at, bootstrap_days=bootstrap_days,
                    research_mode=research_mode, strategy_version=strategy_version,
                    backfill_signals=backfill_signals, chart_bars=chart_bars,
                )
                rows.append(row)
                all_new.extend(new_events)
                self.signal_repository.record_instrument(run_id, row)
            delivery = self._deliver_pending(run_id)
        except KeyboardInterrupt:
            self.signal_repository.interrupt_run(run_id, _aware_utc(self._now()).isoformat())
            raise
        finished_at = _aware_utc(self._now())
        summary = _summary(rows, len(all_new), delivery)
        self.signal_repository.finish_run(run_id, finished_at.isoformat(), summary)
        report_date = started_at.astimezone(BEIJING).date().isoformat()
        snapshot = {
            "schema_version": "2", "run_id": run_id, "report_date": report_date,
            "started_at": started_at.isoformat(), "finished_at": finished_at.isoformat(),
            "timezone": "Asia/Shanghai", "timeframe": "D1",
            "configuration": {
                "bootstrap_days": bootstrap_days, "research_mode": research_mode,
                "strategy_version": strategy_version, "backfill_signals": backfill_signals,
                "chart_bars": chart_bars, "formal_quality_statuses": ["CURATED"],
                "symbols": [asset.symbol for asset in selected],
            },
            "symbols": rows, "summary": summary,
        }
        snapshot_path = self.output_dir / f"{report_date}.json"
        _atomic_write_json(snapshot_path, snapshot)
        return DailyMarketScanResult(snapshot_path, self.database_path, self.signal_log_dir, snapshot)

    def _selected_assets(self, symbols: Iterable[str] | None) -> tuple[AssetConfig, ...]:
        d1_assets = {name: asset for name, asset in self.assets.items() if "D1" in asset.timeframes}
        if symbols is None:
            return tuple(d1_assets[name] for name in sorted(d1_assets))
        wanted = tuple(dict.fromkeys(str(symbol).upper() for symbol in symbols))
        unknown = sorted(set(wanted) - set(d1_assets))
        if unknown:
            raise ValueError(f"symbols are not configured for D1: {unknown}")
        return tuple(d1_assets[symbol] for symbol in wanted)

    def _run_symbol(
        self, asset: AssetConfig, *, run_id: str, started_at: datetime, bootstrap_days: int,
        research_mode: bool, strategy_version: str, backfill_signals: bool, chart_bars: int,
    ) -> tuple[dict[str, Any], list[SignalEvent]]:
        instrument = self.historical_service.instrument(asset.symbol)
        before = self.historical_service.lake.current_version(asset.symbol, "D1")
        row: dict[str, Any] = {
            "symbol": asset.symbol, "instrument": asset.instrument, "instrument_id": instrument.instrument_id,
            "market": asset.market.value, "timeframe": "D1",
            "provider": {"primary": instrument.primary_source, "fallbacks": list(instrument.fallback_sources)},
        }
        refresh_error: Exception | None = None
        manifest = None
        try:
            if not self.refresh_data:
                if before is None:
                    raise FileNotFoundError(f"no current dataset for {asset.symbol}/D1")
                row["update"] = {"status": "unchanged", "mode": "scan_only", "dataset_version": str(before["version"]), "refresh_failed": False, "skipped": True}
            else:
                manifest = self._refresh(asset.symbol, started_at, bootstrap_days)
                after = self.historical_service.lake.current_version(asset.symbol, "D1")
                if not manifest.quality_passed or after is None:
                    raise RuntimeError(f"refresh not published: status={manifest.quality_status}, score={manifest.quality_score}")
                operation = "updated" if before is None or before.get("version") != after.get("version") else "unchanged"
                row["update"] = {"status": operation, "mode": "bootstrap" if before is None else "incremental", "provider": manifest.data_source, "dataset_version": str(after["version"]), "quality_status": manifest.quality_status, "quality_score": manifest.quality_score, "row_count": manifest.row_count, "refresh_failed": False}
        except Exception as exc:
            refresh_error = exc
            row["update"] = {"status": "failed", "error_type": type(exc).__name__, "error": str(exc), "refresh_failed": True, "previous_current_preserved": before is not None}
            if before is None:
                row["run_status"] = "blocked" if type(exc).__name__ == "ProviderIdentityError" else "failed"
                row["scan"] = {"status": "not_run", "reason": "update_failed_no_current"}
                return row, []
        try:
            bars = self.historical_service.load_bars(
                asset.symbol, "D1", completed_only=True, allow_research=True
            )
            data = _data_metadata(bars)
            row["data"] = data
            freshness = self.freshness_policy.evaluate(instrument, data["latest_complete_bar"], now=started_at)
            row["freshness"] = freshness.to_dict()
            prepared = prepare_daily_signal_frame(bars)
            analysis = analyze_prepared_daily_signals(prepared)
            row["scan"] = analysis
            row["chart"] = _chart_payload(prepared.tail(chart_bars))
            formal_eligible = data["quality_statuses"] == ["CURATED"] and freshness.is_fresh
            reason = "stale_data" if not freshness.is_fresh else ("non_curated_data" if not formal_eligible else None)
            row["alert_policy"] = {"formal_eligible": formal_eligible, "research_mode": research_mode, "suppressed_reason": reason}
            detected: list[SignalEvent] = []
            replayed = 0
            cursor_before = self.signal_repository.load_cursor(instrument.instrument_id, "D1", strategy_version)
            if len(prepared):
                if backfill_signals:
                    positions = range(len(prepared))
                elif cursor_before is None:
                    positions = (len(prepared) - 1,)
                else:
                    positions = tuple(index for index, timestamp in enumerate(prepared.index) if timestamp.isoformat() > cursor_before)
                    if not positions:
                        positions = (len(prepared) - 1,)
                for position in positions:
                    replay = analyze_prepared_daily_signals(prepared, position)
                    detected.extend(self._signal_events(instrument.instrument_id, asset, replay, data["dataset_version"], started_at.isoformat(), refresh_error is not None, strategy_version))
                    replayed += 1
            row["signals_detected"] = len(detected)
            if formal_eligible:
                latest_time = prepared.index[-1].isoformat()
                new, duplicates = self.signal_repository.commit_events_and_cursor(detected, run_id=run_id, instrument_id=instrument.instrument_id, timeframe="D1", strategy_version=strategy_version, last_signal_time=latest_time, enqueue_notifications=not backfill_signals)
                row["signals_new"], row["signals_duplicate"] = len(new), duplicates
            else:
                new = []
                row["signals_new"], row["signals_duplicate"] = 0, 0
                row["research_signals"] = [event.to_dict() for event in detected] if research_mode else []
            row["cursor"] = {"strategy_version": strategy_version, "before": cursor_before, "after": prepared.index[-1].isoformat() if formal_eligible and len(prepared) else cursor_before, "bars_replayed": replayed}
            if not freshness.is_fresh:
                row["run_status"] = "stale"
            elif refresh_error is not None:
                row["run_status"] = "blocked" if manifest is not None and not manifest.quality_passed else "failed"
            else:
                row["run_status"] = row["update"]["status"]
            return row, new
        except Exception as exc:
            row["run_status"] = "failed"
            row["scan"] = {"status": "failed", "error_type": type(exc).__name__, "error": str(exc)}
            return row, []

    def _refresh(self, symbol: str, now: datetime, bootstrap_days: int):
        daily_end = _daily_refresh_end(now)
        if self.historical_service.lake.current_version(symbol, "D1") is not None:
            return self.historical_service.update(symbol, "D1", end=daily_end, retries=self.provider_retries)
        instrument = self.historical_service.instrument(symbol)
        start = daily_end - timedelta(days=bootstrap_days)
        if instrument.earliest_valid_date:
            start = max(start, _earliest_utc(instrument.earliest_valid_date))
        return self.historical_service.ingest(symbol, "D1", start, daily_end, retries=self.provider_retries)

    @staticmethod
    def _signal_events(instrument_id: str, asset: AssetConfig, analysis: Mapping[str, Any], dataset_version: str, detected_at: str, refresh_failed: bool, strategy_version: str) -> list[SignalEvent]:
        close = analysis.get("latest_bar", {}).get("close")
        if close is None:
            return []
        events: list[SignalEvent] = []
        for raw in analysis.get("signals", []):
            identity = _signal_identity(str(raw["indicator"]), str(raw["event"]))
            indicator = analysis["indicators"][raw["indicator"]]
            events.append(SignalEvent.create(strategy_version=strategy_version, instrument_id=instrument_id, symbol=asset.symbol, timeframe="D1", signal_type=identity[0], direction=str(raw["direction"]).upper(), signal_time=str(raw["signal_time"]), detected_at=detected_at, trigger_price=float(close), reference_value=_reference_value(str(raw["indicator"]), indicator), dataset_version=dataset_version, indicator_name=str(raw["indicator"]), indicator_parameters=identity[1], metadata={"market": asset.market.value, "refresh_failed": refresh_failed}))
        return events

    def _deliver_pending(self, run_id: str) -> dict[str, int]:
        result = {"notified": 0, "errors": 0, "recovered": 0}
        current = self.signal_repository.pending_notifications(run_id)
        for event in current:
            try:
                self.notifier.notify(event)
                self.signal_repository.mark_notified(event.signal_id, _aware_utc(self._now()).isoformat())
                result["notified"] += 1
            except Exception as exc:
                self.signal_repository.mark_delivery_error(event.signal_id, str(exc))
                result["errors"] += 1
        for event in self.signal_repository.pending_notifications():
            if event in current:
                continue
            try:
                self.notifier.notify(event)
                self.signal_repository.mark_notified(event.signal_id, _aware_utc(self._now()).isoformat())
                result["recovered"] += 1
            except Exception as exc:
                self.signal_repository.mark_delivery_error(event.signal_id, str(exc))
        return result


def _signal_identity(indicator: str, event: str) -> tuple[str, dict[str, Any]]:
    if indicator == "turtle_20":
        return "TURTLE_20_BREAKOUT", {"period": 20, "confirmation": "close"}
    if indicator == "turtle_55":
        return "TURTLE_55_BREAKOUT", {"period": 55, "confirmation": "close"}
    if indicator == "sma_10_20":
        return ("MA_GOLDEN_CROSS_10_20" if event == "golden_cross" else "MA_DEATH_CROSS_10_20"), {"fast": 10, "slow": 20, "average": "SMA"}
    if indicator == "macd_12_26_9":
        return ("MACD_GOLDEN_CROSS" if event == "golden_cross" else "MACD_DEATH_CROSS"), {"fast": 12, "slow": 26, "signal": 9, "adjust": False}
    raise ValueError(f"unsupported daily signal indicator: {indicator}")


def _reference_value(name: str, indicator: Mapping[str, Any]) -> float | None:
    if name.startswith("turtle_"):
        return indicator.get("breakout_level")
    if name == "sma_10_20":
        return indicator.get("sma_20")
    if name == "macd_12_26_9":
        return indicator.get("dea")
    return None


def _data_metadata(bars: Any) -> dict[str, Any]:
    values = bars.get("quality_status")
    qualities = sorted({str(value) for value in values.dropna().tolist()}) if values is not None else []
    return {"dataset_version": str(bars.attrs.get("dataset_version", "unknown")), "quality_statuses": qualities, "completed_bars": len(bars), "latest_complete_bar": _timestamp_text(bars["timestamp"].iloc[-1]) if len(bars) else None}


def _chart_payload(frame: Any) -> list[dict[str, Any]]:
    columns = ("open", "high", "low", "close", "volume", "channel_high_20", "channel_low_20", "channel_high_55", "channel_low_55", "sma_10", "sma_20", "dif", "dea", "histogram")
    rows: list[dict[str, Any]] = []
    for timestamp, values in frame.iterrows():
        row: dict[str, Any] = {"timestamp": timestamp.isoformat()}
        for column in columns:
            try:
                value = float(values.get(column))
            except (TypeError, ValueError):
                value = float("nan")
            row[column] = value if value == value and abs(value) != float("inf") else None
        rows.append(row)
    return rows


def _summary(rows: list[Mapping[str, Any]], signals_new: int, delivery: Mapping[str, int]) -> dict[str, int]:
    statuses = [str(row.get("run_status", "failed")) for row in rows]
    return {"selected": len(rows), "updated": statuses.count("updated"), "unchanged": statuses.count("unchanged"), "blocked": statuses.count("blocked"), "stale": statuses.count("stale"), "failed": statuses.count("failed"), "scanned": sum(1 for row in rows if isinstance(row.get("scan", {}).get("status"), Mapping)), "signals_detected": sum(int(row.get("signals_detected", 0)) for row in rows), "signals_new": signals_new, "signals_duplicate": sum(int(row.get("signals_duplicate", 0)) for row in rows), "signals_notified": delivery.get("notified", 0), "delivery_errors": delivery.get("errors", 0), "recovered_deliveries": delivery.get("recovered", 0), "error_count": sum(1 for row in rows if row.get("run_status") in {"failed", "blocked", "stale"})}


def _timestamp_text(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _daily_refresh_end(value: datetime) -> datetime:
    return _aware_utc(value).replace(hour=0, minute=0, second=0, microsecond=0)


def _earliest_utc(value: str) -> datetime:
    return _aware_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False, default=str)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


__all__ = ["DEFAULT_BOOTSTRAP_DAYS", "DailyMarketScanResult", "DailyMarketScanService"]
