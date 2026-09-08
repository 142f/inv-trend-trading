"""只读核对v3补丁的前/后SHA256，拒绝覆盖本地冲突；不会修改任何文件。"""
from __future__ import annotations
import argparse,hashlib,json
from pathlib import Path,PurePosixPath

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,required=True)
    p.add_argument('--manifest',type=Path,required=True)
    a=p.parse_args(argv);root=a.root.resolve();manifest=json.loads(a.manifest.read_text(encoding='utf-8'))
    rows=[]
    for item in manifest['files']:
        rel=PurePosixPath(item['path'])
        if rel.is_absolute() or '..' in rel.parts or ':' in str(rel):raise ValueError('补丁清单路径不安全')
        path=root.joinpath(*rel.parts)
        if path.is_symlink() or (root not in path.resolve().parents):raise ValueError('目标包含符号链接或越界')
        actual=hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        status='ALREADY_APPLIED' if actual==item['after_sha256'] else 'BASELINE_MATCH' if actual==item['before_sha256'] else 'CONFLICT'
        rows.append({'path':str(rel),'status':status})
    ok=all(r['status']!='CONFLICT' for r in rows)
    print(json.dumps({'ok':ok,'files':rows,'note':'数据目录必须整体切换，不能把新旧data混合解压；清单自身以外部交付SHA256校验'},ensure_ascii=False,indent=2))
    return 0 if ok else 2

if __name__=='__main__':raise SystemExit(main())
