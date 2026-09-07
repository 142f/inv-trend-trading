"""Certify only intentional execution changes; preserve the original golden fixture."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", action="store_true", required=True)
    parser.parse_args()
    from tests.golden_master_support import build_current_snapshot, expected_path, first_difference
    old_bytes = expected_path().read_bytes()
    old = json.loads(old_bytes)
    actual = build_current_snapshot()
    unchanged = [key for key in actual if not key.startswith("multi_")]
    for key in unchanged:
        assert first_difference(actual[key], old["outputs"][key]) is None, key
    differences = {key: first_difference(actual[key], old["outputs"][key])
                   for key in actual if key.startswith("multi_")}
    record = dict(schema_version=1, baseline_commit="332f70e69458b931feeeaacd79fa5cb0aefb95d8",
                  legacy_expected_sha256=hashlib.sha256(old_bytes).hexdigest(),
                  input_sha256=old["input_sha256"], unchanged_paths=unchanged,
                  reason="Entry-only expiry no longer cancels exits; opening protective stops precede pending adds; each fill uses current equity.",
                  first_differences=differences,
                  outputs={key: value for key, value in actual.items() if key.startswith("multi_")})
    path = ROOT / "tests/fixtures/执行真实性修复期望_v1.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(differences, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
