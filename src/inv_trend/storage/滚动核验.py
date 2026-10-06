"""离线核验滚动回放制品；只读，不执行记录中的代码或反序列化对象。"""

from __future__ import annotations

from inv_trend.data.integrity import sha256_file
import json
from pathlib import Path


def verify_research(directory: str | Path) -> dict:
    from .研究结果_v3 import store_for

    store = store_for(directory)
    if store is not None:
        return _verify_database_research(store, directory)
    root = Path(directory).resolve(strict=True)
    identity = json.loads((root / "实验协议冻结.json").read_text(encoding="utf-8"))
    count = files = 0
    for record in sorted(root.rglob("运行记录.json")):
        metadata = json.loads(record.read_text(encoding="utf-8"))
        checks = metadata.get("files_sha256")
        if not isinstance(checks, dict) or not checks:
            raise ValueError(f"缺少制品哈希: {record.relative_to(root)}")
        for name, expected in checks.items():
            if Path(name).name != name or len(expected) != 64:
                raise ValueError("不安全路径或无效哈希")
            target = record.parent / name
            if target.is_symlink() or not target.is_file():
                raise ValueError(f"制品不存在或为符号链接: {target}")
            if root not in target.resolve().parents:
                raise ValueError("制品越出研究目录")
            actual = sha256_file(target)
            if actual != expected:
                raise ValueError(f"制品校验失败: {target.relative_to(root)}")
            files += 1
        if (
            "data_version" in metadata
            and metadata["data_version"] != identity["protocol"]["dataset_sha256"]
        ):
            raise ValueError("数据版本不一致")
        count += 1
    if count == 0:
        raise ValueError("研究目录没有完整运行记录")
    # 还核验锁定参数与汇总中保存的合同完全一致，不能只改HTML或锁定文件。
    _verify_parameter_locks(root)
    return _receipt(identity, count, files)


def _verify_parameter_locks(root):
    summary = json.loads((root / "完整迭代摘要.json").read_text(encoding="utf-8"))
    for step in summary:
        for lock in step["locks"]:
            disk = json.loads(
                (root / "参数锁定" / step["iteration"] / (lock["window_id"] + ".json")).read_text(
                    encoding="utf-8"
                )
            )
            if disk != lock:
                raise ValueError("窗口参数锁定与不可变迭代记录不一致")


def _receipt(identity, count, files):
    return {
        "verified": True,
        "runs": count,
        "files": files,
        "source_sha256": identity["source_sha256"],
        "data_sha256": identity["protocol"]["dataset_sha256"],
        "limitation": "哈希能检验一致性，不能替代第三方签名或证明研究者从未看到历史",
    }


def _verify_database_research(store, directory):
    """Verify v3's authoritative documents and result tables without CSV exports."""
    from .研究结果_v3 import ResultRepository
    from .结构化存储_v3 import CatalogPath, StoreError

    prefix = store.key(directory).rstrip("/") + "/"
    root = CatalogPath(store.root / prefix)
    identity = store.read_document(prefix + "实验协议冻结.json")
    # Literal prefix matching keeps '_' and '%' in study IDs from becoming SQL wildcards.
    runs = store.rows(
        "SELECT * FROM result_runs WHERE substr(logical_path,1,?)=? ORDER BY logical_path",
        (len(prefix), prefix),
    )
    if not runs:
        raise ValueError("研究目录没有完整运行记录")
    repository = ResultRepository(store)
    files = 0
    for run in runs:
        metadata = store.read_document(run["metadata_path"])
        if run["status"] != "SUCCEEDED":
            raise StoreError("历史摘要不能作为完整回放验收")
        tables = store.rows(
            "SELECT table_name,sha256 FROM result_tables WHERE run_id=? ORDER BY table_name",
            (run["run_id"],),
        )
        if not tables:
            raise StoreError("完整运行缺少结果表")
        hashes = {table["table_name"]: table["sha256"] for table in tables}
        if metadata.get("table_hashes") != hashes:
            raise StoreError("运行记录与结果表哈希不一致")
        if (
            "data_version" in metadata
            and metadata["data_version"] != identity["protocol"]["dataset_sha256"]
        ):
            raise ValueError("数据版本不一致")
        for table in tables:
            repository.read_table(run["logical_path"], table["table_name"])
            files += 1
    _verify_parameter_locks(root)
    return _receipt(identity, len(runs), files)
