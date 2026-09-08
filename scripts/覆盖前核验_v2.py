"""只读检查v2补丁；任何本地改动、路径越界或符号链接均拒绝覆盖。"""
from pathlib import Path
import argparse
import hashlib
import json


def inspect(root, manifest):
    root=Path(root).resolve(strict=True)
    result=[]
    for item in manifest['files']:
        relative=Path(item['path']);path=root/relative
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('非法补丁路径')
        linked=any((root/Path(*relative.parts[:i])).is_symlink() for i in range(1,len(relative.parts)+1))
        actual=hashlib.sha256(path.read_bytes()).hexdigest() if not linked and path.is_file() else None
        if linked or (path.exists() and not path.is_file()):status='CONFLICT'
        elif actual==item['after_sha256']:status='ALREADY_APPLIED'
        elif actual==item['before_sha256']:status='BASELINE_MATCH'
        else:status='CONFLICT'
        result.append({'path':item['path'],'status':status,'actual_sha256':actual})
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',required=True,type=Path)
    parser.add_argument('--manifest',required=True,type=Path)
    args=parser.parse_args()
    rows=inspect(args.root,json.loads(args.manifest.read_text(encoding='utf-8')))
    conflicts=[r for r in rows if r['status']=='CONFLICT']
    print(json.dumps({'ok':not conflicts,'checked_files':len(rows),'conflicts':conflicts,'results':rows},ensure_ascii=False,indent=2))
    return int(bool(conflicts))


if __name__=='__main__':
    raise SystemExit(main())
