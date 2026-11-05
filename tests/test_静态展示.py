from __future__ import annotations

from http.client import HTTPConnection
from importlib import resources
from pathlib import Path
from threading import Thread
from urllib.parse import quote

import pytest

from inv_trend.observability.静态展示 import (
    PAGES,
    create_server,
    export_all,
    export_page,
    render_decision_desk,
    render_direction_overview,
    render_market_thermometer,
    render_snapshot_page,
)

PACKAGE = resources.files("inv_trend.observability.静态展示")
PAGES_ROOT = PACKAGE.joinpath("pages")


def test_catalog_has_exactly_the_seven_showcase_pages():
    assert [page.id for page in PAGES] == [
        "pipeline-board",
        "audit-report",
        "refactor-acceptance-s1-s5",
        "us-top100",
        "us-direction-map",
        "us-decision-desk",
        "us-market-thermometer",
    ]
    assert [page.kind for page in PAGES] == [
        "asset", "asset", "asset", "generated", "generated", "generated", "generated",
    ]


def test_all_fixed_page_resources_are_packaged():
    for path in (
        "数据管理流水线任务看板_v1.html",
        "美股前100指标_审计验收汇报.html",
        "refactor-acceptance-s1-s5/refactor-acceptance-s1-s5.html",
        "refactor-acceptance-s1-s5/assets/charts.js",
        "refactor-acceptance-s1-s5/_shared/js/echarts.min.js",
        "refactor-acceptance-s1-s5/_shared/fonts/WorkSans-Regular.ttf",
    ):
        assert PAGES_ROOT.joinpath(*Path(path).parts).is_file(), path


def test_snapshot_renderer_keeps_title_and_escapes_closing_script_tag():
    payload = {
        "partial": False,
        "rows": [{"symbol": "</script>", "bias": "多"}],
        "failed": [],
        "incomplete": [],
        "universe": "TEST",
        "price_basis": "provider_adjusted_close",
        "data_cutoff": "2026-10-26T20:00:00Z",
    }
    html = render_snapshot_page(payload)
    assert "<title>美股前100指标看板</title>" in html
    assert "<\\/script>" in html
    assert "</script>" not in html.split("<script>", 1)[0]


def test_direction_overview_renderer_keeps_title_escapes_and_lists_every_system():
    payload = {
        "partial": False,
        "rows": [
            {
                "symbol": "</script>",
                "funds": "QQQ",
                "close": 100.0,
                "adjusted_close": 100.0,
                "adjustment_factor": 1.0,
                "bias": "多",
                "pairs": {"10_20": "多", "10_55": "多", "20_55": "多"},
                "sma_bias": "多",
                "macd": "多",
                "adx": 30.0,
                "plus_di": 28.0,
                "minus_di": 12.0,
                "relative_volume": 1.8,
                "warmup_ok": True,
                "bar_count": 957,
                "channel": {"high_20": 90.0, "low_20": 80.0, "high_55": 95.0, "low_55": 70.0},
                "spark": {"close": [1.0], "ema10": [1.0], "ema20": [1.0], "ema55": [1.0]},
            },
        ],
        "failed": [],
        "incomplete": [],
        "universe": "TEST",
        "price_basis": "provider_adjusted_close",
        "data_cutoff": "2026-10-26T20:00:00Z",
    }
    html = render_direction_overview(payload, {"provider": "yahoo_chart"})
    assert "<title>美股前100方向偏度总览</title>" in html
    assert "<\\/script>" in html
    assert "</script>" not in html.split("<script>", 1)[0]
    for system in ("EMA 10/20", "EMA 10/55", "EMA 20/55", "SMA 排列", "MACD", "DMI ±DI", "唐奇安 20 位置", "唐奇安 55 位置"):
        assert system in html
    assert "yahoo_chart" in html


def test_export_all_writes_fixed_assets_and_generated_outputs(tmp_path):
    root = tmp_path / "repo"
    deliverables = root / "deliverables"
    deliverables.mkdir(parents=True)
    for name in (
        "美股前100指标看板.html",
        "美股前100方向偏度总览.html",
        "美股前100投资决策面板.html",
        "美股前100市场温度面板.html",
        "市场温度历史.json",
        "美股前100指标数据.json",
        "美股前100指标_回执.json",
    ):
        (deliverables / name).write_text("generated", encoding="utf-8")

    output = tmp_path / "out"
    result = export_all(output, root=root)

    assert result.missing == ()
    assert (output / "数据管理流水线任务看板_v1.html").is_file()
    assert (output / "美股前100指标_审计验收汇报.html").is_file()
    assert (output / "refactor-acceptance-s1-s5" / "refactor-acceptance-s1-s5.html").is_file()
    assert (output / "refactor-acceptance-s1-s5" / "assets" / "charts.js").is_file()
    assert (output / "美股前100指标看板.html").is_file()
    assert (output / "美股前100方向偏度总览.html").is_file()
    assert (output / "美股前100投资决策面板.html").is_file()
    assert (output / "美股前100市场温度面板.html").is_file()


def test_export_single_generated_page_requires_source_file(tmp_path):
    with pytest.raises(FileNotFoundError, match="generated page is missing"):
        export_page("us-top100", tmp_path / "out", root=tmp_path)


