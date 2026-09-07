"""按 JUnit 已完成节点补全中断的全量回归，不改测试与生产代码。"""
from pathlib import Path
import json
import sys
import xml.etree.ElementTree as ET
import pytest

ROOT=Path(__file__).resolve().parents[2]
OUT=Path(__file__).resolve().parent
sys.path[:0]=[str(ROOT/'src'),str(ROOT)]

class Inventory:
    def pytest_collection_finish(self, session):
        (OUT/'回归节点.json').write_text(json.dumps([item.nodeid for item in session.items],ensure_ascii=False,indent=2),encoding='utf-8')

def key(node):
    parts=node.split('::')
    return (parts[0][:-3].replace('/','.').replace('\\','.')+('.'+'.'.join(parts[1:-1]) if len(parts)>2 else ''),parts[-1])

if len(sys.argv)==1:
    code=pytest.main(['--collect-only','-qq','-p','no:cacheprovider'],plugins=[Inventory()])
    if code: raise SystemExit(code)
    nodes=json.loads((OUT/'回归节点.json').read_text(encoding='utf-8'))
    completed={(x.attrib['classname'],x.attrib['name']) for x in ET.parse(OUT/'全量回归.xml').iter('testcase') if 'classname' in x.attrib}
    missing=[n for n in nodes if key(n) not in completed]
    chunks=[missing[i:i+30] for i in range(0,len(missing),30)]
    (OUT/'回归补全分组.json').write_text(json.dumps(chunks,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(collected=len(nodes),completed=len(completed),missing=len(missing),chunks=len(chunks))))
else:
    i=int(sys.argv[1]); chunks=json.loads((OUT/'回归补全分组.json').read_text(encoding='utf-8'))
    raise SystemExit(pytest.main(['-q','-p','no:cacheprovider',f'--basetemp=.tmp/audit-part-{i}',f'--junitxml={OUT / ("补全回归"+str(i)+".xml")}',*chunks[i]]))
