/* 数据驱动拓扑渲染工具（仪表盘 / 拓扑管理页共用）
 *
 * 关键点：节点一律用设备 id 作 key（node-<id>、badge-<id>），不再用设备名匹配，
 * 这样更换 eNSP 拓扑与设备后，节点和连线都会跟着数据自动变，无需改代码。
 *
 * 提供：
 *   topoAutoLayout(nodes)                   按角色分层的自动布局
 *   renderTopo(svgEl, nodes, links)         全量重建几何 + 状态
 *   applyTopoStatus(nodes)                  只刷新在线状态与 VRRP 徽章（不重建 DOM）
 *   topoAttachDrag(svgEl, nodes, onMove, onEnd)  拖拽节点改坐标
 */
const TOPO_NS = 'http://www.w3.org/2000/svg';
const TOPO_W = 1000, TOPO_H = 400;
const ROLE_CN = {core: '核心', aggregation: '汇聚', access: '接入', host: '终端', other: ''};
const ROLE_LAYER = {core: 0, aggregation: 1, access: 2, other: 2, host: 3};

const _topo = {svg: null, nodes: [], links: [], pos: {}};

function topoSvgEl(tag, attrs, text) {
  const e = document.createElementNS(TOPO_NS, tag);
  for (const k in attrs) {
    const v = attrs[k];
    if (v !== null && v !== undefined) e.setAttribute(k, v);
  }
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}

function topoNodeSize(role) {
  if (role === 'core') return {w: 96, h: 46};
  if (role === 'aggregation') return {w: 92, h: 34};
  if (role === 'access') return {w: 84, h: 30};
  if (role === 'host') return {w: 64, h: 26};
  return {w: 86, h: 34};
}

function topoNodeClass(n) {
  if (n.role === 'host') return 'tnode t-host';
  if (!n.enabled) return 'tnode t-disabled';
  if (n.online === 1) return 'tnode t-online';
  if (n.online === 0) return 'tnode t-offline';
  return 'tnode';
}

function topoCaption(n) {
  if (n.role === 'host') return n.name;
  const r = ROLE_CN[n.role] || '';
  return r ? n.name + ' ' + r : n.name;
}

/* 按角色分层自动布局：核心在最上、汇聚居中、接入与终端在下 */
function topoAutoLayout(nodes) {
  const groups = {};
  nodes.forEach(n => {
    const l = (n.role in ROLE_LAYER) ? ROLE_LAYER[n.role] : 2;
    (groups[l] = groups[l] || []).push(n);
  });
  const pos = {};
  Object.keys(groups).forEach(l => {
    const arr = groups[l], y = 64 + Number(l) * 96;
    arr.forEach((n, i) => {
      pos[n.id] = {x: Math.round(TOPO_W * (i + 1) / (arr.length + 1)), y: y};
    });
  });
  return pos;
}

function topoClamp(role, p) {
  const s = topoNodeSize(role);
  return {
    x: Math.min(Math.max(p.x, s.w / 2 + 6), TOPO_W - s.w / 2 - 6),
    y: Math.min(Math.max(p.y, s.h / 2 + 6), TOPO_H - s.h / 2 - 6),
  };
}

/* 有坐标用坐标，没坐标的用自动布局兜底 —— 保证新加的设备一定出现在图上 */
function topoResolve(nodes) {
  const auto = topoAutoLayout(nodes);
  const pos = {};
  nodes.forEach(n => {
    const has = n.pos_x !== null && n.pos_x !== undefined &&
                n.pos_y !== null && n.pos_y !== undefined;
    pos[n.id] = topoClamp(n.role, has ? {x: Number(n.pos_x), y: Number(n.pos_y)} : auto[n.id]);
  });
  return pos;
}

function topoLinkLayer(nodes, pos) {
  const g = topoSvgEl('g', {class: 'tlink-layer'});
  _topo.links.forEach(lk => {
    const a = pos[lk.from_id], b = pos[lk.to_id];
    if (!a || !b) return;
    if (lk.style === 'eth-trunk') {
      const dx = b.x - a.x, dy = b.y - a.y;
      const len = Math.sqrt(dx * dx + dy * dy) || 1;
      const ox = -dy / len * 3, oy = dx / len * 3;
      [1, -1].forEach(s => g.appendChild(topoSvgEl('line', {
        x1: a.x + ox * s, y1: a.y + oy * s, x2: b.x + ox * s, y2: b.y + oy * s, class: 'tlink'})));
    } else {
      g.appendChild(topoSvgEl('line', {x1: a.x, y1: a.y, x2: b.x, y2: b.y, class: 'tlink'}));
    }
    if (lk.label) {
      g.appendChild(topoSvgEl('text', {
        x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 - 5,
        'text-anchor': 'middle', class: 'tlink-label'}, lk.label));
    }
  });
  return g;
}

