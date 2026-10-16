"""故障演练模拟器（答辩演示用）。

与 eNSP 的联动：开始演练时会先从 eNSP 真实设备（source='telnet'）最近一轮采集里
抓一份**基线快照**——设备名、拓扑坐标、VRRP VRID 与虚拟 IP、OSPF 邻居、接口、路由条目数。
演练设备的各个相位都在这份真实基线上做「降级 → 收敛 → 恢复」，
所以演练页上看到的 VRID、路由数、邻居数就是 eNSP 上那台设备的真实数字，
演练设备在拓扑图上也紧挨着它对应的真实设备。
若某项在真实采集里取不到（eNSP 没跑 / 该设备未采集），逐项回落到 DEFAULT_BASE。

产出的数据结构与 collector.collect_device 完全一致，因此后续链路
（scheduler 写库 → detector 检测 → 事件与 T1~T4 时间线）与真实故障毫无区别。

时间基准：演练开始时把起始毫秒写进 devices.drill_state，
每轮用调度器传入的 now_str（毫秒级字符串）算出已运行秒数，再按相位表取当前状态。
相位间隔按 >= 2×采集间隔设计，且故障期各相位路由档位互不相同，
保证 detector 的"连续两轮确认"一定被触发、T2 与 T3 落在不同轮次。
"""
import json

from .detector import parse_ms

# 采集间隔（秒）
SIM_INTERVAL = 2

# 与前端拓扑画布一致：1000x400，节点半高最大 23，故 y 上限 377
TOPO_MAX_Y = 377
# 演练设备相对其真实设备的纵向偏移（摆在真实设备正下方，一眼看出对应关系）
TOPO_DY = 52

_OSPF = [{'neighbor': '10.0.0.2', 'interface': 'GigabitEthernet0/0/1', 'area': '0.0.0.0'},
         {'neighbor': '10.0.0.3', 'interface': 'GigabitEthernet0/0/2', 'area': '0.0.0.0'}]
_IFACES = ['GigabitEthernet0/0/1', 'GigabitEthernet0/0/2']
_VRRP = [{'vrid': 10, 'state': 'Master', 'interface': 'Vlanif10', 'vip': '192.168.10.254'},
         {'vrid': 30, 'state': 'Master', 'interface': 'Vlanif30', 'vip': '192.168.30.254'}]

# 取不到真实数据时的默认基线（路由档位与真实 eNSP 拓扑量级接近）
DEFAULT_BASE = {
    'core': {'vrrp': [dict(v) for v in _VRRP], 'ospf': [dict(o) for o in _OSPF],
             'ifaces': list(_IFACES), 'routes': 26},
    'aggregation': {'vrrp': [], 'ospf': [dict(o) for o in _OSPF],
                    'ifaces': list(_IFACES), 'routes': 16},
    'access': {'vrrp': [], 'ospf': [dict(o) for o in _OSPF],
               'ifaces': list(_IFACES), 'routes': 12},
    'other': {'vrrp': [], 'ospf': [dict(o) for o in _OSPF],
              'ifaces': list(_IFACES), 'routes': 8},
}

# 内置剧本：nodes 的 key 用于连线与 drill_state；phases 的 kind 决定该相位的指标
#   normal  正常基线
#   offline 设备离线（采集失败）
#   fail    故障发生：VRRP 主备翻转、丢 OSPF 邻居、掉路由、业务不通
#   ospf    OSPF 邻居恢复 Full（T2）
#   routes  路由条目回基线（T3）
#   recover 业务连通性恢复（T4）
SCENARIOS = {
    'core_failover': {
        'name': '核心路由器宕机 · VRRP 主备切换',
        'desc': '模拟核心路由器断电、备用核心接管业务：产生设备离线事件、VRRP 主备切换事件与完整 T1~T4 时间线；核心恢复后自动回切。',
        'duration': 84,
        'nodes': {
            'A': {'label': 'CORE1', 'role': 'core', 'x': 258, 'y': 88,
                  'phases': [{'at': 0, 'kind': 'normal'},
                             {'at': 12, 'kind': 'offline'},
                             {'at': 66, 'kind': 'normal'}]},
            'B': {'label': 'CORE2', 'role': 'core', 'x': 742, 'y': 88,
                  'phases': [{'at': 0, 'kind': 'normal'},
                             {'at': 12, 'kind': 'fail'},
                             {'at': 24, 'kind': 'ospf'},
                             {'at': 36, 'kind': 'routes'},
                             {'at': 48, 'kind': 'recover'}]},
        },
        'links': [('A', 'B', 'Eth-Trunk1', 'eth-trunk')],
    },
    'access_offline': {
        'name': '接入层设备离线',
        'desc': '模拟接入交换机停电：连续两轮采集失败后确认离线并产生严重告警，恢复供电后自动关闭事件。',
        'duration': 46,
        'nodes': {
            'SW': {'label': 'SW1', 'role': 'access', 'x': 500, 'y': 341,
                   'phases': [{'at': 0, 'kind': 'normal'},
                              {'at': 8, 'kind': 'offline'},
                              {'at': 40, 'kind': 'normal'}]},
        },
        'links': [],
    },
}


