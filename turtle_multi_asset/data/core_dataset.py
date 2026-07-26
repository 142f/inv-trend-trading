"""Build and evaluate the metal + tech core dataset without changing strategy logic."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

import pandas as pd

from .exporter import ensure_processed_layout, export_csv, export_json, export_metadata_rows
from .loader import load_backtest_ready_csv
from .pipeline import (
    align_assets,
    clean_data,
    export_backtest_ready,
    inspect_duplicates,
    inspect_missing_values,
    inspect_price_anomalies,
    merge_assets,
    quality_report_row,
    read_data,
    resample_ohlcv,
    standardize_fields,
    unify_dates,
    validate_data,
)
from ..profiles.asset_profiles import infer_asset_fields
from ..strategy.profiles import turtle_rules
from ..backtest.runner import TurtleBacktester
from ..config import BacktestConfig
from ..models.domain import AssetSpec


CORE_DATASET_NAME = "metal_tech_core"
TARGET_TIMEFRAME = "D1"

CORE_SELECTION = {
    "XAUUSD_DUKAS": {"theme": "metal", "priority": "core", "reason": "最长金属样本，连续性最好"},
    "XAGUSD_DUKAS": {"theme": "metal", "priority": "core", "reason": "白银样本长、质量稳定，可补充贵金属方向"},
    "QQQ": {"theme": "technology", "priority": "core", "reason": "科技指数代理，样本长，适合作为核心基准"},
    "NVDA": {"theme": "technology", "priority": "core", "reason": "AI/算力龙头，样本完整"},
    "MSFT": {"theme": "technology", "priority": "core", "reason": "云计算/软件龙头，样本完整"},
    "AVGO": {"theme": "technology", "priority": "core", "reason": "半导体核心龙头，样本完整"},
    "AMD": {"theme": "technology", "priority": "core", "reason": "算力/芯片代表，样本完整"},
    "TSM": {"theme": "technology", "priority": "core", "reason": "晶圆制造核心资产，样本完整"},
    "AMZN": {"theme": "technology", "priority": "core", "reason": "云与平台龙头，样本完整"},
    "ORCL": {"theme": "technology", "priority": "core", "reason": "企业软件/云基础设施代表，样本完整"},
    "AAPL": {"theme": "technology", "priority": "candidate", "reason": "质量稳定，但与其他龙头覆盖略重叠"},
    "META": {"theme": "technology", "priority": "candidate", "reason": "质量稳定，但偏平台流量，不是第一优先"},
    "MU": {"theme": "technology", "priority": "candidate", "reason": "存储半导体代表，可作为半导体补充"},
    "NFLX": {"theme": "technology", "priority": "candidate", "reason": "样本稳定，但科技属性偏内容平台"},
    "TSLA": {"theme": "technology", "priority": "candidate", "reason": "数据稳定，但行业属性混合"},
    "PLTR": {"theme": "technology", "priority": "backup", "reason": "AI 概念强，但历史较短"},
    "SNDK": {"theme": "technology", "priority": "backup", "reason": "样本过短，不适合纳入核心集"},
    "SPY": {"theme": "technology", "priority": "backup", "reason": "宽基指数，不够聚焦科技"},
    "XLY": {"theme": "technology", "priority": "backup", "reason": "消费可选板块，不够聚焦科技"},
    "XAUUSDc": {"theme": "metal", "priority": "candidate", "reason": "MT5 黄金样本较短，且与 DUKAS 黄金重复"},
    "BTCUSDc": {"theme": "technology", "priority": "backup", "reason": "当前归类更接近加密资产，不纳入科技核心版"},
    "BTCUSDT_BINANCE": {"theme": "technology", "priority": "backup", "reason": "当前归类更接近加密资产，不纳入科技核心版"},
    "ETHUSDT_BINANCE": {"theme": "technology", "priority": "backup", "reason": "当前归类更接近加密资产，不纳入科技核心版"},
}


@dataclass(frozen=True)
class SourceRecord:
    dataset: str
    source: str
    timeframe: str
    symbol: str
    path: Path


def build_metal_tech_core_dataset(
    processed_dir: str | Path = "processed_data",
    output_dir: str | Path = "processed_data",
    reports_dir: str | Path = "outputs",
    backtest_config: BacktestConfig | None = None,
) -> dict[str, object]:
    processed_root = Path(processed_dir)
    outputs = ensure_processed_layout(output_dir)
    source_catalog = discover_cleaned_sources(processed_root)
    audit_rows: list[dict[str, object]] = []
    decision_rows: list[dict[str, object]] = []
    source_inventory_rows: list[dict[str, object]] = []
    selected_frames: dict[str, pd.DataFrame] = {}
    selected_source_rows: list[dict[str, object]] = []

    for source in source_catalog:
        source_inventory_rows.append(
            {
                "symbol": source.symbol,
                "dataset": source.dataset,
                "source": source.source,
                "timeframe": source.timeframe,
                "cleaned_input_path": str(source.path),
                "cleaned_input_exists": source.path.exists(),
                "raw_source_available_in_workspace": False,
            }
        )
        raw = read_data(source.path)
        standardized = standardize_fields(raw, symbol=source.symbol, source=source.source, timeframe=source.timeframe)
        standardized = unify_dates(standardized)
        missing = inspect_missing_values(standardized)
        duplicates = inspect_duplicates(standardized)
        anomalies = inspect_price_anomalies(standardized)
        cleaned = clean_data(standardized)
        target_frame = resample_ohlcv(cleaned, TARGET_TIMEFRAME)
        cleaned_report = validate_data(target_frame, symbol=source.symbol, timeframe=TARGET_TIMEFRAME)
        cleaned_report_row = quality_report_row(cleaned_report)
        cleaned_report_row.update(
            {
                "dataset": source.dataset,
                "original_source": source.source,
                "original_timeframe": source.timeframe,
                "input_path": str(source.path),
                "input_missing_values": int(sum(missing.values())),
                "input_duplicate_rows": int(duplicates["duplicate_date_symbol_rows"]),
                "input_full_duplicate_rows": int(duplicates["duplicate_full_rows"]),
                "input_bad_ohlc_rows": int(anomalies["bad_ohlc_rows"]),
                "input_non_positive_price_values": int(anomalies["non_positive_price_values"]),
                "resampled_to": TARGET_TIMEFRAME,
            }
        )
        audit_rows.append(cleaned_report_row)

        selection = CORE_SELECTION.get(source.symbol, {"theme": "other", "priority": "backup", "reason": "未在核心主题范围内"})
        include = selection["priority"] == "core" and cleaned_report.rows > 500
        clean_out = outputs["cleaned"] / CORE_DATASET_NAME / f"{source.symbol.lower()}_{TARGET_TIMEFRAME.lower()}_cleaned.csv"
        export_csv(target_frame, clean_out)

        decision_rows.append(
            {
                "symbol": source.symbol,
                "theme": selection["theme"],
                "data_source": f"{source.dataset}/{source.source}",
                "original_timeframe": source.timeframe,
                "export_timeframe": TARGET_TIMEFRAME,
                "start_date": cleaned_report.start_date,
                "end_date": cleaned_report.end_date,
                "effective_rows": cleaned_report.rows,
                "missing_values": cleaned_report.missing_values,
                "duplicate_rows": cleaned_report.duplicate_rows,
                "anomaly_rows": cleaned_report.anomaly_rows,
                "include_in_final_dataset": include,
                "decision": selection["priority"],
                "reason": selection["reason"] if include else exclusion_reason(selection["priority"], selection["reason"], cleaned_report.rows),
                "cleaned_path": str(clean_out),
            }
        )
        if include:
            selected_frames[source.symbol] = target_frame
            selected_source_rows.append(
                {
                    "symbol": source.symbol,
                    "dataset": source.dataset,
                    "source": source.source,
                    "timeframe": TARGET_TIMEFRAME,
                    "path": str(clean_out),
                }
            )

    aligned_frames, alignment_report = align_assets(selected_frames)
    merged = merge_assets(aligned_frames)
    merged_path = export_csv(
        merged,
        outputs["merged"] / CORE_DATASET_NAME / f"{CORE_DATASET_NAME}_{TARGET_TIMEFRAME.lower()}_merged.csv",
    )
    backtest_path = export_backtest_ready(
        merged,
        outputs["backtest_ready"] / CORE_DATASET_NAME / f"{CORE_DATASET_NAME}_{TARGET_TIMEFRAME.lower()}_backtest_ready.csv",
    )

    export_metadata_rows(audit_rows, outputs["logs"] / f"{CORE_DATASET_NAME}_quality_audit.csv")
    export_metadata_rows(decision_rows, outputs["metadata"] / f"{CORE_DATASET_NAME}_universe.csv")
    export_metadata_rows(selected_source_rows, outputs["metadata"] / f"{CORE_DATASET_NAME}_selected_sources.csv")
    export_metadata_rows(source_inventory_rows, outputs["metadata"] / f"{CORE_DATASET_NAME}_source_inventory.csv")
    export_json(
        {
            "required_fields": [
                {"name": "date", "type": "datetime64[ns, UTC]", "description": "统一 UTC 日期时间"},
                {"name": "symbol", "type": "string", "description": "品种代码"},
                {"name": "open", "type": "float", "description": "开盘价"},
                {"name": "high", "type": "float", "description": "最高价"},
                {"name": "low", "type": "float", "description": "最低价"},
                {"name": "close", "type": "float", "description": "收盘价"},
                {"name": "volume", "type": "float", "description": "成交量或代理成交量"},
                {"name": "spread", "type": "float", "description": "点差，缺失时置为 0"},
                {"name": "source", "type": "string", "description": "来源标签"},
                {"name": "timeframe", "type": "string", "description": "统一后的周期标签"},
            ]
        },
        outputs["metadata"] / f"{CORE_DATASET_NAME}_field_dictionary.json",
    )
    export_json(
        {
            "dataset_name": CORE_DATASET_NAME,
            "target_timeframe": TARGET_TIMEFRAME,
            "selected_symbols": sorted(selected_frames),
            "alignment_report": alignment_report,
            "available_symbols": sorted(record.symbol for record in source_catalog),
            "notes": [
                "原始 data_* 源目录当前不在工作区，构建基于现有 processed_data/cleaned 文件完成。",
                "为统一金属与科技资产频率，H4 金属数据按自然日聚合为 D1。",
                "未做前向填充，不修改策略逻辑、手续费、滑点和仓位规则。",
            ],
        },
        outputs["metadata"] / f"{CORE_DATASET_NAME}_build_summary.json",
    )

    report_payload = run_core_backtest_and_compare(
        backtest_path=backtest_path,
        selected_frames=selected_frames,
        reports_dir=reports_dir,
        backtest_config=backtest_config,
    )

    report_json_path = Path(reports_dir) / CORE_DATASET_NAME / "report.json"
    report_json_path.parent.mkdir(parents=True, exist_ok=True)
    report_json_path.write_text(json.dumps(report_payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    return {
        "dataset_name": CORE_DATASET_NAME,
        "selected_symbol_count": len(selected_frames),
        "selected_symbols": sorted(selected_frames),
        "merged_path": str(merged_path),
        "backtest_ready_path": str(backtest_path),
        "report_path": str(report_json_path),
    }


def discover_cleaned_sources(processed_root: Path) -> list[SourceRecord]:
    records: list[SourceRecord] = []
    for path in sorted((processed_root / "cleaned").rglob("*.csv")):
        if path.parent.name == CORE_DATASET_NAME:
            continue
        if "_cleaned" not in path.stem:
            continue
        sample = pd.read_csv(path, nrows=1)
        if sample.empty or "symbol" not in sample.columns:
            continue
        dataset = path.parent.name
        symbol = str(sample["symbol"].iloc[0])
        timeframe = str(sample["timeframe"].iloc[0]).upper() if "timeframe" in sample.columns else ""
        source = str(sample["source"].iloc[0]) if "source" in sample.columns else infer_source_from_path(path)
        records.append(
            SourceRecord(
                dataset=dataset,
                source=source,
                timeframe=timeframe,
                symbol=symbol,
                path=path,
            )
        )
    deduped: dict[str, SourceRecord] = {}
    for record in records:
        existing = deduped.get(record.symbol)
        if existing is None or score_source(record) > score_source(existing):
            deduped[record.symbol] = record
    return sorted(deduped.values(), key=lambda item: (item.symbol, item.dataset))


def infer_source_from_path(path: Path) -> str:
    lower = str(path).lower()
    if "dukas" in lower or "binance" in lower:
        return "external"
    if "equities" in lower or "nasdaq" in lower:
        return "nasdaq"
    return "mt5"
def score_source(record: SourceRecord) -> tuple[int, int]:
    preference = {
        "XAUUSD_DUKAS": 3,
        "XAGUSD_DUKAS": 3,
        "QQQ": 3,
    }
    dataset_score = 2 if "external" in record.dataset else 1
    return preference.get(record.symbol, 1), dataset_score


def exclusion_reason(priority: str, base_reason: str, rows: int) -> str:
    if rows <= 500:
        return f"数据长度仅 {rows} 行，未纳入核心集"
    if priority == "candidate":
        return f"列为候选: {base_reason}"
    return f"未纳入核心集: {base_reason}"


def build_asset_specs(symbols: list[str]) -> dict[str, AssetSpec]:
    specs: dict[str, AssetSpec] = {}
    for symbol in symbols:
        inferred = infer_asset_fields(symbol)
        specs[symbol] = AssetSpec(
            symbol=symbol,
            asset_class=str(inferred["asset_class"]),
            cluster=str(inferred["cluster"]),
            point_value=1.0,
            qty_step=1.0,
            min_qty=0.0,
            can_long=True,
            can_short=True,
            max_units=int(inferred["max_units"]),
            unit_1n_risk_pct=float(inferred["unit_1n_risk_pct"]),
            max_symbol_1n_risk_pct=float(inferred["max_symbol_1n_risk_pct"]),
            max_symbol_leverage=float(inferred["max_symbol_leverage"]),
            cost_bps=float(inferred["cost_bps"]),
            slippage_bps=float(inferred["slippage_bps"]),
        )
    return specs


def run_core_backtest_and_compare(
    *,
    backtest_path: Path,
    selected_frames: dict[str, pd.DataFrame],
    reports_dir: str | Path,
    backtest_config: BacktestConfig | None = None,
) -> dict[str, object]:
    reports_root = Path(reports_dir) / CORE_DATASET_NAME
    reports_root.mkdir(parents=True, exist_ok=True)

    original_data = {
        symbol: frame.sort_values("date").set_index("date")[["open", "high", "low", "close", "volume", "spread"]]
        for symbol, frame in selected_frames.items()
    }
    merged_data = load_backtest_ready_csv(backtest_path)
    specs = build_asset_specs(sorted(selected_frames))
    rules = turtle_rules("classic-bars")

    baseline_result = TurtleBacktester(
        original_data,
        specs,
        rules,
        config=backtest_config,
    ).run()
    rebuilt_result = TurtleBacktester(
        merged_data,
        specs,
        rules,
        config=backtest_config,
    ).run()

    baseline_summary = summarize_backtest_result(baseline_result)
    rebuilt_summary = summarize_backtest_result(rebuilt_result)
    comparison = compare_summaries(baseline_summary, rebuilt_summary)

    export_backtest_outputs(baseline_result, reports_root / "baseline")
    export_backtest_outputs(rebuilt_result, reports_root / "rebuilt")
    export_csv(build_signal_table(rebuilt_result.orders), reports_root / "buy_sell_signals.csv")
    export_csv(build_contribution_table(rebuilt_result.trades), reports_root / "symbol_contribution.csv")
    export_csv(build_daily_equity_curve(rebuilt_result.equity_curve), reports_root / "daily_equity_curve.csv")
    export_json(baseline_summary, reports_root / "baseline_metrics.json")
    export_json(rebuilt_summary, reports_root / "rebuilt_metrics.json")
    export_json(comparison, reports_root / "comparison.json")

    return {
        "baseline": baseline_summary,
        "rebuilt": rebuilt_summary,
        "comparison": comparison,
    }


def export_backtest_outputs(result: object, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    result.equity_curve.to_csv(out_dir / "equity_curve.csv")
    result.orders.to_csv(out_dir / "orders.csv", index=False)
    result.trades.to_csv(out_dir / "trades.csv", index=False)
    result.trade_details.to_csv(out_dir / "trade_details.csv", index=False)
    export_json(result.metrics, out_dir / "metrics.json")


def summarize_backtest_result(result: object) -> dict[str, object]:
    trades = result.trades.copy()
    equity = result.equity_curve.copy()
    wins = pd.Series(dtype=bool) if trades.empty else trades["pnl"] > 0
    gross_profit = float(trades.loc[trades["pnl"] > 0, "pnl"].sum()) if not trades.empty else 0.0
    gross_loss = float(-trades.loc[trades["pnl"] < 0, "pnl"].sum()) if not trades.empty else 0.0
    payoff = gross_profit / gross_loss if gross_loss > 0 else 0.0
    holding_days = 0.0
    if not trades.empty:
        holding_days = float(
            ((pd.to_datetime(trades["exit_time"], utc=True) - pd.to_datetime(trades["entry_time"], utc=True)).dt.total_seconds() / 86400.0).mean()
        )
    summary = dict(result.metrics)
    summary.update(
        {
            "win_rate": float(wins.mean()) if len(wins) else 0.0,
            "trade_count": int(len(trades)),
            "payoff_ratio": float(payoff),
            "avg_holding_days": float(holding_days),
            "final_equity": float(equity.iloc[-1]) if not equity.empty else 0.0,
            "daily_curve_path": "daily_equity_curve.csv",
            "signal_count": int(len(result.orders)),
        }
    )
    return summary


def compare_summaries(baseline: dict[str, object], rebuilt: dict[str, object]) -> dict[str, object]:
    numeric_keys = [
        "total_return",
        "cagr",
        "max_drawdown",
        "sharpe_like",
        "win_rate",
        "trade_count",
        "payoff_ratio",
        "avg_holding_days",
        "final_equity",
    ]
    diffs = {}
    for key in numeric_keys:
        old = float(baseline.get(key, 0.0))
        new = float(rebuilt.get(key, 0.0))
        diffs[key] = {
            "before": old,
            "after": new,
            "delta": new - old,
        }
    changed = any(abs(item["delta"]) > 1e-9 for item in diffs.values())
    return {
        "metrics": diffs,
        "changed": changed,
        "difference_reason": "同一批标准化数据在重新导出后回测一致" if not changed else "结果变化来自数据处理差异，请结合质量报告检查",
    }


def build_daily_equity_curve(equity_curve: pd.Series) -> pd.DataFrame:
    daily = equity_curve.resample("1D").last().dropna()
    return pd.DataFrame(
        {
            "date": daily.index,
            "equity": daily.values,
            "daily_return": daily.pct_change().fillna(0.0).values,
        }
    )


def build_signal_table(orders: pd.DataFrame) -> pd.DataFrame:
    if orders.empty:
        return pd.DataFrame(columns=["time", "symbol", "action", "side", "reason", "fill_price"])
    keep = [column for column in ["time", "symbol", "action", "side", "reason", "fill_price", "system"] if column in orders.columns]
    return orders[keep].sort_values("time").reset_index(drop=True)


def build_contribution_table(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame(columns=["symbol", "net_pnl", "trade_count", "win_rate"])
    grouped = trades.groupby("symbol", sort=True)
    out = grouped["pnl"].sum().rename("net_pnl").to_frame()
    out["trade_count"] = grouped.size()
    out["win_rate"] = grouped.apply(lambda frame: float((frame["pnl"] > 0).mean()))
    return out.reset_index().sort_values("net_pnl", ascending=False).reset_index(drop=True)
