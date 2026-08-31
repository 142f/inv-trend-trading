from __future__ import annotations

import json
from pathlib import Path

import pytest

from inv_trend.adapters.daily.artifact_publisher import DailyRunArtifactWriter
from inv_trend.adapters.daily.composition import create_daily_run_artifact_writer
from inv_trend.observability.daily import render_complete_analysis


def _snapshot() -> dict[str, object]:
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


def _writer(tmp_path: Path) -> DailyRunArtifactWriter:
    """Use the production composition so legacy HTML behaviour stays covered."""

    return create_daily_run_artifact_writer(
        tmp_path / "artifacts",
        signal_log_root=tmp_path / "signals",
    )


class _RecordingRenderer:
    def __init__(self) -> None:
        self.single_payloads: list[dict[str, object]] = []
        self.aggregate_payloads: list[dict[str, dict[str, object]]] = []

    def render_complete_analysis(self, complete_result) -> str:
        self.single_payloads.append(dict(complete_result))
        return "<html>single canonical report</html>"

    def render_complete_analyses(self, complete_results) -> str:
        self.aggregate_payloads.append(
            {str(symbol): dict(payload) for symbol, payload in complete_results.items()}
        )
        return "<html>aggregate canonical report</html>"


def test_filesystem_publisher_uses_an_injected_renderer_port_only(tmp_path: Path) -> None:
    renderer = _RecordingRenderer()
    writer = DailyRunArtifactWriter(
        tmp_path / "artifacts",
        signal_log_root=tmp_path / "signals",
        renderer=renderer,  # type: ignore[arg-type]
    )

    publication = writer.publish(_snapshot(), render_html=True)

    report = publication.run_directory / "BTC" / "02_report" / "trend_analysis_report.html"
    assert report.read_text(encoding="utf-8") == "<html>single canonical report</html>"
    assert publication.compatibility_html is not None
    assert publication.compatibility_html.read_text(encoding="utf-8") == (
        "<html>aggregate canonical report</html>"
    )
    assert renderer.single_payloads[0]["hashes"]["hash_chain_valid"] is True
    assert renderer.aggregate_payloads[0]["BTC"]["hashes"]["hash_chain_valid"] is True
    assert len(renderer.single_payloads) == 1
    assert len(renderer.aggregate_payloads) == 1

    source = (
        Path(__file__).parents[2]
        / "src"
        / "inv_trend"
        / "adapters"
        / "daily"
        / "artifact_publisher.py"
    ).read_text(encoding="utf-8")
    assert "inv_trend.observability" not in source


def test_legacy_writer_constructor_lazily_composes_the_default_html_renderer(
    tmp_path: Path,
) -> None:
    """Existing direct writer callers keep their default HTML behaviour."""

    writer = DailyRunArtifactWriter(
        tmp_path / "artifacts",
        signal_log_root=tmp_path / "signals",
    )

    publication = writer.publish(_snapshot(), render_html=True)

    assert publication.compatibility_html is not None
    assert publication.compatibility_html.is_file()
    assert (
        publication.run_directory / "BTC" / "02_report" / "trend_analysis_report.html"
    ).is_file()


