"""拓扑配置接口（数据驱动拓扑）。

- GET    /api/topology            读取节点（含最新采集状态）与连线
- PUT    /api/topology/nodes      批量保存节点布局（拖拽/表单保存）
- POST   /api/topology/links      新增连线
- DELETE /api/topology/links/<id> 删除连线
- GET    /api/topology/io         导出拓扑 JSON（不含密码），?download=1 触发下载
- POST   /api/topology/io         导入拓扑 JSON，按设备名替换，未出现的设备停用并隐藏
"""
import json

from flask import Response, g, request

from ..auth import admin_required, login_required
from ..db import get_db, log_action
from . import bp
from .devices import LINK_STYLES, ROLES, err, ok
from .stats import _LATEST


def _load_nodes(db):
    """设备节点 = devices 行 + 各自最新一轮采集状态"""
    nodes = []
    for d in db.execute('SELECT * FROM devices ORDER BY id').fetchall():
        s = db.execute(_LATEST, (d['id'],)).fetchone()
        n = {'id': d['id'], 'name': d['name'], 'role': d['role'] or 'other',
             'pos_x': d['pos_x'], 'pos_y': d['pos_y'],
             'topo_visible': d['topo_visible'], 'enabled': d['enabled'],
             'online': None, 'vrrp': [], 'ospf': '-', 'routes': '-',
             'ping': None, 'collected_at': None}
        if s:
            n['online'] = s['online']
            n['collected_at'] = s['collected_at']
            if s['online']:
                n['vrrp'] = json.loads(s['vrrp_state'] or '[]')
                n['ospf'] = f"{s['ospf_full']}/{s['ospf_neighbors']}"
                n['routes'] = s['route_count']
                n['ping'] = s['biz_ping_ok']
        nodes.append(n)
    return nodes


def _load_links(db):
    rows = db.execute('SELECT id, from_id, to_id, label, style FROM topology_links '
                      'ORDER BY id').fetchall()
    return [dict(r) for r in rows]


@bp.get('/topology')
@login_required
def get_topology():
    db = get_db()
    return ok({'nodes': _load_nodes(db), 'links': _load_links(db)})


def _coord(v, field):
    """坐标解析：空值 -> None，非法 -> 抛 ValueError"""
    if v in (None, ''):
        return None
    f = float(v)
    if not -10000 <= f <= 10000:
        raise ValueError(field)
    return f


@bp.put('/topology/nodes')
@admin_required
def save_nodes():
    """保存节点布局（拖拽结束后或表格微调后调用）"""
    items = (request.get_json(silent=True) or {}).get('nodes')
    if not isinstance(items, list):
        return err('nodes 必须是数组')
    db = get_db()
    saved = 0
    for it in items:
        did = it.get('id')
        if not db.execute('SELECT id FROM devices WHERE id=?', (did,)).fetchone():
            continue
        try:
            x, y = _coord(it.get('pos_x'), 'pos_x'), _coord(it.get('pos_y'), 'pos_y')
        except (TypeError, ValueError):
            return err(f'节点 {did} 的坐标不合法')
        role = it.get('role')
        if role not in ROLES:
            role = 'other'
        db.execute('UPDATE devices SET role=?, pos_x=?, pos_y=?, topo_visible=? WHERE id=?',
                   (role, x, y, 1 if it.get('topo_visible') else 0, did))
        saved += 1
    db.commit()
    log_action('保存拓扑布局', f'更新 {saved} 个节点位置', g.user['username'])
    return ok({'saved': saved})


@bp.post('/topology/links')
@admin_required
def add_link():
    data = request.get_json(silent=True) or {}
    try:
        fid, tid = int(data.get('from_id')), int(data.get('to_id'))
    except (TypeError, ValueError):
        return err('起点、终点必须是设备ID')
    if fid == tid:
        return err('不能把设备连到自己')
    db = get_db()
    names = {r['id']: r['name'] for r in db.execute(
        'SELECT id, name FROM devices WHERE id IN (?,?)', (fid, tid)).fetchall()}
    if fid not in names or tid not in names:
        return err('设备不存在', 404)
    if db.execute('SELECT id FROM topology_links WHERE (from_id=? AND to_id=?) '
                  'OR (from_id=? AND to_id=?)', (fid, tid, tid, fid)).fetchone():
        return err('这两台设备之间已有连线')
    style = data.get('style') if data.get('style') in LINK_STYLES else 'link'
    cur = db.execute('INSERT INTO topology_links (from_id, to_id, label, style) VALUES (?,?,?,?)',
                     (fid, tid, (data.get('label') or '').strip()[:50], style))
    db.commit()
    log_action('新增连线', f"{names[fid]} — {names[tid]}", g.user['username'])
    return ok({'id': cur.lastrowid})


@bp.delete('/topology/links/<int:lid>')
@admin_required
def delete_link(lid):
    db = get_db()
    row = db.execute('SELECT label FROM topology_links WHERE id=?', (lid,)).fetchone()
    if not row:
        return err('连线不存在', 404)
    db.execute('DELETE FROM topology_links WHERE id=?', (lid,))
    db.commit()
    log_action('删除连线', f'连线 #{lid} {row["label"]}', g.user['username'])
    return ok()


