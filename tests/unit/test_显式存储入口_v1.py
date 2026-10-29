import pytest

from inv_trend.data.models import DataLineageError
from inv_trend.data.storage import DataLake, open_data_lake
from inv_trend.data.数据湖适配_v3 import DatabaseDataLake
from inv_trend.storage import StorageIntegrityError, open_storage
from inv_trend.storage.仓库 import Storage
from inv_trend.storage.统一仓库适配_v3 import DatabaseStorage
from inv_trend.storage.结构化存储_v3 import UnifiedStore


def test_data_lake_explicit_backends_and_auto(tmp_path):
    legacy = open_data_lake(tmp_path / "legacy", backend="legacy")
    assert type(legacy) is DataLake
    with pytest.raises(DataLineageError, match="v3 authority is missing"):
        open_data_lake(tmp_path / "missing", backend="v3")

    root = tmp_path / "v3"
    UnifiedStore(root, create=True)
    assert isinstance(open_data_lake(root), DatabaseDataLake)
    with pytest.raises(DataLineageError, match="legacy backend"):
        open_data_lake(root, backend="legacy")
    with pytest.warns(DeprecationWarning, match="open_data_lake"):
        assert isinstance(DataLake(root), DatabaseDataLake)


def test_storage_explicit_backends_and_auto(tmp_path):
    legacy = open_storage(tmp_path / "legacy", backend="legacy")
    assert type(legacy) is Storage
    project = tmp_path / "project"
    UnifiedStore(project / "data", create=True)
    assert isinstance(open_storage(project), DatabaseStorage)
    with pytest.raises(StorageIntegrityError, match="legacy backend"):
        open_storage(project, backend="legacy")
    with pytest.warns(DeprecationWarning, match="open_storage"):
        assert isinstance(Storage(project), DatabaseStorage)


@pytest.mark.parametrize("opener", [open_data_lake, open_storage])
def test_invalid_backend_fails_closed(tmp_path, opener):
    with pytest.raises(ValueError, match="backend must"):
        opener(tmp_path, backend="guess")


def test_one_declared_authority_is_shared_by_both_stacks():
    from inv_trend.infra import backend
    from inv_trend.storage.结构化存储_v3 import DB_RELATIVE

    assert DB_RELATIVE == backend.V3_AUTHORITY_PATH
    assert backend.LEGACY_CATALOGS == ("catalog.sqlite3", "catalog.duckdb")
    assert backend.LEGACY_STATUS == "retirement_window"


def test_opened_backend_reports_its_authority(tmp_path):
    assert open_data_lake(tmp_path / "legacy", backend="legacy").authority == "legacy"
    root = tmp_path / "v3"
    UnifiedStore(root, create=True)
    assert open_data_lake(root).authority == "v3"

    project_legacy = tmp_path / "project-legacy"
    assert open_storage(project_legacy, backend="legacy").authority == "legacy"
    project = tmp_path / "project"
    UnifiedStore(project / "data", create=True)
    assert open_storage(project).authority == "v3"


def test_authority_resolution_follows_the_declared_root_conventions(tmp_path):
    from inv_trend.infra.backend import resolve_authority

    project = tmp_path / "project"
    UnifiedStore(project / "data", create=True)
    assert resolve_authority(project, project_scoped=True) == "v3"
    assert resolve_authority(project / "data") == "v3"
    assert resolve_authority(tmp_path / "empty") == "legacy"
