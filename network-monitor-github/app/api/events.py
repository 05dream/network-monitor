"""故障事件 + 告警 + CSV 导出接口"""
import csv
import io

from flask import Response, g, request

from ..auth import admin_required, login_required
from ..db import get_db, log_action
from . import bp
from .devices import err, ok


def _event_sql(where):
    return ('SELECT e.*, d.name AS device_name, '
            't.t1_at, t.t2_at, t.t3_at, t.t4_at, '
            't.detect_ms, t.converge_ms, t.recover_ms, t.total_ms '
            'FROM switch_events e LEFT JOIN devices d ON d.id = e.device_id '
            'LEFT JOIN event_timeline t ON t.event_id = e.id ' + where +
            ' ORDER BY e.id DESC')


def _filters():
    where, args = [], []
    if request.args.get('status') in ('pending', 'confirmed', 'closed'):
        where.append('e.status=?')
        args.append(request.args['status'])
    if request.args.get('type'):
        where.append('e.event_type=?')
        args.append(request.args['type'])
    if request.args.get('device_id', type=int):
        where.append('e.device_id=?')
        args.append(request.args['device_id'])
    return (' WHERE ' + ' AND '.join(where)) if where else '', args


@bp.get('/events')
@login_required
def list_events():
    where, args = _filters()
    rows = get_db().execute(_event_sql(where), args).fetchall()
    return ok([dict(r) for r in rows])


@bp.get('/events/export')
@login_required
def export_events():
    where, args = _filters()
    rows = get_db().execute(_event_sql(where), args).fetchall()
    log_action('导出事件', f'导出故障事件 {len(rows)} 条', g.user['username'])
    buf = io.StringIO()
    buf.write('﻿')  # BOM，保证 Excel 打开中文不乱码
    w = csv.writer(buf)
    w.writerow(['事件ID', '设备', '类型', '标题', '开始时间', '结束时间', '状态',
                'T1(VRRP切换)', 'T2(OSPF Full)', 'T3(路由恢复)', 'T4(业务恢复)',
                '检测时间ms', '收敛时间ms', '业务恢复ms', '总中断ms'])
    for r in rows:
        w.writerow([r['id'], r['device_name'], r['event_type'], r['title'],
                    r['started_at'], r['ended_at'] or '', r['status'],
                    r['t1_at'] or '', r['t2_at'] or '', r['t3_at'] or '', r['t4_at'] or '',
                    r['detect_ms'] if r['detect_ms'] is not None else '',
                    r['converge_ms'] if r['converge_ms'] is not None else '',
                    r['recover_ms'] if r['recover_ms'] is not None else '',
                    r['total_ms'] if r['total_ms'] is not None else ''])
    return Response(buf.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=events.csv'})


@bp.post('/events/<int:eid>/confirm')
@admin_required
def confirm_event(eid):
    """事件确认：pending -> confirmed（人工核实过，等待自动或手动关闭）"""
    db = get_db()
    e = db.execute('SELECT * FROM switch_events WHERE id=?', (eid,)).fetchone()
    if not e:
        return err('事件不存在', 404)
    if e['status'] != 'pending':
        return err('仅待确认事件可执行确认')
    db.execute("UPDATE switch_events SET status='confirmed' WHERE id=?", (eid,))
    db.commit()
    log_action('确认事件', f'确认事件 #{eid}: {e["title"]}', g.user['username'])
    return ok()


@bp.post('/events/<int:eid>/close')
@admin_required
def close_event(eid):
    """手动关闭事件（如故障已人工处理完）"""
    db = get_db()
    e = db.execute('SELECT * FROM switch_events WHERE id=?', (eid,)).fetchone()
    if not e:
        return err('事件不存在', 404)
    if e['status'] == 'closed':
        return err('事件已关闭')
    db.execute("UPDATE switch_events SET status='closed', "
               "ended_at=datetime('now','localtime') WHERE id=?", (eid,))
    db.commit()
    log_action('关闭事件', f'手动关闭事件 #{eid}: {e["title"]}', g.user['username'])
    return ok()


@bp.get('/status/export')
@login_required
def export_status():
    """导出采集历史 CSV（支持按设备、时间范围过滤）"""
    device_id = request.args.get('device_id', type=int)
    hours = request.args.get('hours', type=int) or 24
    where = ["s.collected_at >= datetime('now','localtime',?)"]
    args = [f'-{hours} hours']
    if device_id:
        where.append('s.device_id=?')
        args.append(device_id)
    rows = get_db().execute(
        'SELECT s.*, d.name AS device_name FROM device_status s '
        'LEFT JOIN devices d ON d.id = s.device_id '
        'WHERE ' + ' AND '.join(where) + ' ORDER BY s.id DESC LIMIT 5000',
        args).fetchall()
    log_action('导出历史', f'导出采集历史 {len(rows)} 条', g.user['username'])
    buf = io.StringIO()
    buf.write('\ufeff')  # BOM，保证 Excel 打开中文不乱码
    w = csv.writer(buf)
    w.writerow(['设备', '采集时间', '在线', 'VRRP状态', 'OSPF邻居', 'OSPF Full',
                '路由数', '业务ping', '异常说明'])
    ping_cn = {1: '通', 0: '不通', -1: '未测'}
    for r in rows:
        w.writerow([r['device_name'], r['collected_at'],
                    '在线' if r['online'] else '离线',
                    r['vrrp_state'], r['ospf_neighbors'], r['ospf_full'],
                    r['route_count'], ping_cn.get(r['biz_ping_ok'], ''),
                    (r['error'] or '')[:200]])
    return Response(buf.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=status_history.csv'})


@bp.get('/alerts')
@login_required
def list_alerts():
    status = request.args.get('status', '')
    sql = ('SELECT a.*, d.name AS device_name FROM alerts a '
           'LEFT JOIN devices d ON d.id = a.device_id')
    args = []
    if status in ('pending', 'confirmed'):
        sql += ' WHERE a.status=?'
        args.append(status)
    sql += ' ORDER BY a.id DESC LIMIT 100'
    rows = get_db().execute(sql, args).fetchall()
    return ok([dict(r) for r in rows])


@bp.post('/alerts/<int:aid>/confirm')
@admin_required
def confirm_alert(aid):
    db = get_db()
    a = db.execute('SELECT * FROM alerts WHERE id=?', (aid,)).fetchone()
    if not a:
        return err('告警不存在', 404)
    if a['status'] == 'confirmed':
        return err('该告警已确认')
    db.execute("UPDATE alerts SET status='confirmed', confirmed_by=?, "
               "confirmed_at=datetime('now','localtime') WHERE id=?",
               (g.user['username'], aid))
    db.commit()
    log_action('确认告警', f'确认告警 #{aid}: {a["message"][:60]}', g.user['username'])
    return ok()
