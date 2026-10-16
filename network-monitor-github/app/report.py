"""巡检报告生成（纯标准库，输出自包含 HTML）。

设计要点：
- 报告是**自包含**的：CSS 内联、拓扑与趋势图都是服务端生成的 SVG，
  没有外部链接（不引 style.css、不引任何 JS），所以可以离线打开、打印、拷走存档。
- 拓扑布局复刻前端 topo.js 的规则（同一个画布 1000x400、同样的分层与自动落位、
  同样的坐标夹取、同样的节点尺寸），保证报告里的图和系统里看到的一致。
- 所有插入的文本都过 html.escape，避免设备名/日志内容破坏 HTML。
"""
import html
import json
from datetime import datetime

# 与 static/topo.js 保持一致
TOPO_W, TOPO_H = 1000, 400
ROLE_CN = {'core': '核心', 'aggregation': '汇聚', 'access': '接入', 'host': '终端', 'other': ''}
ROLE_LAYER = {'core': 0, 'aggregation': 1, 'access': 2, 'other': 2, 'host': 3}
NODE_SIZE = {'core': (96, 46), 'aggregation': (92, 34), 'access': (84, 30),
             'host': (64, 26), 'other': (86, 34)}
EVENT_CN = {'vrrp_switch': 'VRRP 主备切换', 'offline': '设备离线', 'ospf_change': 'OSPF 变化',
            'recover': '故障恢复'}
PING_CN = {1: '通', 0: '不通', -1: '未测'}

_LATEST = '''SELECT * FROM device_status s WHERE s.device_id=?
             ORDER BY s.collected_at DESC, s.id DESC LIMIT 1'''


def esc(v):
    return html.escape('' if v is None else str(v))


def fmt_ms(ms):
    if ms is None:
        return '-'
    return f'{ms / 1000:.1f}s' if ms >= 1000 else f'{round(ms)}ms'


def _fmt_dt(s):
    """去掉毫秒，只留到秒"""
    return (s or '').split('.')[0] or '-'


def _hm(s):
    """取时间戳的 HH:MM 作为图表横轴标签"""
    return (s or '')[11:16] or (s or '-')


def _size(role):
    return NODE_SIZE.get(role or 'other', NODE_SIZE['other'])


def _layer(role):
    return ROLE_LAYER.get(role or 'other', 2)


def _clamp(role, p):
    w, h = _size(role)
    return {'x': min(max(p['x'], w / 2 + 6), TOPO_W - w / 2 - 6),
            'y': min(max(p['y'], h / 2 + 6), TOPO_H - h / 2 - 6)}


def _resolve(nodes):
    """有坐标用坐标，没有的按角色分层自动落位（与 topoResolve 同规则）"""
    groups = {}
    for n in nodes:
        groups.setdefault(_layer(n['role']), []).append(n)
    auto = {}
    for layer, arr in groups.items():
        y = 64 + layer * 96
        for i, n in enumerate(arr):
            auto[n['id']] = {'x': round(TOPO_W * (i + 1) / (len(arr) + 1)), 'y': y}
    pos = {}
    for n in nodes:
        if n['pos_x'] is not None and n['pos_y'] is not None:
            pos[n['id']] = _clamp(n['role'], {'x': float(n['pos_x']), 'y': float(n['pos_y'])})
        else:
            pos[n['id']] = _clamp(n['role'], auto[n['id']])
    return pos


def _stroke(n):
    if n['role'] == 'host':
        return '#e4e7ed'
    if not n['enabled']:
        return '#909399'
    if n['online'] == 1:
        return '#16a34a'
    if n['online'] == 0:
        return '#dc2626'
    return '#909399'


