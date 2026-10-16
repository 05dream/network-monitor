"""故障演练冒烟：python -m tests.smoke_drill

在临时库上驱动【调度器 → simulator → device_status → detector】完整链路：
- 剧本①：A 离线（offline 事件 + critical 告警，恢复后关闭）、B 主备切换
  （vrrp_switch 事件 + T1~T4 与 detect/converge/recover/total 全部算出，最终关闭）
- 剧本②：接入设备离线后恢复（offline 事件自动关闭）
- detector.forget 清理内存态
"""
import json
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app
from app.collector import detector, scheduler, simulator
from app.db import init_db


def _prepare_app():
    app = create_app()
    fd, tmpdb = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    app.config['DATABASE'] = tmpdb
    with app.app_context():
        init_db()
    conn = sqlite3.connect(tmpdb)
    conn.row_factory = sqlite3.Row
    return app, tmpdb, conn


def _seed(conn, scenario):
    """按剧本创建演练设备（source='sim'），与 drill_start 接口写库方式一致"""
    started = detector.parse_ms(detector.now_ms())
    ids = {}
    for spec in simulator.drill_devices(scenario):
        cur = conn.execute(
            '''INSERT INTO devices (name, host, port, username, password, collect_interval,
               ping_target, role, pos_x, pos_y, enabled, source, drill_state)
               VALUES (?,?,?,?,?,?,?,?,?,?,1,'sim',?)''',
            (spec['name'], '127.0.0.1', 9999, '', '', spec['interval'], '',
             spec['role'], spec['x'], spec['y'],
             simulator.build_state(scenario, spec['key'], started)))
        conn.execute("INSERT INTO collect_tasks (device_id, status) VALUES (?,'running')",
                     (cur.lastrowid,))
        ids[spec['key']] = cur.lastrowid
    conn.commit()
    return ids


def _jump(conn, did, sec):
    """把演练起始时刻往前挪，使下一轮采集正好落在剧本的第 sec 秒"""
    row = conn.execute('SELECT drill_state FROM devices WHERE id=?', (did,)).fetchone()
    st = json.loads(row['drill_state'])
    st['started_ms'] = detector.parse_ms(detector.now_ms()) - int(sec * 1000)
    conn.execute('UPDATE devices SET drill_state=? WHERE id=?', (json.dumps(st), did))
    conn.commit()


def _round(app, conn, did):
    """跑一轮真实采集链路（simulator 产出 → 写 device_status → detector 检测）"""
    time.sleep(0.003)   # 保证相邻两轮时间戳落在不同毫秒，避免收敛/恢复耗时被算成 0
    dev = dict(conn.execute('SELECT * FROM devices WHERE id=?', (did,)).fetchone())
    scheduler._collect_one(app, conn, dev)


def _run(app, conn, did, sec, rounds=2):
    _jump(conn, did, sec)
    for _ in range(rounds):
        _round(app, conn, did)


def test_briefs():
    briefs = simulator.scenario_briefs()
    assert len(briefs) == 2, briefs
    by_key = {b['key']: b for b in briefs}
    assert by_key['core_failover']['device_count'] == 2
    assert by_key['access_offline']['device_count'] == 1
    assert all(b['interval'] >= 1 and b['duration'] > 0 for b in briefs)
    print('PASS: 剧本简介')


