import pandas as pd
from main.run_backtest_binance_archive import run_chunk
import logging

logging.basicConfig(level=logging.WARNING)
print("Running chunk test...")
from pathlib import Path
res = run_chunk(
    "ETHUSDT", 
    pd.Timestamp("2026-01-01T00:00:00Z"), 
    pd.Timestamp("2026-02-01T00:00:00Z"), 
    fetch_start=pd.Timestamp("2025-11-01T00:00:00Z"), 
    cache_dir=Path("data_cache/binance_vision/futures_um/monthly/15m")
)
print(f"Stats: {len(res.fills)} fills")
from collections import Counter
c = Counter(res.alerts)
print("Alerts:")
for k, v in c.most_common(10):
    print(k, v)
