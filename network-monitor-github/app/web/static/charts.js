/* 轻量 SVG 图表工具（零依赖，离线可用）
 * lineChart(el, points, opts)   折线图   points: [{x:'HH:MM', y:number|null}]
 * sparkbar(el, points, colorFn) 状态条   points: [{x:label, v:any}]  v 经 colorFn 映射颜色
 * timelineBar(el, segs)         分段时间条 segs: [{label, ms, color}]
 * fmtMs(ms)                     毫秒格式化
 */

function escHtml(s) {
  return String(s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
}

function fmtMs(ms) {
  if (ms === null || ms === undefined || isNaN(ms)) return '-';
  return ms >= 1000 ? (ms / 1000).toFixed(1) + 's' : Math.round(ms) + 'ms';
}

/* 折线图：自适应容器宽度，y=null 断线；opts: {color, height, yMin, yMax, unit} */
function lineChart(el, points, opts = {}) {
  if (!el) return;
  const H = opts.height || 200, padL = 46, padR = 12, padT = 14, padB = 24;
  const W = Math.max(el.clientWidth || 600, 300);
  const ys = points.map(p => p.y).filter(v => v !== null && v !== undefined && !isNaN(v));
  let yMin = opts.yMin !== undefined ? opts.yMin : Math.min(...(ys.length ? ys : [0]));
  let yMax = opts.yMax !== undefined ? opts.yMax : Math.max(...(ys.length ? ys : [1]));
  if (yMax === yMin) yMax = yMin + 1;
  const span = yMax - yMin;
  yMin -= span * 0.08; yMax += span * 0.08;          // 上下留白
  const X = i => padL + (points.length <= 1 ? 0 : (W - padL - padR) * i / (points.length - 1));
  const Y = v => padT + (H - padT - padB) * (1 - (v - yMin) / (yMax - yMin));
  let grid = '', path = '', dots = '', labels = '';
  for (let g = 0; g <= 4; g++) {
    const v = yMin + (yMax - yMin) * g / 4, y = Y(v);
    grid += `<line x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}" class="chart-grid"/>` +
      `<text x="${padL - 6}" y="${y + 4}" text-anchor="end" class="axis-txt">${(Math.round(v * 10) / 10)}</text>`;
  }
  let started = false, d = '';
  points.forEach((p, i) => {
    if (p.y === null || p.y === undefined || isNaN(p.y)) { started = false; return; }
    d += (started ? 'L' : 'M') + X(i).toFixed(1) + ' ' + Y(p.y).toFixed(1) + ' ';
    started = true;
  });
  if (d) path = `<path d="${d}" fill="none" stroke="${opts.color || 'var(--primary)'}" stroke-width="2" stroke-linejoin="round"/>`;
  points.forEach((p, i) => {
    if (p.y === null || p.y === undefined || isNaN(p.y)) return;
    dots += `<circle cx="${X(i).toFixed(1)}" cy="${Y(p.y).toFixed(1)}" r="2.5" fill="${opts.color || 'var(--primary)'}"><title>${escHtml(p.x)}　${p.y}${opts.unit || ''}</title></circle>`;
  });
  const step = Math.max(1, Math.ceil(points.length / 8));
  points.forEach((p, i) => {
    if (i % step === 0) labels += `<text x="${X(i)}" y="${H - 6}" text-anchor="middle" class="axis-txt">${escHtml(p.x)}</text>`;
  });
  el.innerHTML = `<svg width="${W}" height="${H}">${grid}${path}${dots}${labels}</svg>`;
}

/* 状态条：每轮采集一个小方块，颜色由 colorFn(v) 决定 */
function sparkbar(el, points, colorFn) {
  if (!el) return;
  const W = Math.max(el.clientWidth || 600, 300), H = 40, gap = 1.5;
  const n = points.length;
  if (!n) { el.innerHTML = '<span class="muted">暂无数据</span>'; return; }
  const bw = Math.max((W - n * gap) / n, 1);
  const bars = points.map((p, i) =>
    `<rect x="${(i * (bw + gap)).toFixed(1)}" y="6" width="${bw.toFixed(1)}" height="${H - 16}" rx="2" fill="${colorFn(p.v)}"><title>${escHtml(p.x)}</title></rect>`).join('');
  el.innerHTML = `<svg width="100%" height="${H}" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${bars}</svg>`;
}

/* 分段时间条（T1~T4 时间线）：segs: [{label, ms, color}] 按时长占比横铺 */
function timelineBar(el, segs) {
  if (!el) return;
  const total = segs.reduce((s, x) => s + Math.max(x.ms || 0, 0), 0);
  if (!total) { el.innerHTML = '<span class="muted">暂无时间线数据</span>'; return; }
  let bars = '', legend = '', x = 0;
  segs.forEach(s => {
    const ms = Math.max(s.ms || 0, 0);
    if (!ms) return;
    const w = ms / total * 100;
    bars += `<rect x="${x.toFixed(2)}%" y="6" width="${w.toFixed(2)}%" height="22" rx="3" fill="${s.color}"><title>${escHtml(s.label)}：${fmtMs(ms)}</title></rect>`;
    x += w;
    legend += `<span class="tl-legend"><i style="background:${s.color}"></i>${escHtml(s.label)} <b>${fmtMs(ms)}</b></span>`;
  });
  el.innerHTML = `<svg width="100%" height="34">${bars}</svg><div class="tl-legend-row">${legend}</div>`;
}
