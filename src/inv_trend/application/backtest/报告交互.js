(() => {
  'use strict';
  const model = JSON.parse(document.getElementById('reportData').textContent);
  const charts = model.charts, ranking = model.ranking;
  const q = id => document.getElementById(id);
  const colors = ['#2261bf','#098779','#ba6d14','#b13b62','#7255b1','#348595','#9a734c'];
  const numeric = value => value !== null && value !== '' && Number.isFinite(Number(value));
  const number = value => numeric(value) ? Number(value).toLocaleString('zh-CN',{maximumFractionDigits:3}) : '不可用';
  const percent = value => numeric(value) ? (Number(value)*100).toFixed(2)+'%' : '不可用';
  const color = id => colors[Math.max(0,charts.equity_series.findIndex(row=>row.combination_id===id))%colors.length];
  const element = (tag,text,cls) => { const node=document.createElement(tag); if(text!=null) node.textContent=String(text); if(cls)node.className=cls; return node; };
  function card(parent,label,value,note='',signed=null) {
    const node=element('div',null,'card');node.append(element('span',label),element('b',value,signed==null?'':signed<0?'negative':'positive'));
    if(note)node.append(element('small',note));parent.append(node);
  }
  const rows = [...ranking].sort((a,b)=>(a.rank??1e9)-(b.rank??1e9));
  let active = model.best_combination_id || rows[0]?.combination_id || '';
  let ledgerKind='trades', page=0, filtered=[], columns=[], pending=0;
  const geometry = new Map();
  const sampleSize=50;
  card(q('summary'),'参数组合',ranking.length,'预先确定的有限网格');
  card(q('summary'),'验证状态',model.basic_information.validation_status,'与最终留出分离');
  card(q('summary'),'最终确认',model.best_combination_id?'已确认':'未确认','未通过时不另选留出赢家');
  card(q('summary'),'验证邻域',model.stable_combination_ids.length,'通过最终确认后展示');
  for(const [key,value] of Object.entries(model.basic_information)) {
    const node=element('div');node.append(element('dt',key),element('dd',typeof value==='object'?JSON.stringify(value):value??'不可用'));q('lineage').append(node);
  }
  q('parameters').textContent=JSON.stringify(model.assumptions.parameter_space,null,2);
  q('assumptions').textContent=JSON.stringify({...model.assumptions,parameter_space:undefined},null,2);
  for(const row of rows) {const option=element('option',row.combination_id);option.value=row.combination_id;q('activeSelect').append(option);}
  charts.equity_series.forEach((row,index)=>{const option=element('option',row.combination_id);option.value=row.combination_id;option.selected=index<3;q('curveSelect').append(option);});
  function selected(){return ranking.find(row=>row.combination_id===active)||{};}
  function detail(){return (charts.detail_series||[]).find(row=>row.combination_id===active)||{};}
  function chosen(){const ids=new Set([...q('curveSelect').selectedOptions].map(option=>option.value));if(!ids.size)ids.add(active);return ids;}
  function range(){const lo=q('startDate').value,hi=q('endDate').value;return [lo?Date.parse(lo): -Infinity,hi?Date.parse(hi)+86399999:Infinity];}
  function inRange(time){const [lo,hi]=range(), t=Date.parse(time);return Number.isFinite(t)&&t>=lo&&t<=hi;}
  function updateActive(){
    q('activeSelect').value=active;const row=selected(), full=row.metrics||{},oos=row.validation_metrics||{}, hold=row.holdout_metrics||{};
    q('activeStatus').textContent=row.qualified?'验证集达标；最终确认状态见概览':'验证集未达标或样本不足；仅供研究比较';
    q('metricCards').replaceChildren();
    card(q('metricCards'),'验证 · 年化收益',percent(oos.annualized_return),'各验证折统计均值',oos.annualized_return);
    card(q('metricCards'),'验证 · 最大回撤',percent(oos.max_drawdown),'最差验证折',oos.max_drawdown);
    card(q('metricCards'),'验证 · Sharpe',number(oos.sharpe_ratio),'不以全样本收益排名');
    card(q('metricCards'),'留出 · 总收益',percent(hold.total_return),'最终确认，不参与选参',hold.total_return);
    card(q('metricCards'),'全样本 · 已平仓交易',number(full.closed_trade_count??full.trade_count),'未平仓 '+number(full.open_position_count??0));
    card(q('metricCards'),'全样本 · 已平仓交易成本',number(full.total_cost),'含已入账手续费、滑点及持仓费');
    q('details').textContent=JSON.stringify(row,null,2);page=0;renderLedger();schedule();
  }
  const definitions={
    trades:[['symbol','品种'],['side_name','方向'],['entry_time','入场时间'],['exit_time','出场时间'],['avg_entry','平均入场'],['exit_price','出场价'],['qty','数量'],['pnl','净盈亏'],['total_cost','总成本'],['holding_bars','持有 K 线'],['exit_reason','出场原因']],
    orders:[['time','执行/处理时间'],['symbol','品种'],['action','动作'],['side','方向'],['status','状态'],['qty','数量'],['signal_price','信号参考价'],['fill_price','执行/检查价'],['cost','执行成本'],['reason','触发原因'],['resolution','未成交原因']],
    signals:[['signal_time','信号确认时间'],['symbol','品种'],['system','系统'],['direction','方向'],['signal_type','信号'],['channel_value','通道价'],['trigger_price','参考价'],['n','N 波动值']]
  };
  function field(row,key){
    if(key==='status')return row.status||'filled';
    if(key==='side')return row.side===1?'long':row.side===-1?'short':row.side;
    return row[key];
  }
  function renderLedger(){
    columns=definitions[ledgerKind];const source=ledgerKind==='signals'?(charts.signal_series?.[detail().signal_parameter_hash]||[]):(detail()[ledgerKind]||[]),text=q('ledgerSearch').value.trim().toLowerCase();
    filtered=source.filter(row=>inRange(row.exit_time||row.time||row.signal_time)&&(!text||columns.some(([key])=>String(field(row,key)??'').toLowerCase().includes(text))));
    const pages=Math.max(1,Math.ceil(filtered.length/sampleSize));page=Math.max(0,Math.min(page,pages-1));
    const head=element('tr');columns.forEach(([,title])=>head.append(element('th',title)));q('ledgerHead').replaceChildren(head);
    const fragment=document.createDocumentFragment();
    filtered.slice(page*sampleSize,(page+1)*sampleSize).forEach(row=>{
      const tr=element('tr');columns.forEach(([key])=>{
        const raw=field(row,key);let value=raw??'—';if(typeof raw==='number')value=number(raw);
        const td=element('td',value,key==='pnl'&&numeric(raw)?raw<0?'negative':'positive':'');td.title=String(value);tr.append(td);
      });fragment.append(tr);
    });
    if(!filtered.length){const tr=element('tr'),td=element('td','当前筛选没有记录');td.colSpan=columns.length;tr.append(td);fragment.append(tr);}
    q('ledgerBody').replaceChildren(fragment);q('pageStatus').textContent=`共 ${filtered.length} 条 · ${page+1}/${pages} 页 · 每页 ${sampleSize} 条`;
    q('prevPage').disabled=page===0;q('nextPage').disabled=page>=pages-1;
    q('ledgerDescription').textContent=ledgerKind==='signals'?'这是策略原始信号，不等于实际成交；执行还受仓位、资金和风险约束。':ledgerKind==='orders'?'未成交行的价格是检查参考价，状态和原因说明是否实际成交。': '仅展示已平仓交易；净盈亏已扣成本，开仓持仓不混入胜率。日期筛选按平仓时间。';
  }
  document.querySelectorAll('[data-ledger]').forEach(button=>button.addEventListener('click',()=>{
    ledgerKind=button.dataset.ledger;page=0;document.querySelectorAll('[data-ledger]').forEach(node=>node.setAttribute('aria-pressed',String(node===button)));renderLedger();
  }));
  q('ledgerSearch').addEventListener('input',()=>{page=0;renderLedger();});
  q('prevPage').addEventListener('click',()=>{page--;renderLedger();});q('nextPage').addEventListener('click',()=>{page++;renderLedger();});
  q('exportLedger').addEventListener('click',()=>{
    const csvCell=value=>{let s=String(value??'');if(typeof value==='string'&&/^[=+@\-\t\r]/.test(s))s="'"+s;return '"'+s.replaceAll('"','""')+'"';};
    const records=[columns.map(([,label])=>label),...filtered.map(row=>columns.map(([key])=>field(row,key)))];
    const url=URL.createObjectURL(new Blob(['\uFEFF'+records.map(row=>row.map(csvCell).join(',')).join('\r\n')],{type:'text/csv;charset=utf-8'}));
    const link=element('a');link.href=url;link.download=`${active}_${ledgerKind}.csv`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  });
  function setup(id){const canvas=q(id),rect=canvas.getBoundingClientRect();if(rect.width<2)return null;const ratio=window.devicePixelRatio||1;canvas.width=Math.round(rect.width*ratio);canvas.height=Math.round(rect.height*ratio);const ctx=canvas.getContext('2d');ctx.setTransform(ratio,0,0,ratio,0,0);ctx.clearRect(0,0,rect.width,rect.height);return {ctx,w:rect.width,h:rect.height,left:65,right:rect.width-18,top:18,bottom:rect.height-36};}
  function extent(values,includeZero=false){let lo=includeZero?0:Infinity,hi=includeZero?0:-Infinity;for(const value of values){if(!numeric(value))continue;lo=Math.min(lo,Number(value));hi=Math.max(hi,Number(value));}if(!Number.isFinite(lo))return [0,1];if(hi===lo){const pad=Math.abs(hi)*.05||1;return [lo-pad,hi+pad];}return [lo,hi];}
  function axes(s,lo,hi,format=number){const {ctx,left,right,top,bottom}=s;ctx.font='11px Segoe UI';for(let i=0;i<=4;i++){const y=top+(bottom-top)*i/4;ctx.strokeStyle='#e3e9f1';ctx.beginPath();ctx.moveTo(left,y);ctx.lineTo(right,y);ctx.stroke();ctx.fillStyle='#687a92';ctx.textAlign='right';ctx.fillText(format(hi-(hi-lo)*i/4),left-8,y+4);}ctx.textAlign='left';}
  function empty(s,message='当前范围没有可用数据'){s.ctx.fillStyle='#687a92';s.ctx.fillText(message,s.left,s.top+35);}
  function sample(points,key,limit=1200){if(points.length<=limit)return points;const step=Math.ceil(points.length/(limit/2)),out=[];for(let i=0;i<points.length;i+=step){let min=i,max=i;for(let j=i;j<Math.min(i+step,points.length);j++){if(points[j][key]<points[min][key])min=j;if(points[j][key]>points[max][key])max=j;}for(const j of [...new Set([min,max])].sort((a,b)=>a-b))out.push(points[j]);}if(out[0]!==points[0])out.unshift(points[0]);if(out[out.length-1]!==points[points.length-1])out.push(points[points.length-1]);return out;}
  function line(id,series,key){
    const s=setup(id);if(!s)return;const picked=series.filter(row=>chosen().has(row.combination_id)).map(row=>({...row,points:row.points.filter(p=>numeric(p[key])&&inRange(p.time))}));
    const points=picked.flatMap(row=>row.points);if(!points.length){empty(s);geometry.delete(id);return;}
    const [lo,hi]=extent(points.map(p=>p[key]),key==='drawdown'),[from,to]=extent(points.map(p=>Date.parse(p.time)));
    axes(s,lo,hi,key==='drawdown'?percent:number);const x=time=>s.left+(Date.parse(time)-from)*(s.right-s.left)/(to-from),y=value=>s.top+(hi-value)*(s.bottom-s.top)/(hi-lo);
    for(const row of picked){s.ctx.strokeStyle=color(row.combination_id);s.ctx.lineWidth=row.combination_id===active?2.4:1.3;s.ctx.beginPath();sample(row.points,key).forEach((p,i)=>i?s.ctx.lineTo(x(p.time),y(p[key])):s.ctx.moveTo(x(p.time),y(p[key])));s.ctx.stroke();}
    s.ctx.fillStyle='#687a92';s.ctx.textAlign='left';s.ctx.fillText(new Date(from).toISOString().slice(0,10),s.left,s.h-10);s.ctx.textAlign='right';s.ctx.fillText(new Date(to).toISOString().slice(0,10),s.right,s.h-10);s.ctx.textAlign='left';
    geometry.set(id,{from,to,rows:picked,key,left:s.left,right:s.right});
  }
  function bars(id,rows,valueKey,labelKey){const s=setup(id);if(!s)return;if(!rows.length){empty(s);return;}const [lo,hi]=extent(rows.map(row=>row[valueKey]),true),y=value=>s.top+(hi-value)*(s.bottom-s.top)/(hi-lo),width=(s.right-s.left)/rows.length;axes(s,lo,hi,valueKey==='annualized_return'?percent:number);rows.forEach((row,i)=>{if(!numeric(row[valueKey]))return;const value=Number(row[valueKey]),top=y(Math.max(0,value)),height=Math.abs(y(value)-y(0));s.ctx.fillStyle=value>=0?'#098779':'#c33d52';s.ctx.fillRect(s.left+i*width+3,top,Math.max(1,width-6),height);s.ctx.fillStyle='#687a92';if(rows.length<16)s.ctx.fillText(String(row[labelKey]??i).slice(0,12),s.left+i*width+3,s.h-12);});}
  function scatter(){const s=setup('scatterChart');if(!s)return;const rows=charts.risk_return.filter(r=>numeric(r.max_drawdown)&&numeric(r.annualized_return));if(!rows.length){empty(s);return;}const [,mx]=extent(rows.map(r=>Math.abs(r.max_drawdown)),true),[lo,hi]=extent(rows.map(r=>r.annualized_return),true);axes(s,lo,hi,percent);for(const row of rows){s.ctx.fillStyle=row.combination_id===active?'#2261bf':row.qualified?'#098779':'#9ba9bb';s.ctx.beginPath();s.ctx.arc(s.left+Math.abs(row.max_drawdown)*(s.right-s.left)/mx,s.top+(hi-row.annualized_return)*(s.bottom-s.top)/(hi-lo),row.combination_id===active?7:4,0,2*Math.PI);s.ctx.fill();}s.ctx.fillStyle='#687a92';s.ctx.fillText('最大回撤绝对值：0 → '+percent(mx),s.left,s.h-10);}
  function heat(){const s=setup('heatmapChart'),heatmap=charts.parameter_heatmap;if(!s)return;if(!heatmap.cells.length){empty(s,'至少需要两个变化参数与有效评分');return;}const xs=[...new Set(heatmap.cells.map(c=>String(c.x)))],ys=[...new Set(heatmap.cells.map(c=>String(c.y)))],width=(s.right-s.left)/xs.length,height=(s.bottom-s.top)/ys.length;for(const cell of heatmap.cells){const x=s.left+xs.indexOf(String(cell.x))*width,y=s.top+ys.indexOf(String(cell.y))*height,score=Number(cell.score);s.ctx.fillStyle=`rgba(34,97,191,${.08+.82*Math.max(0,Math.min(100,score))/100})`;s.ctx.fillRect(x,y,width-3,height-3);s.ctx.fillStyle=score>60?'#fff':'#172c49';s.ctx.fillText(`${cell.x} / ${cell.y}: ${number(score)}`,x+8,y+height/2);}s.ctx.fillStyle='#687a92';s.ctx.fillText(heatmap.x_parameter+' × '+heatmap.y_parameter,s.left,s.h-10);}
  function draw(){
    const [lo,hi]=range();q('rangeStatus').textContent=lo>hi?'开始日期晚于结束日期':'图表与明细按所选日期筛选';
    q('chartLegend').replaceChildren();charts.equity_series.filter(row=>chosen().has(row.combination_id)).forEach(row=>{const label=element('span'),swatch=element('i');swatch.style.background=color(row.combination_id);label.append(swatch,document.createTextNode(row.combination_id));q('chartLegend').append(label);});
    line('equityChart',charts.equity_series,'equity');line('drawdownChart',charts.drawdown_series,'drawdown');scatter();heat();
    bars('foldChart',(charts.fold_stability.find(row=>row.combination_id===active)||{}).folds||[],'annualized_return','fold');
    const row=selected();bars('sideChart',[{side:'long',pnl:row.long_metrics?.pnl},{side:'short',pnl:row.short_metrics?.pnl}],'pnl','side');
    const pnl=(detail().trades||[]).map(t=>t.pnl).filter(numeric).map(Number);const [min,max]=extent(pnl),bins=Array.from({length:10},(_,i)=>({label:Math.round(min+(max-min)*i/10),count:0}));pnl.forEach(value=>bins[Math.min(9,Math.floor((value-min)/(max-min)*10))].count++);bars('tradeChart',pnl.length?bins:[],'count','label');
  }
  function schedule(){cancelAnimationFrame(pending);pending=requestAnimationFrame(draw);}
  for(const id of ['equityChart','drawdownChart']){
    q(id).addEventListener('pointermove',event=>{const g=geometry.get(id);if(!g)return;const rect=q(id).getBoundingClientRect(),time=g.from+(event.clientX-rect.left-g.left)/(g.right-g.left)*(g.to-g.from);const lines=[];for(const row of g.rows){if(!row.points.length)continue;let lo=0,hi=row.points.length-1;while(lo<hi){const mid=(lo+hi)>>1;if(Date.parse(row.points[mid].time)<time)lo=mid+1;else hi=mid;}const point=row.points[lo];lines.push(`${point.time.slice(0,10)}  ${row.combination_id}\n${g.key==='drawdown'?percent(point[g.key]):number(point[g.key])}`);}q('chartTooltip').textContent=lines.join('\n');q('chartTooltip').hidden=false;q('chartTooltip').style.left=Math.max(8,Math.min(event.clientX+14,innerWidth-350))+'px';q('chartTooltip').style.top=Math.max(8,Math.min(event.clientY+14,innerHeight-180))+'px';});
    q(id).addEventListener('pointerleave',()=>{q('chartTooltip').hidden=true;});
  }
  q('activeSelect').addEventListener('change',()=>{active=q('activeSelect').value;updateActive();});
  document.querySelectorAll('#comparison tbody tr').forEach(tr=>{tr.tabIndex=0;const select=()=>{active=tr.dataset.id;updateActive();};tr.addEventListener('click',select);tr.addEventListener('keydown',event=>{if(event.key==='Enter')select();});});
  q('curveSelect').addEventListener('change',schedule);
  for(const id of ['startDate','endDate'])q(id).addEventListener('change',()=>{page=0;renderLedger();schedule();});
  q('resetRange').addEventListener('click',()=>{q('startDate').value='';q('endDate').value='';renderLedger();schedule();});
  new ResizeObserver(schedule).observe(document.querySelector('main'));
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)schedule();});window.addEventListener('pageshow',schedule);
  updateActive();
})();
