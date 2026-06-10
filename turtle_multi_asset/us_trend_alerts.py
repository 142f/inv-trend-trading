"""美股 ETF 持仓趋势突破预警。"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd


LOGGER = logging.getLogger(__name__)

SIGNAL_BREAK_20_HIGH = "BREAK_20_HIGH"
SIGNAL_BREAK_55_HIGH = "BREAK_55_HIGH"
SIGNAL_BREAK_20_LOW = "BREAK_20_LOW"
SIGNAL_BREAK_55_LOW = "BREAK_55_LOW"

DEFAULT_ETFS = ("SPY", "QQQ")
DEFAULT_OUTPUT_DIR = "outputs/us_trend_alerts"
DEFAULT_CACHE_DIR = "processed_data/us_trend_alerts/cache"
DEFAULT_TOP_N = 100
DEFAULT_LOOKBACK_DAYS = 180
DEFAULT_HOLDINGS_CACHE_HOURS = 24
DEFAULT_BARS_CACHE_HOURS = 12
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


@dataclass(frozen=True)
class TrendAlertConfig:
    """美股趋势预警配置。"""

    etfs: tuple[str, ...] = DEFAULT_ETFS
    top_n: int = DEFAULT_TOP_N
    lookback_days: int = DEFAULT_LOOKBACK_DAYS
    holdings_cache_hours: int = DEFAULT_HOLDINGS_CACHE_HOURS
    bars_cache_hours: int = DEFAULT_BARS_CACHE_HOURS
    cache_dir: Path = Path(DEFAULT_CACHE_DIR)
    output_dir: Path = Path(DEFAULT_OUTPUT_DIR)
    local_bars_dir: Path | None = None
    force_refresh: bool = False
    request_sleep_seconds: float = 0.15
    yahoo_timeout_seconds: int = 20
    schwab_timeout_seconds: int = 20

    def __post_init__(self) -> None:
        object.__setattr__(self, "cache_dir", Path(self.cache_dir))
        object.__setattr__(self, "output_dir", Path(self.output_dir))
        if self.local_bars_dir is not None:
            object.__setattr__(self, "local_bars_dir", Path(self.local_bars_dir))
        if self.top_n < 1:
            raise ValueError("top_n 必须大于 0")
        if self.lookback_days < 80:
            raise ValueError("lookback_days 至少需要 80 天")


@dataclass(frozen=True)
class Holding:
    """单只股票在 ETF 持仓中的信息。"""

    symbol: str
    company_name: str
    etfs: str
    weights: str


def run_us_trend_alerts(config: TrendAlertConfig) -> pd.DataFrame:
    """运行 SPY/QQQ 前 N 大持仓趋势突破扫描，并导出结果。"""

    _ensure_dirs(config)
    holdings = build_monitor_pool(config)
    LOGGER.info("监控股票池数量：%s", len(holdings))

    rows: list[dict[str, object]] = []
    for idx, holding in enumerate(holdings, start=1):
        LOGGER.info("检查 %s/%s：%s %s", idx, len(holdings), holding.symbol, holding.company_name)
        try:
            bars = fetch_daily_bars(holding.symbol, config)
            alerts = detect_trend_breakouts(bars)
            for alert in alerts:
                rows.append({**_holding_to_row(holding), **alert})
        except Exception as exc:  # noqa: BLE001 - 扫描任务需要单票容错。
            LOGGER.warning("跳过 %s，原因：%s", holding.symbol, exc)
        if config.request_sleep_seconds > 0:
            time.sleep(config.request_sleep_seconds)

    result = pd.DataFrame(rows, columns=_alert_columns())
    export_alerts(result, config)
    print_alerts(result)
    return result


def build_monitor_pool(config: TrendAlertConfig) -> list[Holding]:
    """获取并合并 ETF 前 N 大持仓，重复股票保留 ETF 来源和权重。"""

    by_symbol: dict[str, dict[str, object]] = {}
    for etf in config.etfs:
        rows = fetch_etf_holdings(etf, config)[: config.top_n]
        for row in rows:
            symbol = _normalize_symbol(str(row["symbol"]))
            if not symbol:
                continue
            item = by_symbol.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "company_name": str(row["company_name"]),
                    "etfs": [],
                    "weights": {},
                },
            )
            item["company_name"] = item["company_name"] or str(row["company_name"])
            item["etfs"].append(etf)
            item["weights"][etf] = float(row["weight"])

    holdings: list[Holding] = []
    for symbol, item in sorted(by_symbol.items()):
        etfs = "+".join(str(x) for x in item["etfs"])
        weights = ";".join(f"{etf}:{float(weight):.4f}%" for etf, weight in item["weights"].items())
        holdings.append(
            Holding(
                symbol=symbol,
                company_name=str(item["company_name"]),
                etfs=etfs,
                weights=weights,
            )
        )
    return holdings


def fetch_etf_holdings(etf: str, config: TrendAlertConfig) -> list[dict[str, object]]:
    """获取 ETF 持仓，优先读取缓存，缓存失效后请求 Schwab 持仓分页接口。"""

    etf = etf.upper()
    cache_path = config.cache_dir / "holdings" / f"{etf}.json"
    if not config.force_refresh:
        cached = _read_json_cache(cache_path, max_age_hours=config.holdings_cache_hours)
        if cached:
            return list(cached["holdings"])

    try:
        holdings = _fetch_schwab_holdings(etf, top_n=config.top_n, timeout=config.schwab_timeout_seconds)
        _write_json(cache_path, {"etf": etf, "fetched_at": _now_iso(), "holdings": holdings})
        return holdings
    except Exception:
        cached = _read_json_cache(cache_path, max_age_hours=None)
        if cached:
            LOGGER.warning("%s 持仓请求失败，使用本地缓存：%s", etf, cache_path)
            return list(cached["holdings"])
        raise


def fetch_daily_bars(symbol: str, config: TrendAlertConfig) -> pd.DataFrame:
    """获取日 K 数据，优先缓存，失败时回退到已有缓存。"""

    symbol = _normalize_symbol(symbol)
    cache_path = config.cache_dir / "bars" / f"{symbol}.csv"
    local_path = Path(config.local_bars_dir) / f"{symbol}.csv" if config.local_bars_dir else None
    if local_path and local_path.exists() and not config.force_refresh:
        return _normalize_bars(pd.read_csv(local_path), symbol)
    if not config.force_refresh and _is_cache_fresh(cache_path, config.bars_cache_hours):
        cached = pd.read_csv(cache_path)
        return _normalize_bars(cached, symbol)

    try:
        bars = _fetch_yahoo_daily_bars(symbol, config)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        bars.to_csv(cache_path, index=False)
        return bars
    except Exception:
        if cache_path.exists():
            LOGGER.warning("%s 行情请求失败，使用本地缓存：%s", symbol, cache_path)
            cached = pd.read_csv(cache_path)
            return _normalize_bars(cached, symbol)
        raise


def detect_trend_breakouts(bars: pd.DataFrame) -> list[dict[str, object]]:
    """检测最新 K 线是否突破/跌破 20 日和 55 日通道。"""

    df = _normalize_bars(bars, symbol=str(bars["symbol"].iloc[0]) if "symbol" in bars.columns and not bars.empty else "")
    if len(df) < 56:
        raise ValueError("日 K 数量不足，至少需要 56 根 K 线")

    out = df.copy()
    out["high_20"] = out["high"].rolling(20).max().shift(1)
    out["high_55"] = out["high"].rolling(55).max().shift(1)
    out["low_20"] = out["low"].rolling(20).min().shift(1)
    out["low_55"] = out["low"].rolling(55).min().shift(1)
    latest = out.iloc[-1]
    levels = {
        SIGNAL_BREAK_20_HIGH: float(latest["high_20"]),
        SIGNAL_BREAK_55_HIGH: float(latest["high_55"]),
        SIGNAL_BREAK_20_LOW: float(latest["low_20"]),
        SIGNAL_BREAK_55_LOW: float(latest["low_55"]),
    }
    if any(pd.isna(value) for value in levels.values()):
        raise ValueError("通道指标为空，历史数据不足或存在缺失")

    close = float(latest["close"])
    high = float(latest["high"])
    low = float(latest["low"])
    trigger_date = pd.Timestamp(latest["date"]).date().isoformat()

    checks = [
        (SIGNAL_BREAK_20_HIGH, high >= levels[SIGNAL_BREAK_20_HIGH] or close >= levels[SIGNAL_BREAK_20_HIGH], max(high, close), levels[SIGNAL_BREAK_20_HIGH]),
        (SIGNAL_BREAK_55_HIGH, high >= levels[SIGNAL_BREAK_55_HIGH] or close >= levels[SIGNAL_BREAK_55_HIGH], max(high, close), levels[SIGNAL_BREAK_55_HIGH]),
        (SIGNAL_BREAK_20_LOW, low <= levels[SIGNAL_BREAK_20_LOW] or close <= levels[SIGNAL_BREAK_20_LOW], min(low, close), levels[SIGNAL_BREAK_20_LOW]),
        (SIGNAL_BREAK_55_LOW, low <= levels[SIGNAL_BREAK_55_LOW] or close <= levels[SIGNAL_BREAK_55_LOW], min(low, close), levels[SIGNAL_BREAK_55_LOW]),
    ]

    alerts: list[dict[str, object]] = []
    for signal, triggered, trigger_price, level in checks:
        if not triggered:
            continue
        amplitude = (trigger_price - level) / level if "HIGH" in signal else (level - trigger_price) / level
        alerts.append(
            {
                "latest_close": round(close, 4),
                "high_20": round(levels[SIGNAL_BREAK_20_HIGH], 4),
                "high_55": round(levels[SIGNAL_BREAK_55_HIGH], 4),
                "low_20": round(levels[SIGNAL_BREAK_20_LOW], 4),
                "low_55": round(levels[SIGNAL_BREAK_55_LOW], 4),
                "signal_type": signal,
                "break_pct": round(float(amplitude) * 100, 4),
                "trigger_date": trigger_date,
            }
        )
    return alerts


def export_alerts(alerts: pd.DataFrame, config: TrendAlertConfig) -> dict[str, Path]:
    """导出 CSV、JSON 和 Markdown 预警结果。"""

    config.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    csv_path = config.output_dir / f"us_trend_alerts_{stamp}.csv"
    json_path = config.output_dir / f"us_trend_alerts_{stamp}.json"
    md_path = config.output_dir / f"us_trend_alerts_{stamp}.md"

    alerts.to_csv(csv_path, index=False, encoding="utf-8-sig")
    _write_json(json_path, alerts.to_dict(orient="records"))
    md_path.write_text(_to_markdown(alerts), encoding="utf-8")
    LOGGER.info("预警结果已导出：%s", config.output_dir)
    return {"csv": csv_path, "json": json_path, "markdown": md_path}


def print_alerts(alerts: pd.DataFrame) -> None:
    """控制台输出预警摘要。"""

    if alerts.empty:
        print("今日未发现 SPY/QQQ 前 100 大持仓趋势突破或跌破信号。")
        return
    print(f"发现 {len(alerts)} 条趋势突破/跌破预警：")
    print(_to_markdown(alerts))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="扫描 SPY/QQQ 前 100 大持仓的 20/55 日趋势突破预警。")
    parser.add_argument("--etfs", nargs="+", default=list(DEFAULT_ETFS), help="ETF 代码，默认：SPY QQQ")
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N, help="每个 ETF 获取前 N 大持仓")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS, help="行情回看自然日数量")
    parser.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR, help="缓存目录")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="输出目录")
    parser.add_argument("--local-bars-dir", default=None, help="本地日 K CSV 目录，文件名格式如 AAPL.csv")
    parser.add_argument("--force-refresh", action="store_true", help="忽略缓存并重新请求")
    parser.add_argument("--verbose", action="store_true", help="输出详细日志")
    return parser


def main(argv: Iterable[str] | None = None) -> pd.DataFrame:
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config = TrendAlertConfig(
        etfs=tuple(str(x).upper() for x in args.etfs),
        top_n=args.top_n,
        lookback_days=args.lookback_days,
        cache_dir=Path(args.cache_dir),
        output_dir=Path(args.output_dir),
        local_bars_dir=Path(args.local_bars_dir) if args.local_bars_dir else None,
        force_refresh=args.force_refresh,
    )
    return run_us_trend_alerts(config)


def _fetch_schwab_holdings(etf: str, *, top_n: int, timeout: int) -> list[dict[str, object]]:
    url = f"https://www.schwab.wallst.com/schwab/Prospect/research/etfs/schwabETF/index.asp?symbol={etf}&type=holdings"
    html = _http_get_text(url, timeout=timeout)
    wsod = _search_required(r"gSymbolWSODIssue = '([^']+)'", html, f"{etf} 缺少 Schwab wsodissue")

    holdings = _parse_schwab_module_rows(
        _fetch_schwab_holdings_page(etf, wsod, page=1, num_rows=min(60, max(top_n, 60)), timeout=timeout)
    )
    if not holdings:
        holdings = _parse_schwab_html_rows(html)
    page = 2
    while len(holdings) < top_n:
        text = _fetch_schwab_holdings_page(etf, wsod, page=page, num_rows=min(60, top_n), timeout=timeout)
        rows = _parse_schwab_module_rows(text)
        if not rows:
            break
        holdings.extend(row for row in rows if row["symbol"] not in {x["symbol"] for x in holdings})
        page += 1
    if not holdings:
        raise ValueError(f"{etf} 未获取到持仓数据")
    return holdings[:top_n]


def _fetch_schwab_holdings_page(etf: str, wsod: str, *, page: int, num_rows: int, timeout: int) -> str:
    payload = {
        "module": "schwabETFHoldingsTable",
        "moduleArgs": {
            "ModuleID": "holdingsTableContainer",
            "symbol": etf,
            "wsodissue": wsod,
            "sortDir": "desc",
            "sortBy": "PctNetAssets",
            "page": page,
            "numRows": num_rows,
            "isThirdPartyETF": True,
        },
    }
    data = urlencode(
        {
            "inputs": "B64ENC" + base64.b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii"),
            "..contenttype..": "text/javascript",
            "..requester..": "ContentBuffer",
        }
    ).encode("utf-8")
    url = "https://www.schwab.wallst.com/schwab/Prospect/research/resources/server/Module/SchwabETF.ModuleAPI.asp"
    request = Request(
        url,
        data=data,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": f"https://www.schwab.wallst.com/schwab/Prospect/research/etfs/schwabETF/index.asp?symbol={etf}&type=holdings",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="ignore")


def _parse_schwab_html_rows(html: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.I | re.S):
        values = [unescape(value) for value in re.findall(r'tsraw="([^"]*)"', row_html)]
        if len(values) >= 3:
            rows.append({"symbol": _normalize_symbol(values[0]), "company_name": values[1], "weight": _to_float(values[2])})
    return rows


def _parse_schwab_module_rows(text: str) -> list[dict[str, object]]:
    match = re.search(r"this\.apiReturn\s*=\s*(\{.*\})\s*;?\s*$", text, flags=re.S)
    if not match:
        return []
    payload = json.loads(match.group(1))
    module = payload.get("module", {})
    rows: list[dict[str, object]] = []
    for tr_node in _walk_nodes(module, tag="tr"):
        values: list[str] = []
        for td_node in tr_node.get("c", []):
            attrs = td_node.get("a", {}) if isinstance(td_node, dict) else {}
            if "tsraw" in attrs:
                values.append(str(attrs["tsraw"]))
        if len(values) >= 3:
            rows.append({"symbol": _normalize_symbol(values[0]), "company_name": values[1], "weight": _to_float(values[2])})
    return rows


def _fetch_yahoo_daily_bars(symbol: str, config: TrendAlertConfig) -> pd.DataFrame:
    end = datetime.now(tz=timezone.utc)
    start = end - timedelta(days=config.lookback_days)
    query = urlencode(
        {
            "period1": int(start.timestamp()),
            "period2": int(end.timestamp()),
            "interval": "1d",
            "events": "history",
            "includeAdjustedClose": "true",
        }
    )
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?{query}"
    payload = json.loads(_http_get_text(url, timeout=config.yahoo_timeout_seconds))
    result = (payload.get("chart", {}).get("result") or [None])[0]
    if not result:
        raise ValueError(f"{symbol} Yahoo 未返回行情")
    timestamps = result.get("timestamp") or []
    quotes = (result.get("indicators", {}).get("quote") or [{}])[0]
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(timestamps, unit="s", utc=True),
            "symbol": symbol,
            "open": quotes.get("open", []),
            "high": quotes.get("high", []),
            "low": quotes.get("low", []),
            "close": quotes.get("close", []),
            "volume": quotes.get("volume", []),
        }
    )
    return _normalize_bars(df, symbol)


def _normalize_bars(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if df.empty:
        raise ValueError(f"{symbol} 日 K 数据为空")
    out = df.copy()
    if "date" not in out.columns and out.index.name in {"date", "time"}:
        out = out.reset_index()
    required = ["date", "open", "high", "low", "close", "volume"]
    missing = [col for col in required if col not in out.columns]
    if missing:
        raise ValueError(f"{symbol} 日 K 缺少字段：{missing}")
    out["date"] = pd.to_datetime(out["date"], utc=True)
    out["symbol"] = symbol or str(out.get("symbol", "").iloc[0])
    for column in ["open", "high", "low", "close", "volume"]:
        out[column] = pd.to_numeric(out[column], errors="coerce")
    out = out.dropna(subset=["date", "open", "high", "low", "close"])
    out = out[(out[["open", "high", "low", "close"]] > 0).all(axis=1)]
    out = out.sort_values("date").drop_duplicates(subset=["date"], keep="last").reset_index(drop=True)
    if out.empty:
        raise ValueError(f"{symbol} 日 K 清洗后为空")
    return out[["date", "symbol", "open", "high", "low", "close", "volume"]]


def _http_get_text(url: str, *, timeout: int) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="ignore")
    except (HTTPError, URLError) as exc:
        raise RuntimeError(f"请求失败：{url}，原因：{exc}") from exc


def _holding_to_row(holding: Holding) -> dict[str, object]:
    return {
        "symbol": holding.symbol,
        "company_name": holding.company_name,
        "etfs": holding.etfs,
        "etf_weights": holding.weights,
    }


def _alert_columns() -> list[str]:
    return [
        "symbol",
        "company_name",
        "etfs",
        "etf_weights",
        "latest_close",
        "high_20",
        "high_55",
        "low_20",
        "low_55",
        "signal_type",
        "break_pct",
        "trigger_date",
    ]


def _to_markdown(df: pd.DataFrame) -> str:
    if df.empty:
        return "| 结果 |\n|---|\n| 今日无触发信号 |\n"
    columns = list(df.columns)
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for _, row in df.iterrows():
        values = [str(row[col]).replace("|", "\\|") for col in columns]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines) + "\n"


def _ensure_dirs(config: TrendAlertConfig) -> None:
    (Path(config.cache_dir) / "holdings").mkdir(parents=True, exist_ok=True)
    (Path(config.cache_dir) / "bars").mkdir(parents=True, exist_ok=True)
    Path(config.output_dir).mkdir(parents=True, exist_ok=True)


def _walk_nodes(node: object, *, tag: str) -> Iterable[dict[str, object]]:
    if isinstance(node, dict):
        if node.get("t") == tag:
            yield node
        for child in node.get("c", []):
            yield from _walk_nodes(child, tag=tag)
    elif isinstance(node, list):
        for child in node:
            yield from _walk_nodes(child, tag=tag)


def _read_json_cache(path: Path, max_age_hours: int | None) -> object | None:
    if not path.exists():
        return None
    if max_age_hours is not None and not _is_cache_fresh(path, max_age_hours):
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _is_cache_fresh(path: Path, max_age_hours: int) -> bool:
    if not path.exists():
        return False
    age_seconds = time.time() - path.stat().st_mtime
    return age_seconds <= max_age_hours * 3600


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _search_required(pattern: str, text: str, message: str) -> str:
    match = re.search(pattern, text)
    if not match:
        raise ValueError(message)
    return match.group(1)


def _normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper().replace(".", "-")


def _to_float(value: object) -> float:
    text = str(value).replace("%", "").replace(",", "").strip()
    return float(text) if text else 0.0


if __name__ == "__main__":
    main()
