"""Headless local HTML verification in a fresh workspace-only browser profile."""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
CHECKS = r"""
<script>
window.addEventListener('load',()=>setTimeout(async()=>{
 const checks={},q=id=>document.getElementById(id);
 try {
  checks.cards=q('metricCards').children.length===6;
  checks.paged_rows=q('ledgerBody').children.length<=50 && q('ledgerBody').children.length>0;
  const previous=q('ledgerBody').textContent;
  if(!q('nextPage').disabled){q('nextPage').click();checks.pagination=previous!==q('ledgerBody').textContent;}else checks.pagination=true;
  q('ledgerSearch').value='NO_MATCH_123';q('ledgerSearch').dispatchEvent(new Event('input'));
  checks.empty_search=q('ledgerBody').textContent.includes('当前筛选没有记录');
  q('ledgerSearch').value='';q('ledgerSearch').dispatchEvent(new Event('input'));
  document.querySelector('[data-ledger=orders]').click();checks.orders=q('ledgerHead').textContent.includes('状态');
  document.querySelector('[data-ledger=signals]').click();checks.signals=q('ledgerHead').textContent.includes('信号确认时间')&&!q('ledgerBody').textContent.includes('当前筛选没有记录');
  const selector=q('activeSelect');if(selector.options.length>1){selector.selectedIndex=1;selector.dispatchEvent(new Event('change'));checks.selection=q('details').textContent.includes(selector.value);}else checks.selection=true;
  q('startDate').value='2099-01-01';q('startDate').dispatchEvent(new Event('change'));checks.date_filter=q('ledgerBody').textContent.includes('当前筛选没有记录');
  q('resetRange').click();checks.reset=q('startDate').value==='';
  checks.no_injected_markup=document.querySelector('[data-audit-injection]')===null&&window.auditInjected!==true;
  checks.no_horizontal_overflow=document.documentElement.scrollWidth<=innerWidth;
  checks.viewport_width=innerWidth===Number(document.documentElement.dataset.auditWidth);
  checks.canvas_pixels=q('equityChart').width>0&&q('equityChart').height>0;
  checks.no_script_errors=(window.auditErrors||[]).length===0;
  document.querySelector('[data-ledger=trades]').click();
  let exported;const create=URL.createObjectURL,click=HTMLAnchorElement.prototype.click;
  URL.createObjectURL=blob=>{exported=blob;return create(blob);};HTMLAnchorElement.prototype.click=()=>{};
  q('exportLedger').click();URL.createObjectURL=create;HTMLAnchorElement.prototype.click=click;
  checks.csv_export=exported instanceof Blob&&(await exported.text()).includes('净盈亏');
  q('ledger').scrollIntoView();
 }catch(error){checks.exception=String(error);}
 const result=document.createElement('pre');result.id='auditResults';result.textContent=JSON.stringify(checks);document.body.append(result);
 if(parent!==window)parent.postMessage({auditResults:checks},'*');
},400));
</script>
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    output = ROOT / "优化验证"
    page = args.input.resolve()
    if args.check:
        content=page.read_text(encoding="utf-8")
        embedded=re.search(r'(<script id="reportData" type="application/json">)(.*?)(</script>)',content,re.S)
        assert embedded
        payload=json.loads(embedded.group(2))
        payload["basic_information"]["run_id"]='<img data-audit-injection src=x onerror="window.auditInjected=true">'
        safe_json=json.dumps(payload,ensure_ascii=False).replace("</","<\\/")
        content=content[:embedded.start(2)]+safe_json+content[embedded.end(2):]
        content=content.replace("<head>", "<head><script>window.auditErrors=[];window.addEventListener('error',e=>auditErrors.push(e.message));</script>")
        content=content.replace('<html lang="zh-CN">',f'<html lang="zh-CN" data-audit-width="{args.width}">')
        content=content.replace("</body>",CHECKS+"</body>")
        page=output / f"{args.label}自检.html"
        page.write_text(content,encoding="utf-8")
    if args.width<500 or args.check:
        # Chromium enforces a minimum desktop window width. An exact-width
        # iframe tests the real narrow CSS viewport instead of cropping 500px.
        wrapper=output/f"{args.label}移动框架.html"
        wrapper.write_text(
            f'<html><body style="margin:0"><iframe style="border:0;width:{args.width}px;height:1200px" src="{page.as_uri()}"></iframe>'
            '<script>addEventListener("message",e=>{if(e.data&&e.data.auditResults){const p=document.createElement("pre");p.id="auditResults";p.textContent=JSON.stringify(e.data.auditResults);document.body.append(p);}});</script></body></html>',
            encoding="utf-8",
        )
        page=wrapper
    browser=Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    profile=ROOT / ".tmp" / f"report-preview-{args.width}"
    screenshot=output / f"{args.label}.png"
    command=[str(browser),"--headless","--disable-gpu","--no-first-run","--disable-background-networking",
             f"--user-data-dir={profile}",f"--screenshot={screenshot}","--dump-dom",
             f"--window-size={args.width+30 if args.check and args.width>=500 else args.width},1200","--virtual-time-budget=2000",page.as_uri()]
    result=subprocess.run(command,capture_output=True,timeout=60,creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8",errors="replace")[-3000:])
    if args.check:
        dom=result.stdout.decode("utf-8",errors="replace")
        match=re.search(r'<pre id="auditResults">(.*?)</pre>',dom,re.S)
        assert match, "browser did not complete the interaction checks"
        checks=json.loads(html.unescape(match.group(1)))
        (output/f"{args.label}自检.json").write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding="utf-8")
        assert all(value is True for value in checks.values()),checks
        print(json.dumps(checks))
    assert screenshot.is_file()
    print(screenshot)


if __name__ == "__main__":
    main()
