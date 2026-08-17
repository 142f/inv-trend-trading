"""Explicitly re-certify the static D1 Golden Master.

Normal pytest runs only read ``tests/fixtures/golden_d1_expected.json``.  This
tool is the sole refresh entry point: it refuses to write unless ``--confirm``
is supplied, evaluates Git 121f901 in an isolated archive, and prints the first
business difference instead of silently replacing a baseline.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[1]
BASELINE_COMMIT = "121f9016f8490baf42f5c2212aa46d97b9247290"
WORKSPACE_TEMP_ROOT = ROOT / ".tmp" / "golden-master"


def _workspace_cache() -> Path:
    """Return a stable, ignored cache outside the workstation's ACL-restricted temp."""

    WORKSPACE_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    return WORKSPACE_TEMP_ROOT


def _current_support():
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "src"))
    from tests import golden_master_support

    return golden_master_support


def _baseline_snapshot(fixture: Path) -> dict:
    """Run the shared, pre-refactor paths from the archived baseline checkout."""

    import math
    from collections.abc import Mapping

    import numpy as np
    import pandas as pd

    from turtle_detector.backtest.engine import DetectorBacktester
    from turtle_detector.engine.scanner import TurtleScanner
    from turtle_detector.models import AssetConfig, Market, StrategyConfig
    from turtle_multi_asset import AssetSpec, TurtleBacktester, TurtleRules

    volatile = {"generated_at", "intent_id"}

    def canonical(value):
        if isinstance(value, Mapping):
            return {
                str(key): canonical(item)
                for key, item in value.items()
                if str(key) not in volatile
            }
        if isinstance(value, (list, tuple)):
            return [canonical(item) for item in value]
        if isinstance(value, pd.Timestamp):
            timestamp = value.tz_localize("UTC") if value.tzinfo is None else value
            return timestamp.isoformat()
        if isinstance(value, np.bool_):
            return bool(value)
        if isinstance(value, np.integer):
            return int(value)
        if isinstance(value, (float, np.floating)):
            number = float(value)
            return number if math.isfinite(number) else None
        if pd.isna(value) if not isinstance(value, (str, bytes)) else False:
            return None
        return value

    def asset():
        return AssetConfig(
            symbol="BTC",
            instrument="BTCUSDT.BINANCE.SPOT",
            market=Market.CRYPTO,
            data_source="binance",
            timeframes=("D1",),
            adjustment="none",
            allow_short=False,
        )

    def config():
        return StrategyConfig(
            atr_period=20,
            system1_entry=20,
            system2_entry=55,
            system1_exit=10,
            system2_exit=20,
            volatility_lookback=60,
            trend_ma_period=20,
            long_trend_ma_period=55,
            false_breakout_bars=3,
        )

    def rules():
        return TurtleRules(
            n_period=20,
            fast_entry=20,
            slow_entry=55,
            fast_exit=10,
            slow_exit=20,
            skip_fast_after_win=True,
            max_total_1n_risk_pct=0.12,
        )

    def curves(curve):
        return [
            {"timestamp": pd.Timestamp(timestamp).isoformat(), "equity": float(value)}
            for timestamp, value in curve.items()
        ]

    bars = pd.read_csv(fixture, parse_dates=["timestamp"]).set_index("timestamp")
    prepared = TurtleScanner(config()).prepare(bars, asset(), "D1")
    detector = DetectorBacktester(config(), initial_equity=100_000, cost_bps=5).run(
        bars, asset(), "D1"
    )
    multi = TurtleBacktester(
        {"BTC": bars},
        {
            "BTC": AssetSpec(
                "BTC", "crypto", "crypto", qty_step=0.001, can_short=False
            )
        },
        rules(),
        initial_equity=100_000,
    ).run()
    return canonical(
        {
            "feature_tail": {
                column: float(prepared.iloc[-1][column])
                for column in (
                    "atr",
                    "channel_high_20",
                    "channel_low_20",
                    "channel_high_55",
                    "channel_low_55",
                )
            },
            "detector_events": [
                {
                    key: value
                    for key, value in event.items()
                    if key
                    in {
                        "signal_type",
                        "raw_signal_type",
                        "direction",
                        "signal_time",
                        "trigger_price",
                        "tradeable",
                        "filtered_reasons",
                    }
                }
                for event in detector.signals.to_dict("records")
            ],
            "detector_trades": detector.trades.to_dict("records"),
            "detector_equity": curves(detector.equity_curve),
            "detector_metrics": detector.metrics,
            "multi_orders": multi.orders.drop(columns=["intent_id"], errors="ignore").to_dict("records"),
            "multi_trades": multi.trades.to_dict("records"),
            "multi_equity": curves(multi.equity_curve),
            "multi_metrics": multi.metrics,
        }
    )


