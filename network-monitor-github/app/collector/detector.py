"""事件检测状态机（创新点）。

规则：
- 连续两轮异常才确认事件（防抖动）；连续两轮恢复才解除事件
- VRRP 主备角色变化 -> 生成 vrrp_switch 事件 + T1 时间线
- T1~T4 时间线：
    T1 VRRP 角色互换确认（时间取第一轮观察到变化的时刻，更贴近真实切换点）
    T2 OSPF 邻居全部恢复 Full
    T3 路由表收敛（恢复到基线，或切换后路由数稳定在降级拓扑）
    T4 业务 ping 恢复
- 指标：检测时间 = T1 - 故障发生（近似取最后一轮正常状态时刻）
        路由收敛 = T3 - T2；业务恢复 = T4 - T1；总中断 = 检测 + 业务恢复
运行期状态保存在内存（单进程单调度线程，重启后从未关闭事件继续推进）。
"""
import json
from datetime import datetime

_state = {}  # device_id -> 运行期状态


def now_ms():
    t = datetime.now()
    return t.strftime('%Y-%m-%d %H:%M:%S.') + f'{t.microsecond // 1000:03d}'


def parse_ms(s):
    if not s:
        return None
    fmt = '%Y-%m-%d %H:%M:%S.%f' if '.' in s else '%Y-%m-%d %H:%M:%S'
    return int(datetime.strptime(s, fmt).timestamp() * 1000)


def forget(did):
    """丢弃某台设备的运行期检测状态（删除设备/清理演练数据时调用，避免内存态累积）"""
    _state.pop(did, None)


def _st(did):
    return _state.setdefault(did, {
        'vrrp_normal': None,   # 已确认的正常 VRRP 角色基线 {vrid: state}
        'vrrp_cand': None,     # 角色变化候选（第一轮观察）
        'last_normal_ms': None,  # 最后一轮正常状态时刻（近似故障发生时刻）
        'offline_cand': None,  # 离线候选第一轮时刻
        'recover_cand': None,  # 在线恢复候选第一轮时刻
        'close_cand': set(),   # 已记录 T4、等待第二轮 ping 确认关闭的事件 id
    })


def _open_event(conn, did, etype):
    return conn.execute(
        "SELECT * FROM switch_events WHERE device_id=? AND event_type=? AND status='pending' "
        "ORDER BY id DESC LIMIT 1", (did, etype)).fetchone()


def _alert(conn, did, event_id, level, message):
    conn.execute('INSERT INTO alerts (device_id, event_id, level, message) VALUES (?,?,?,?)',
                 (did, event_id, level, message))


def process(conn, dev, cur):
    """每轮采集写入后调用。dev: devices 行(dict)；cur: 刚写入的 device_status 行(dict)"""
    did = dev['id']
    st = _st(did)
    prev = conn.execute(
        'SELECT * FROM device_status WHERE device_id=? AND id<? ORDER BY id DESC LIMIT 1',
        (did, cur['id'])).fetchone()
    _check_offline(conn, dev, cur, prev, st)
    if cur['online']:
        _check_vrrp(conn, dev, cur, prev, st)
        _advance_timeline(conn, dev, cur, st, prev)


def _check_offline(conn, dev, cur, prev, st):
    did, name = dev['id'], dev['name']
    open_ev = _open_event(conn, did, 'offline')
    if not cur['online']:
        st['recover_cand'] = None
        if open_ev:
            return
        if prev and prev['online'] == 0:          # 连续两轮离线 -> 确认
            started = st['offline_cand'] or cur['collected_at']
            c = conn.execute(
                'INSERT INTO switch_events (device_id, event_type, title, started_at, detail) '
                'VALUES (?,?,?,?,?)',
                (did, 'offline', f'{name} 设备离线', started, cur['error'] or ''))
            _alert(conn, did, c.lastrowid, 'critical',
                   f'{name} 连续两轮采集失败，确认离线：{(cur["error"] or "")[:100]}')
            st['offline_cand'] = None
        else:                                      # 第一轮离线 -> 候选
            st['offline_cand'] = cur['collected_at']
    else:
        st['offline_cand'] = None
        if not open_ev:
            return
        if prev and prev['online'] == 1:           # 连续两轮在线 -> 解除
            ended = st['recover_cand'] or cur['collected_at']
            conn.execute("UPDATE switch_events SET ended_at=?, status='closed' WHERE id=?",
                         (ended, open_ev['id']))
            _alert(conn, did, open_ev['id'], 'info', f'{name} 已恢复在线，离线事件关闭')
            st['recover_cand'] = None
        else:
            st['recover_cand'] = cur['collected_at']


