import ipaddress
import re
import socket

from flask import g, request

from ..auth import admin_required, login_required
from ..db import get_db, log_action
from . import bp

# 拓扑角色与连线样式白名单（topology.py 复用）
ROLES = {'core', 'aggregation', 'access', 'host', 'other'}
LINK_STYLES = {'eth-trunk', 'trunk', 'access', 'link'}


def ok(data=None):
    return {'code': 0, 'msg': 'ok', 'data': data}


def err(msg, http=400):
    return {'code': 1, 'msg': msg}, http


def _valid_ip(s):
    try:
        ipaddress.IPv4Address(s)
        return True
    except ValueError:
        return False


def _validate(form):
    """校验设备表单，返回 (cleaned dict, 错误信息)"""
    name = (form.get('name') or '').strip()
    host = (form.get('host') or '').strip()
    ping_target = (form.get('ping_target') or '').strip()
    if not name or len(name) > 50:
        return None, '设备名必填且不超过50字符'
    if not _valid_ip(host):
        return None, 'IP地址格式不正确'
    try:
        port = int(form.get('port'))
        if not 1 <= port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        return None, '端口必须是1-65535的整数'
    try:
        interval = int(form.get('collect_interval') or 5)
        if not 1 <= interval <= 3600:
            raise ValueError
    except (TypeError, ValueError):
        return None, '采集间隔必须是1-3600秒'
    if ping_target and not _valid_ip(ping_target):
        return None, '探测目标IP格式不正确'
    role = (form.get('role') or 'other').strip()
    if role not in ROLES:
        role = 'other'
    return {
        'name': name, 'host': host, 'port': port,
        'username': (form.get('username') or '').strip(),
        'password': form.get('password') or '',
        'collect_interval': interval, 'ping_target': ping_target, 'role': role,
    }, None


@bp.get('/devices')
@login_required
def list_devices():
    """设备列表（带最新一轮采集状态与任务信息），viewer/admin 都可看"""
    kw = (request.args.get('keyword') or '').strip()
    sql = '''
        SELECT d.*, t.status AS task_status, t.last_run_at, t.run_count, t.fail_count,
               s.online, s.vrrp_state, s.ospf_neighbors, s.ospf_full,
               s.route_count, s.biz_ping_ok, s.collected_at
        FROM devices d
        LEFT JOIN collect_tasks t ON t.device_id = d.id
        LEFT JOIN device_status s ON s.id = (
            SELECT id FROM device_status WHERE device_id = d.id
            ORDER BY collected_at DESC, id DESC LIMIT 1)
    '''
    args = []
    if kw:
        sql += ' WHERE d.name LIKE ? OR d.host LIKE ?'
        args = [f'%{kw}%', f'%{kw}%']
    sql += ' ORDER BY d.id'
    rows = get_db().execute(sql, args).fetchall()
    return ok([dict(r) for r in rows])


@bp.post('/devices')
@admin_required
def create_device():
    data, msg = _validate(request.get_json(silent=True) or request.form)
    if msg:
        return err(msg)
    db = get_db()
    # 终端节点（PC/Server 等）不参与采集
    enabled = 0 if data['role'] == 'host' else 1
    cur = db.execute(
        '''INSERT INTO devices (name, host, port, username, password, collect_interval,
           ping_target, role, enabled) VALUES (?,?,?,?,?,?,?,?,?)''',
        (data['name'], data['host'], data['port'], data['username'],
         data['password'], data['collect_interval'], data['ping_target'],
         data['role'], enabled))
    device_id = cur.lastrowid
    db.execute('INSERT INTO collect_tasks (device_id, status) VALUES (?,?)',
               (device_id, 'running' if enabled else 'paused'))
    db.commit()
    log_action('新增设备', f"新增设备 {data['name']}（{data['host']}:{data['port']}）", g.user['username'])
    return ok({'id': device_id})


@bp.put('/devices/<int:did>')
@admin_required
def update_device(did):
    db = get_db()
    if not db.execute('SELECT id FROM devices WHERE id=?', (did,)).fetchone():
        return err('设备不存在', 404)
    data, msg = _validate(request.get_json(silent=True) or request.form)
    if msg:
        return err(msg)
    db = get_db()
    db.execute(
        '''UPDATE devices SET name=?, host=?, port=?, username=?, password=?,
           collect_interval=?, ping_target=?, role=? WHERE id=?''',
        (data['name'], data['host'], data['port'], data['username'],
         data['password'], data['collect_interval'], data['ping_target'],
         data['role'], did))
    # 改成终端节点时同步暂停采集，改回核心/汇聚等设备时恢复采集
    if data['role'] == 'host':
        db.execute('UPDATE devices SET enabled=0 WHERE id=?', (did,))
        db.execute("UPDATE collect_tasks SET status='paused' WHERE device_id=?", (did,))
    db.commit()
    log_action('修改设备', f"修改设备 #{did} {data['name']}", g.user['username'])
    return ok()


