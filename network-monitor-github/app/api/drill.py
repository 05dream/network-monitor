"""故障演练接口（答辩演示用）。

演练设备（devices.source='sim'）是独立于真实设备的虚拟设备：
数据由 simulator 按剧本产出，但走的仍是「调度器 → device_status → detector」同一条链路，
所以事件、T1~T4 时间线、告警都是系统真实算出来的，可放心在答辩时演示。
"""
import json

from flask import g, request

from ..auth import admin_required, login_required
from ..collector import detector, simulator
from ..db import get_db, log_action
from . import bp
from .devices import err, ok
from .events import _event_sql
from .stats import _LATEST

# 演练设备的占位采集地址（不会建立任何连接；仅保证设备表单校验能通过）
SIM_HOST, SIM_PORT = '127.0.0.1', 9999

_SIM_IDS = "SELECT id FROM devices WHERE source='sim'"


def _parse_state(raw):
    try:
        return json.loads(raw or '{}')
    except (TypeError, ValueError):
        return {}


def _real_baselines(db):
    """从 eNSP 真实设备（source='telnet'）最近一轮采集里抓基线，按拓扑角色分组。

    演练设备据此做「降级 → 收敛 → 恢复」，因此演练页上的 VRID / 路由数 / 邻居数
    就是 eNSP 上那台设备的真实数字；取不到则回落到内置默认基线。
    """
    out = {}
    rows = db.execute("SELECT * FROM devices WHERE source='telnet' ORDER BY id").fetchall()
    for d in rows:
        s = db.execute(_LATEST, (d['id'],)).fetchone()
        bl = simulator.baseline_from(dict(d), dict(s) if s else None)
        out.setdefault(d['role'] or 'other', []).append(bl)
    return out


@bp.get('/drill/scenarios')
@login_required
def drill_scenarios():
    return ok(simulator.scenario_briefs())


@bp.get('/drill/status')
@login_required
def drill_status():
    """当前演练概况：设备实时状态 + 本次产生的事件（供演练页实时展示）"""
    db = get_db()
    rows = db.execute("SELECT * FROM devices WHERE source='sim' ORDER BY id").fetchall()
    scenario, started_ms = '', None
    devices = []
    bl_linked, bl_names = False, []
    for d in rows:
        st = _parse_state(d['drill_state'])
        scenario = scenario or st.get('scenario', '')
        started_ms = started_ms or st.get('started_ms')
        o = (st.get('base') or {}).get('origin') or {}
        if o.get('linked'):
            bl_linked = True
            if o.get('name') and o['name'] not in bl_names:
                bl_names.append(o['name'])
        s = db.execute(_LATEST, (d['id'],)).fetchone()
        online = s['online'] if s else None
        devices.append({
            'id': d['id'], 'name': d['name'], 'key': st.get('key', ''), 'role': d['role'],
            'enabled': d['enabled'], 'online': online,
            'vrrp': json.loads(s['vrrp_state'] or '[]') if s and online else [],
            'ospf': f"{s['ospf_full']}/{s['ospf_neighbors']}" if s and online else '-',
            'routes': s['route_count'] if s and online else '-',
            'ping': s['biz_ping_ok'] if s else None,
            'collected_at': s['collected_at'] if s else None,
        })
    events = []
    if rows:
        ph = ','.join('?' * len(rows))
        events = db.execute(_event_sql(f' WHERE e.device_id IN ({ph})') + ' LIMIT 10',
                            [r['id'] for r in rows]).fetchall()
    sc = simulator.SCENARIOS.get(scenario) or {}
    elapsed = None
    if started_ms:
        elapsed = round((detector.parse_ms(detector.now_ms()) - int(started_ms)) / 1000.0, 1)
    return ok({
        'running': bool(rows), 'scenario': scenario, 'scenario_name': sc.get('name', ''),
        'duration': sc.get('duration'), 'elapsed': elapsed, 'interval': simulator.SIM_INTERVAL,
        'devices': devices, 'events': [dict(e) for e in events],
        'baseline': {'linked': bl_linked, 'names': bl_names},
    })