def scenario_briefs():
    """给前端用的剧本简介（不含相位细节）"""
    return [{'key': k, 'name': sc['name'], 'desc': sc['desc'], 'duration': sc['duration'],
             'device_count': len(sc['nodes']), 'interval': SIM_INTERVAL}
            for k, sc in SCENARIOS.items()]


def _json_list(raw):
    try:
        v = json.loads(raw or '[]')
        return v if isinstance(v, list) else []
    except (TypeError, ValueError):
        return []


def baseline_from(dev, status):
    """把一台真实设备（devices 行 + 最新 device_status 行）整理成演练基线"""
    vrrp = _json_list(status['vrrp_state']) if status else []
    ospf = _json_list(status['ospf_state']) if status else []
    ifaces = [i.get('name', '') for i in (_json_list(status['interfaces']) if status else [])
              if i.get('name')]
    routes = (status['route_count'] if status else 0) or dev.get('route_count_baseline') or 0
    return {
        'name': dev.get('name') or '', 'host': dev.get('host') or '',
        'port': dev.get('port') or 0,
        'pos_x': dev.get('pos_x'), 'pos_y': dev.get('pos_y'),
        'vrrp': vrrp, 'ospf': ospf, 'ifaces': ifaces, 'routes': routes,
        'linked': bool(vrrp or ospf or ifaces),
    }


def _merge(role, real, label):
    """真实基线缺失的字段用默认基线补齐，保证任何情况下都能生成合理数据"""
    d = DEFAULT_BASE.get(role) or DEFAULT_BASE['other']
    real = real or {}
    return {
        'name': real.get('name') or label,
        'host': real.get('host') or '127.0.0.1', 'port': real.get('port') or 0,
        'pos_x': real.get('pos_x'), 'pos_y': real.get('pos_y'),
        'vrrp': real.get('vrrp') or [dict(v) for v in d['vrrp']],
        'ospf': real.get('ospf') or [dict(o) for o in d['ospf']],
        'ifaces': real.get('ifaces') or list(d['ifaces']),
        'routes': real.get('routes') or d['routes'],
        'linked': bool(real.get('linked')),
    }


def _place(nd, base):
    """演练设备坐标：跟着真实设备走（正下方一行）；没有坐标则用剧本默认值"""
    x, y = base.get('pos_x'), base.get('pos_y')
    if x is None or y is None:
        return nd['x'], nd['y']
    return round(float(x)), round(min(float(y) + TOPO_DY, TOPO_MAX_Y))


def plan(scenario, baselines=None):
    """按剧本 + 真实基线，产出演练设备规格与连线。

    baselines: {'core': [基线, ...], 'access': [基线, ...]}，由接口层从 eNSP 真实设备抓取
    """
    sc = SCENARIOS.get(scenario)
    if not sc:
        return None
    pool = {}
    for role, arr in (baselines or {}).items():
        pool.setdefault(role, []).extend(arr or [])
    idx, used, devices, linked = {}, [], [], False
    for key, nd in sc['nodes'].items():
        role = nd['role']
        arr = pool.get(role) or []
        i = idx.get(role, 0)
        real = arr[i] if i < len(arr) else None
        if real is not None:
            idx[role] = i + 1
            if real.get('name'):
                used.append(real['name'])
        base = _merge(role, real, nd['label'])
        # linked 表示"这项指标确实来自 eNSP 采集数据"，仅有设备名不算
        got = bool(real and real.get('linked'))
        base['origin'] = {'linked': got, 'name': base['name']}
        linked = linked or got
        x, y = _place(nd, base)
        devices.append({'key': key, 'name': f'演练·{base["name"]}', 'role': role,
                        'x': x, 'y': y, 'interval': SIM_INTERVAL, 'base': base})
    return {'devices': devices, 'links': list(sc.get('links') or []),
            'origin': {'linked': linked, 'names': used}}


def drill_devices(scenario):
    """演练设备规格（不含基线，供离线/测试场景使用）"""
    pl = plan(scenario) or {'devices': []}
    keys = ('key', 'name', 'role', 'x', 'y', 'interval')
    return [{k: d[k] for k in keys} for d in pl['devices']]


