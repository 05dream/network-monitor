"""仪表盘汇总 + 设备详情/历史 查询接口"""
import json

from ..auth import login_required
from ..db import get_db
from . import bp
from .devices import err, ok

_LATEST = '''SELECT * FROM device_status s WHERE s.device_id=?
             ORDER BY s.collected_at DESC, s.id DESC LIMIT 1'''


@bp.get('/dashboard')
@login_required
def dashboard():
    db = get_db()
    devs = db.execute('SELECT * FROM devices ORDER BY id').fetchall()
    cards = []
    online = offline = masters = backups = ospf_n = ospf_f = 0
    last_at = ''
    for d in devs:
        s = db.execute(_LATEST, (d['id'],)).fetchone()
        card = {'id': d['id'], 'name': d['name'], 'enabled': d['enabled'],
                'online': None, 'vrrp': [], 'ospf': '-', 'routes': '-',
                'ping': None, 'collected_at': None}
        if s:
            card['online'] = s['online']
            card['collected_at'] = s['collected_at']
            last_at = max(last_at, s['collected_at'])
            if s['online']:
                online += 1
                vrrp = json.loads(s['vrrp_state'] or '[]')
                card['vrrp'] = vrrp
                masters += sum(1 for v in vrrp if v['state'] == 'Master')
                backups += sum(1 for v in vrrp if v['state'] == 'Backup')
                card['ospf'] = f"{s['ospf_full']}/{s['ospf_neighbors']}"
                ospf_n += s['ospf_neighbors']
                ospf_f += s['ospf_full']
                card['routes'] = s['route_count']
                card['ping'] = s['biz_ping_ok']
            else:
                offline += 1
                card['error'] = s['error']
        cards.append(card)
    pending = db.execute("SELECT COUNT(*) c FROM alerts WHERE status='pending'").fetchone()['c']
    events = db.execute(
        '''SELECT e.id, e.title, e.event_type, e.started_at, e.status, d.name AS device_name
           FROM switch_events e LEFT JOIN devices d ON d.id = e.device_id
           ORDER BY e.id DESC LIMIT 5''').fetchall()
    return ok({
        'devices_total': len(devs), 'devices_online': online, 'devices_offline': offline,
        'vrrp_masters': masters, 'vrrp_backups': backups,
        'ospf_neighbors': ospf_n, 'ospf_full': ospf_f,
        'pending_alerts': pending, 'last_collect_at': last_at or '-',
        'cards': cards, 'recent_events': [dict(e) for e in events],
    })


@bp.get('/stats/summary')
@login_required
def stats_summary():
    """近24小时运行统计：事件分布、平均切换指标、在线率、采集成功率"""
    db = get_db()
    ev_rows = db.execute(
        "SELECT event_type, COUNT(*) c FROM switch_events "
        "WHERE started_at >= datetime('now','localtime','-24 hours') GROUP BY event_type"
    ).fetchall()
    tm = db.execute(
        'SELECT AVG(detect_ms) avg_detect, AVG(converge_ms) avg_converge, '
        'AVG(recover_ms) avg_recover, AVG(total_ms) avg_total, COUNT(*) cnt '
        "FROM event_timeline WHERE t1_at >= datetime('now','localtime','-24 hours')"
    ).fetchone()
    st = db.execute(
        'SELECT COUNT(*) samples, SUM(online) online_cnt, '
        'SUM(biz_ping_ok = 1) ping_ok, SUM(biz_ping_ok >= 0) ping_total '
        'FROM device_status WHERE collected_at >= '
        "datetime('now','localtime','-24 hours')"
    ).fetchone()

    def pct(part, total):
        return round(part * 100.0 / total, 1) if total else None

    return ok({
        'events_by_type': {r['event_type']: r['c'] for r in ev_rows},
        'events_24h': sum(r['c'] for r in ev_rows),
        'switches_24h': tm['cnt'],
        'avg_detect_ms': int(tm['avg_detect']) if tm['avg_detect'] is not None else None,
        'avg_converge_ms': int(tm['avg_converge']) if tm['avg_converge'] is not None else None,
        'avg_recover_ms': int(tm['avg_recover']) if tm['avg_recover'] is not None else None,
        'avg_total_ms': int(tm['avg_total']) if tm['avg_total'] is not None else None,
        'uptime_pct': pct(st['online_cnt'], st['samples']),
        'ping_ok_pct': pct(st['ping_ok'], st['ping_total']),
        'samples': st['samples'],
    })


@bp.get('/devices/<int:did>/detail')
@login_required
def device_detail(did):
    db = get_db()
    d = db.execute('SELECT * FROM devices WHERE id=?', (did,)).fetchone()
    if not d:
        return err('设备不存在', 404)
    t = db.execute('SELECT * FROM collect_tasks WHERE device_id=?', (did,)).fetchone()
    s = db.execute(_LATEST, (did,)).fetchone()
    data = dict(d)
    data['task'] = dict(t) if t else None
    if s:
        latest = dict(s)
        for f in ('vrrp_state', 'ospf_state', 'interfaces'):
            latest[f] = json.loads(latest[f] or '[]')
        data['latest'] = latest
    else:
        data['latest'] = None
    return ok(data)


@bp.get('/devices/<int:did>/history')
@login_required
def device_history(did):
    from flask import request
    limit = min(int(request.args.get('limit', 30)), 200)
    rows = get_db().execute(
        'SELECT * FROM device_status WHERE device_id=? ORDER BY id DESC LIMIT ?',
        (did, limit)).fetchall()
    return ok([dict(r) for r in rows])