def _write_static_fixture(destination: Path) -> None:
    """Materialize the known published BTC slice as an LF-only fixture."""

    import pandas as pd

    source = ROOT / (
        "data/curated/asset_class=crypto/instrument=BTCUSDT.BINANCE.SPOT/"
        "timeframe=D1/version=028585a633484f05948afa5f/bars.parquet"
    )
    if not source.is_file():
        raise FileNotFoundError(f"published BTC D1 source is unavailable: {source}")
    frame = pd.read_parquet(source)
    timestamps = pd.to_datetime(frame["timestamp"], utc=True)
    selected = frame.loc[
        (timestamps >= pd.Timestamp("2022-01-01", tz="UTC"))
        & (timestamps <= pd.Timestamp("2023-02-24", tz="UTC")),
        ["timestamp", "open", "high", "low", "close", "volume", "is_complete"],
    ].copy()
    selected["timestamp"] = pd.to_datetime(selected["timestamp"], utc=True)
    if len(selected) != 420:
        raise RuntimeError(f"expected 420 BTC D1 bars, found {len(selected)}")
    if selected["timestamp"].min() != pd.Timestamp("2022-01-01", tz="UTC"):
        raise RuntimeError("fixture start does not match the certified BTC slice")
    if selected["timestamp"].max() != pd.Timestamp("2023-02-24", tz="UTC"):
        raise RuntimeError("fixture end does not match the certified BTC slice")
    destination.parent.mkdir(parents=True, exist_ok=True)
    selected.to_csv(
        destination,
        index=False,
        lineterminator="\n",
        date_format="%Y-%m-%dT%H:%M:%S%z",
        float_format="%.17g",
    )
    if b"\r\n" in destination.read_bytes():
        raise RuntimeError("fixture write produced CRLF line endings")


def _run_baseline(fixture: Path) -> dict:
    """Archive the pinned revision so no current source can leak into it."""

    cache = _workspace_cache()
    archive = cache / f"baseline-{BASELINE_COMMIT}.zip"
    baseline_root = cache / f"baseline-{BASELINE_COMMIT}"
    snapshot_path = cache / f"baseline-{BASELINE_COMMIT}-snapshot.json"
    baseline_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "archive", "--format=zip", f"--output={archive}", BASELINE_COMMIT],
        cwd=ROOT,
        check=True,
    )
    with zipfile.ZipFile(archive) as zip_file:
        zip_file.extractall(baseline_root)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(baseline_root)
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--_runtime",
            "baseline",
            "--fixture",
            str(fixture),
            "--snapshot-output",
            str(snapshot_path),
        ],
        cwd=baseline_root,
        env=environment,
        check=True,
    )
    return json.loads(snapshot_path.read_text(encoding="utf-8"))


def _internal_runtime(arguments: argparse.Namespace) -> int:
    if arguments._runtime != "baseline":
        raise ValueError(f"unsupported internal runtime: {arguments._runtime}")
    snapshot = _baseline_snapshot(Path(arguments.fixture))
    Path(arguments.snapshot_output).write_text(
        json.dumps(snapshot, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return 0


def _refresh(arguments: argparse.Namespace) -> int:
    if not arguments.confirm:
        print(
            "Refusing to overwrite the Golden Master. Re-run with --confirm after reviewing "
            "the business-difference report.",
            file=sys.stderr,
        )
        return 2
    support = _current_support()
    fixture = support.fixture_path()
    if arguments.rebuild_fixture:
        _write_static_fixture(fixture)
    if not fixture.is_file():
        print(
            f"Golden fixture is missing: {fixture}. Re-run with --confirm --rebuild-fixture.",
            file=sys.stderr,
        )
        return 2

    current = support.build_current_snapshot(support.load_bars(fixture))
    if support.expected_path().is_file():
        existing = json.loads(support.expected_path().read_text(encoding="utf-8"))
        if existing.get("input_sha256") != support.fixture_sha256(fixture):
            print(
                "First business difference against the existing Golden Master: "
                "$.input_sha256 (fixture bytes changed)."
            )
        else:
            existing_difference = support.first_difference(
                current, existing.get("outputs", {})
            )
            if existing_difference:
                print(
                    "First business difference against the existing Golden Master: "
                    f"{existing_difference}"
                )
            else:
                print("Existing Golden Master has no business difference.")
    baseline = _run_baseline(fixture)
    difference = support.first_difference(
        support.baseline_comparable_snapshot(current), baseline
    )
    if difference:
        print(
            "Golden Master was not refreshed because Git 121f901 differs from the current "
            f"shared strategy behavior. First business difference: {difference}",
            file=sys.stderr,
        )
        return 1

    payload = support.expected_payload(
        current,
        input_sha256=support.fixture_sha256(fixture),
        certification={
            "status": "re-certified",
            "baseline_commit": BASELINE_COMMIT,
            "baseline_comparison": "equivalent",
            "covered_paths": [
                "Wilder ATR and prior-bar Donchian features",
                "Detector events, trades, equity, and metrics",
                "Multi-asset orders, trades, equity, and metrics",
            ],
            "not_covered_by_121f901": [
                "Daily Scanner SMA/MACD analysis (not present in the baseline revision)",
            ],
            "volatile_fields_excluded": ["generated_at", "intent_id"],
            "numeric_relative_tolerance": support.FLOAT_REL_TOLERANCE,
        },
    )
    support.expected_path().write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        f"Re-certified {support.expected_path().relative_to(ROOT)} against {BASELINE_COMMIT}; "
        f"fixture SHA-256={payload['input_sha256']}."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="required acknowledgement before rewriting expected JSON",
    )
    parser.add_argument(
        "--rebuild-fixture",
        action="store_true",
        help="recreate the static BTC D1 fixture from the pinned local data version",
    )
    parser.add_argument("--_runtime", choices=("baseline",), help=argparse.SUPPRESS)
    parser.add_argument("--fixture", help=argparse.SUPPRESS)
    parser.add_argument("--snapshot-output", help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if arguments._runtime:
        return _internal_runtime(arguments)
    return _refresh(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
