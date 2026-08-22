from __future__ import annotations

from pathlib import Path

from inv_trend.application.daily_models import InstrumentReportBundle
from inv_trend.observability.report_renderer import write_instrument_report


def test_renderer_explains_changes_and_does_not_load_external_runtime(tmp_path: Path) -> None:
    bundle = InstrumentReportBundle(
        symbol="BTC",
        instrument_id="BTCUSDT.BINANCE.SPOT",
        timeframe="D1",
        dataset_version="renderer-test-v2",
        generated_at="2026-08-21T19:00:00+08:00",
        latest_bar={"timestamp": "2026-08-20T00:00:00+00:00", "close": 100.0},
        series=({"timestamp": "2026-08-20T00:00:00+00:00", "close": 100.0},),
        signals=(),
        rule_evaluations=(),
        summary={"signals": 0, "rules": 0, "anomalies": 0},
        change_log=(
            {
                "module": "core/features.py",
                "change": "单次特征准备",
                "reason": "避免重复计算",
                "effect": "复用同一结构化结果",
            },
        ),
    )
    payload = bundle.to_dict()
    payload["data_notice"] = "离线 smoke fixture，不冒充公开市场数据。"
    path = write_instrument_report(payload, tmp_path / "报告.html")
    html = path.read_text(encoding="utf-8")
    assert "本次修改内容、理由与效果" in html
    assert "数据与验证范围" in html
    assert "策略条件明细" in html
    assert "ReportBundle" in html
    assert "单次特征准备" in html
    assert bundle.result_hash in html
    # Runtime is fully self-contained; strategy data is embedded as JSON.
    assert '<script src="http' not in html
    assert "HTML 不重新计算策略逻辑" in html