def test_publication_adds_self_identifying_names_and_navigation_indexes(
    tmp_path: Path,
) -> None:
    writer = _writer(tmp_path)

    publication = writer.publish(_snapshot(), render_html=True)
    run_root = publication.run_directory
    symbol_root = run_root / "BTC"
    batch_index = json.loads((run_root / "批次索引.json").read_text(encoding="utf-8"))
    result_index = json.loads((symbol_root / "结果索引.json").read_text(encoding="utf-8"))
    output_index = json.loads(
        (tmp_path / "artifacts" / "输出索引.json").read_text(encoding="utf-8")
    )

    assert batch_index["layout_version"] == "2"
    assert batch_index["symbols"] == [
        {
            "symbol": "BTC",
            "timeframe": "D1",
            "directory": "BTC",
            "result_index": "BTC/结果索引.json",
        }
    ]
    assert result_index["symbol"] == "BTC"
    assert result_index["timeframe"] == "D1"
    assert result_index["report_date"] == "2026-08-20"
    assert result_index["run_id"] == "artifact-retry-1"
    assert output_index["latest_batch"] == {
        "run_id": "artifact-retry-1",
        "report_date": "2026-08-20",
        "timeframe": "D1",
        "run_directory": "runs/2026-08-20/artifact-retry-1",
        "batch_index": "runs/2026-08-20/artifact-retry-1/批次索引.json",
    }
    assert output_index["latest_symbols"]["BTC"]["result_index"] == (
        "latest/BTC/结果索引.json"
    )

    named_report = symbol_root / result_index["result_types"]["report"]
    named_complete = symbol_root / result_index["result_types"]["complete_analysis"]
    assert named_report.name == (
            "BTC_D1_2026-08-20_artifact-retry-1_趋势分析报告_v5.html"
    )
    assert named_complete.name == (
            "BTC_D1_2026-08-20_artifact-retry-1_完整分析_v5.json"
    )
    assert named_report.read_bytes() == (
        symbol_root / "02_report" / "trend_analysis_report.html"
    ).read_bytes()
    assert named_complete.read_bytes() == (
        symbol_root / "01_canonical" / "complete_analysis_result.json"
    ).read_bytes()
    for key, filename in {
        "indicator_analyses": "指标当前状态_v5.csv",
        "indicator_signal_episodes": "指标信号生命周期_v5.csv",
        "decision_evidence_chain": "最终决策证据链_v5.csv",
    }.items():
        assert (symbol_root / "03_exports" / filename).is_file()
        assert (symbol_root / result_index["result_types"][key]).is_file()

    summary_reports = list(
        (tmp_path / "artifacts" / "汇总结果" / "2026-08-20" / "artifact-retry-1").glob(
            "*_汇总报告_v5.html"
        )
    )
    assert len(summary_reports) == 1
    assert summary_reports[0].read_bytes() == publication.compatibility_html.read_bytes()


