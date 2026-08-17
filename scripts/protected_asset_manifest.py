"""Write a deterministic SHA-256 manifest for explicitly protected assets.

The tool intentionally has no timestamp so an unchanged protected set produces
byte-for-byte identical JSON.  It never descends into ``.tmp/k`` because that
path is a known local ACL blocker and must not be touched by cleanup tooling.

Example:
    python scripts/protected_asset_manifest.py --repo-root . \
      --output docs/PROTECTED_ASSET_MANIFEST_PRE_CLEANUP.json \
      --head 149b8f673c998d3e6104569626e88705dc41ca05 \
      --archive-path inv-trend-trading-core-20260814.zip \
      --exclude ".tmp/k=Known local ACL blocker; intentionally not traversed or modified." \
      data processed_data deliverables inv-trend-trading-core-20260814.zip
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sys
from typing import Iterator


CHUNK_BYTES = 1024 * 1024
BLOCKED_PATH = PurePosixPath(".tmp/k")
BLOCKED_REASON = "Known Windows ACL blocker; intentionally not traversed or modified."


class ManifestError(RuntimeError):
    """Raised when a requested protected asset cannot be safely recorded."""


def _as_relative_path(repo_root: Path, raw_path: str) -> tuple[Path, PurePosixPath]:
    candidate = Path(raw_path)
    absolute = Path(os.path.abspath(candidate if candidate.is_absolute() else repo_root / candidate))
    try:
        relative = absolute.relative_to(repo_root)
    except ValueError as exc:
        raise ManifestError(f"Path is outside repository root: {raw_path}") from exc
    if relative == Path("."):
        raise ManifestError("Repository root cannot be a protected asset input.")
    return absolute, PurePosixPath(relative.as_posix())


def _is_under(path: PurePosixPath, parent: PurePosixPath) -> bool:
    return path == parent or parent in path.parents


def _hash_file(path: Path) -> tuple[int, str]:
    try:
        before = path.stat()
        digest = hashlib.sha256()
        byte_count = 0
        with path.open("rb") as stream:
            while chunk := stream.read(CHUNK_BYTES):
                digest.update(chunk)
                byte_count += len(chunk)
        after = path.stat()
    except OSError as exc:
        raise ManifestError(f"Cannot read protected file {path}: {exc}") from exc

    if before.st_size != byte_count or before.st_mtime_ns != after.st_mtime_ns:
        raise ManifestError(f"Protected file changed while being hashed: {path}")
    return byte_count, digest.hexdigest()


def _iter_directory_files(
    directory: Path,
    relative_directory: PurePosixPath,
    excluded_paths: set[PurePosixPath],
) -> Iterator[tuple[Path, PurePosixPath]]:
    try:
        entries = sorted(os.scandir(directory), key=lambda entry: entry.name.casefold())
    except OSError as exc:
        raise ManifestError(f"Cannot read protected directory {directory}: {exc}") from exc

    for entry in entries:
        child_path = Path(entry.path)
        child_relative = relative_directory / entry.name
        if any(_is_under(child_relative, excluded) for excluded in excluded_paths):
            continue
        try:
            if entry.is_symlink():
                raise ManifestError(f"Refusing to hash symlink inside protected asset: {child_path}")
            if entry.is_dir(follow_symlinks=False):
                yield from _iter_directory_files(child_path, child_relative, excluded_paths)
            elif entry.is_file(follow_symlinks=False):
                yield child_path, child_relative
            else:
                raise ManifestError(f"Unsupported protected filesystem entry: {child_path}")
        except OSError as exc:
            raise ManifestError(f"Cannot inspect protected asset {child_path}: {exc}") from exc


def _file_record(path: Path, relative_path: PurePosixPath) -> dict[str, object]:
    size_bytes, sha256 = _hash_file(path)
    return {
        "path": relative_path.as_posix(),
        "size_bytes": size_bytes,
        "sha256": sha256,
    }


def _directory_record(
    path: Path,
    relative_path: PurePosixPath,
    excluded_paths: set[PurePosixPath],
) -> dict[str, object]:
    files = [
        _file_record(file_path, file_relative)
        for file_path, file_relative in _iter_directory_files(path, relative_path, excluded_paths)
    ]
    tree_digest = hashlib.sha256()
    for file in files:
        tree_digest.update(
            (
                f"{file['path']}\0{file['size_bytes']}\0{file['sha256']}\n"
            ).encode("utf-8")
        )
    return {
        "path": relative_path.as_posix(),
        "kind": "directory",
        "hash_scope": "recursive_file_tree",
        "size_bytes": sum(int(file["size_bytes"]) for file in files),
        "sha256": tree_digest.hexdigest(),
        "files": files,
    }


def _parse_exclusion(raw_value: str, repo_root: Path) -> tuple[PurePosixPath, str]:
    path_text, separator, reason = raw_value.partition("=")
    if not separator or not path_text or not reason:
        raise ManifestError("--exclude must be formatted as RELATIVE_PATH=REASON")
    _, relative_path = _as_relative_path(repo_root, path_text)
    return relative_path, reason


def _manifest(args: argparse.Namespace) -> dict[str, object]:
    repo_root = Path(args.repo_root).resolve()
    if not repo_root.is_dir():
        raise ManifestError(f"Repository root is not a readable directory: {repo_root}")

    exclusions = {BLOCKED_PATH: BLOCKED_REASON}
    for raw_exclusion in args.exclude:
        relative_path, reason = _parse_exclusion(raw_exclusion, repo_root)
        exclusions[relative_path] = reason
    excluded_paths = set(exclusions)

    assets: list[dict[str, object]] = []
    all_files: list[dict[str, object]] = []
    seen_paths: set[PurePosixPath] = set()
    for raw_path in args.paths:
        absolute_path, relative_path = _as_relative_path(repo_root, raw_path)
        if relative_path in seen_paths:
            raise ManifestError(f"Protected asset was supplied more than once: {relative_path}")
        if any(_is_under(relative_path, excluded) for excluded in excluded_paths):
            raise ManifestError(f"Protected asset is intentionally excluded: {relative_path}")
        seen_paths.add(relative_path)

        try:
            if absolute_path.is_symlink():
                raise ManifestError(f"Refusing to hash protected symlink: {relative_path}")
            if absolute_path.is_file():
                record = _file_record(absolute_path, relative_path)
                assets.append({"kind": "file", "hash_scope": "file_contents", **record})
                all_files.append(record)
            elif absolute_path.is_dir():
                record = _directory_record(absolute_path, relative_path, excluded_paths)
                assets.append(record)
                all_files.extend(record["files"])
            else:
                raise ManifestError(f"Protected asset does not exist or is not readable: {relative_path}")
        except OSError as exc:
            raise ManifestError(f"Cannot inspect protected asset {relative_path}: {exc}") from exc

    all_files.sort(key=lambda record: str(record["path"]).casefold())
    assets.sort(key=lambda record: str(record["path"]).casefold())
    archive_path = None
    if args.archive_path:
        _, archive_relative = _as_relative_path(repo_root, args.archive_path)
        archive_path = archive_relative.as_posix()
        archive_matches = [record for record in all_files if record["path"] == archive_path]
        if len(archive_matches) != 1:
            raise ManifestError(
                "--archive-path must be present once in the explicitly supplied protected assets: "
                f"{archive_path}"
            )
        archive = archive_matches[0]
    else:
        archive = None

    return {
        "schema_version": 1,
        "manifest_tool": "scripts/protected_asset_manifest.py",
        "repository_head": args.head,
        "archive_snapshot": archive,
        "intentional_exclusions": [
            {"path": path.as_posix(), "reason": exclusions[path]}
            for path in sorted(exclusions, key=lambda item: item.as_posix().casefold())
        ],
        "assets": assets,
        "summary": {
            "asset_count": len(assets),
            "file_count": len(all_files),
            "total_bytes": sum(int(record["size_bytes"]) for record in all_files),
        },
    }


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="Explicit repository-relative files or directories to protect.")
    parser.add_argument("--repo-root", default=".", help="Repository root (default: current directory).")
    parser.add_argument("--output", required=True, help="Repository-relative JSON manifest path to write.")
    parser.add_argument("--head", required=True, help="Git HEAD recorded as the manifest baseline.")
    parser.add_argument(
        "--archive-path",
        help="Explicit protected file whose SHA-256 is also recorded as archive_snapshot.",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="RELATIVE_PATH=REASON",
        help="Path intentionally excluded from traversal, with its reason (repeatable).",
    )
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    try:
        repo_root = Path(args.repo_root).resolve()
        manifest = _manifest(args)
        output_path, _ = _as_relative_path(repo_root, args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    except ManifestError as exc:
        print(f"protected-asset-manifest: error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"protected-asset-manifest: error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
