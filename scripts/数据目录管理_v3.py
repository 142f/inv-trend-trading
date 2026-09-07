"""结构化数据迁移、核验、只读清理计划和一致性数据库备份。"""
from __future__ import annotations
import argparse,json,stat,tempfile,zipfile
from pathlib import Path,PurePosixPath
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from inv_trend.storage.结构化存储_v3 import UnifiedStore,StoreError


def safe_extract(path,target):
    with zipfile.ZipFile(path) as z:
        if sum(i.file_size for i in z.infolist())>20*1024**3:raise StoreError('来源ZIP超过20GiB，需显式人工解压核对')
        seen=set()
        for item in z.infolist():
            raw=item.filename.replace('\\','/');p=PurePosixPath(raw)
            if p.is_absolute() or '..' in p.parts or ':' in raw or raw in seen:raise StoreError('ZIP路径不安全或重复')
            if stat.S_ISLNK(item.external_attr>>16):raise StoreError('ZIP含符号链接')
            seen.add(raw)
        z.extractall(target)
    root=target/'data'
    if not root.is_dir():raise StoreError('ZIP必须只有一个明确的 data/ 根目录')
    if any(p!=root for p in target.iterdir()):raise StoreError('ZIP包含data以外内容；请先独立解压再指定目录')
    return root


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    m=sub.add_parser('migrate');m.add_argument('--source',type=Path,required=True);m.add_argument('--target',type=Path,required=True)
    m.add_argument('--research-data',type=Path);m.add_argument('--evidence',type=Path)
    v=sub.add_parser('verify');v.add_argument('--data',type=Path,default=Path('data'));v.add_argument('--strict-parquet',action='store_true')
    c=sub.add_parser('clean');c.add_argument('--data',type=Path,default=Path('data'));c.add_argument('--apply',action='store_true');c.add_argument('--min-age-days',type=int,default=7)
    b=sub.add_parser('backup');b.add_argument('--data',type=Path,default=Path('data'));b.add_argument('--destination',type=Path,required=True)
    s=sub.add_parser('summary');s.add_argument('--data',type=Path,default=Path('data'))
    a=p.parse_args(argv)
    if a.command=='migrate':
        from inv_trend.storage.数据迁移_v3 import migrate
        from inv_trend.storage.附加资料迁移_v3 import import_supplements
        if a.source.is_file():
            with tempfile.TemporaryDirectory(prefix='数据迁移_') as tmp:
                source=safe_extract(a.source,Path(tmp));result=migrate(source,a.target,round_id=6)
        else:result=migrate(a.source,a.target,round_id=6)
        store=UnifiedStore(a.target)
        result['supplements']=import_supplements(store,research_data=a.research_data,evidence=a.evidence)
        store.checkpoint(vacuum=True)
    elif a.command=='verify':
        from inv_trend.storage.数据生命周期_v3 import ready_to_replace
        result=ready_to_replace(a.data,strict_parquet=a.strict_parquet)
    elif a.command=='clean':
        from inv_trend.storage.数据生命周期_v3 import cleanup
        result=cleanup(a.data,apply=a.apply,min_age_days=a.min_age_days)
    elif a.command=='backup':
        from inv_trend.storage.数据生命周期_v3 import backup_database
        result={'database_backup':str(backup_database(a.data,a.destination)),'note':'此命令只备份数据库；完整备份须同时复制不可变对象并核验'}
    else:
        store=UnifiedStore(a.data)
        result={'current':store.rows('SELECT * FROM v_current_datasets'),'space':store.rows('SELECT * FROM v_storage_usage'),
                'runs':store.rows('SELECT status,count(*) n FROM result_runs GROUP BY status'),
                'issues':store.rows('SELECT category,count(*) n FROM migration_issues GROUP BY category')}
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 1 if isinstance(result,dict) and result.get('ok') is False else 0

if __name__=='__main__':raise SystemExit(main())
