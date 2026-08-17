"""Human and machine-readable reports for a completed breakout scan."""
from __future__ import annotations
from datetime import datetime
from io import StringIO
import json
from pathlib import Path
from typing import Iterable
from rich.console import Console
from rich.table import Table
from .breakout import AlertScanResult, ScanState, ScanStatus

def status_table(statuses: Iterable[ScanStatus]) -> Table:
    table = Table(title="海龟 D1 突破扫描", show_lines=False)
    for heading, justify in (("品种", "left"), ("基金/排名", "left"), ("完整K线", "left"), ("收盘", "right"), ("20日高/低", "right"), ("55日高/低", "right"), ("信号", "left"), ("数据版本", "left")):
        table.add_column(heading, justify=justify, no_wrap=heading in {"品种", "完整K线"})
    for item in statuses:
        signal = " / ".join(item.signals) if item.signals else _state_label(item)
        table.add_row(item.symbol, item.ranks or item.funds or "—", _short_time(item.bar_time), _number(item.current_price), _range(item.high_20, item.low_20), _range(item.high_55, item.low_55), signal, _short_version(item.data_version), style=_style(item.state))
    return table

def render_and_write_report(result: AlertScanResult, log_dir: str | Path, *, no_color: bool = False, now: datetime | None = None, console: Console | None = None) -> tuple[Path, Path]:
    now = now or datetime.now().astimezone()
    root = Path(log_dir)
    root.mkdir(parents=True, exist_ok=True)
    stem = now.date().isoformat()
    text_path, jsonl_path = root / f"{stem}.log", root / f"{stem}.jsonl"
    table = status_table(result.statuses)
    summary = f"扫描完成：成功 {result.scanned_symbols}，新预警 {len(result.alerts)}，失败 {len(result.failures)}"
    display = console or Console(no_color=no_color)
    display.print(table)
    display.print(summary)
    buffer = StringIO()
    file_console = Console(file=buffer, no_color=True, width=180)
    file_console.print(table)
    file_console.print(summary)
    _ensure_utf8_bom(text_path)
    with text_path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n[{now.isoformat()}]\n{buffer.getvalue()}")
        for item in result.statuses:
            if item.error:
                handle.write(f"失败 {item.symbol}: {item.error}\n")
    with jsonl_path.open("a", encoding="utf-8") as handle:
        for item in result.statuses:
            handle.write(json.dumps({"scan_time": now.isoformat(), **item.to_dict()}, ensure_ascii=False, allow_nan=False) + "\n")
    return text_path, jsonl_path

def _style(state: ScanState) -> str:
    if state is ScanState.BREAKOUT_UP:
        return "bold red"
    if state is ScanState.BREAKOUT_DOWN:
        return "bold green"
    if state in {ScanState.DATA_UNAVAILABLE, ScanState.QUALITY_REJECTED, ScanState.INSUFFICIENT_HISTORY}:
        return "yellow"
    return ""

def _state_label(item: ScanStatus) -> str:
    return {ScanState.NO_BREAKOUT: "—", ScanState.INSUFFICIENT_HISTORY: "历史不足", ScanState.DATA_UNAVAILABLE: "数据缺失", ScanState.QUALITY_REJECTED: "质量未通过"}.get(item.state, item.state.value)

def _number(value: float | None) -> str: return "—" if value is None else f"{value:.6g}"
def _range(high: float | None, low: float | None) -> str: return "—" if high is None or low is None else f"{high:.6g} / {low:.6g}"
def _short_time(value: str | None) -> str: return "—" if not value else value.replace("T00:00:00+00:00", "")
def _short_version(value: str) -> str: return value if len(value) <= 12 else value[:12]

def _ensure_utf8_bom(path: Path) -> None:
    bom = b"\xef\xbb\xbf"
    if not path.exists():
        path.write_bytes(bom)
        return
    content = path.read_bytes()
    if content and not content.startswith(bom):
        path.write_bytes(bom + content)
