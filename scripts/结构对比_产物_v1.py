#!/usr/bin/env python
"""S3 产物生成器：在两棵树里各跑一次，生成一致性对比所需的离线产物。

脚本**自身**只有一份（改后树持有，改前树用同一份副本），因此"生成方式"不构成
两树差异；差异只可能来自被重构的 src/。

用法::

    PYTHONPATH=<tree>/src python scripts/结构对比_产物_v1.py \
        --surface all --out .tmp/compare/{before|after} \
        --processed-dir <共享只读输入>/processed_data

产物（全部落在 --out 之下，绝不写入受保护目录）::

    processed/            核心数据集构建输出（cleaned/merged/backtest_ready/metadata/logs）
    reports/metal_tech_core/  回测报告（baseline/rebuilt/*.csv/*.json/*.html）
    cli_stdout.txt        CLI 真实入口的 stdout/stderr
    daily/ signals/       日更产物发布树
    daily_readback.json   inv_trend.storage.制品.read_daily 的读取回读
    raw/ normalized/ curated/ manifests/ quality_reports/
                      --surface data-pipeline 离线写入 --out 的 K 线制品
    readback.json         data-pipeline 读取结果的列名、dtype 与 frame_hash
    golden_snapshot.json  Golden Master 快照
    probe.json / immutable_json_probe.json   不可变写入探针
    manifest.json         surface 清单 + 树级溯源
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

TREE_ROOT = Path(__file__).resolve().parents[1]

PROVENANCE_TREE_COMMIT = "tree_commit"
PROVENANCE_MODULE_COUNT = "python_module_count"


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------


def _env() -> dict[str, str]:
    env = dict(os.environ)
    src = str(TREE_ROOT / "src")
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = src + (os.pathsep + existing if existing else "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _guard() -> str:
    """断言实际加载的 inv_trend 落在本树内；否则两树会加载同一份代码，对比失效。"""

    code = (
        "import pathlib, inv_trend;"
        "p = pathlib.Path(inv_trend.__file__).resolve();"
        f"assert pathlib.Path({str(TREE_ROOT)!r}).resolve() in p.parents, f'WRONG TREE: {{p}}';"
        "print(p)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=_env(),
        cwd=str(TREE_ROOT),
    )
    if proc.returncode != 0:
        raise SystemExit(
            "树隔离守卫失败：import inv_trend 未落在本树内。\n"
            f"stdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return proc.stdout.strip()


def _tree_commit() -> str:
    proc = subprocess.run(
        ["git", "-C", str(TREE_ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip() if proc.returncode == 0 else "unknown"


def _python_module_count() -> int:
    src = TREE_ROOT / "src"
    return sum(
        1
        for path in src.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True, default=str),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# surface 1：核心数据集 + 回测报告（真实 CLI 入口）
# ---------------------------------------------------------------------------


def surface_core_dataset(out: Path, processed_dir: Path) -> None:
    command = [
        sys.executable,
        "-m",
        "inv_trend.cli.data",
        "build-metal-tech-core",
        "--processed-dir",
        str(processed_dir),
        "--output-dir",
        str(out / "processed"),
        "--reports-dir",
        str(out / "reports"),
    ]
    proc = subprocess.run(
        command, capture_output=True, text=True, env=_env(), cwd=str(TREE_ROOT)
    )
    (out / "cli_stdout.txt").write_text(proc.stdout, encoding="utf-8")
    (out / "cli_stderr.txt").write_text(proc.stderr, encoding="utf-8")
    # 只记录解释器 basename：sys.executable 的绝对路径落在 venv 里，
    # 会把「环境路径」混进产物对比，与本轮重构无关。
    _write_json(
        out / "cli_argv.json",
        {
            "interpreter": Path(sys.executable).name,
            "argv": command[1:],
            "returncode": proc.returncode,
        },
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"core-dataset surface 失败（returncode={proc.returncode}）\n"
            f"{proc.stdout}\n{proc.stderr}"
        )


# ---------------------------------------------------------------------------
# surface 2：日更产物发布 + 回读
# ---------------------------------------------------------------------------


def _daily_snapshot() -> dict[str, object]:
    """与 tests/unit/test_daily_artifact_publication.py 同形的固定 snapshot。"""

    return {
        "schema_version": "4",
        "report_schema_version": "5",
        "report_date": "2026-08-20",
        "run_id": "artifact-retry-1",
        "configuration": {},
        "symbols": [
            {
                "symbol": "BTC",
                "instrument_id": "BTC.TEST.SPOT",
                "timeframe": "D1",
                "run_status": "updated",
                "data_update_result": {
                    "result_hash": "data-hash",
                    "dataset_version": "dataset-v1",
                    "latest_complete_d1": "2026-08-20T00:00:00+00:00",
                },
                "strategy_screening_result": {
                    "result_hash": "screening-hash",
                    "input_data_hash": "data-hash",
                    "conditions": [],
                },
                "trend_decision_result": {
                    "result_hash": "decision-hash",
                    "input_data_hash": "data-hash",
                    "input_screening_hash": "screening-hash",
                    "as_of": "2026-08-20T00:00:00+00:00",
                },
                "report_bundle": {"symbol": "BTC", "summary": {}},
            }
        ],
    }


def surface_daily_publish(out: Path) -> None:
    from inv_trend.adapters.daily.composition import create_daily_run_artifact_writer
    from inv_trend.storage.制品 import read_daily

    writer = create_daily_run_artifact_writer(
        out / "daily", signal_log_root=out / "signals"
    )
    publication = writer.publish(_daily_snapshot(), render_html=True)
    readback = read_daily(publication.compatibility_json)
    _write_json(out / "daily_readback.json", readback)
    _write_json(
        out / "daily_publication.json",
        {
            "run_directory": str(publication.run_directory),
            "compatibility_json": str(publication.compatibility_json),
            "compatibility_html": (
                str(publication.compatibility_html)
                if publication.compatibility_html is not None
                else None
            ),
            "complete_results": {
                symbol: str(path) for symbol, path in publication.complete_results.items()
            },
        },
    )


# ---------------------------------------------------------------------------
# surface 3：Golden Master 快照
# ---------------------------------------------------------------------------


def surface_golden_master(out: Path) -> None:
    if str(TREE_ROOT) not in sys.path:
        sys.path.insert(0, str(TREE_ROOT))
    from tests.golden_master_support import build_current_snapshot, load_bars

    snapshot = build_current_snapshot(load_bars())
    _write_json(out / "golden_snapshot.json", snapshot)


# ---------------------------------------------------------------------------
# 不可变写入探针（覆盖 P4 的搬动）
# ---------------------------------------------------------------------------


def immutable_probe(out: Path) -> None:
    try:
        from inv_trend.storage.基础 import immutable_json
    except ImportError:
        from inv_trend.core.滚动协议 import immutable_json

    probe_path = out / "probe.json"
    if probe_path.exists():
        probe_path.unlink()
    payload = {
        "symbol": "BTC",
        "as_of": "2026-08-20T00:00:00+00:00",
        "value": 1.5,
        "note": "结构重构不可变写入探针",
    }
    returned = immutable_json(probe_path, payload)
    _write_json(
        out / "immutable_json_probe.json",
        {
            "returned_sha256": returned,
            "file_sha256": hashlib.sha256(probe_path.read_bytes()).hexdigest(),
            "payload": payload,
        },
    )


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------


def _pin_run_id(run_id: str) -> None:
    """在导入产品代码之前固定 uuid4，使两侧制品文件名可以对齐。"""

    import uuid

    token = run_id.strip().lower().replace("-", "")
    if not token or any(char not in "0123456789abcdef" for char in token):
        raise SystemExit("--run-id 必须是十六进制字符串")
    token = (token + ("0" * 32))[:32]

    class _FixedId:
        hex = token

        def __str__(self) -> str:
            return token

    uuid.uuid4 = lambda: _FixedId()  # type: ignore[method-assign, assignment]


def _drop_runtime_noise(out: Path) -> None:
    """这些文件每次运行都会变，不参与 data-pipeline 的制品对比。"""

    for relative in (".locks", "logs"):
        target = out / relative
        if target.exists():
            shutil.rmtree(target)
    for name in ("catalog.duckdb", "catalog.duckdb.wal", "catalog.duckdb-shm", "catalog.duckdb-wal"):
        target = out / name
        if target.is_file():
            target.unlink()


def surface_data_pipeline(out: Path, *, run_id: str | None = None, fixture: Path | None = None) -> None:
    """离线跑一次 K 线 ingest，把链路制品和读回摘要写到 --out。不访问网络。"""

    if run_id:
        _pin_run_id(run_id)

    import pandas as pd

    from inv_trend.data.api import HistoricalDataService
    from inv_trend.infra.integrity import strategy_frame_hash
    from inv_trend.data.providers import CsvBarsProvider

    fixture = (fixture or (TREE_ROOT / "tests" / "fixtures" / "pipeline_bars_d1.csv")).resolve()
    if not fixture.is_file():
        raise SystemExit(f"data-pipeline surface 缺少固定样本: {fixture}")

    lake = out
    service = HistoricalDataService(
        lake, providers={"licensed_csv": CsvBarsProvider(fixture)}, startup_check=False,
    )
    base = service.instruments["BTC"]
    service.instruments["BTC"] = base.__class__(
        **{**base.__dict__, "primary_source": "licensed_csv"}
    )
    frame = pd.read_csv(fixture)
    start = pd.Timestamp(frame["timestamp"].iloc[0]).to_pydatetime()
    end = pd.Timestamp(frame["timestamp"].iloc[-1]).to_pydatetime()
    manifest = service.ingest("BTC", "D1", start, end)
    bars = service.load_bars("BTC", "D1", start, end)
    _write_json(
        out / "readback.json",
        {
            "symbol": "BTC",
            "timeframe": "D1",
            "dataset_version": manifest.dataset_version,
            "rows": int(len(bars)),
            "columns": [str(column) for column in bars.columns],
            "dtypes": {str(column): str(dtype) for column, dtype in bars.dtypes.items()},
            "frame_hash": strategy_frame_hash(bars),
        },
    )
    _drop_runtime_noise(out)


def _manifest(out: Path, surfaces: list[str], *, omit_provenance: bool = False) -> None:
    files = sorted(
        path.relative_to(out).as_posix()
        for path in out.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    )
    payload: dict[str, object] = {"surface": surfaces, "files": files}
    if not omit_provenance:
        payload[PROVENANCE_TREE_COMMIT] = _tree_commit()
        payload[PROVENANCE_MODULE_COUNT] = _python_module_count()
    _write_json(out / "manifest.json", payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--surface",
        choices=("core-dataset", "daily-publish", "golden-master", "data-pipeline", "all"),
        default="all",
    )
    parser.add_argument("--out", required=True)
    parser.add_argument("--processed-dir")
    parser.add_argument("--run-id", default=None, help="data-pipeline 专用：固定 uuid4，便于两侧文件名对齐")
    parser.add_argument("--fixture", default=None, help="data-pipeline 专用：CSV 绝对路径。两侧必须相同")
    args = parser.parse_args(argv)

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    processed_dir = Path(args.processed_dir).resolve() if args.processed_dir else None

    print(f"tree      : {TREE_ROOT}")
    print(f"guard     : {_guard()}")
    print(f"out       : {out}")
    print(f"processed : {processed_dir}")

    selected: list[str]
    if args.surface == "all":
        selected = ["core-dataset", "daily-publish", "golden-master"]
    else:
        selected = [args.surface]

    if "core-dataset" in selected:
        if processed_dir is None:
            raise SystemExit("core-dataset surface 需要 --processed-dir")
        print("[1/4] core-dataset ...")
        surface_core_dataset(out, processed_dir)
    if "daily-publish" in selected:
        print("[2/4] daily-publish ...")
        surface_daily_publish(out)
    if "golden-master" in selected:
        print("[3/4] golden-master ...")
        surface_golden_master(out)
    if "data-pipeline" in selected:
        print("[4/4] data-pipeline ...")
        surface_data_pipeline(
            out,
            run_id=args.run_id,
            fixture=Path(args.fixture).resolve() if args.fixture else None,
        )

    data_pipeline_only = selected == ["data-pipeline"]
    if not data_pipeline_only:
        print("[probe] immutable_json ...")
        immutable_probe(out)
    _manifest(out, selected, omit_provenance=data_pipeline_only)
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
