from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "公开数据" / "BTCUSDT_D1_2023_2024.csv"


def test_public_btc_fixture_end_to_end(tmp_path: Path) -> None:
    if not FIXTURE.exists():
        pytest.skip("公开 Binance fixture 未下载；先运行 scripts/下载公开BTC测试数据_v1.py")
    frame = pd.read_csv(FIXTURE)
    assert len(frame) == 731
    spec = importlib.util.spec_from_file_location(
        "public_btc_e2e", ROOT / "scripts" / "运行公开BTC端到端_v1.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    json_path, html_path = module.run(FIXTURE, tmp_path)
    assert json_path.exists() and html_path.exists()
    assert "本次修改内容、理由与效果" in html_path.read_text(encoding="utf-8")
