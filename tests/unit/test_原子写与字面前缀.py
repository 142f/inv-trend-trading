"""Regressions for shared atomic writes, literal prefixes and code-version capture."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pandas as pd
import pytest

from inv_trend.infra.文件操作 import atomic_copy, atomic_write
from inv_trend.storage.结构化存储_v3 import UnifiedStore
from inv_trend.storage.研究结果_v3 import ResultRepository


def test_atomic_write_and_copy_leave_no_scratch_files(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "值.json"
    atomic_write(target, b'{"a": 1}')
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}

    source = tmp_path / "source.json"
    source.write_text('{"b": 2}', encoding="utf-8")
    copied = tmp_path / "nested" / "拷贝.json"
    atomic_copy(source, copied)
    assert json.loads(copied.read_text(encoding="utf-8")) == {"b": 2}

    leftovers = [path.name for path in tmp_path.rglob("*.tmp")]
    assert leftovers == []


def test_atomic_write_rejects_overwrite_when_replace_is_disabled(tmp_path: Path) -> None:
    target = tmp_path / "immutable.json"
    atomic_write(target, b"first")
    with pytest.raises(FileExistsError):
        atomic_write(target, b"second", replace=False)
    assert target.read_bytes() == b"first"


def test_code_version_capture_degrades_when_git_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A release archive has no .git; the supervisor must still start."""

    from inv_trend.monitoring.扫描器 import CodeVersion

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "module.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")

    def _boom(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", _boom)
    version = CodeVersion.capture(tmp_path)
    assert version.git_commit == "unknown"
    assert version.source_hash


def _freeze_study(store: UnifiedStore, study: str, strategy_version: str) -> None:
    store.put_document(
        f"{study}/实验协议冻结.json",
        {
            "protocol": {
                "dataset_sha256": "d" * 64,
                "strategy_version": strategy_version,
                "production_enabled": False,
            },
            "source_sha256": "s" * 64,
        },
        kind="research_protocol",
    )


def test_study_prefix_binding_is_literal_not_a_like_pattern(tmp_path: Path) -> None:
    """A study id containing '_' must not capture a sibling study's runs.

    The old query used LIKE, where '_' matches any single character, so a run
    published under the unregistered 'qXw_x' inherited the lineage of the
    registered 'q_w_x' study instead of staying unbound.
    """

    store = UnifiedStore(tmp_path, create=True)
    _freeze_study(store, "backtests/q_w_x", "registered-strategy")

    repository = ResultRepository(store)
    repository.publish(
        "backtests/qXw_x/iter1",
        {"收益": pd.Series([1.0, 2.0], name="收益")},
        {"parameters": {"lookback": 20}},
    )

    sibling = store.rows(
        "SELECT strategy_version FROM result_runs WHERE logical_path=?",
        ("backtests/qXw_x/iter1",),
    )[0]
    assert sibling["strategy_version"] is None
    assert store.rows("SELECT study_id FROM study_runs_v3") == []

    # A run published under the registered study is still bound to it.
    repository.publish(
        "backtests/q_w_x/iter1",
        {"收益": pd.Series([1.0], name="收益")},
        {"parameters": {"lookback": 20}},
    )
    owned = store.rows(
        "SELECT strategy_version FROM result_runs WHERE logical_path=?",
        ("backtests/q_w_x/iter1",),
    )[0]
    assert owned["strategy_version"] == "registered-strategy"
    assert store.rows("SELECT study_id FROM study_runs_v3")[0]["study_id"] == "backtests/q_w_x"