def _svg_topology(nodes, links, pos):
    """服务端生成拓扑 SVG：分层布局 + 状态着色 + Eth-Trunk 双线"""
    parts = [f'<svg viewBox="0 0 {TOPO_W} {TOPO_H}" width="100%" height="400" '
             f'style="background:#f8fafc;border:1px solid #e4e7ed;border-radius:8px">']
    for lk in links:
        a, b = pos.get(lk['from_id']), pos.get(lk['to_id'])
        if not a or not b:
            continue
        if lk['style'] == 'eth-trunk':
            dx, dy = b['x'] - a['x'], b['y'] - a['y']
            ln = (dx * dx + dy * dy) ** 0.5 or 1
            ox, oy = -dy / ln * 3, dx / ln * 3
            for s in (1, -1):
                parts.append(f'<line x1="{a["x"] + ox * s:.1f}" y1="{a["y"] + oy * s:.1f}" '
                             f'x2="{b["x"] + ox * s:.1f}" y2="{b["y"] + oy * s:.1f}" '
                             f'stroke="#94a3b8" stroke-width="2" opacity="0.6"/>')
        else:
            parts.append(f'<line x1="{a["x"]:.1f}" y1="{a["y"]:.1f}" x2="{b["x"]:.1f}" '
                         f'y2="{b["y"]:.1f}" stroke="#94a3b8" stroke-width="2" opacity="0.6"/>')
        if lk['label']:
            parts.append(f'<text x="{(a["x"] + b["x"]) / 2:.1f}" y="{(a["y"] + b["y"]) / 2 - 5:.1f}" '
                         f'text-anchor="middle" fill="#64748b" font-size="12">'
                         f'{esc(lk["label"])}</text>')
    for n in nodes:
        if not n['topo_visible']:
            continue
        w, h = _size(n['role'])
        p = pos[n['id']]
        is_host = n['role'] == 'host'
        dash = ' stroke-dasharray="5 3"' if (not n['enabled'] and not is_host) else ''
        fill = '#e2e8f0' if is_host else '#ffffff'
        tx_fill = '#909399' if is_host else '#303133'
        tx_size = 11 if is_host else 13
        cap = n['name'] if is_host else f'{n["name"]} {ROLE_CN.get(n["role"], "")}'.strip()
        parts.append(f'<rect x="{p["x"] - w / 2:.1f}" y="{p["y"] - h / 2:.1f}" width="{w}" '
                     f'height="{h}" rx="6" fill="{fill}" stroke="{_stroke(n)}" '
                     f'stroke-width="2"{dash}/>')
        parts.append(f'<text x="{p["x"]:.1f}" y="{p["y"] + 5:.1f}" text-anchor="middle" '
                     f'fill="{tx_fill}" font-size="{tx_size}" font-weight="600">{esc(cap)}</text>')
    parts.append('</svg>')
    return ''.join(parts)


