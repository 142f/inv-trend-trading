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
    render_snapshot_page,
)

PACKAGE = resources.files("inv_trend.observability.静态展示")
PAGES_ROOT = PACKAGE.joinpath("pages")


def test_catalog_has_exactly_the_four_showcase_pages():
    assert [page.id for page in PAGES] == [
        "pipeline-board",
        "audit-report",
        "refactor-acceptance-s1-s5",
        "us-top100",
    ]
    assert [page.kind for page in PAGES] == ["asset", "asset", "asset", "generated"]


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


def test_export_all_writes_fixed_assets_and_generated_outputs(tmp_path):
    root = tmp_path / "repo"
    deliverables = root / "deliverables"
    deliverables.mkdir(parents=True)
    for name in ("美股前100指标看板.html", "美股前100指标数据.json", "美股前100指标_回执.json"):
        (deliverables / name).write_text("generated", encoding="utf-8")

    output = tmp_path / "out"
    result = export_all(output, root=root)

    assert result.missing == ()
    assert (output / "数据管理流水线任务看板_v1.html").is_file()
    assert (output / "美股前100指标_审计验收汇报.html").is_file()
    assert (output / "refactor-acceptance-s1-s5" / "refactor-acceptance-s1-s5.html").is_file()
    assert (output / "refactor-acceptance-s1-s5" / "assets" / "charts.js").is_file()
    assert (output / "美股前100指标看板.html").is_file()


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
