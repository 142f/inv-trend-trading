"""Storage-level benchmark: catalog connection cost and the startup consistency pass.

Measured on a synthetic legacy catalog so the numbers are reproducible on any
machine and contain no network or market-data dependency.
"""
from __future__ import annotations

import json
import statistics
import tempfile
import time
from pathlib import Path

from inv_trend.data.storage import open_data_lake

OPEN_SAMPLES = 40
IDENTITIES = 60
REPEATS = 5


def _seed(lake, count: int) -> None:
    with lake._connect() as db:
        for index in range(count):
            symbol, timeframe = f"SYM{index}", "D1"
            db.execute(
                "INSERT OR REPLACE INTO current_versions VALUES(?,?,?,?,?)",
                (symbol, timeframe, f"v{index}", f"run-{index}", "2026-01-01T00:00:00+00:00"),
            )
    for index in range(count):
        symbol, timeframe = f"SYM{index}", "D1"
        pointer = lake.root / "curated" / f"symbol={symbol}" / f"timeframe={timeframe}" / "current.json"
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(
            json.dumps(
                {
                    "version": f"v{index}",
                    "run_id": f"run-{index}",
                    "channel": "curated",
                    "path": f"curated/symbol={symbol}/timeframe={timeframe}/version=v{index}/bars.parquet",
                }
            ),
            encoding="utf-8",
        )


def _per_connection_ms(lake) -> float:
    samples = []
    for _ in range(OPEN_SAMPLES):
        started = time.perf_counter()
        with lake._connect():
            pass
        samples.append((time.perf_counter() - started) * 1000)
    return statistics.median(samples)


def _consistency_ms(lake) -> float:
    samples = []
    for _ in range(REPEATS):
        started = time.perf_counter()
        result = lake.check_current_consistency(verify_lineage=False)
        samples.append((time.perf_counter() - started) * 1000)
        assert result["ok"], result
    return statistics.median(samples)


def _v3_open_benchmark() -> dict[str, object]:
    """Measure the authoritative v3 (SQLite) open path used by audit_universe."""

    from inv_trend.storage.结构化存储_v3 import UnifiedStore

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        UnifiedStore(root, create=True)
        samples = []
        for _ in range(20):
            started = time.perf_counter()
            UnifiedStore(root)
            samples.append((time.perf_counter() - started) * 1000)
        return {
            "open_ms_median": round(statistics.median(samples), 3),
            "open_ms_max": round(max(samples), 3),
            "note": "each open runs the schema migration chain check; writes only when incomplete",
        }


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        lake = open_data_lake(Path(raw), startup_check=False)
        _seed(lake, IDENTITIES)
        per_open = _per_connection_ms(lake)
        pass_ms = _consistency_ms(lake)
        report = {
            "identities": IDENTITIES,
            "connection_open_ms_median": round(per_open, 4),
            "consistency_pass_ms_median": round(pass_ms, 2),
            "connections_in_one_pass": 1,
            "removed_connections_per_identity": 4,
            "modelled_saving_ms_for_universe": round(per_open * 4 * IDENTITIES, 1),
            "per_identity_ms": round(pass_ms / IDENTITIES, 3),
        }
    report["v3_open"] = _v3_open_benchmark()
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
