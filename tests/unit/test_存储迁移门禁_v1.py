import sqlite3

import pytest

from inv_trend.storage.数据生命周期_v3 import ready_to_replace
from inv_trend.storage.结构化存储_v3 import StoreError, UnifiedStore


def test_schema_migrations_are_ordered_and_complete(tmp_path):
    store = UnifiedStore(tmp_path / "data", create=True)
    assert [row["version"] for row in store.rows(
        "SELECT version FROM store_migrations ORDER BY version"
    )] == [1, 2, 3]
    assert ready_to_replace(store)["schema_migrations"] == [1, 2, 3]


def test_registered_schema_checksum_cannot_be_rewritten(tmp_path):
    store = UnifiedStore(tmp_path / "data", create=True)
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE store_migrations SET checksum='bad' WHERE version=2")
    with pytest.raises(StoreError, match="校验和不一致"):
        UnifiedStore(store.root)


def test_unresolved_migration_error_blocks_cutover(tmp_path):
    store = UnifiedStore(tmp_path / "data", create=True)
    with store.connect() as db:
        db.execute("INSERT INTO migration_issues(severity,category,source_path,details) VALUES(?,?,?,?)",
                   ("ERROR", "TEST", "source", "broken"))
    with pytest.raises(StoreError, match="未解决错误"):
        ready_to_replace(store)
