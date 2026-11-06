from pathlib import Path
import importlib.util
import pytest

path=Path(__file__).resolve().parents[1]/'scripts/目录治理_v1.py'
spec=importlib.util.spec_from_file_location('cleanup_v1',path)
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def tree(tmp_path):
    for relative in ('build/lib/副本.py','src/包/__pycache__/缓存.pyc','data/raw/行情.csv','src/包/正式.py','.env'):
        p=tmp_path/relative;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(relative)
    return tmp_path


def test_cleanup_only_removes_reproducible_files_and_is_idempotent(tmp_path):
    root=tree(tmp_path);plan=module.inventory(root)
    assert plan['count']==2
    result=module.apply(root,plan)
    assert result['deleted']==2
    for relative in ('data/raw/行情.csv','src/包/正式.py','.env'):
        assert (root/relative).exists()
    assert module.apply(root,plan)['already_absent']==2


def test_cleanup_hash_conflict_aborts_before_any_deletion(tmp_path):
    root=tree(tmp_path);plan=module.inventory(root)
    (root/'src/包/__pycache__/缓存.pyc').write_text('changed')
    with pytest.raises(ValueError,match='整批停止'):module.apply(root,plan)
    assert (root/'build/lib/副本.py').exists()


def test_cleanup_rejects_protected_paths_even_with_correct_hash(tmp_path):
    root=tree(tmp_path);p=root/'data/raw/行情.csv'
    plan={'files':[{'path':'data/raw/行情.csv','sha256':module.sha(p),'bytes':p.stat().st_size}]}
    with pytest.raises(ValueError,match='非授权'):module.apply(root,plan)
    assert p.exists()


def test_cleanup_skips_ruff_cache_even_when_it_holds_bytecode(tmp_path):
    root=tree(tmp_path)
    cached=root/'.ruff_cache'/'__pycache__'/'缓存.pyc'
    cached.parent.mkdir(parents=True)
    cached.write_text('ruff')
    plan=module.inventory(root)
    assert all(not item['path'].startswith('.ruff_cache/') for item in plan['files'])
    assert plan['count']==2


def test_real_repository_root_keeps_protected_paths_out_of_cleanup():
    root=Path(__file__).resolve().parents[1]
    assert (root/'data'/'metadata'/'研究目录_v3.sqlite3').is_file()
    assert (root/'processed_data'/'us_trend_alerts'/'cache').is_dir()
    assert any((root/'data'/'metadata').glob('目标迁移备份_*.sqlite3'))
    assert (root/'.env').is_file()
    plan=module.inventory(root)
    for item in plan['files']:
        path=item['path']
        assert not path.startswith('.git/')
        assert not path.startswith('.venv/')
        assert not path.startswith('.ruff_cache/')
        assert path != '.env'
        assert not path.startswith('data/')
        assert not path.startswith('processed_data/')
        assert item['reason']


def test_cleanup_rejects_symlink_ancestor_to_protected_data(tmp_path):
    root=tree(tmp_path)
    import os
    import subprocess
    if os.name == 'nt':
        # Real NTFS junctions need no developer/admin symlink privilege and
        # expose the same protected-data redirection risk on Windows.
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(root/'build/链接'), str(root/'data/raw')],
                       check=True, capture_output=True)
    else:
        (root/'build/链接').symlink_to(root/'data/raw',target_is_directory=True)
    p=root/'data/raw/行情.csv'
    plan={'files':[{'path':'build/链接/行情.csv','sha256':module.sha(p),'bytes':p.stat().st_size}]}
    with pytest.raises(ValueError,match='符号链接'):module.apply(root,plan)
    assert p.exists()