@bp.post('/drill/start')
@admin_required
def drill_start():
    key = (request.get_json(silent=True) or request.form).get('scenario') or ''
    if key not in simulator.SCENARIOS:
        return err('剧本不存在')
    db = get_db()
    if db.execute(f'SELECT COUNT(*) c FROM ({_SIM_IDS})').fetchone()['c']:
        return err('已有演练正在进行，请先「结束演练」并「清理演练数据」')
    started = detector.parse_ms(detector.now_ms())
    pl = simulator.plan(key, _real_baselines(db))
    key_to_id = {}
    for spec in pl['devices']:
        cur = db.execute(
            '''INSERT INTO devices (name, host, port, username, password, collect_interval,
               ping_target, role, pos_x, pos_y, enabled, source, drill_state)
               VALUES (?,?,?,?,?,?,?,?,?,?,1,'sim',?)''',
            (spec['name'], SIM_HOST, SIM_PORT, '', '', spec['interval'], '',
             spec['role'], spec['x'], spec['y'],
             simulator.build_state(key, spec['key'], started, spec['base'])))
        key_to_id[spec['key']] = cur.lastrowid
        db.execute("INSERT INTO collect_tasks (device_id, status) VALUES (?,'running')",
                   (cur.lastrowid,))
    for a, b, label, style in pl['links']:
        db.execute('INSERT INTO topology_links (from_id, to_id, label, style) VALUES (?,?,?,?)',
                   (key_to_id[a], key_to_id[b], label, style))
    db.commit()
    origin = pl['origin']
    src = ('eNSP 真实设备 ' + '、'.join(origin['names'])) if origin['linked'] else '内置默认基线'
    log_action('开始演练',
               f'开始故障演练「{simulator.SCENARIOS[key]["name"]}」，'
               f'创建 {len(key_to_id)} 台演练设备（不参与真实采集），数据基线取自 {src}',
               g.user['username'])
    return ok({'scenario': key, 'devices': len(key_to_id),
               'duration': simulator.SCENARIOS[key]['duration'],
               'interval': simulator.SIM_INTERVAL, 'origin': origin, 'baseline': src})


@bp.post('/drill/stop')
@admin_required
def drill_stop():
    """结束演练：暂停演练设备采集，数据保留下来便于复盘"""
    db = get_db()
    rows = db.execute("SELECT id, name FROM devices WHERE source='sim'").fetchall()
    if not rows:
        return err('当前没有正在进行的演练')
    db.execute("UPDATE devices SET enabled=0 WHERE source='sim'")
    db.execute("UPDATE collect_tasks SET status='paused' WHERE device_id IN (" + _SIM_IDS + ')')
    db.commit()
    log_action('结束演练', f'停用 {len(rows)} 台演练设备的采集（采集数据保留）', g.user['username'])
    return ok({'stopped': len(rows)})


@bp.delete('/drill/clean')
@admin_required
def drill_clean():
    """清理演练数据：删除演练设备、采集历史与连线；历史故障事件保留（设备名显示为空）"""
    db = get_db()
    rows = db.execute("SELECT id, name FROM devices WHERE source='sim'").fetchall()
    if not rows:
        return err('没有可清理的演练数据')
    for r in rows:
        did = r['id']
        db.execute('DELETE FROM topology_links WHERE from_id=? OR to_id=?', (did, did))
        db.execute('DELETE FROM device_status WHERE device_id=?', (did,))
        db.execute('DELETE FROM collect_tasks WHERE device_id=?', (did,))
        db.execute('UPDATE switch_events SET device_id=NULL WHERE device_id=?', (did,))
        db.execute('UPDATE alerts SET device_id=NULL WHERE device_id=?', (did,))
        db.execute('DELETE FROM devices WHERE id=?', (did,))
        detector.forget(did)
    db.commit()
    log_action('清理演练数据',
               f'删除 {len(rows)} 台演练设备及其采集数据（保留历史故障事件）', g.user['username'])
    return ok({'removed': len(rows)})
