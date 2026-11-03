"""数据管理流水线 P0 契约：K 线链路的 pinned 基准必须可被当前实现复现。

本文件在流水线子包引入之前即生效：它把「改动前的行为」变成可执行断言，
之后 P2 的逐行搬移只要造成任何语义漂移，这里就会立刻失败。

基准来源：``tests/fixtures/数据流水线基准_v1.json``，由改前实现（commit 见其中
``captured_at_commit``）在**两套独立 lake** 上双跑确认确定性后固化。

``provider_metadata.location`` 会进入 ``dataset_version``。真实 CSV provider 记录的是
调用方传入的绝对路径，若直接固化会让基准依赖克隆位置。本测试把 location 固定为仓库内
相对标识后再比对；产品 provider 行为不变。

等价口径（与基准采集脚本完全一致）：
- 固化内容寻址身份（``dataset_version`` / ``raw_snapshot_id``）；
- 固化 curated 内容的**语义** ``frame_hash``，不固化 Parquet 文件字节 sha256
  （写入元数据不稳定，不代表内容差异）；
- 固化 ``quality_reports/{dataset_version}.json`` 的**解析后内容**，不固化其文件字节 sha256；
- 剔除墙钟与随机身份字段（见 ``VOLATILE``）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from inv_trend.data.api import HistoricalDataService
from inv_trend.infra.integrity import strategy_frame_hash
from inv_trend.data.providers import CsvBarsProvider

REPO = Path(__file__).resolve().parents[1]
BASELINE = REPO / "tests" / "fixtures" / "数据流水线基准_v1.json"
FIXTURE = REPO / "tests" / "fixtures" / "pipeline_bars_d1.csv"

# 与采集脚本保持逐项一致：墙钟、随机身份、文件字节哈希都不参与等价判定。
VOLATILE = frozenset({
    "downloaded_at", "ingested_at", "generated_at", "evaluated_at", "created_at",
    "updated_at", "recorded_at", "published_at",
    "run_id", "publication_run_id", "request_id",
    "processing_run_id", "source_run_id", "file_paths", "file_hashes",
    "dataset_manifest_path", "provider_metadata",
    "curated_sha256", "quality_report_sha256",
})


_STABLE_PROVIDER_LOCATION = "tests/fixtures/pipeline_bars_d1.csv"


class _PinnedLocationCsvBarsProvider(CsvBarsProvider):
    """把 ``provider_metadata.location`` 固定为仓库内相对标识。

    只用于测试：让 pinned 基准不随克隆路径变化；产品 provider 行为不变。
    """

    def fetch(self, request):
        result = super().fetch(request)
        result.metadata = {**result.metadata, "location": _STABLE_PROVIDER_LOCATION}
        return result


def _prune(value):
    if isinstance(value, dict):
        return {k: _prune(v) for k, v in value.items() if k not in VOLATILE}
    if isinstance(value, list):
        return [_prune(v) for v in value]
    return value


def _observe(lake: Path) -> dict:
    """用当前实现重放同一条链路，返回与基准同形的观测值。"""

    service = HistoricalDataService(
        lake, providers={"licensed_csv": _PinnedLocationCsvBarsProvider(FIXTURE)}
    )
    base = service.instruments["BTC"]
    service.instruments["BTC"] = base.__class__(
        **{**base.__dict__, "primary_source": "licensed_csv"}
    )

    frame = pd.read_csv(FIXTURE)
    start = pd.Timestamp(frame["timestamp"].iloc[0]).to_pydatetime()
    end = pd.Timestamp(frame["timestamp"].iloc[-1]).to_pydatetime()

    manifest = service.ingest("BTC", "D1", start, end)
    # 显式传 start/end：load_bars 默认把终点取为 now()，会让质量报告的
    # requested_end / expected_rows 随墙钟变化。
    bars = service.load_bars("BTC", "D1", start, end)
    versioned = service.load_bars_version("BTC", "D1", manifest.dataset_version, start, end)
    reprocessed = service.reprocess_raw("BTC", "D1", manifest.request_id)

    curated = sorted(lake.rglob("bars.parquet"))
    assert len(curated) == 1, f"expected exactly one curated parquet, got {len(curated)}"
    curated_frame = pd.read_parquet(curated[0])

    quality = json.loads(
        (lake / "quality_reports" / f"{manifest.dataset_version}.json").read_text(encoding="utf-8")
    )
    dataset_manifest = json.loads(
        (lake / "dataset_manifests" / f"{manifest.dataset_version}.json").read_text(encoding="utf-8")
    )

    return {
        "dataset_version": manifest.dataset_version,
        "raw_snapshot_id": manifest.raw_snapshot_id,
        "curated_version": manifest.curated_version,
        "row_count": int(manifest.row_count),
        "quality_passed": bool(manifest.quality_passed),
        "quality_score": float(manifest.quality_score),
        "quality_status": str(manifest.quality_status),
        "actual_start": str(manifest.actual_start),
        "actual_end": str(manifest.actual_end),
        "timeframe": manifest.timeframe,
        "instrument_id": manifest.instrument_id,
        "cleaning_rule_version": manifest.cleaning_rule_version,
        "manifest_semantic": _prune(manifest.__dict__),
        "quality_report": _prune(quality),
        "dataset_manifest": _prune(dataset_manifest),
        "curated_frame_hash": strategy_frame_hash(curated_frame),
        "curated_rows": int(len(curated_frame)),
        "read_rows": int(len(bars)),
        "read_columns": [str(c) for c in bars.columns],
        "read_dtypes": {str(c): str(t) for c, t in bars.dtypes.items()},
        "read_frame_hash": strategy_frame_hash(bars),
        "versioned_rows": int(len(versioned)),
        "versioned_frame_hash": strategy_frame_hash(versioned),
        "reprocessed_dataset_version": reprocessed.dataset_version,
        "reprocessed_frame_hash": strategy_frame_hash(
            service.load_bars_version("BTC", "D1", reprocessed.dataset_version, start, end)
        ),
    }


@pytest.fixture(scope="module")
def baseline() -> dict:
    return json.loads(BASELINE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def observed(tmp_path_factory) -> dict:
    return _observe(tmp_path_factory.mktemp("pipeline_baseline"))


def test_baseline_fixture_is_present_and_pinned(baseline: dict) -> None:
    assert FIXTURE.is_file(), f"missing pinned fixture: {FIXTURE}"
    assert baseline["fixture"]["path"] == "tests/fixtures/pipeline_bars_d1.csv"
    assert baseline["fixture"]["provider_location"] == _STABLE_PROVIDER_LOCATION
    assert baseline["fixture"]["rows"] == 200
    assert baseline["dataset_version"]
    assert len(baseline["dataset_version"]) == 24


def test_chain_identity_reproduces(baseline: dict, observed: dict) -> None:
    """内容寻址身份必须逐项一致：这是 dataset_version 不可漂移的第一道门。"""

    for key in (
        "dataset_version",
        "raw_snapshot_id",
        "curated_version",
        "row_count",
        "quality_passed",
        "quality_score",
        "quality_status",
        "actual_start",
        "actual_end",
        "timeframe",
        "instrument_id",
        "cleaning_rule_version",
    ):
        assert observed[key] == baseline[key], (
            f"{key} drifted: baseline={baseline[key]!r} observed={observed[key]!r}"
        )


def test_manifest_and_quality_payloads_reproduce(baseline: dict, observed: dict) -> None:
    """Manifest / 质量报告 / DatasetManifest 的语义字段逐项一致。"""

    assert observed["manifest_semantic"] == baseline["manifest_semantic"]
    assert observed["quality_report"] == baseline["quality_report"]
    assert observed["dataset_manifest"] == baseline["dataset_manifest"]


def test_curated_and_read_frames_reproduce(baseline: dict, observed: dict) -> None:
    """curated 内容与读取结果的列序 / dtype / 行数 / 语义哈希一致。"""

    assert observed["curated_rows"] == baseline["curated_rows"]
    assert observed["curated_frame_hash"] == baseline["curated_frame_hash"]
    assert observed["read_rows"] == baseline["read_rows"]
    assert observed["read_columns"] == baseline["read_columns"]
    assert observed["read_dtypes"] == baseline["read_dtypes"]
    assert observed["read_frame_hash"] == baseline["read_frame_hash"]
    # curated 与可见读取必须同源同值
    assert observed["curated_frame_hash"] == observed["read_frame_hash"]


def test_versioned_read_matches_current(baseline: dict, observed: dict) -> None:
    assert observed["versioned_rows"] == baseline["versioned_rows"]
    assert observed["versioned_frame_hash"] == baseline["versioned_frame_hash"]
    assert observed["versioned_frame_hash"] == observed["read_frame_hash"]


def test_reprocess_raw_reproduces_the_same_dataset(baseline: dict, observed: dict) -> None:
    """reprocess_raw 不得产生新的 dataset_version 或不同的内容。"""

    assert observed["reprocessed_dataset_version"] == baseline["dataset_version"]
    assert observed["reprocessed_frame_hash"] == baseline["read_frame_hash"]


# ---------------------------------------------------------------------------
# P1：四阶段契约 + 注册表 + 转发 shim 的行为等价
# ---------------------------------------------------------------------------


def test_pipeline_stages_are_the_standard_four() -> None:
    from inv_trend.data import PIPELINE_STAGES

    assert PIPELINE_STAGES == ("acquire", "transform", "persist", "read")


def test_registry_resolves_bars_with_all_four_stages(tmp_path: Path) -> None:
    from inv_trend.data import MarketDataPipeline, get_pipeline
    from inv_trend.data.pipeline.契约_v1 import (
        DataAcquirePort,
        DataPersistPort,
        DataReadPort,
        DataTransformPort,
    )

    service = _service(tmp_path / "lake")
    builder = get_pipeline("bars")
    assert callable(builder)

    pipeline = MarketDataPipeline.for_kind(
        "bars",
        instruments=service.instruments,
        providers=service.providers,
        lake=service.lake,
        backend=service,
    )
    spec = pipeline.spec
    assert spec.kind == "bars"
    assert isinstance(spec.acquire, DataAcquirePort)
    assert isinstance(spec.transform, DataTransformPort)
    assert isinstance(spec.persist, DataPersistPort)
    assert isinstance(spec.read, DataReadPort)


def test_registry_rejects_unknown_and_duplicate_kind() -> None:
    from inv_trend.data import PipelineRegistry

    local = PipelineRegistry()

    def builder(**_kwargs):  # pragma: no cover - 只验证注册语义
        return None

    local.register("demo", builder)
    assert local.kinds() == ("demo",)
    assert local.resolve("demo") is builder
    with pytest.raises(KeyError):
        local.resolve("missing")
    with pytest.raises(ValueError):
        local.register("demo", builder)
    with pytest.raises(ValueError):
        local.register("", builder)


def _service(lake: Path):
    from inv_trend.data.api import HistoricalDataService

    service = HistoricalDataService(
        lake, providers={"licensed_csv": _PinnedLocationCsvBarsProvider(FIXTURE)}
    )
    base = service.instruments["BTC"]
    service.instruments["BTC"] = base.__class__(
        **{**base.__dict__, "primary_source": "licensed_csv"}
    )
    return service


def _bars_pipeline(service):
    from inv_trend.data import MarketDataPipeline

    return MarketDataPipeline.for_kind(
        "bars",
        instruments=service.instruments,
        providers=service.providers,
        lake=service.lake,
        backend=service,
    )


def test_pipeline_ingest_matches_legacy_service(tmp_path: Path) -> None:
    """P1 转发 shim：pipeline.ingest 与 service.ingest 必须产出同一身份与内容。"""

    from inv_trend.data import DatasetKey
    from inv_trend.infra.integrity import strategy_frame_hash

    frame = pd.read_csv(FIXTURE)
    start = pd.Timestamp(frame["timestamp"].iloc[0]).to_pydatetime()
    end = pd.Timestamp(frame["timestamp"].iloc[-1]).to_pydatetime()

    legacy = _service(tmp_path / "legacy").ingest("BTC", "D1", start, end)

    service = _service(tmp_path / "pipeline")
    pipeline = _bars_pipeline(service)
    via_pipeline = pipeline.ingest(DatasetKey("BTC", "D1"), start, end)

    assert via_pipeline.dataset_version == legacy.dataset_version
    assert via_pipeline.raw_snapshot_id == legacy.raw_snapshot_id
    assert via_pipeline.quality_status == legacy.quality_status
    assert via_pipeline.quality_score == legacy.quality_score
    assert via_pipeline.row_count == legacy.row_count

    bars = pipeline.read(DatasetKey("BTC", "D1"), start=start, end=end)
    assert strategy_frame_hash(bars) == strategy_frame_hash(
        service.load_bars("BTC", "D1", start, end)
    )


def test_transform_does_not_write_normalized_or_curated(tmp_path: Path) -> None:
    """阶段边界：transform 只计算。normalized / curated 只能在 persist 之后出现。"""

    from inv_trend.data import DatasetKey

    frame = pd.read_csv(FIXTURE)
    start = pd.Timestamp(frame["timestamp"].iloc[0]).to_pydatetime()
    end = pd.Timestamp(frame["timestamp"].iloc[-1]).to_pydatetime()
    service = _service(tmp_path / "lake")
    pipeline = _bars_pipeline(service)
    context = pipeline._context(DatasetKey("BTC", "D1"), start, end)
    acquired = pipeline.spec.acquire.acquire(context)
    lake = service.lake.root

    def parquet_under(*names: str) -> list[Path]:
        found: list[Path] = []
        for name in names:
            root = lake / name
            if root.exists():
                found.extend(path for path in root.rglob("*.parquet") if path.is_file())
        return found

    assert parquet_under("raw"), "acquire 应写下 raw"
    assert parquet_under("normalized", "curated") == []

    transformed = pipeline.spec.transform.transform(acquired, context)
    assert parquet_under("normalized", "curated") == []
    assert transformed.dataset_version

    published = pipeline.spec.persist.persist(transformed, context)
    assert published.manifest is not None
    assert parquet_under("normalized")
    assert parquet_under("curated")


def test_pipeline_read_forwards_version_and_filters(tmp_path: Path) -> None:
    from inv_trend.data import DatasetKey

    frame = pd.read_csv(FIXTURE)
    start = pd.Timestamp(frame["timestamp"].iloc[0]).to_pydatetime()
    end = pd.Timestamp(frame["timestamp"].iloc[-1]).to_pydatetime()

    service = _service(tmp_path / "lake")
    pipeline = _bars_pipeline(service)
    manifest = pipeline.ingest(DatasetKey("BTC", "D1"), start, end)

    pinned = pipeline.read(
        DatasetKey("BTC", "D1"), version=manifest.dataset_version, start=start, end=end
    )
    assert len(pinned) == manifest.row_count
    assert list(pinned.columns) == list(
        service.load_bars_version("BTC", "D1", manifest.dataset_version, start, end).columns
    )
