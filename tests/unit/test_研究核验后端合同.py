from __future__ import annotations

import pandas as pd
import pytest

from inv_trend.storage.研究结果_v3 import ResultRepository
from inv_trend.storage.结构化存储_v3 import StoreError, UnifiedStore
from inv_trend.storage.滚动核验 import verify_research


def prepare(store, *, summary_only=False):
    prefix = "backtests/核验研究"
    store.put_document(
        prefix + "/实验协议冻结.json",
        {
            "source_sha256": "source-v1",
            "protocol": {"dataset_sha256": "data-v1"},
        },
    )
    lock = {
        "window_id": "window-1",
        "parameters": {"period": 20},
        "candidate_id": "candidate-1",
        "train_start": "2023-01-01T00:00:00Z",
        "train_end": "2023-06-01T00:00:00Z",
        "test_start": "2023-07-01T00:00:00Z",
        "test_end_exclusive": "2024-01-01T00:00:00Z",
    }
    store.put_document(prefix + "/完整迭代摘要.json", [{"iteration": "1", "locks": [lock]}])
    store.put_document(prefix + "/参数锁定/1/window-1.json", lock)
    ResultRepository(store).publish(
        prefix + "/1/window-1",
        {"trades": pd.DataFrame({"pnl": [1.0, -2.0]})},
        {"data_version": "data-v1", "parameters": {"period": 20}},
        summary_only=summary_only,
    )
    return store.root / prefix


def test_database_research_verifier_uses_authoritative_tables_and_detects_tampering(tmp_path):
    store = UnifiedStore(tmp_path / "data", create=True)
    directory = prepare(store)
    result = verify_research(directory)
    assert result["verified"] is True
    assert result["runs"] == result["files"] == 1
    assert result["data_sha256"] == "data-v1"
    with store.connect() as db:
        db.execute("UPDATE result_rows SET payload=? WHERE ordinal=0", (b"[0,[999.0]]",))
    with pytest.raises(StoreError, match="哈希不一致"):
        verify_research(directory)


def test_historical_summary_is_not_accepted_as_full_replay(tmp_path):
    store = UnifiedStore(tmp_path / "data", create=True)
    directory = prepare(store, summary_only=True)
    with pytest.raises(StoreError, match="历史摘要"):
        verify_research(directory)