def _svg_line(points, color, unit='', height=140, y_min=None):
    """服务端折线图（points: [(label, value)]，value 为 None 时断线）"""
    W, pad_l, pad_r, pad_t, pad_b = 900, 46, 12, 14, 24
    H = height
    ys = [v for _, v in points if v is not None]
    lo = y_min if y_min is not None else (min(ys) if ys else 0)
    hi = max(ys) if ys else 1
    if hi == lo:
        hi = lo + 1
    span = hi - lo
    lo -= span * 0.08
    hi += span * 0.08

    def px(i):
        return pad_l + (W - pad_l - pad_r) * (i / (len(points) - 1) if len(points) > 1 else 0)

    def py(v):
        return pad_t + (H - pad_t - pad_b) * (1 - (v - lo) / (hi - lo))

    out = [f'<svg viewBox="0 0 {W} {H}" width="100%" height="{H}">']
    for g in range(5):
        v = lo + (hi - lo) * g / 4
        y = py(v)
        out.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{W - pad_r}" y2="{y:.1f}" '
                   f'stroke="#e4e7ed" stroke-dasharray="3 3"/>')
        out.append(f'<text x="{pad_l - 6}" y="{y + 4:.1f}" text-anchor="end" fill="#909399" '
                   f'font-size="11">{round(v, 1)}</text>')
    d, started = '', False
    for i, (_, v) in enumerate(points):
        if v is None:
            started = False
            continue
        d += f'{"L" if started else "M"}{px(i):.1f} {py(v):.1f} '
        started = True
    if d:
        out.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2"/>')
    step = max(1, len(points) // 8)
    for i, (label, v) in enumerate(points):
        if i % step == 0:
            out.append(f'<text x="{px(i):.1f}" y="{H - 6}" text-anchor="middle" fill="#909399" '
                       f'font-size="11">{esc(label)}</text>')
        if v is not None:
            out.append(f'<circle cx="{px(i):.1f}" cy="{py(v):.1f}" r="2.5" fill="{color}">'
                       f'<title>{esc(label)} {v}{esc(unit)}</title></circle>')
    out.append('</svg>')
    return ''.join(out)


def _svg_online_bar(rows):
    """在线状态条：每轮一个小方块，绿=在线 红=离线 灰=无数据"""
    if not rows:
        return '<p class="muted">暂无采集数据</p>'
    W, H, gap = 900, 26, 1.5
    n = len(rows)
    bw = max((W - n * gap) / n, 1)
    bars = []
    for i, r in enumerate(rows):
        color = '#16a34a' if r['online'] else '#dc2626'
        bars.append(f'<rect x="{i * (bw + gap):.1f}" y="4" width="{bw:.1f}" height="{H - 8}" rx="2" '
                    f'fill="{color}"><title>{esc(r["collected_at"])}</title></rect>')
    return (f'<svg viewBox="0 0 {W} {H}" width="100%" height="{H}" '
            f'preserveAspectRatio="none">{"".join(bars)}</svg>')


def _load(db, hours):
    """一次取齐报告需要的数据"""
    devs = [dict(r) for r in db.execute('SELECT * FROM devices ORDER BY id').fetchall()]
    for d in devs:
        s = db.execute(_LATEST, (d['id'],)).fetchone()
        d['latest'] = dict(s) if s else None
        d['online'] = d['latest']['online'] if s else None
        t = db.execute('SELECT * FROM collect_tasks WHERE device_id=?', (d['id'],)).fetchone()
        d['task'] = dict(t) if t else None
        rows = db.execute(
            'SELECT collected_at, online, route_count, ospf_full, ospf_neighbors '
            "FROM device_status WHERE device_id=? AND collected_at >= datetime('now','localtime',?) "
            'ORDER BY id DESC LIMIT 60', (d['id'], f'-{hours} hours')).fetchall()
        d['trend'] = [dict(r) for r in reversed(rows)]      # 旧 -> 新
        d['rounds'] = db.execute(
            "SELECT COUNT(*) c FROM device_status WHERE device_id=? "
            "AND collected_at >= datetime('now','localtime',?)",
            (d['id'], f'-{hours} hours')).fetchone()['c']
    links = [dict(r) for r in db.execute(
        'SELECT id, from_id, to_id, label, style FROM topology_links ORDER BY id').fetchall()]
    events = [dict(r) for r in db.execute(
        'SELECT e.*, d.name AS device_name, t.t1_at, t.t2_at, t.t3_at, t.t4_at, '
        't.detect_ms, t.converge_ms, t.recover_ms, t.total_ms '
        'FROM switch_events e LEFT JOIN devices d ON d.id = e.device_id '
        'LEFT JOIN event_timeline t ON t.event_id = e.id '
        "WHERE e.started_at >= datetime('now','localtime',?) ORDER BY e.id DESC",
        (f'-{hours} hours',)).fetchall()]
    st = db.execute(
        'SELECT COUNT(*) samples, SUM(online) online_cnt '
        'FROM device_status WHERE collected_at >= '
        "datetime('now','localtime',?)", (f'-{hours} hours',)).fetchone()
    return devs, links, events, st


def _conclusions(events, devs, avg, st):
    """按阈值自动生成结论与建议（答辩时可以直接念）"""
    tips = []
    offline_ev = [e for e in events if e['event_type'] == 'offline']
    switch_ev = [e for e in events if e['event_type'] == 'vrrp_switch']
    if not st['samples']:
        tips.append(('warn', '本期没有任何采集数据，请确认采集任务是否启用、设备串口是否可达。'))
    if not events:
        tips.append(('ok', '本期未发生故障事件，核心网运行稳定。'))
    if offline_ev:
        tips.append(('danger', f'本期出现 {len(offline_ev)} 次设备离线事件，'
                              '建议检查串口连接、设备供电与 eNSP 工程状态。'))
    if switch_ev:
        tips.append(('info', f'本期发生 {len(switch_ev)} 次 VRRP 主备切换，'
                             '高可用机制已实际生效，说明备份路径可用。'))
    if avg['detect'] is not None and avg['detect'] > 3000:
        tips.append(('warn', f'平均检测时间 {fmt_ms(avg["detect"])} 偏长（采集间隔 {len(devs)} 台设备共享），'
                             '建议缩短采集间隔或引入 BFD 联动以加快故障感知。'))
    if avg['converge'] is not None and avg['converge'] > 5000:
        tips.append(('warn', f'平均路由收敛时间 {fmt_ms(avg["converge"])} 偏长，'
                             '建议检查 OSPF Hello/Dead 计时器与链路质量。'))
    if avg['total'] is not None and avg['recover'] is not None and avg['total'] > 0 \
            and avg['recover'] > avg['total'] * 0.5:
        tips.append(('info', '业务恢复耗时占总中断时间的比重较大，'
                             '瓶颈更多在业务侧（ping 恢复）而非路由收敛。'))
    if st['samples']:
        pct = (st['online_cnt'] or 0) * 100.0 / st['samples']
        tips.append(('ok' if pct >= 99 else 'warn',
                     f'设备整体在线率 {pct:.1f}%（{st["online_cnt"] or 0}/{st["samples"]} 轮采集）。'))
    sims = [d for d in devs if d['source'] == 'sim']
    if sims:
        tips.append(('info', f'报告期内存在 {len(sims)} 台故障演练虚拟设备（名称以「演练·」开头），'
                             '其数据由模拟器按剧本产生，非真实网络数据。'))
    return tips


def _avg(events):
    def mean(field):
        vals = [e[field] for e in events if e[field] is not None]
        return round(sum(vals) / len(vals)) if vals else None
    return {'detect': mean('detect_ms'), 'converge': mean('converge_ms'),
            'recover': mean('recover_ms'), 'total': mean('total_ms')}


CSS = '''
* { box-sizing: border-box; }
body { margin: 0; padding: 28px 34px 60px; background: #f0f2f5; color: #303133;
  font-family: "Microsoft YaHei", "Segoe UI", sans-serif; font-size: 14px; line-height: 1.6; }
h1 { font-size: 22px; margin: 0 0 6px; }
h2 { font-size: 17px; margin: 26px 0 10px; padding-left: 9px; border-left: 4px solid #2563eb; }
.sub { color: #909399; font-size: 13px; }
.muted { color: #909399; }
.card { background: #fff; border: 1px solid #e4e7ed; border-radius: 8px; padding: 16px 18px; margin-bottom: 14px; }
.stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
.stat { background: #fff; border: 1px solid #e4e7ed; border-radius: 8px; padding: 13px 15px; }
.stat .num { font-size: 23px; font-weight: 700; }
.stat .label { color: #909399; font-size: 13px; }
table { width: 100%; border-collapse: collapse; }
th, td { border: 1px solid #e4e7ed; padding: 7px 9px; text-align: left; font-size: 13px; }
th { background: #f8fafc; }
.tag { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 12px; }
.tag.ok { background: #dcfce7; color: #16a34a; }
.tag.warn { background: #fef3c7; color: #d97706; }
.tag.danger { background: #fee2e2; color: #dc2626; }
.tag.info { background: #dbeafe; color: #2563eb; }
.trend { margin-bottom: 14px; }
.trend .t { color: #909399; font-size: 12px; margin: 6px 0 2px; }
.tips li { margin: 5px 0; }
@media print { body { background: #fff; padding: 0; } .card { break-inside: avoid; } }
'''


def render_report(db, hours=24):
    """生成完整的巡检报告 HTML（自包含，可直接打印或存档）"""
    devs, links, events, st = _load(db, hours)
    avg = _avg(events)
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    online = sum(1 for d in devs if d['online'] == 1)
    offline = sum(1 for d in devs if d['online'] == 0)
    pos = _resolve(devs)
    vis = [d for d in devs if d['topo_visible']]
    ev_type_cnt = {}
    for e in events:
        ev_type_cnt[e['event_type']] = ev_type_cnt.get(e['event_type'], 0) + 1

    P = [f'<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">',
         f'<title>巡检报告 {now}</title><style>{CSS}</style></head><body>']
    P.append(f'<h1>企业核心网高可用路由监控与故障分析系统 · 巡检报告</h1>'
             f'<p class="sub">生成时间：{now}　|　统计周期：最近 {hours} 小时　|　'
             f'数据来源：eNSP 串口采集（VRRP / OSPF / 路由表 / 业务连通性）</p>')

    # 一、总览
    P.append('<h2>一、总览指标</h2><div class="stat-grid">')
    cards = [('设备总数', len(devs)), ('在线', online), ('离线', offline),
             ('故障事件', len(events)), ('采集样本', st['samples'] or 0),
             ('平均检测', fmt_ms(avg['detect'])), ('平均收敛', fmt_ms(avg['converge'])),
             ('平均业务恢复', fmt_ms(avg['recover'])), ('平均总中断', fmt_ms(avg['total']))]
    for label, val in cards:
        P.append(f'<div class="stat"><div class="num">{esc(val)}</div>'
                 f'<div class="label">{esc(label)}</div></div>')
    P.append('</div>')

    # 二、拓扑
    P.append('<h2>二、网络拓扑</h2><div class="card">')
    P.append(_svg_topology(vis, links, pos))
    P.append('<p class="muted" style="font-size:12px;margin:8px 0 0">'
             '绿框=在线　红框=离线　灰虚线框=已停用　淡色节点=终端；'
             '节点位置取自拓扑管理的配置，未配置坐标的按角色分层自动落位。</p></div>')

    # 三、结论与建议
    P.append('<h2>三、结论与建议</h2><div class="card"><ul class="tips">')
    for level, text in _conclusions(events, devs, avg, st):
        P.append(f'<li>{esc(text)}</li>')
    P.append('</ul></div>')

    # 四、设备明细
    P.append('<h2>四、设备明细</h2><div class="card"><table><thead><tr>'
             '<th>设备</th><th>角色</th><th>采集地址</th><th>状态</th><th>VRRP</th>'
             '<th>OSPF</th><th>路由数</th><th>业务 ping</th><th>采集轮数</th>'
             '<th>最后采集</th></tr></thead><tbody>')
    for d in devs:
        s = d['latest']
        vrrp = []
        if s:
            try:
                vrrp = json.loads(s['vrrp_state'] or '[]')
            except ValueError:
                vrrp = []
        state = ('<span class="tag info">终端</span>' if d['role'] == 'host' else
                 ('<span class="tag warn">已停用</span>' if not d['enabled'] else
                  ('<span class="tag ok">在线</span>' if d['online'] == 1 else
                   ('<span class="tag danger">离线</span>' if d['online'] == 0 else
                    '<span class="muted">未采集</span>'))))
        vrrp_txt = ' '.join('VRID%s %s' % (v['vrid'], v['state']) for v in vrrp) or '-'
        ospf_txt = ('%s/%s' % (s['ospf_full'], s['ospf_neighbors'])) if (s and d['online']) else '-'
        route_txt = s['route_count'] if (s and d['online']) else '-'
        ping_txt = PING_CN.get(s['biz_ping_ok'], '-') if s else '-'
        last_txt = _fmt_dt(s['collected_at']) if s else '-'
        P.append(
            f'<tr><td>{esc(d["name"])}</td><td>{esc(ROLE_CN.get(d["role"], "其他"))}</td>'
            f'<td class="muted">{esc(d["host"])}:{esc(d["port"])}</td><td>{state}</td>'
            f'<td>{esc(vrrp_txt)}</td><td>{esc(ospf_txt)}</td><td>{esc(route_txt)}</td>'
            f'<td>{esc(ping_txt)}</td><td>{d["rounds"]}</td>'
            f'<td class="muted">{esc(last_txt)}</td></tr>')
    P.append('</tbody></table></div>')

    # 五、事件清单
    P.append('<h2>五、故障事件与时间线</h2><div class="card">')
    P.append(f'<p class="muted">按类型统计：'
             + esc('、'.join(f'{EVENT_CN.get(k, k)} {v} 次' for k, v in ev_type_cnt.items())
                   or '本期无事件') + '</p>')
    if events:
        P.append('<table><thead><tr><th>ID</th><th>设备</th><th>类型</th><th>开始</th>'
                 '<th>结束</th><th>T1</th><th>T2</th><th>T3</th><th>T4</th>'
                 '<th>检测</th><th>收敛</th><th>业务恢复</th><th>总中断</th></tr></thead><tbody>')
        for e in events:
            P.append(f'<tr><td>{e["id"]}</td><td>{esc(e["device_name"] or "-")}</td>'
                     f'<td>{esc(EVENT_CN.get(e["event_type"], e["event_type"]))}</td>'
                     f'<td class="muted">{esc(_fmt_dt(e["started_at"]))}</td>'
                     f'<td class="muted">{esc(_fmt_dt(e["ended_at"]))}</td>'
                     f'<td class="muted">{esc(_fmt_dt(e["t1_at"]))}</td>'
                     f'<td class="muted">{esc(_fmt_dt(e["t2_at"]))}</td>'
                     f'<td class="muted">{esc(_fmt_dt(e["t3_at"]))}</td>'
                     f'<td class="muted">{esc(_fmt_dt(e["t4_at"]))}</td>'
                     f'<td>{esc(fmt_ms(e["detect_ms"]))}</td><td>{esc(fmt_ms(e["converge_ms"]))}</td>'
                     f'<td>{esc(fmt_ms(e["recover_ms"]))}</td>'
                     f'<td><b>{esc(fmt_ms(e["total_ms"]))}</b></td></tr>')
        P.append('</tbody></table>')
    else:
        P.append('<p class="muted">本期没有产生故障事件。</p>')
    P.append('</div>')

    # 六、趋势
    P.append('<h2>六、设备指标趋势</h2><div class="card">')
    for d in devs:
        tr = d['trend']
        if not tr:
            continue
        P.append(f'<div class="trend"><p><b>{esc(d["name"])}</b>'
                 f'<span class="muted">（最近 {len(tr)} 轮，'
                 f'{esc(_hm(tr[0]["collected_at"]))} ~ {esc(_hm(tr[-1]["collected_at"]))}）</span></p>')
        P.append('<p class="t">路由条目数</p>')
        P.append(_svg_line([(_hm(r['collected_at']), r['route_count'] if r['online'] else None)
                            for r in tr], '#2563eb', unit=' 条'))
        P.append('<p class="t">OSPF 邻居 / Full</p>')
        P.append(_svg_line([(_hm(r['collected_at']), r['ospf_neighbors'] if r['online'] else None)
                            for r in tr], '#16a34a', unit=' 个', y_min=0))
        P.append('<p class="t">在线状态（红=离线）</p>')
        P.append(_svg_online_bar(tr))
        P.append('</div>')
    if not any(d['trend'] for d in devs):
        P.append('<p class="muted">本期没有采集数据，无法绘制趋势。</p>')
    P.append('</div>')

    P.append(f'<p class="sub" style="margin-top:22px">'
             f'本报告由系统自动生成，全部数据来自设备实时采集，可直接打印或存档。　'
             f'生成时间 {now}</p>')
    P.append('</body></html>')
    return '\n'.join(P)