def _check_vrrp(conn, dev, cur, prev, st):
    did, name = dev['id'], dev['name']
    roles = {str(v['vrid']): v['state']
             for v in json.loads(cur['vrrp_state'] or '[]')}
    if not roles:
        return
    cur_ms = parse_ms(cur['collected_at'])
    if st['vrrp_normal'] is None:                  # 首次观察，学习基线
        st['vrrp_normal'] = roles
        st['last_normal_ms'] = cur_ms
        return
    if roles == st['vrrp_normal']:
        st['vrrp_cand'] = None
        st['last_normal_ms'] = cur_ms
        return
    cand = st['vrrp_cand']
    if cand and cand['roles'] == roles:            # 连续两轮同一新角色 -> 确认切换
        t1_s, t1_ms = cand['first_at'], cand['first_ms']
        fault_ms = cand['fault_ms']
        detect = max(t1_ms - fault_ms, 0) if fault_ms else None
        detail = json.dumps({'from': st['vrrp_normal'], 'to': roles}, ensure_ascii=False)
        c = conn.execute(
            'INSERT INTO switch_events (device_id, event_type, title, started_at, detail) '
            'VALUES (?,?,?,?,?)', (did, 'vrrp_switch', f'{name} VRRP 主备角色切换', t1_s, detail))
        eid = c.lastrowid
        conn.execute('INSERT INTO event_timeline (event_id, t1_at, detect_ms) VALUES (?,?,?)',
                     (eid, t1_s, detect))
        _alert(conn, did, eid, 'warning',
               f'{name} VRRP 角色切换（连续两轮确认）：{st["vrrp_normal"]} → {roles}')
        st['vrrp_normal'] = roles
        st['vrrp_cand'] = None
    else:                                          # 第一轮变化 -> 候选
        st['vrrp_cand'] = {'roles': roles, 'first_at': cur['collected_at'],
                           'first_ms': cur_ms, 'fault_ms': st['last_normal_ms']}


def _advance_timeline(conn, dev, cur, st, prev):
    did = dev['id']
    rows = conn.execute(
        "SELECT e.id AS eid, t.id AS tid, t.t1_at, t.t2_at, t.t3_at, t.t4_at, t.detect_ms "
        'FROM switch_events e JOIN event_timeline t ON t.event_id = e.id '
        "WHERE e.device_id=? AND e.event_type='vrrp_switch' AND e.status='pending'",
        (did,)).fetchall()
    cur_s = cur['collected_at']
    for r in rows:
        t2_at, t3_at, t4_at = r['t2_at'], r['t3_at'], r['t4_at']
        # T2：OSPF 邻居全部 Full
        if t2_at is None and cur['ospf_neighbors'] > 0 \
                and cur['ospf_full'] == cur['ospf_neighbors']:
            t2_at = cur_s
            conn.execute('UPDATE event_timeline SET t2_at=? WHERE id=?', (t2_at, r['tid']))
        # T3：路由收敛。两种判定满足其一即可：
        #   1) 路由条目恢复到基线（故障恢复场景）
        #   2) OSPF 全 Full 且路由数与上一轮相同（切换后稳定在降级拓扑的收敛场景，
        #      此时路由数可能永远低于故障前基线，如备机接管后丢失对端宣告的路由）
        if t2_at and t3_at is None and cur['online']:
            base = dev['route_count_baseline'] or 0
            stable = (prev and prev['online'] and prev['route_count'] > 0
                      and cur['route_count'] == prev['route_count'])
            if (base and cur['route_count'] >= base) or stable:
                t3_at = cur_s
                conn.execute('UPDATE event_timeline SET t3_at=?, converge_ms=? WHERE id=?',
                             (t3_at, parse_ms(t3_at) - parse_ms(t2_at), r['tid']))
        # T4：业务 ping 恢复
        if t4_at is None and cur['biz_ping_ok'] == 1:
            t4_at = cur_s
            recover = parse_ms(t4_at) - parse_ms(r['t1_at'])
            total = (r['detect_ms'] or 0) + recover
            conn.execute(
                'UPDATE event_timeline SET t4_at=?, recover_ms=?, total_ms=? WHERE id=?',
                (t4_at, recover, total, r['tid']))
            st['close_cand'].add(r['eid'])
        elif r['eid'] in st['close_cand']:
            if cur['biz_ping_ok'] == 1:            # 连续两轮 ping 通 -> 关闭事件
                conn.execute("UPDATE switch_events SET ended_at=?, status='closed' WHERE id=?",
                             (t4_at, r['eid']))
                _alert(conn, did, r['eid'], 'info',
                       f'{dev["name"]} 业务连通性已恢复（连续两轮确认），切换事件关闭')
                st['close_cand'].discard(r['eid'])
            else:                                  # ping 又失败，撤销关闭候选
                st['close_cand'].discard(r['eid'])
