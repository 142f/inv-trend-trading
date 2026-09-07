"""完全离线的中文滚动回测报告；只读取已冻结结果，不重新计算交易。"""
from __future__ import annotations
import html
import json
from pathlib import Path
import numpy as np
import pandas as pd


def esc(value):return html.escape(str(value))

def number(value, percent=False):
    if value is None or (isinstance(value,float) and not np.isfinite(value)):return '不适用'
    if isinstance(value,bool):return '通过' if value else '未通过'
    if isinstance(value,(int,np.integer)):return f'{int(value):,}'
    if isinstance(value,(float,np.floating)):return f'{value:.2%}' if percent else f'{value:,.3f}'
    return esc(value)


def table(rows,columns):
    head=''.join(f'<th>{esc(label)}</th>' for key,label,fmt in columns)
    body=''.join('<tr>'+''.join(f'<td>{number(row.get(key),fmt=="pct")}</td>' for key,label,fmt in columns)+'</tr>' for row in rows)
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def line_chart(series,title,percent=False):
    width,height,pad=980,300,58
    usable={k:pd.Series(v).dropna() for k,v in series.items() if len(v)}
    if not usable:return '<p>无可绘制记录</p>'
    x0=min(v.index[0].value for v in usable.values());x1=max(v.index[-1].value for v in usable.values())
    lo=min(float(v.min()) for v in usable.values());hi=max(float(v.max()) for v in usable.values())
    if abs(hi-lo)<1e-10:hi=lo+1
    span=hi-lo;lo-=span*.06;hi+=span*.06
    colors=['#215ca3','#c45632','#138278','#7d53aa','#7b6b36']
    parts=[f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{esc(title)}">']
    for y in np.linspace(lo,hi,5):
        yy=height-pad-(y-lo)/(hi-lo)*(height-2*pad)
        label=f'{y:.0%}' if percent else f'{y:,.0f}'
        parts.append(f'<path d="M {pad} {yy:.2f} H {width-pad}" stroke="#e3e8ee"/><text x="{pad-8}" y="{yy+4:.1f}" text-anchor="end">{label}</text>')
    for i,(name,v) in enumerate(usable.items()):
        points=' '.join(f'{pad+(t.value-x0)/max(x1-x0,1)*(width-2*pad):.2f},{height-pad-(float(y)-lo)/(hi-lo)*(height-2*pad):.2f}' for t,y in v.items())
        color=colors[i%len(colors)]
        parts.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="1.8"/>')
        parts.append(f'<text x="{pad+i*240}" y="25" fill="{color}">{esc(name)}</text>')
    for t,x,anchor in [(pd.Timestamp(x0,tz='UTC'),pad,'start'),(pd.Timestamp(x1,tz='UTC'),width-pad,'end')]:
        parts.append(f'<text x="{x}" y="{height-18}" text-anchor="{anchor}">{t.date()}</text>')
    parts.append('</svg>');return ''.join(parts)


def histogram(values,title):
    values=np.asarray(values,float)
    if not len(values):return '<p>没有已平仓交易。</p>'
    counts,edges=np.histogram(values,bins=min(15,max(3,int(np.sqrt(len(values))))))
    width,height,pad=980,260,55
    cols=[];step=(width-2*pad)/len(counts)
    for i,c in enumerate(counts):
        hh=float(c)/max(float(counts.max()),1)*(height-2*pad)
        color='#c45632' if edges[i+1]<=0 else '#138278'
        cols.append(f'<rect x="{pad+i*step+2:.1f}" y="{height-pad-hh:.1f}" width="{step-4:.1f}" height="{hh:.1f}" fill="{color}"/><text x="{pad+(i+.5)*step:.1f}" y="{height-pad-hh-6:.1f}" text-anchor="middle">{int(c)}</text>')
    cols.append(f'<text x="{pad}" y="{height-15}">净亏损端 {edges[0]:,.0f}</text><text x="{width-pad}" y="{height-15}" text-anchor="end">净盈利端 {edges[-1]:,.0f}</text>')
    return f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{esc(title)}">'+''.join(cols)+'</svg>'


def read_csv(path):
    from inv_trend.storage.研究结果_v3 import read_research_csv
    try:return read_research_csv(path)
    except pd.errors.EmptyDataError:return pd.DataFrame()


def render_report(output,path):
    from inv_trend.storage.研究结果_v3 import research_path
    output,path=research_path(output),Path(path)
    load=lambda name:json.loads((output/name).read_text(encoding='utf-8'))
    identity=load('实验协议冻结.json');cfg=identity['protocol'];records=load('完整迭代摘要.json')
    windows=load('窗口划分.json');training=load('完整训练摘要.json');gate=load('发布门禁.json')
    stats=load('多重比较审计.json');data_audit=load('数据审计.json');env=load('环境核验.json')
    stress=load('压力摘要.json') if (output/'压力摘要.json').exists() else []
    first,last=records[0],records[-1];m=last['metrics']
    metric_columns=[('iteration','迭代',''),('total_return','净收益','pct'),('annualized_return','年化收益','pct'),
        ('max_drawdown','最大回撤','pct'),('sharpe_ratio','Sharpe',''),('calmar_ratio','Calmar',''),
        ('win_rate','胜率','pct'),('payoff_ratio','盈亏比',''),('profit_factor','Profit Factor',''),
        ('trade_count','交易次数',''),('positive_fold_ratio','正收益窗口','pct'),('fee_cost','手续费',''),
        ('slippage_cost','滑点损耗',''),('carry_cost','融资/借用费','')]
    results_table=table([{'iteration':r['iteration'],**r['metrics']} for r in records],metric_columns)
    series={}
    for r in records:
        f=read_csv(output/'样本外证据'/r['iteration']/'权益曲线.csv.gz')
        series[r['iteration']]=pd.Series(f.iloc[:,1].to_numpy(),index=pd.to_datetime(f.iloc[:,0],utc=True)).resample('1D').last().ffill()
    major={first['iteration']+'：冻结v4基准':series[first['iteration']],last['iteration']+'：研究候选池':series[last['iteration']]}
    if first is last:major={first['iteration']:series[first['iteration']]}
    dd=lambda eq:eq/np.maximum(eq.cummax(),100000.)-1
    curve=line_chart(major,'连续资金权益曲线')
    drawdown=line_chart({k:dd(v) for k,v in major.items()},'回撤曲线',True)
    per_iteration=[]
    for r in records:
        rows=[]
        for fold,lock in zip(r['folds'],r['locks']):
            ranked=next((x for x in lock['ranking'] if x['candidate_id']==lock['candidate_id']),{})
            rows.append({'window':fold['window_id'],'start':fold['test_start'][:10],
                'end':fold['test_end_exclusive'][:10],'candidate':lock['candidate_id'],
                'train_robustness':ranked.get('positive_inner_ratio'),**fold['metrics']})
        content=table(rows,[('window','窗口',''),('start','执行开始',''),('end','执行结束（不含）',''),
            ('candidate','训练锁定参数',''),('train_robustness','训练内正收益段','pct'),
            ('total_return','样本外净收益','pct'),('max_drawdown','回撤','pct'),('sharpe_ratio','Sharpe',''),('trade_count','交易','')])
        chart=line_chart({r['iteration']:series[r['iteration']]},r['iteration']+'资金曲线')
        per_iteration.append(f'<details><summary>{esc(r["iteration"])} · {esc(r["change"])} · 未升级</summary>{content}{chart}<p>较前轮收益变化 {number(r["delta_previous"].get("total_return"),True)}；较冻结基准 {number(r["delta_original"].get("total_return"),True)}。差异仅作诊断，不用于事后升级。</p></details>')
    folder=output/'样本外证据'/last['iteration'];trades=read_csv(folder/'交易记录.csv.gz');orders=read_csv(folder/'订单记录.csv.gz')
    attribution=read_csv(folder/'品种归因.csv.gz') if (folder/'品种归因.csv.gz').exists() else pd.DataFrame()
    ledger=read_csv(folder/'资金流水.csv.gz')
    instrument=attribution.groupby('symbol',sort=True).tail(1).to_dict('records') if len(attribution) else []
    instrument_table=table(instrument,[('symbol','品种',''),('realized_pnl','已实现净损益',''),('open_net_pnl','未平仓净损益',''),('total_pnl','权益贡献','')])
    sides=[]
    for side,label in [(1,'多头'),(-1,'空头')]:
        part=trades[trades.side==side] if len(trades) else trades
        pnl=part.pnl if len(part) else pd.Series(dtype=float)
        sides.append(dict(label=label,count=len(part),net=float(pnl.sum()),win=float(pnl.gt(0).mean()) if len(part) else None))
    side_table=table(sides,[('label','方向',''),('count','已平仓交易',''),('net','净损益',''),('win','胜率','pct')])
    train_rows=[]
    for window in windows:
        for c in cfg['candidates']:
            r=training.get(window['window_id'],{}).get(c['id'])
            if r is None:continue
            slices=r['slices'];valid_sr=[x['sharpe_ratio'] for x in slices if x['sharpe_ratio'] is not None]
            train_rows.append(dict(window=window['window_id'],candidate=c['id'],name=c['name'],
                median_return=float(np.median([x['total_return'] for x in slices])),
                median_sharpe=float(np.median(valid_sr)) if valid_sr else None,
                positive=float(np.mean([x['total_return']>0 for x in slices])),
                worst_drawdown=max(x['max_drawdown'] for x in slices)))
    train_table=table(train_rows,[('window','所属训练窗口',''),('candidate','候选',''),('name','规则',''),
        ('median_return','训练内分段净收益中位数','pct'),('median_sharpe','训练内Sharpe中位数',''),
        ('positive','训练内正收益段','pct'),('worst_drawdown','最差分段回撤','pct')])
    parameter_table=table([dict(id=c['id'],name=c['name'],rules=json.dumps(c['rules'],ensure_ascii=False),
        specs=json.dumps(c['spec_overrides'],ensure_ascii=False),take=c['take_profit_n']) for c in cfg['candidates']],
        [('id','编号',''),('name','策略差异',''),('rules','完整规则',''),('specs','仓位差异',''),('take','触价市价止盈N','')])
    gate_table=table([dict(name=k,passed=v) for k,v in gate['gates'].items()],[('name','发布要求',''),('passed','判定','')])
    stress_table=table([dict(iteration=x['scenario']['name'],**x['metrics']) for x in stress],metric_columns)
    stat_table=table([dict(iteration=x['iteration'],p0=x['adjusted_p_vs_zero'],pd=x['adjusted_p_vs_baseline'],
        lo=x['daily_mean_ci95'][0],hi=x['daily_mean_ci95'][1]) for x in stats['results']],
        [('iteration','迭代',''),('p0','相对零均值校正p值',''),('pd','相对基准校正p值',''),
         ('lo','日均收益95%下界','pct'),('hi','日均收益95%上界','pct')])
    eq=series[last['iteration']];returns=eq.pct_change().fillna(eq.iloc[0]/100000.-1)
    years=[];capital=100000.
    for year,part in eq.groupby(eq.index.year):
        years.append(dict(year=str(year),net=float(part.iloc[-1]/capital-1),
                          dd=float(-(part/np.maximum(part.cummax(),capital)-1).min())))
        capital=float(part.iloc[-1])
    year_table=table(years,[('year','年度',''),('net','净收益','pct'),('dd','年度内最大回撤','pct')])
    worst=table([dict(day=str(t.date()),net=float(x)) for t,x in returns.nsmallest(8).items()],
                [('day','较差交易日（事后归因）',''),('net','组合日收益','pct')])
    allocation=table([dict(candidate=k,count=v) for k,v in pd.Series([x['candidate_id'] for x in last['locks']]).value_counts().items()],
                     [('candidate','训练选择结果',''),('count','窗口数量','')])
    window_table=table([dict(id=x['window_id'],a=x['train_start'][:10],b=x['train_end'][:10],c=x['test_start'][:10],d=x['test_end_exclusive'][:10]) for x in windows],
        [('id','窗口',''),('a','训练开始',''),('b','训练结束（不含）',''),('c','样本外开始',''),('d','样本外结束（不含）','')])
    env_table=table([dict(name=k,**v) for k,v in env['packages'].items()], [('name','依赖',''),('version','实际版本',''),('available','可用性','')])
    evidence=table([dict(kind='行情内容',hash=cfg['dataset_sha256']),dict(kind='执行源码',hash=identity['source_sha256'])],
                   [('kind','身份',''),('hash','SHA256','')])
    framework=[]
    # 可选的工程轮次摘要由交付汇总注入；复现入口不伪造历史测试次数。
    framework_path=output/'框架迭代汇总.json'
    if framework_path.exists():framework=json.loads(framework_path.read_text())
    framework_html=table(framework,[('round','框架轮次',''),('change','修改与验证',''),('tests','测试',''),
        ('return','固定基准净收益','pct'),('drawdown','最大回撤','pct'),('sharpe','Sharpe',''),('carry','融资费','')]) if framework else '<p>此运行记录未携带工程轮次汇总；完整工程测试见统一修改说明和独立证据包。</p>'
    fulltests=''
    if (output/'全量回归摘要.json').exists():
        fulltests='<pre>'+esc(json.dumps(load('全量回归摘要.json'),ensure_ascii=False,indent=2))+'</pre>'
    limitations=''.join('<li>'+esc(x)+'</li>' for x in cfg['limitations'])
    distribution=histogram(trades.pnl if len(trades) else [],'扣除费用后的交易盈亏分布')
    body=f'''
<header><div class="eyebrow">可复现研究 · 版本 v2 · 历史滚动验证</div><h1>滚动回测与执行可信度审计</h1>
<p>一个执行器 · 连续资金账户 · 训练内选参 · 窗口前锁定 · 全候选留痕</p>
<div class="banner">未批准策略升级／未获得实盘认证。历史为研究者已见数据，不伪装成新的盲测。</div></header>
<nav><a href="#conclusion">发布门禁</a><a href="#method">滚动协议</a><a href="#iterations">全部迭代</a><a href="#curves">资金曲线</a><a href="#risk">风险与压力</a><a href="#evidence">审计身份</a></nav>
<main><section id="conclusion"><h2>01 · 结论与发布门禁</h2>
<div class="cards"><div><span>最后研究轮净收益</span><strong>{number(m['total_return'],True)}</strong></div><div><span>最大回撤</span><strong>{number(m['max_drawdown'],True)}</strong></div><div><span>Sharpe / Calmar</span><strong>{number(m['sharpe_ratio'])} / {number(m['calmar_ratio'])}</strong></div><div><span>正收益窗口</span><strong>{number(m['positive_fold_ratio'],True)}</strong></div></div>
<p>展示对象预先指定为最后研究轮，不是事后收益冠军。此前冻结的v4仍是参考基准；没有候选通过全部门槛时不强行宣告“最佳生产策略”。</p>{gate_table}</section>
<section id="method"><h2>02 · 时间协议与策略说明</h2><div class="flow">完整历史前缀 → 过去3年内分3段评估 → 仅训练记录选参 → SHA256参数锁 → 未来6个月逐事件执行 → 下一窗口</div>
<p>训练与外层之间隔离7个日历日。13个外层窗口自2020年1月推进至2026年4月。第一轮固定v4作为基准；后续轮次在逐步扩展的预注册候选池内选参，训练门槛不满足则持有现金。每次换窗使用同一账户和剩余资金；旧仓在下一真实开盘退出并计费，不重置本金。</p>
<p>MA、ADX、连续确认、ATR历史分位和移位突破通道构成趋势证据。ATR用于风险单位，不被当成独立方向投票。所有候选与增量假设在批量外层运行前已定义；观察外层后没有增加新的收益参数。</p>{window_table}
<details><summary>完整候选参数与风险预算</summary>{parameter_table}</details>
<p>成交假设：股票使用纽约常规交易时段，金属使用UTC日线区间；二者都未获得来源认证。下一Bar开盘成交，滑点进入买卖成交价；跳空按开盘处理；同Bar止盈止损双触发优先止损；OHLC区间触发在收盘确认入账，不伪造具体分钟。全额抵押研究账户、总名义敞口上限1倍，融资按实际日历时间计费；股票无借券清单则禁空。开盘参与率只读取此前已完成Bar成交量。</p></section>
<section><h2>03 · 回测框架迭代与测试</h2>{framework_html}{fulltests}<h3>依赖环境</h3>{env_table}<p>未使用Parquet仿真层。依赖缺失、旧接口回归失败和市场认证缺失不会因为研究链路可运行而被写成“全项目通过”。</p></section>
<section id="iterations"><h2>04 · 全部策略迭代</h2>{results_table}<p>收益、风险或稳定性下降的轮次同样保留。下表中的外层表现不能用于再次选择历史冠军；“比较”与“正式升级”是两回事。</p>{''.join(per_iteration)}</section>
<section id="curves"><h2>05 · 连续资金曲线与回撤</h2>{curve}{drawdown}<p>初始资金100,000。收益采用完整终点净权益，包含未平仓盈亏；胜率与Profit Factor仅按已平仓交易计算。年化按实际时间，Sharpe基于365.2425日历日净收益且无风险利率为0。不同持仓状态下不能只用已实现交易总和代替完整账户收益。</p></section>
<section><h2>06 · 样本内、样本外与参数稳定性</h2>{allocation}<p>最后一轮换参比例 {number(m['selection_change_ratio'],True)}；现金窗口 {m['cash_window_count']}。训练分段净收益中位数与外层半年度收益不是同一时长，不能直接相减认定泛化能力。</p>
<details><summary>全部训练窗口×参数组合摘要（不删负收益）</summary>{train_table}</details><p>MA40/60/80、ADX15/20/25用于预设邻域诊断。其他候选是结构差异，不冒充局部参数邻域。缺少足够平坦、跨窗口一致的正收益区域时，不声称参数稳健。</p>{stat_table}<p>{esc(stats['method'])}。{esc(stats['caveat'])}。这些区间和p值是历史诊断，不是未来盈利概率。</p></section>
<section><h2>07 · 交易分布、多空表现与单品种贡献</h2>{distribution}{side_table}{instrument_table}<p>品种贡献包含已实现损益与期末未平仓净损益，合计应与终点权益减初始资金一致。空头仅允许已声明可空的研究资产；这不是证明真实借券或交易所撮合可得。</p></section>
<section id="risk"><h2>08 · 成本、压力与市场阶段</h2><p>最后研究轮手续费 {number(m['fee_cost'])}、滑点损耗 {number(m['slippage_cost'])}、融资/借用费 {number(m['carry_cost'])}。滑点已进入成交价和持仓盈亏，不再作为同额现金费用重复扣款。</p>{stress_table}
<p>压力测试复用已锁定的参数与选择路径，不按压力结果重新训练。成本、延迟和它们的组合全部保留；不将其中最高收益情景作为新策略。</p>{year_table}{worst}
<p>年度与最差交易日属于描述性归因，不参与训练筛选。极端跳空、资金约束、双向滑点及资金对账另外由合成反例测试覆盖；不能代替未提供的盘口、停牌、半日市和券商保证金数据。</p></section>
<section id="evidence"><h2>09 · 数据身份、限制与复现</h2>{evidence}<p>协议登记时间：{esc(cfg['registered_at'])}。全部候选、训练区间、外层区间、锁定记录、源码身份和每次回放原始账本在证据包中保留。</p><ul>{limitations}</ul>
<p>本报告不保证未来盈利。能够修复的是可识别的时间顺序、会计、参数流向与审计缺陷；研究者看过的历史数据无法通过重命名变成未见数据。只有新的、预先登记且没有参与规则设计的前瞻数据及完整市场认证，才可能解除当前发布阻断。</p>
<details><summary>数据质量摘要</summary><pre>{esc(json.dumps(data_audit,ensure_ascii=False,indent=2))}</pre></details></section></main>
<footer>滚动策略_v2 · 离线报告 · 不联网 · 不修改参数 · 不生成实盘订单</footer>'''
    css='''*{box-sizing:border-box}body{margin:0;background:#eef2f6;color:#172335;font:15px/1.75 system-ui,"Noto Sans CJK SC","Microsoft YaHei",sans-serif}header{background:#142a43;color:#fff;padding:46px max(5vw,24px) 32px}.eyebrow{letter-spacing:.13em;color:#91bacd;font-size:13px}h1{font-size:34px;line-height:1.3;margin:12px 0}header p{color:#d5e2ea}.banner{background:#614329;padding:12px 18px;border-radius:7px;margin-top:18px}nav{position:sticky;top:0;background:#fff;z-index:5;display:flex;gap:22px;flex-wrap:wrap;padding:13px 5vw;box-shadow:0 1px 5px #0002}nav a{color:#245681;text-decoration:none}main{max-width:1250px;margin:28px auto;padding:0 20px}section{background:#fff;padding:28px 30px;margin:24px 0;border-radius:10px;box-shadow:0 3px 12px #142a4308;scroll-margin-top:65px}h2{font-size:23px;margin:0 0 18px;color:#153a55}h3{font-size:18px}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:15px;margin:20px 0}.cards>div{background:#f0f5f8;border-top:3px solid #3d7b92;padding:18px}.cards span{display:block;font-size:12px;color:#526773}.cards strong{display:block;font-size:25px;margin-top:7px}.flow{background:#eaf3f5;border-left:4px solid #347e8e;padding:17px;font-weight:600}.table-wrap{overflow:auto;margin:18px 0;max-height:630px;border:1px solid #dce4eb;border-radius:6px}table{border-collapse:collapse;width:100%;font-size:12px}th{background:#e8eff4;position:sticky;top:0;text-align:left;color:#284761}th,td{padding:10px 12px;border-bottom:1px solid #e4eaf0;white-space:nowrap}td:last-child{max-width:750px;white-space:normal;overflow-wrap:anywhere}tr:nth-child(even){background:#f7f9fb}svg{width:100%;height:auto;background:#fafcfd;border:1px solid #e3e9ef;margin:12px 0}svg text{font:12px system-ui,"Noto Sans CJK SC",sans-serif;fill:#526575}details{margin:15px 0;border:1px solid #dfe7ed;border-radius:6px;padding:12px 15px}summary{cursor:pointer;font-weight:600;color:#24475f}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.7 ui-monospace,monospace;background:#f4f7f9;padding:16px}p{max-width:1080px}li{margin:8px 0}footer{text-align:center;padding:30px;color:#617484}@media(max-width:760px){h1{font-size:26px}header{padding:28px 20px}.cards{grid-template-columns:repeat(2,1fr)}section{padding:20px 16px}main{padding:0 10px}nav{gap:12px;font-size:12px}.cards strong{font-size:21px}}@media print{nav{position:static}section{break-inside:auto;box-shadow:none}body{background:white}.table-wrap{max-height:none;overflow:visible}details{display:block}}'''
    page='<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>中文滚动回测与可信度审计_v2</title><style>'+css+'</style></head><body>'+body+'</body></html>'
    path.write_text(page,encoding='utf-8');return path