@bp.patch('/devices/<int:did>/toggle')
@admin_required
def toggle_device(did):
    db = get_db()
    dev = db.execute('SELECT id, name, enabled, role FROM devices WHERE id=?', (did,)).fetchone()
    if not dev:
        return err('设备不存在', 404)
    if dev['role'] == 'host':
        return err('终端节点不参与采集，无需启停')
    new_enabled = 0 if dev['enabled'] else 1
    db.execute('UPDATE devices SET enabled=? WHERE id=?', (new_enabled, did))
    db.execute('UPDATE collect_tasks SET status=? WHERE device_id=?',
               ('running' if new_enabled else 'paused', did))
    db.commit()
    log_action('启停采集', f"设备 {dev['name']} 采集已{'启用' if new_enabled else '停用'}",
               g.user['username'])
    return ok({'enabled': new_enabled})


@bp.post('/devices/<int:did>/collect')
@admin_required
def collect_now(did):
    """请求立即采集：只做标记，由调度线程执行，避免与后台采集写库冲突"""
    from ..collector import scheduler
    dev = get_db().execute('SELECT id, name, enabled FROM devices WHERE id=?', (did,)).fetchone()
    if not dev:
        return err('设备不存在', 404)
    if not dev['enabled']:
        return err('设备已停用，请先启用采集')
    scheduler.request_collect(did)
    log_action('手动采集', f"设备 {dev['name']} 已请求立即采集", g.user['username'])
    return ok({'queued': True})


@bp.post('/devices/<int:did>/test')
@admin_required
def test_device(did):
    """同步测试串口连通性：登录读提示符 + display version，不落库"""
    from ..collector.telnet_client import SerialTelnet, TelnetError
    dev = get_db().execute('SELECT host, port, name, source FROM devices WHERE id=?',
                           (did,)).fetchone()
    if not dev:
        return err('设备不存在', 404)
    if dev['source'] == 'sim':
        return ok({'ok': True, 'sysname': '', 'msg': '该设备为演练虚拟设备，无真实串口，无需测试连接'})
    try:
        with SerialTelnet(dev['host'], dev['port'], timeout=5) as tn:
            banner = tn.wake()
            try:
                banner += tn.execute('display version', timeout=8)
            except TelnetError:
                pass                            # version 读不到不算失败
        m = re.search(r'[<\[]([\w\-.]+)[>\]]', banner)
        sysname = m.group(1) if m else ''
        msg = f'连接成功，设备名：{sysname}' if sysname else '连接成功（未取得设备名）'
        log_action('测试连接', f"设备 {dev['name']}: {msg}", g.user['username'])
        return ok({'ok': True, 'sysname': sysname, 'msg': msg})
    except (TelnetError, OSError, socket.timeout) as e:
        msg = f'{type(e).__name__}: {e}'
        log_action('测试连接', f"设备 {dev['name']}: 失败 {msg}", g.user['username'])
        return ok({'ok': False, 'msg': f'连接失败：{msg}'})


@bp.delete('/devices/<int:did>')
@admin_required
def delete_device(did):
    db = get_db()
    dev = db.execute('SELECT name FROM devices WHERE id=?', (did,)).fetchone()
    if not dev:
        return err('设备不存在', 404)
    # 先删子表，再删设备。
    # switch_events / alerts 的 device_id 是可空外键，这里置 NULL 保留历史事件，
    # 否则在 foreign_keys=ON 下会因外键约束删不掉设备
    db.execute('DELETE FROM topology_links WHERE from_id=? OR to_id=?', (did, did))
    db.execute('DELETE FROM device_status WHERE device_id=?', (did,))
    db.execute('DELETE FROM collect_tasks WHERE device_id=?', (did,))
    db.execute('UPDATE switch_events SET device_id=NULL WHERE device_id=?', (did,))
    db.execute('UPDATE alerts SET device_id=NULL WHERE device_id=?', (did,))
    db.execute('DELETE FROM devices WHERE id=?', (did,))
    db.commit()
    log_action('删除设备', f"删除设备 {dev['name']}", g.user['username'])
    return ok()
