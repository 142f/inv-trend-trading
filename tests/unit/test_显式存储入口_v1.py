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