@bp.get('/topology/io')
@login_required
def export_topology():
    """导出拓扑：不含 password，避免密码外泄"""
    db = get_db()
    nodes = [dict(r) for r in db.execute(
        'SELECT name, role, pos_x, pos_y, host, port, username, collect_interval, '
        'ping_target, enabled, topo_visible FROM devices ORDER BY id').fetchall()]
    links = [{'from': r['f'], 'to': r['t'], 'label': r['label'], 'style': r['style']}
             for r in db.execute(
                 'SELECT a.name AS f, b.name AS t, l.label, l.style FROM topology_links l '
                 'JOIN devices a ON a.id = l.from_id JOIN devices b ON b.id = l.to_id '
                 'ORDER BY l.id').fetchall()]
    payload = {'version': 1, 'nodes': nodes, 'links': links}
    if request.args.get('download'):
        return Response(json.dumps(payload, ensure_ascii=False, indent=2),
                        mimetype='application/json',
                        headers={'Content-Disposition': 'attachment; filename=topology.json'})
    return ok(payload)


@bp.post('/topology/io')
@admin_required
def import_topology():
    """导入拓扑：同名设备更新（保留历史），新设备创建，未出现的设备停用并隐藏"""
    payload = request.get_json(silent=True) or {}
    nodes, links = payload.get('nodes') or [], payload.get('links') or []
    if not isinstance(nodes, list) or not isinstance(links, list):
        return err('nodes / links 必须是数组')
    names = [(n.get('name') or '').strip() for n in nodes]
    if any(not x for x in names):
        return err('每个节点都必须填写设备名')
    if len(set(names)) != len(names):
        return err('节点设备名有重复，请检查后再导入')

    db = get_db()
    warnings, id_by_name, keep = [], {}, set()
    created = updated = 0
    for nd in nodes:
        name = (nd.get('name') or '').strip()
        role = nd.get('role') if nd.get('role') in ROLES else 'other'
        host = (nd.get('host') or '').strip()
        ping = (nd.get('ping_target') or '').strip()
        try:
            port = int(nd.get('port'))
            interval = int(nd.get('collect_interval') or 5)
            x, y = _coord(nd.get('pos_x'), 'pos_x'), _coord(nd.get('pos_y'), 'pos_y')
        except (TypeError, ValueError):
            return err(f'设备 {name} 的端口、采集间隔或坐标不合法')
        if not host or not 1 <= port <= 65535 or not 1 <= interval <= 3600:
            return err(f'设备 {name} 的 IP 或端口不合法')
        # 终端节点不采集，强制停用，避免采出一串失败
        enabled = 0 if role == 'host' else (1 if nd.get('enabled') else 0)
        visible = 1 if nd.get('topo_visible') else 0
        row = db.execute('SELECT id FROM devices WHERE name=?', (name,)).fetchone()
        if row:
            did = row['id']
            db.execute('UPDATE devices SET role=?, pos_x=?, pos_y=?, topo_visible=?, host=?, '
                       'port=?, username=?, collect_interval=?, ping_target=?, enabled=? '
                       'WHERE id=?',
                       (role, x, y, visible, host, port, (nd.get('username') or '').strip(),
                        interval, ping, enabled, did))
            if nd.get('password'):
                db.execute('UPDATE devices SET password=? WHERE id=?', (nd['password'], did))
            updated += 1
        else:
            cur = db.execute(
                'INSERT INTO devices (name, host, port, username, password, collect_interval, '
                'ping_target, role, pos_x, pos_y, topo_visible, enabled) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (name, host, port, (nd.get('username') or '').strip(), nd.get('password') or '',
                 interval, ping, role, x, y, visible, enabled))
            did = cur.lastrowid
            created += 1
        keep.add(did)
        id_by_name[name] = did
        task = db.execute('SELECT id FROM collect_tasks WHERE device_id=?', (did,)).fetchone()
        if task:
            db.execute('UPDATE collect_tasks SET status=? WHERE device_id=?',
                       ('running' if enabled else 'paused', did))
        else:
            db.execute('INSERT INTO collect_tasks (device_id, status) VALUES (?,?)',
                       (did, 'running' if enabled else 'paused'))
    # 本次 JSON 中未出现的设备：停止采集、不在图上显示，但保留设备与历史
    disabled = 0
    for r in db.execute('SELECT id FROM devices').fetchall():
        if r['id'] not in keep:
            db.execute('UPDATE devices SET enabled=0, topo_visible=0 WHERE id=?', (r['id'],))
            db.execute("UPDATE collect_tasks SET status='paused' WHERE device_id=?", (r['id'],))
            disabled += 1
    # 连线按名字映射回 id 后整表重建（坐标与角色同批写入，重建无副作用）
    db.execute('DELETE FROM topology_links')
    linked = 0
    for lk in links:
        f = (lk.get('from') or '').strip()
        t = (lk.get('to') or '').strip()
        fid, tid = id_by_name.get(f), id_by_name.get(t)
        if not fid or not tid:
            warnings.append(f'连线 {f}—{t} 的端点不在本次导入的设备里，已跳过')
            continue
        if fid == tid:
            warnings.append(f'连线 {f}—{t} 两端相同，已跳过')
            continue
        style = lk.get('style') if lk.get('style') in LINK_STYLES else 'link'
        db.execute('INSERT OR IGNORE INTO topology_links (from_id, to_id, label, style) '
                   'VALUES (?,?,?,?)', (fid, tid, (lk.get('label') or '').strip()[:50], style))
        linked += 1
    db.commit()
    log_action('拓扑导入', f'新建 {created} 台 / 更新 {updated} 台 / 停用 {disabled} 台 / '
                          f'连线 {linked} 条', g.user['username'])
    return ok({'created': created, 'updated': updated, 'disabled': disabled,
               'links': linked, 'warnings': warnings})