def test_preview_server_serves_index_pages_and_rejects_traversal(tmp_path):
    deliverables = tmp_path / "deliverables"
    deliverables.mkdir()
    (deliverables / "美股前100指标看板.html").write_text(
        "<title>generated snapshot</title>", encoding="utf-8",
    )

    server = create_server(tmp_path, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        for path, expected in (
            ("/", "固定展示页面"),
            ("/pages/数据管理流水线任务看板_v1.html", "数据管理流水线"),
            ("/pages/refactor-acceptance-s1-s5/refactor-acceptance-s1-s5.html", "S1–S5"),
            ("/pages/美股前100指标看板.html", "generated snapshot"),
        ):
            connection = HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", quote(path, safe="/"))
            response = connection.getresponse()
            body = response.read().decode("utf-8")
            assert response.status == 200, path
            assert expected in body, path
            connection.close()

        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", "/pages/%2e%2e/pyproject.toml")
        response = connection.getresponse()
        assert response.status == 404
        response.read()
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _decision_payload() -> dict:
    return {
        "partial": False,
        "rows": [
            {
                "symbol": "</script>",
                "funds": "QQQ",
                "close": 100.0,
                "adjusted_close": 100.0,
                "as_of": "2026-10-26",
                "adx": 30.0,
                "plus_di": 28.0,
                "minus_di": 12.0,
                "atr": 2.0,
                "dif": 1.0,
                "dea": 0.5,
                "histogram": 0.5,
                "relative_volume": 1.8,
                "warmup_ok": True,
                "channel": {"high_10": 99.0, "low_10": 90.0, "high_20": 98.0,
                            "low_20": 88.0, "high_55": 105.0, "low_55": 80.0},
                "timeline10": [{"date": "2026-10-26", "close": 100.0, "histogram": 0.5, "adx": 30.0}],
                "decision": {
                    "family_votes": {"turtle": "long", "sma": "long", "ema": "long", "macd": "long"},
                    "rating": {"grade": "A", "direction": "long", "score": 9.0,
                               "aligned_families": ["turtle", "sma", "ema", "macd"],
                               "quality_adjustments": ["adx_dmi_confirmed:+1"]},
                    "volatility": {"state": "normal", "percentile": 0.5},
                    "trend_quality": {"adx": 30.0, "confirmed": True},
                    "volume": {"relative_volume": 1.8, "confirmed": True},
                    "breakouts": [
                        {"period": 20, "direction": "long", "level": 98.0, "distance_pct": -2.0, "triggered": True},
                        {"period": 20, "direction": "short", "level": 88.0, "distance_pct": 12.0, "triggered": False},
                        {"period": 55, "direction": "long", "level": 105.0, "distance_pct": 5.0, "triggered": False},
                        {"period": 55, "direction": "short", "level": 80.0, "distance_pct": 20.0, "triggered": False},
                    ],
                    "stops": {"long": 96.0, "short": 104.0},
                    "exits": {"long_fast": 90.0, "long_slow": 88.0, "short_fast": 99.0, "short_slow": 98.0},
                    "suggested_qty": {"long": 125, "short": 125},
                },
            },
        ],
        "failed": [{"symbol": "BAD", "funds": "SPY", "error": "boom"}],
        "incomplete": [{"symbol": "HONA", "bar_count": 93, "reason": "warmup<169"}],
        "universe": "TEST",
        "price_basis": "provider_adjusted_close",
        "data_cutoff": "2026-10-26T20:00:00Z",
        "market": {
            "as_of": "2026-10-26", "generated_at": "2026-10-26T21:00:00Z",
            "rating_dist": {"A_long": 1},
            "breakout_counts": {"long_20": 1, "long_55": 0, "short_20": 0, "short_55": 0},
            "volatility_counts": {"low": 0, "normal": 1, "high": 0, "unavailable": 0},
            "breadth": {"above_sma20_pct": 100.0, "above_ema55_pct": 100.0,
                        "macd_long_pct": 100.0, "adx_strong_pct": 100.0},
            "median_adx": 30.0, "median_relative_volume": 1.8,
            "temperature_score": 88.0, "triggered_count": 1, "near_count": 0,
            "counted_rows": 1,
            "data_health": {"rows": 1, "failed": 1, "failed_symbols": ["BAD"],
                            "incomplete": 1, "partial": 0, "sources": ["yahoo_cache"]},
        },
    }


def test_decision_desk_renderer_zones_disclaimer_and_escape():
    html = render_decision_desk(_decision_payload(), {"provider": "yahoo_chart", "equity": 100000.0})
    assert "<title>美股前100投资决策面板</title>" in html
    assert "<\\/script>" in html
    assert "</script>" not in html.split("<script>", 1)[0]
    for text in ("已触发", "临近触发", "观察仓", "全部", "下单由用户手动执行", "止损价", "建议股数", "退出通道", "yahoo_chart", "100000.0"):
        assert text in html, text


def test_market_thermometer_renderer_health_history_and_escape():
    history = [
        {"as_of": "2026-10-23", "temperature_score": 61.0,
         "rating_dist": {"A_long": 5, "A_short": 2},
         "breakout_counts": {"long_20": 3, "long_55": 1, "short_20": 1, "short_55": 0}},
        {"as_of": "2026-10-26", "temperature_score": 88.0,
         "rating_dist": {"A_long": 1, "A_short": 0},
         "breakout_counts": {"long_20": 1, "long_55": 0, "short_20": 0, "short_55": 0}},
    ]
    html = render_market_thermometer(_decision_payload(), {"provider": "yahoo_chart"}, history)
    assert "<title>美股前100市场温度面板</title>" in html
    assert "<\\/script>" in html
    assert "</script>" not in html.split("<script>", 1)[0]
    for text in ("市场温度", "评级分布", "波动状态", "数据新鲜度", "温度历史", "非投资建议", "88.0"):
        assert text in html, text
