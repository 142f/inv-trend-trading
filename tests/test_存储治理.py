import json
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest

from inv_trend.storage import Storage, StorageIntegrityError
from inv_trend.storage.基础 import digest, encoded, lease_active, redact
from inv_trend.storage.仓库 import parquet_bytes


def test_cross_run_dedup_and_latest_recovery(tmp_path):
    store = Storage(tmp_path)
    payload = {"输入/结果.json": encoded({"value": 1})}
    store.publish("测试", "a", "2026-09-07", payload)
    store.publish("测试", "b", "2026-09-07", payload)
    assert len(store.rows("SELECT * FROM blobs")) == 1
    assert len(store.rows("SELECT * FROM artifact_refs")) == 2
    store.recover()
    latest = json.loads((tmp_path / "outputs/测试/latest/latest.json").read_text(encoding="utf-8"))
    assert latest["run_id"] == "b"
    assert set(latest) == {"run_id", "committed_at", "manifest_hash"}
    assert store.verify()["ok"]


@pytest.mark.parametrize("phase", ["STAGING", "FILES_PUBLISHED", "DB_COMMITTED", "RUN_SUCCEEDED", "LATEST_UPDATED"])
def test_crash_each_boundary_is_idempotent(tmp_path, phase):
    store = Storage(tmp_path)
    def fail(value):
        if value == phase:
            raise RuntimeError("crash")
    with pytest.raises(RuntimeError, match="crash"):
        store.publish("测试", "r", "2026-09-07", {"结果.json": b"{}"}, fault=fail)
    store.publish("测试", "r", "2026-09-07", {"结果.json": b"{}"})
    assert store.rows("SELECT phase FROM runs")[0]["phase"] == "LATEST_UPDATED"
    assert len(store.rows("SELECT * FROM artifacts")) == 1
    assert store.verify()["ok"]


def test_corruption_blocks_success_and_gc(tmp_path):
    store = Storage(tmp_path)
    paths = store.publish("测试", "r", "2026-09-07", {"结果.json": b"{}"})
    paths["结果.json"].write_bytes(b"bad")
    assert store.recover()[0]["status"] == "FAILED"
    assert not store.verify()["ok"]
    with pytest.raises(StorageIntegrityError):
        store.gc(apply=True)


def test_identity_conflict(tmp_path):
    store = Storage(tmp_path)
    store.publish("测试", "r", "2026-09-07", {"结果.json": b"{}"})
    with pytest.raises(StorageIntegrityError, match="identity"):
        store.publish("测试", "r", "2026-09-07", {"结果.json": b'{"a":1}'})


def test_concurrent_publication(tmp_path):
    store = Storage(tmp_path)
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda i: store.publish("测试", str(i), "2026-09-07", {"结果.json": b"{}"}), range(6)))
    assert len(store.rows("SELECT * FROM blobs")) == 1
    assert len(store.rows("SELECT * FROM runs WHERE status='SUCCEEDED'")) == 6
    assert store.verify()["ok"]


def test_metric_dimensions(tmp_path):
    store = Storage(tmp_path)
    metrics = [dict(sample_role=role, scenario={"cost": cost}, metric_name="return", value=cost)
               for role in ("VALIDATION", "UNSEEN_TEST") for cost in (1, 2)]
    store.publish("测试", "r", "2026-09-07", {"结果.json": b"{}"}, facts={"experiments": [{"id": "e", "metrics": metrics}]})
    assert len(store.rows("SELECT * FROM metrics")) == 4


def test_protected_roots_and_retirement(tmp_path):
    store = Storage(tmp_path)
    store.publish("测试", "a", "2026-09-07", {"结果.json": b"{}"})
    store.publish("测试", "b", "2026-09-07", {"结果.json": b'{"b":1}'})
    store.protect("a", "baseline:x", "BASELINE", "基线")
    store.retire("a")
    store.tx(lambda db: db.execute("UPDATE blobs SET created_at='2000-01-01'"))
    assert store.gc() == []
    with pytest.raises(ValueError, match="current"):
        store.retire("b")


def test_unknown_lease_is_protective(tmp_path):
    path = tmp_path / "lease.json"
    path.write_text("{bad")
    assert lease_active(path)
    assert lease_active(tmp_path / "missing")


def test_parquet_schema_and_redaction():
    import pyarrow as pa
    import pyarrow.parquet as pq
    data = parquet_bytes(pd.DataFrame({"time": pd.to_datetime(["2020-01-01"]), "close": [1.]}))
    table = pq.read_table(pa.BufferReader(data))
    assert table.schema.metadata[b"schema_version"] == b"3"
    assert table.schema.field("time").type.tz == "UTC"
    assert digest(redact({"api_key": "a", "period": 2})) == digest(redact({"api_key": "b", "period": 2}))


def test_backup_api(tmp_path):
    store = Storage(tmp_path)
    store.publish("测试", "r", "2026-09-07", {"结果.json": b"{}"})
    backup = store.backup(tmp_path / "备份.sqlite3")
    import sqlite3
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
