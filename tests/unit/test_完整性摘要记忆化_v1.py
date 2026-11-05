"""The file-digest memo must never hide a change."""

from __future__ import annotations

import os
import time
from pathlib import Path

from inv_trend.infra.integrity import clear_hash_memo, sha256_file


def _rewrite(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)
    # Give the filesystem a distinct mtime even on coarse-grained clocks.
    time.sleep(0.001)
    os.utime(path, None)


def test_repeated_digest_of_an_unchanged_file_is_stable_and_memoised(tmp_path: Path) -> None:
    target = tmp_path / "artifact.bin"
    target.write_bytes(b"first payload")
    clear_hash_memo()

    first = sha256_file(target)
    second = sha256_file(target)

    assert first == second
    # A memoised hit is indistinguishable from a real read.
    assert sha256_file(target, memoize=False) == first


def test_rewritten_file_invalidates_the_digest(tmp_path: Path) -> None:
    target = tmp_path / "artifact.bin"
    target.write_bytes(b"first payload")
    clear_hash_memo()
    first = sha256_file(target)

    _rewrite(target, b"second payload")
    assert sha256_file(target) != first


def test_same_size_rewrite_is_not_served_from_the_memo(tmp_path: Path) -> None:
    target = tmp_path / "artifact.bin"
    target.write_bytes(b"aaaaaaaa")
    clear_hash_memo()
    first = sha256_file(target)

    _rewrite(target, b"bbbbbbbb")  # identical size, different content
    fresh = sha256_file(target, memoize=False)
    assert sha256_file(target) == fresh
    assert fresh != first
