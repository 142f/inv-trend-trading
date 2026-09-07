"""离线核验滚动回放制品；只读，不执行记录中的代码或反序列化对象。"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path


def verify_research(directory: str | Path) -> dict:
    from .研究结果_v3 import store_for
    if store_for(directory):
        from .滚动核验_v3 import verify_database_research
        return verify_database_research(directory)
    root = Path(directory).resolve(strict=True)
    identity = json.loads((root / '实验协议冻结.json').read_text(encoding='utf-8'))
    count = files = 0
    for record in sorted(root.rglob('运行记录.json')):
        metadata = json.loads(record.read_text(encoding='utf-8'))
        checks = metadata.get('files_sha256')
        if not isinstance(checks, dict) or not checks:
            raise ValueError(f'缺少制品哈希: {record.relative_to(root)}')
        for name, expected in checks.items():
            if Path(name).name != name or len(expected) != 64:
                raise ValueError('不安全路径或无效哈希')
            target = record.parent / name
            if target.is_symlink() or not target.is_file():
                raise ValueError(f'制品不存在或为符号链接: {target}')
            if root not in target.resolve().parents:
                raise ValueError('制品越出研究目录')
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            if actual != expected:
                raise ValueError(f'制品校验失败: {target.relative_to(root)}')
            files += 1
        if 'data_version' in metadata and metadata['data_version'] != identity['protocol']['dataset_sha256']:
            raise ValueError('数据版本不一致')
        count += 1
    if count == 0:
        raise ValueError('研究目录没有完整运行记录')
    # 还核验锁定参数与汇总中保存的合同完全一致，不能只改HTML或锁定文件。
    summary = json.loads((root / '完整迭代摘要.json').read_text(encoding='utf-8'))
    for step in summary:
        for lock in step['locks']:
            disk = json.loads((root / '参数锁定' / step['iteration'] / (lock['window_id']+'.json')).read_text(encoding='utf-8'))
            if disk != lock:
                raise ValueError('窗口参数锁定与不可变迭代记录不一致')
    return {'verified': True, 'runs': count, 'files': files,
            'source_sha256': identity['source_sha256'],
            'data_sha256': identity['protocol']['dataset_sha256'],
            'limitation': '哈希能检验一致性，不能替代第三方签名或证明研究者从未看到历史'}
