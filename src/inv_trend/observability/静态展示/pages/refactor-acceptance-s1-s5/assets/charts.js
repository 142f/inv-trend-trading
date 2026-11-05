(function () {
  var style = getComputedStyle(document.documentElement);
  var accent = style.getPropertyValue('--accent').trim();
  var accent2 = style.getPropertyValue('--accent2').trim();
  var ink = style.getPropertyValue('--ink').trim();
  var muted = style.getPropertyValue('--muted').trim();
  var rule = style.getPropertyValue('--rule').trim();
  var bg2 = style.getPropertyValue('--bg2').trim();

  // --- Chart: package convergence ---
  var convergence = echarts.init(document.getElementById('chart-convergence'), null, { renderer: 'svg' });
  convergence.setOption({
    animation: false,
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis', appendToBody: true, borderColor: rule, textStyle: { color: ink } },
    legend: {
      top: 0,
      left: 'center',
      itemGap: 28,
      itemWidth: 12,
      itemHeight: 12,
      textStyle: { color: muted, fontSize: 12 }
    },
    grid: { left: 8, right: 8, top: 58, bottom: 34, containLabel: true },
    xAxis: {
      type: 'category',
      data: ['起点', 'P1', 'P2', 'P3', 'P4', 'P5', 'P6 本轮'],
      axisLine: { lineStyle: { color: rule } },
      axisTick: { show: false },
      axisLabel: { color: muted, fontSize: 12, interval: 0 }
    },
    yAxis: [
      {
        type: 'value',
        min: 30,
        max: 44,
        splitNumber: 4,
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: muted, fontSize: 11 },
        splitLine: { lineStyle: { color: rule, type: 'dashed' } }
      },
      {
        type: 'value',
        min: 0,
        max: 4,
        splitNumber: 4,
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: muted, fontSize: 11 },
        splitLine: { show: false }
      }
    ],
    series: [
      {
        name: '双向包对（右轴，个）',
        type: 'bar',
        yAxisIndex: 1,
        barWidth: 26,
        data: [4, 3, 3, 3, 2, 1, 0],
        itemStyle: { color: accent2, opacity: 0.85, borderRadius: [2, 2, 0, 0] },
        label: { show: true, position: 'top', color: accent2, fontSize: 11, fontWeight: 700 }
      },
      {
        name: '包级依赖边（左轴，条）',
        type: 'line',
        smooth: false,
        symbol: 'circle',
        symbolSize: 8,
        data: [37, 39, 39, 38, 37, 35, 40],
        itemStyle: { color: accent },
        lineStyle: { color: accent, width: 2 },
        label: { show: true, position: 'bottom', color: accent, fontSize: 11, fontWeight: 700 }
      }
    ]
  });
  window.addEventListener('resize', function () { convergence.resize(); });

  // --- Chart: targeted suites ---
  var suites = echarts.init(document.getElementById('chart-suites'), null, { renderer: 'svg' });
  var suiteNames = [
    '增量发布 + Windows 锁',
    '目录故障分类',
    '架构契约',
    '监控看板',
    '结构化存储 v3',
    '数据封版',
    'tests/unit'
  ].reverse();
  var suiteValues = [26, 27, 13, 33, 41, 93, 187].reverse();
  suites.setOption({
    animation: false,
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'item',
      appendToBody: true,
      borderColor: rule,
      textStyle: { color: ink },
      formatter: function (p) { return p.name + '<br/>通过 ' + p.value + ' 项'; }
    },
    grid: { left: 8, right: 26, top: 12, bottom: 8, containLabel: true },
    xAxis: {
      type: 'value',
      max: 200,
      axisLine: { lineStyle: { color: rule } },
      axisTick: { show: false },
      axisLabel: { color: muted, fontSize: 11 },
      splitLine: { lineStyle: { color: rule, type: 'dashed' } }
    },
    yAxis: {
      type: 'category',
      data: suiteNames,
      axisLine: { lineStyle: { color: rule } },
      axisTick: { show: false },
      axisLabel: { color: ink, fontSize: 12 }
    },
    series: [
      {
        name: '通过用例',
        type: 'bar',
        barWidth: 16,
        data: suiteValues,
        itemStyle: { color: accent, borderRadius: [0, 2, 2, 0] },
        label: { show: true, position: 'right', color: accent, fontSize: 11, fontWeight: 700 }
      }
    ]
  });
  window.addEventListener('resize', function () { suites.resize(); });

  // --- Diagrams ---
  if (window.mermaid) {
    mermaid.initialize({ startOnLoad: true, theme: 'neutral', securityLevel: 'loose' });
  }
})();
