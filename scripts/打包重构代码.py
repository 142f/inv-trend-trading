"""Package tracked project files and this refactor's additions, with SHA-256 hashes."""
from __future__ import annotations

import hashlib
import argparse
import json
from pathlib import Path
import subprocess
from zipfile import ZIP_DEFLATED, ZipFile


def main():
    root = Path(__file__).resolve().parents[1]
    parser=argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="重构验证")
    parser.add_argument("--name", default="完整重构代码.zip")
    args=parser.parse_args()
    output = (root / args.output_dir).resolve()
    if not output.is_relative_to(root):
        raise ValueError("delivery directory must stay inside the project")
    output.mkdir(parents=True,exist_ok=True)
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode("utf-8")
    paths = {Path(name) for name in tracked.split("\0") if name}
    additions = subprocess.check_output(["git","ls-files","--others","--exclude-standard","-z"],cwd=root).decode("utf-8")
    paths.update(Path(name) for name in additions.split("\0") if name
                 and Path(name).parts[0] in {"src","tests","scripts","config","docs"})
    paths.update(Path(name) for name in (
        "src/inv_trend/core/行情校验.py", "tests/test_五轮重构.py",
        "scripts/五轮重构基准.py", "scripts/重构证据审计.py", "scripts/打包重构代码.py",
    ))
    paths.update(path.relative_to(root) for path in output.rglob("*")
                 if path.suffix in {".md", ".json", ".xml", ".html", ".png"}
                 and path.name != "交付文件校验.json")
    paths = {path for path in paths if (root / path).is_file()
             and path.name not in {".env", "完整重构代码.zip", "交付文件校验.json"}}
    files = {}
    archive = output / args.name
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as package:
        for relative in sorted(paths):
            content = (root / relative).read_bytes()
            name = relative.as_posix()
            files[name] = hashlib.sha256(content).hexdigest()
            package.writestr(name, content)
        package.writestr((output.relative_to(root)/"包内文件校验.json").as_posix(), json.dumps(files, indent=2, ensure_ascii=False))
    with ZipFile(archive) as package:
        assert package.testzip() is None
        for name, expected in files.items():
            assert hashlib.sha256(package.read(name)).hexdigest() == expected
    report = dict(archive=archive.name, file_count=len(files), bytes=archive.stat().st_size,
                  sha256=hashlib.sha256(archive.read_bytes()).hexdigest(), files=files)
    (output / "交付文件校验.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    print(json.dumps({key: value for key, value in report.items() if key != "files"}, indent=2))


if __name__ == "__main__":
    main()