function renderTopo(svgEl, nodes, links) {
  _topo.svg = svgEl;
  _topo.nodes = nodes || [];
  _topo.links = links || [];
  _topo.pos = topoResolve(_topo.nodes);
  svgEl.textContent = '';
  svgEl.appendChild(topoLinkLayer(_topo.nodes, _topo.pos));
  _topo.nodes.forEach(n => {
    if (!n.topo_visible) return;              // 隐藏的设备不画
    const s = topoNodeSize(n.role), p = _topo.pos[n.id], isHost = n.role === 'host';
    n._p = p;
    const g = topoSvgEl('g', {id: 'node-' + n.id, class: topoNodeClass(n), 'data-id': n.id});
    g.appendChild(topoSvgEl('rect', {
      x: p.x - s.w / 2, y: p.y - s.h / 2, width: s.w, height: s.h,
      rx: n.role === 'core' ? 8 : 6}));
    g.appendChild(topoSvgEl('text', {
      x: p.x, y: p.y + 5, 'text-anchor': 'middle',
      class: isHost ? 'thost-txt' : 'tnode-txt'}, topoCaption(n)));
    if (!isHost) {
      g.appendChild(topoSvgEl('circle', {
        id: 'badge-' + n.id, class: 'tbadge', cx: p.x + s.w / 2, cy: p.y - s.h / 2,
        r: 11, style: 'display:none'}));
      g.appendChild(topoSvgEl('text', {
        id: 'badge-t-' + n.id, x: p.x + s.w / 2, y: p.y - s.h / 2 + 4,
        'text-anchor': 'middle', class: 'tbadge-txt', style: 'display:none'}, 'M'));
    }
    svgEl.appendChild(g);
  });
  applyTopoStatus(_topo.nodes);
}

/* 每轮轮询只更新状态与徽章，几何不动，避免打断正在进行的拖拽 */
function applyTopoStatus(nodes) {
  nodes.forEach(n => {
    const g = document.getElementById('node-' + n.id);
    if (g) {
      const sel = g.classList.contains('sel');
      g.setAttribute('class', topoNodeClass(n) + (sel ? ' sel' : ''));
    }
    const b = document.getElementById('badge-' + n.id);
    const bt = document.getElementById('badge-t-' + n.id);
    if (b && bt) {
      // 离线/未采到数据时 VRRP 状态未知，徽章一律隐藏，避免显示过期的 M 计数
      const m = n.online ? (n.vrrp || []).filter(v => v.state === 'Master').length : 0;
      b.style.display = bt.style.display = m ? '' : 'none';
      bt.textContent = 'M' + m;
    }
  });
}

function topoMoveNodeGeom(n) {
  const g = document.getElementById('node-' + n.id);
  if (!g || !n._p) return;
  const s = topoNodeSize(n.role), p = n._p;
  _topo.pos[n.id] = p;
  const rect = g.querySelector('rect');
  rect.setAttribute('x', p.x - s.w / 2);
  rect.setAttribute('y', p.y - s.h / 2);
  const txt = g.querySelector('.tnode-txt') || g.querySelector('.thost-txt');
  if (txt) { txt.setAttribute('x', p.x); txt.setAttribute('y', p.y + 5); }
  const c = g.querySelector('circle');
  if (c) { c.setAttribute('cx', p.x + s.w / 2); c.setAttribute('cy', p.y - s.h / 2); }
  const bt = g.querySelector('.tbadge-txt');
  if (bt) { bt.setAttribute('x', p.x + s.w / 2); bt.setAttribute('y', p.y - s.h / 2 + 4); }
  // 只重画连线层，节点元素保持不动（否则会丢失拖拽的指针捕获）
  const old = _topo.svg.querySelector('.tlink-layer');
  const layer = topoLinkLayer(_topo.nodes, _topo.pos);
  if (old) _topo.svg.replaceChild(layer, old);
  else _topo.svg.insertBefore(layer, _topo.svg.firstChild);
}

function topoAttachDrag(svgEl, nodes, onMove, onEnd) {
  const byId = {};
  nodes.forEach(n => { byId[n.id] = n; });
  let drag = null;

  function toSvg(ev) {
    const pt = svgEl.createSVGPoint();
    pt.x = ev.clientX;
    pt.y = ev.clientY;
    return pt.matrixTransform(svgEl.getScreenCTM().inverse());
  }

  svgEl.addEventListener('pointerdown', ev => {
    const g = ev.target.closest && ev.target.closest('g[id^="node-"]');
    if (!g) return;
    const n = byId[g.getAttribute('data-id')];
    if (!n || !n._p) return;
    const p = toSvg(ev);
    drag = {n: n, el: g, dx: p.x - n._p.x, dy: p.y - n._p.y};
    g.classList.add('sel');
    g.setPointerCapture(ev.pointerId);
    ev.preventDefault();
  });

  svgEl.addEventListener('pointermove', ev => {
    if (!drag) return;
    const p = toSvg(ev);
    const c = topoClamp(drag.n.role, {x: p.x - drag.dx, y: p.y - drag.dy});
    drag.n._p = c;
    drag.n.pos_x = Math.round(c.x);
    drag.n.pos_y = Math.round(c.y);
    topoMoveNodeGeom(drag.n);
    if (onMove) onMove(drag.n);
  });

  function finish(ev) {
    if (!drag) return;
    const n = drag.n, g = drag.el;
    drag = null;
    g.classList.remove('sel');
    if (onEnd) onEnd(n);
  }
  svgEl.addEventListener('pointerup', finish);
  svgEl.addEventListener('pointercancel', finish);
}
