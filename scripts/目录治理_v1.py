"""缓存/构建产物治理。默认只预览；apply 按已审阅清单逐个验证哈希，绝不扫描删除业务数据。"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys


def reason(relative: Path):
    if relative.suffix == '.pyc' or '__pycache__' in relative.parts:
        return 'Python cache; reproducible'
    if relative.parts[0] == 'build':
        return 'generated build copy; canonical source is src/'
    if any(p.endswith('.egg-info') for p in relative.parts):
        return 'generated package metadata; rebuilt by installation'
    if '.pytest_cache' in relative.parts or any(p.startswith('pytest-cache-files-') for p in relative.parts):
        return 'pytest cache; reproducible'
    return None


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def inventory(root):
    root = root.resolve()
    files = []
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if path.is_symlink() or not path.is_file() or reason(relative) is None:
            continue
        files.append({'path': relative.as_posix(), 'sha256': sha(path),
                      'bytes': path.stat().st_size, 'reason': reason(relative)})
    return {'schema_version': 1, 'protected': ['data', 'processed_data', 'metadata', '.env', 'src (except caches)'],
            'files': files, 'count': len(files), 'bytes': sum(x['bytes'] for x in files)}


def apply(root, plan):
    root = root.resolve()
    approved, missing = [], []
    for item in plan['files']:
        relative = Path(item['path'])
        target = root / relative
        if relative.is_absolute() or '..' in relative.parts or reason(relative) is None:
            raise ValueError('清单包含非授权路径: ' + str(relative))
        if not target.resolve().is_relative_to(root) or target.is_symlink() or any(
                p.is_symlink() for p in target.parents if p != root and root in p.parents):
            raise ValueError('清单路径越界或是符号链接')
        if not target.exists():
            missing.append(str(relative)); continue
        if not target.is_file() or sha(target) != item['sha256']:
            raise ValueError('文件已变动，整批停止；请重新生成并审阅清单: ' + str(relative))
        approved.append(target)
    # Preflight every hash before deleting any file. Run only when no tests/builds are writing caches.
    for path in approved:
        if sha(path) != next(i['sha256'] for i in plan['files'] if i['path'] == path.relative_to(root).as_posix()):
            raise ValueError('执行期间文件变化，已停止')
        path.unlink()
    for p in sorted(root.rglob('*'), key=lambda p: len(p.parts), reverse=True):
        if p.is_dir() and not p.is_symlink() and reason(p.relative_to(root)) and not any(p.iterdir()):
            p.rmdir()
    return {'deleted': len(approved), 'already_absent': len(missing),
            'deleted_bytes': sum(i['bytes'] for i in plan['files'] if (root/i['path']) in approved),
            'source_data_deleted': False}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, default=Path.cwd())
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--apply', action='store_true')
    args = p.parse_args(argv)
    try:
        if args.apply:
            result = apply(args.root, json.loads(args.manifest.read_text(encoding='utf-8')))
        else:
            result = inventory(args.root)
            args.manifest.parent.mkdir(parents=True, exist_ok=True)
            with args.manifest.open('x', encoding='utf-8') as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        print(json.dumps({k:v for k,v in result.items() if k != 'files'}, ensure_ascii=False))
        return 0
    except (ValueError, OSError) as exc:
        print(json.dumps({'status':'FAIL','message':str(exc)},ensure_ascii=False),file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