def scripted_links(scenario):
    """返回该剧本的设备间连线 [(from_key, to_key, label, style), ...]"""
    return list((SCENARIOS.get(scenario) or {}).get('links') or [])


def build_state(scenario, key, started_ms, base=None):
    """生成写入 devices.drill_state 的 JSON（含该设备用的基线快照）"""
    if base is None:
        nd = ((SCENARIOS.get(scenario) or {}).get('nodes') or {}).get(key) or {}
        base = _merge(nd.get('role') or 'other', None, nd.get('label') or key)
    return json.dumps({'scenario': scenario, 'key': key, 'started_ms': int(started_ms),
                       'base': base}, ensure_ascii=False)


def _swap(vrrp):
    """主备角色翻转"""
    out = []
    for v in vrrp or []:
        n = dict(v)
        n['state'] = 'Backup' if v.get('state') == 'Master' else 'Master'
        out.append(n)
    return out


def _levels(r):
    """故障期三个路由档位：降级 / 恢复中 / 基线；保证降级与恢复中互不相同"""
    low = max(r - 8, 1)
    mid = max(r - 6, 1)
    if mid == low:
        mid = low + 1
    return low, mid, r


def _offline(name, err, ifaces=None):
    """离线态：接口全 down、无 OSPF 邻居、ping 未测"""
    return {
        'online': 0, 'vrrp_state': '[]', 'ospf_neighbors': 0, 'ospf_full': 0,
        'ospf_state': '[]', 'interfaces': json.dumps(
            [{'name': i, 'phy': 'down', 'protocol': 'down'} for i in (ifaces or _IFACES)]),
        'route_count': 0, 'biz_ping_ok': -1, 'error': err,
    }


def _build(base, vrrp, ospf_full, routes, ping):
    """把一组指标翻译成与 collector.collect_device 相同结构的 dict"""
    ospf = base.get('ospf') or []
    return {
        'online': 1,
        'vrrp_state': json.dumps(vrrp, ensure_ascii=False),
        'ospf_neighbors': len(ospf),
        'ospf_full': int(ospf_full),
        'ospf_state': json.dumps(
            [{'neighbor': o.get('neighbor', ''), 'interface': o.get('interface', ''),
              'area': o.get('area', '0.0.0.0'),
              'state': 'Full' if k < ospf_full else 'Init'}
             for k, o in enumerate(ospf)], ensure_ascii=False),
        'interfaces': json.dumps(
            [{'name': i, 'phy': 'up', 'protocol': 'up'} for i in (base.get('ifaces') or _IFACES)],
            ensure_ascii=False),
        'route_count': int(routes),
        'biz_ping_ok': int(ping),
        'error': '',
    }


def _phase(name, ph, base):
    kind = ph['kind']
    if kind == 'offline':
        host, port = base.get('host') or '127.0.0.1', base.get('port')
        return _offline(name, f'连接超时：{host}:{port or "—"} 无响应', base.get('ifaces'))
    n_ospf, n_routes = len(base.get('ospf') or []), int(base.get('routes') or 0)
    if kind == 'normal':
        return _build(base, base.get('vrrp') or [], n_ospf, n_routes, 1)
    vrrp, full, ping = _swap(base.get('vrrp')), max(n_ospf - 1, 0), 0
    low, mid, _ = _levels(n_routes)
    if kind == 'fail':
        return _build(base, vrrp, full, low, ping)
    full = n_ospf                                  # OSPF 邻居恢复 Full -> T2
    routes = mid if kind == 'ospf' else n_routes   # 路由回基线 -> T3
    if kind == 'recover':
        ping = 1                                   # 业务恢复 -> T4
    return _build(base, vrrp, full, routes, ping)


def collect(dev, now_str):
    """演练设备的一轮采集：dev 为 devices 行，now_str 为调度器生成的时间戳"""
    try:
        st = json.loads(dev['drill_state'] or '{}')
    except (TypeError, ValueError):
        st = {}
    node = ((SCENARIOS.get(st.get('scenario')) or {}).get('nodes') or {}).get(st.get('key'))
    if not node or not st.get('started_ms'):
        return _offline(dev['name'], '演练剧本信息缺失（请清理后重新开始演练）')
    base = st.get('base') or _merge(node['role'], None, node['label'])
    elapsed = (parse_ms(now_str) - int(st['started_ms'])) / 1000.0
    ph = node['phases'][0]
    for p in node['phases']:
        if elapsed >= p['at']:
            ph = p
    return _phase(dev['name'], ph, base)
