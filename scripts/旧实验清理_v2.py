"""仅清理本次批准的旧实验入口；默认只读核验，内容有本地改动时拒绝删除。"""
from pathlib import Path
import argparse
import hashlib
import json


def inspect(root: Path, manifest: dict) -> list[dict]:
    root = root.resolve(strict=True)
    rows = []
    for item in manifest['deletions']:
        relative = Path(item['path'])
        target = root / relative
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('非法清理路径')
        if any((root / Path(*relative.parts[:i])).is_symlink() for i in range(1,len(relative.parts)+1)):
            raise ValueError('清理路径包含符号链接')
        actual = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
        status = 'ALREADY_REMOVED' if not target.exists() else 'BASELINE_MATCH' if actual == item['before_sha256'] else 'CONFLICT'
        rows.append({'path':item['path'],'status':status,'actual_sha256':actual})
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--apply',action='store_true',help='所有文件都通过哈希核验后才执行删除')
    args = parser.parse_args(argv)
    manifest = json.loads((Path(__file__).resolve().parents[1]/'docs/旧实验清理清单_v2.json').read_text(encoding='utf-8'))
    rows = inspect(args.root,manifest)
    conflicts = [r for r in rows if r['status']=='CONFLICT']
    if args.apply and not conflicts:
        # 一次预检全部通过后再操作；每项删除前重新比对，降低并发修改风险。
        for row in rows:
            if row['status'] == 'BASELINE_MATCH':
                target=args.root/row['path']
                if hashlib.sha256(target.read_bytes()).hexdigest()!=row['actual_sha256']:
                    raise RuntimeError('删除前文件发生变化，停止后续清理')
                target.unlink()
    print(json.dumps({'ok':not conflicts,'applied':bool(args.apply and not conflicts),'files':rows},ensure_ascii=False,indent=2))
    return int(bool(conflicts))


if __name__=='__main__':
    raise SystemExit(main())