def test_filesystem_publisher_retries_an_already_promoted_run(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    snapshot = _snapshot()

    first = writer.publish(snapshot, render_html=False)
    complete_path = first.complete_results["BTC"]
    before = complete_path.read_bytes()
    second = writer.publish(snapshot, render_html=False)

    assert second.run_directory == first.run_directory
    assert second.complete_results == first.complete_results
    assert complete_path.read_bytes() == before
    assert json.loads(complete_path.read_text(encoding="utf-8"))["hashes"]["hash_chain_valid"] is True


def test_recovery_rejects_tampered_canonical_json_before_refreshing_projections(
    tmp_path: Path,
) -> None:
    writer = _writer(tmp_path)
    snapshot = _snapshot()
    first = writer.publish(snapshot, render_html=False)
    root = tmp_path / "artifacts"
    compatibility_json = root / "2026-08-20.json"
    latest_json = root / "latest" / "BTC" / "complete_analysis_result.json"
    compatibility_before = compatibility_json.read_bytes()
    latest_before = latest_json.read_bytes()
    complete_path = first.complete_results["BTC"]
    complete_path.write_text(complete_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="canonical artifact hash mismatch"):
        writer.publish(snapshot, render_html=False)

    assert compatibility_json.read_bytes() == compatibility_before
    assert latest_json.read_bytes() == latest_before


def test_retry_rejects_a_conflicting_immutable_html_mode_without_refreshing_projections(
    tmp_path: Path,
) -> None:
    writer = _writer(tmp_path)
    snapshot = _snapshot()

    first = writer.publish(snapshot, render_html=False)
    root = tmp_path / "artifacts"
    compatibility_json = root / "2026-08-20.json"
    latest_json = root / "latest" / "BTC" / "complete_analysis_result.json"
    manifest = json.loads(
        (
            first.run_directory
            / "BTC"
            / "04_audit"
            / "run_manifest.json"
        ).read_text(encoding="utf-8")
    )
    compatibility_before = compatibility_json.read_bytes()
    latest_before = latest_json.read_bytes()

    assert manifest["render_html"] is False
    assert not compatibility_json.with_suffix(".html").exists()
    assert not (latest_json.parent / "trend_analysis_report.html").exists()
    with pytest.raises(ValueError, match="different render_html mode"):
        writer.publish(snapshot, render_html=True)

    assert compatibility_json.read_bytes() == compatibility_before
    assert latest_json.read_bytes() == latest_before
    assert not compatibility_json.with_suffix(".html").exists()
    assert not (latest_json.parent / "trend_analysis_report.html").exists()


def test_no_html_publication_removes_stale_compatibility_and_latest_reports(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    first_snapshot = _snapshot()
    writer.publish(first_snapshot, render_html=True)

    second_snapshot = json.loads(json.dumps(first_snapshot))
    second_snapshot["run_id"] = "artifact-retry-2"
    row = second_snapshot["symbols"][0]
    row["data_update_result"]["result_hash"] = "data-hash-v2"
    row["data_update_result"]["dataset_version"] = "dataset-v2"
    row["strategy_screening_result"]["result_hash"] = "screening-hash-v2"
    row["strategy_screening_result"]["input_data_hash"] = "data-hash-v2"
    row["trend_decision_result"]["result_hash"] = "decision-hash-v2"
    row["trend_decision_result"]["input_data_hash"] = "data-hash-v2"
    row["trend_decision_result"]["input_screening_hash"] = "screening-hash-v2"

    publication = writer.publish(second_snapshot, render_html=False)
    root = tmp_path / "artifacts"
    compatibility_json = root / "2026-08-20.json"
    latest = root / "latest" / "BTC"
    manifest = json.loads(
        (
            publication.run_directory
            / "BTC"
            / "04_audit"
            / "run_manifest.json"
        ).read_text(encoding="utf-8")
    )

    assert manifest["render_html"] is False
    assert not compatibility_json.with_suffix(".html").exists()
    assert not (latest / "trend_analysis_report.html").exists()
    latest_complete = json.loads((latest / "complete_analysis_result.json").read_text(encoding="utf-8"))
    assert latest_complete["metadata"]["dataset_version"] == "dataset-v2"


def test_html_mode_recovery_rejects_missing_archived_report_before_refreshing_latest(
    tmp_path: Path,
) -> None:
    writer = _writer(tmp_path)
    snapshot = _snapshot()
    first = writer.publish(snapshot, render_html=True)
    root = tmp_path / "artifacts"
    compatibility_json = root / "2026-08-20.json"
    latest_json = root / "latest" / "BTC" / "complete_analysis_result.json"
    compatibility_before = compatibility_json.read_bytes()
    latest_before = latest_json.read_bytes()
    archived_html = first.run_directory / "BTC" / "02_report" / "trend_analysis_report.html"
    archived_html.unlink()

    with pytest.raises(FileExistsError, match="missing its required HTML report"):
        writer.publish(snapshot, render_html=True)

    assert compatibility_json.read_bytes() == compatibility_before
    assert latest_json.read_bytes() == latest_before


def test_complete_renderer_consumes_canonical_complete_json_without_recalculation() -> None:
    complete = {
        "metadata": {
            "symbol": "BTC",
            "instrument_id": "BTC.TEST.SPOT",
            "timeframe": "D1",
            "as_of": "2026-08-20T00:00:00+00:00",
            "run_status": "updated",
        },
        "data_update": {"result_hash": "data-hash"},
        "strategy_screening": {"result_hash": "screening-hash"},
        "trend_decision": {
            "result_hash": "decision-hash",
            "decision": "WAIT",
            "execution_state": "WAIT",
        },
        "report_bundle": {"symbol": "BTC", "summary": {}},
    }

    html = render_complete_analysis(complete)

    assert "BTC" in html
    assert '"result_hash": "decision-hash"' in html