def test_core_failover(app, conn):
    ids = _seed(conn, 'core_failover')
    a, b = ids['A'], ids['B']
    # 按剧本时间轴推进：0 正常 -> 12 故障（A离线/B切换）-> 24 OSPF恢复 -> 36 路由收敛 -> 48 业务恢复 -> 66 A回切
    for sec in (0, 12, 24, 36, 48, 66):
        _run(app, conn, a, sec)
        _run(app, conn, b, sec)

    # A：离线事件 + critical 告警，且已自动关闭
    off = conn.execute("SELECT * FROM switch_events WHERE device_id=? AND event_type='offline'",
                       (a,)).fetchone()
    assert off and off['status'] == 'closed', dict(off) if off else 'A 未产生离线事件'
    crit = conn.execute('SELECT * FROM alerts WHERE event_id=? AND level=?',
                        (off['id'], 'critical')).fetchone()
    assert crit, 'A 离线未产生 critical 告警'

    # B：VRRP 切换事件 + 完整 T1~T4 与四项耗时，且已自动关闭
    ev = conn.execute("SELECT * FROM switch_events WHERE device_id=? AND event_type='vrrp_switch'",
                      (b,)).fetchone()
    assert ev, 'B 未产生 VRRP 切换事件'
    assert ev['status'] == 'closed', f"切换事件应已自动关闭: {ev['status']}"
    tl = conn.execute('SELECT * FROM event_timeline WHERE event_id=?', (ev['id'],)).fetchone()
    for f in ('t1_at', 't2_at', 't3_at', 't4_at'):
        assert tl[f], f'{f} 未算出'
    for f in ('detect_ms', 'converge_ms', 'recover_ms', 'total_ms'):
        assert tl[f] is not None and tl[f] >= 0, f'{f} 未算出: {tl[f]}'
    assert tl['total_ms'] == (tl['detect_ms'] or 0) + tl['recover_ms'], '总中断 = 检测 + 业务恢复'
    assert tl['converge_ms'] > 0, 'T3 与 T2 应在不同轮次，收敛时间应大于 0'
    al = conn.execute("SELECT * FROM alerts WHERE event_id=? AND level='warning'",
                      (ev['id'],)).fetchone()
    assert al, 'B 切换未产生 warning 告警'
    print('PASS: 剧本①（核心宕机 + VRRP 主备切换 + 完整 T1~T4）')


def test_access_offline(app, conn):
    ids = _seed(conn, 'access_offline')
    sw = ids['SW']
    for sec in (0, 8, 40):
        _run(app, conn, sw, sec)
    off = conn.execute("SELECT * FROM switch_events WHERE device_id=? AND event_type='offline'",
                       (sw,)).fetchone()
    assert off and off['status'] == 'closed', '接入设备离线事件缺失或未关闭'
    crit = conn.execute('SELECT * FROM alerts WHERE event_id=? AND level=?',
                        (off['id'], 'critical')).fetchone()
    assert crit, '接入设备离线未产生 critical 告警'
    assert conn.execute("SELECT COUNT(*) c FROM switch_events WHERE device_id=?",
                        (sw,)).fetchone()['c'] == 1, '只应产生一条离线事件'
    print('PASS: 剧本②（接入层设备离线 -> 自动恢复关闭）')


def test_offline_phase_metrics(app, conn):
    """故障相位里 ping 必须不通，否则 T4 会在 T1 那一轮立即触发（recover_ms≈0）"""
    ids = _seed(conn, 'core_failover')
    _jump(conn, ids['B'], 12)
    dev = dict(conn.execute('SELECT * FROM devices WHERE id=?', (ids['B'],)).fetchone())
    res = simulator.collect(dev, detector.now_ms())
    assert res['biz_ping_ok'] == 0, '切换到备机那一轮 ping 必须为不通'
    assert res['route_count'] == 18 and res['ospf_full'] == 1, res
    print('PASS: 故障相位指标（ping 不通 / 路由降级 / OSPF 未全 Full）')


def test_forget(app, conn):
    ids = _seed(conn, 'access_offline')
    sw = ids['SW']
    _run(app, conn, sw, 0, rounds=1)
    assert sw in detector._state, '采集后应有运行期检测状态'
    detector.forget(sw)
    assert sw not in detector._state, 'forget 后不应残留运行期状态'
    print('PASS: detector.forget 清理内存态')


def main():
    app, tmpdb, conn = _prepare_app()
    try:
        test_briefs()
        test_core_failover(app, conn)
        test_access_offline(app, conn)
        test_offline_phase_metrics(app, conn)
        test_forget(app, conn)
    finally:
        conn.close()
        os.unlink(tmpdb)
    print('ALL PASS: 故障演练')


if __name__ == '__main__':
    main()
