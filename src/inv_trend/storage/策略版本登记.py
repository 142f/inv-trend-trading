"""Immutable strategy contracts, source archives and verifiable result references."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False, default=str
    ).encode("utf-8")


def immutable(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = encoded(value)
    with path.open("xb") as f:
        f.write(payload)
    return digest(payload)


def snapshot(root, target):
    root, target = Path(root).resolve(), Path(target)
    files = sorted(
        set(
            list((root / "src").rglob("*.py"))
            + list((root / "src").rglob("*.yaml"))
            + list((root / "config").rglob("*.json"))
            + list((root / "scripts").glob("EMA*.py"))
            + [root / "pyproject.toml"]
        )
    )
    manifest = {p.relative_to(root).as_posix(): digest(p.read_bytes()) for p in files}
    source_hash = digest(encoded(manifest))
    target.mkdir(parents=True, exist_ok=True)
    archive = target / f"代码快照_{source_hash}.zip"
    if not archive.exists():
        with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED) as z:
            for p in files:
                z.write(p, p.relative_to(root).as_posix())
    return dict(
        source_hash=source_hash,
        archive=str(archive),
        archive_hash=digest(archive.read_bytes()),
        files=manifest,
        git_commit=subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        dirty_tree=True,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def register(
    root,
    *,
    strategy_id,
    strategy_version,
    previous,
    parameters,
    context,
    summary,
    differences,
    results,
):
    record = dict(
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        previous=previous,
        parameters=parameters,
        context=context,
        result_summary=summary,
        created_at=datetime.now(timezone.utc).isoformat(),
        differences=differences,
        results={str(p): digest(Path(p).read_bytes()) for p in results},
    )
    record["record_hash"] = digest(encoded(record))
    path = Path(root) / strategy_id / f"{strategy_version}.json"
    immutable(path, record)
    return str(path)
