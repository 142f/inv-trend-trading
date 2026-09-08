from __future__ import annotations
from dataclasses import asdict
import json
import sqlite3
import pytest

from inv_trend.storage.阶段运行_v1 import StageRepository
from inv_trend.data.时点行情_v1 import PointInTimeRepository
from inv_trend.core.在线策略_v1 import candidate_grid
from inv_trend.core.阶段协议_v1 import canonical, utc_now, digest


@pytest.fixture
def dataset(tmp_path):
    root = tmp_path / "data"
    repo = StageRepository(root, create=True)
    # Minimal formally registered raw-source document; no fake Parquet engine.
    source = b"synthetic fixture; test only"
    source_hash = digest("fixture")
    repo.store.put_document("metadata/测试数据.csv", source, kind="market_source")
    with repo.store.connect() as db:
        db.execute("INSERT INTO market_datasets_v3 VALUES(?,?,?,?,?,?,?,?)",
                   (1, source_hash, "metadata/测试数据.csv", 3, "{}", "fixture", "RESEARCH_ONLY", utc_now()))
        for i, date in enumerate(("2020-01-02", "2020-01-03", "2025-01-02")):
            db.execute("INSERT INTO market_bars_v3 VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (1, i, date + " 00:00:00+00:00", "QQQ", 100., 102., 98., 101., 1e6, 0., "fixture", "D1"))
    return root, source_hash


def test_repository_does_not_read_quarantined_holdout(dataset):
    root, version = dataset
    with PointInTimeRepository(root, version, allowed_before="2025-01-01T00:00:00+00:00") as repo:
        got = tuple(repo.stream("QQQ", start="2020-01-01T00:00:00+00:00", end="2024-12-31T23:59:00+00:00"))
        assert len(got) == 2
        with pytest.raises(ValueError, match="holdout"):
            tuple(repo.stream("QQQ", start="2020-01-01T00:00:00+00:00", end="2025-01-03T00:00:00+00:00"))
        with pytest.raises(ValueError, match="holdout"):
            repo.history("QQQ", end="2025-01-03T00:00:00+00:00", limit=3)
        assert repo.rows_read == 2


def test_close_availability_not_midnight_date_label(dataset):
    root, version = dataset
    with PointInTimeRepository(root, version, allowed_before="2025-01-01T00:00:00+00:00") as repo:
        assert not repo.history("QQQ", end="2020-01-02T20:59:00+00:00", limit=10)
        assert not repo.history("QQQ", end="2020-01-02T21:00:00+00:00", limit=10)  # end-exclusive
        available = repo.history("QQQ", end="2020-01-02T21:00:01+00:00", limit=10)
        assert len(available) == 1
        assert available[0].available_at == "2020-01-02T21:00:00+00:00"


def test_unsupported_contract_and_missing_version_fail_closed(dataset):
    root, version = dataset
    with pytest.raises(ValueError, match="version"):
        PointInTimeRepository(root, "bad", allowed_before="2025-01-01T00:00:00+00:00")
    with PointInTimeRepository(root, version, allowed_before="2025-01-01T00:00:00+00:00") as repo:
        with pytest.raises(ValueError, match="equities"):
            repo.history("XAUUSD_DUKAS", end="2020-01-01T00:00:00+00:00", limit=100)


def test_database_read_snapshot_cannot_change_mid_study(dataset):
    root, version = dataset
    store = StageRepository(root)
    with PointInTimeRepository(root, version, allowed_before="2025-01-01T00:00:00+00:00") as repo:
        first = repo.history("QQQ", end="2020-01-04T00:00:00+00:00", limit=2)
        with store.store.connect() as db:
            db.execute("UPDATE market_bars_v3 SET close=100 WHERE dataset_id=1")
        second = repo.history("QQQ", end="2020-01-04T00:00:00+00:00", limit=2)
        assert first == second


def test_window_lock_temporal_constraint_and_immutability(dataset):
    root, version = dataset
    store = StageRepository(root)
    store.freeze("test", {"dataset_version": version, "code_version": "fixture"}, candidate_grid())
    good = {"id": "w1", "cutoff": "2020-01-03T21:00:00+00:00",
            "start": "2020-01-04T00:00:00+00:00", "end": "2020-02-01T00:00:00+00:00"}
    store.lock("test", "QQQ", "momentum", good, {"candidate": "fixed"})
    with pytest.raises(sqlite3.IntegrityError):
        store.lock("test", "QQQ", "momentum", good, {"candidate": "changed"})
    with pytest.raises(sqlite3.IntegrityError):
        store.lock("test", "QQQ", "momentum", {**good, "id": "bad", "cutoff": good["end"]}, {})
