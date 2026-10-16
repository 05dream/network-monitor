"""采集器 + 事件检测综合冒烟：python -m tests.smoke_collector

1. parsers 解析样例 VRP 输出
2. mock eNSP 串口 telnet 服务，端到端跑 collect_device
3. 临时库上驱动 detector 状态机，验证两次确认 + T1~T4 + 开关事件
"""
import os
import socket
import sqlite3
import sys
import threading
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app
from app.collector import collector, detector, parsers

# ---------- 样例 VRP 输出 ----------
VRRP_OUT = """
Total:2     Master:1     Backup:1     Non-active:0
VRID  State        Interface                Type     Virtual IP
10    Master       Vlanif10                 Normal   192.168.10.254
30    Master       Vlanif30                 Normal   192.168.30.254
"""
OSPF_OUT = """
	 OSPF Process 1 with Router ID 1.1.1.1
		  Peer Statistic Information
 ----------------------------------------------------------------------------
 Area Id          Interface                        Neighbor id      State
 0.0.0.0          GigabitEthernet0/0/0              2.2.2.2          Full
 0.0.0.0          GigabitEthernet0/0/1              3.3.3.3          2-Way
 ----------------------------------------------------------------------------
"""
INTF_OUT = """
Interface                   PHY   Protocol InUti OutUti   inErrors  outErrors
GigabitEthernet0/0/0        up    up        0%     0%          0          0
GigabitEthernet0/0/1        down  down      0%     0%          0          0
LoopBack0                   up    up(s)     0%     0%          0          0
"""
ROUTE_OUT = """
Route Flags: R - relay, D - download to fib
------------------------------------------------------------------------------
Routing Tables: Public
         Destinations : 4        Routes : 4

Destination/Mask    Proto   Pre  Cost      Flags NextHop         Interface

        1.1.1.1/32  Direct  0    0           D   127.0.0.1       LoopBack0
        2.2.2.2/32  OSPF    10   1           D   10.0.12.2       GigabitEthernet0/0/0
      10.0.12.0/24  Direct  0    0           D   10.0.12.1       GigabitEthernet0/0/0
   192.168.10.0/24  Direct  0    0           D   192.168.10.1    Vlanif10
"""
PING_OK = """
  PING 192.168.30.14: 56  data bytes, press CTRL_C to break
    Reply from 192.168.30.14: bytes=56 Sequence=1 ttl=63 time=2 ms

  --- 192.168.30.14 ping statistics ---
    3 packet(s) transmitted
    3 packet(s) received
    0.00% packet loss
"""
PING_FAIL = """
  --- 192.168.30.14 ping statistics ---
    3 packet(s) transmitted
    0 packet(s) received
    100.00% packet loss
"""

RESPONSES = {
    'display vrrp brief': VRRP_OUT,
    'display ospf peer brief': OSPF_OUT,
    'display interface brief': INTF_OUT,
    'display ip routing-table': ROUTE_OUT,
    'ping -c 3 192.168.30.14': PING_OK,
}


def mock_ensp_server(sock):
    """模拟 eNSP 串口重定向：IAC 协商 + 按命令回样例输出 + <R1> 提示符"""
    conn, _ = sock.accept()
    conn.sendall(bytes([255, 251, 1, 255, 251, 3, 255, 253, 24]))  # IAC 协商
    buf = b''
    while True:
        try:
            data = conn.recv(1024)
        except OSError:
            break
        if not data:
            break
        buf += data
        # 客户端只发 \r 作为行结束符（eNSP 串口把 \n 也当回车会导致输出错位），这里按 \r 拆行
        while b'\r' in buf:
            line, buf = buf.split(b'\r', 1)
            cmd = line.strip().decode('utf-8', 'ignore')
            if not cmd:
                conn.sendall(b'\r\n<R1>')
                continue
            out = RESPONSES.get(cmd, '')
            conn.sendall(b'\r\n' + out.encode() + b'\r\n<R1>')
    conn.close()


def test_parsers():
    vrrp = parsers.parse_vrrp(VRRP_OUT)
    assert vrrp == [{'vrid': 10, 'state': 'Master', 'interface': 'Vlanif10', 'vip': '192.168.10.254'},
                    {'vrid': 30, 'state': 'Master', 'interface': 'Vlanif30', 'vip': '192.168.30.254'}], vrrp
    peers = parsers.parse_ospf_peers(OSPF_OUT)
    assert len(peers) == 2 and peers[0]['neighbor'] == '2.2.2.2' and peers[0]['state'] == 'Full'
    assert peers[1]['state'] == '2-Way'
    intfs = parsers.parse_interfaces(INTF_OUT)
    assert {'name': 'LoopBack0', 'phy': 'up', 'protocol': 'up'} in intfs, intfs
    assert parsers.parse_route_count(ROUTE_OUT) == 4
    assert parsers.parse_ping_ok(PING_OK) is True
    assert parsers.parse_ping_ok(PING_FAIL) is False
    print('PASS: parsers')


def test_collect():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    threading.Thread(target=mock_ensp_server, args=(srv,), daemon=True).start()

    dev = {'host': '127.0.0.1', 'port': port, 'ping_target': '192.168.30.14'}
    res = collector.collect_device(dev, timeout=5)
    assert res['online'] == 1, res
    assert res['ospf_neighbors'] == 2 and res['ospf_full'] == 1, res
    assert res['route_count'] == 4, res
    assert res['biz_ping_ok'] == 1, res
    assert res['error'] == '', res

    # 连不上的端口应收敛为 online=0
    bad = {'host': '127.0.0.1', 'port': 9, 'ping_target': ''}
    res2 = collector.collect_device(bad, timeout=2)
    assert res2['online'] == 0 and res2['error'], res2
    print('PASS: collect_device（mock eNSP + 异常收敛）')


def _insert_status(conn, did, **kw):
    defaults = dict(online=1, vrrp_state='[]', ospf_neighbors=2, ospf_full=2,
                    ospf_state='[]', interfaces='[]', route_count=10, biz_ping_ok=1,
                    error='', collected_at=detector.now_ms())
    defaults.update(kw)
    cur = conn.execute(
        'INSERT INTO device_status (device_id, collected_at, online, vrrp_state, '
        'ospf_neighbors, ospf_full, ospf_state, interfaces, route_count, biz_ping_ok, error) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        (did, defaults['collected_at'], defaults['online'], defaults['vrrp_state'],
         defaults['ospf_neighbors'], defaults['ospf_full'], defaults['ospf_state'],
         defaults['interfaces'], defaults['route_count'], defaults['biz_ping_ok'],
         defaults['error']))
    conn.commit()
    defaults['id'] = cur.lastrowid
    return defaults


def test_detector():
    import tempfile
    from app.db import init_db
    app = create_app()
    fd, tmpdb = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    app.config['DATABASE'] = tmpdb
    with app.app_context():
        init_db()
    conn = sqlite3.connect(tmpdb)
    conn.row_factory = sqlite3.Row
    conn.execute("INSERT INTO devices (name, host, port, ping_target, route_count_baseline) "
                 "VALUES ('R1', '127.0.0.1', 2000, '192.168.30.14', 10)")
    did = conn.execute('SELECT id FROM devices').fetchone()['id']
    dev = dict(conn.execute('SELECT * FROM devices WHERE id=?', (did,)).fetchone())

    master = '[{"vrid": 10, "state": "Master", "interface": "Vlanif10", "vip": "192.168.10.254"}]'
    backup = '[{"vrid": 10, "state": "Backup", "interface": "Vlanif10", "vip": "192.168.10.254"}]'

    # 两轮正常 -> 学习基线
    for _ in range(2):
        detector.process(conn, dev, _insert_status(conn, did, vrrp_state=master))
    assert conn.execute("SELECT COUNT(*) c FROM switch_events").fetchone()['c'] == 0

    # 第一轮 Backup：只是候选，不产生事件
    detector.process(conn, dev, _insert_status(conn, did, vrrp_state=backup))
    assert conn.execute("SELECT COUNT(*) c FROM switch_events").fetchone()['c'] == 0

    # 第二轮 Backup：确认切换 -> 事件 + T1
    time.sleep(0.02)
    detector.process(conn, dev, _insert_status(conn, did, vrrp_state=backup, ospf_full=1,
                                               route_count=8, biz_ping_ok=0))
    ev = conn.execute("SELECT * FROM switch_events WHERE event_type='vrrp_switch'").fetchone()
    assert ev and ev['status'] == 'pending', dict(ev) if ev else None
    tl = conn.execute('SELECT * FROM event_timeline WHERE event_id=?', (ev['id'],)).fetchone()
    assert tl['t1_at'] and tl['detect_ms'] is not None and tl['t2_at'] is None
    alert = conn.execute("SELECT * FROM alerts WHERE event_id=?", (ev['id'],)).fetchone()
    assert alert['level'] == 'warning' and alert['status'] == 'pending'

    # OSPF 全 Full -> T2（路由数较上一轮变化，避免触发"路由稳定"的 T3 判定）
    time.sleep(0.02)
    detector.process(conn, dev, _insert_status(conn, did, vrrp_state=backup, route_count=9,
                                               biz_ping_ok=0))
    tl = conn.execute('SELECT * FROM event_timeline WHERE event_id=?', (ev['id'],)).fetchone()
    assert tl['t2_at'] and tl['t3_at'] is None

    # 路由回基线 -> T3 + 收敛时间
    time.sleep(0.02)
    detector.process(conn, dev, _insert_status(conn, did, vrrp_state=backup, biz_ping_ok=0))
    tl = conn.execute('SELECT * FROM event_timeline WHERE event_id=?', (ev['id'],)).fetchone()
    assert tl['t3_at'] and tl['converge_ms'] is not None and tl['t4_at'] is None

    # 第一轮 ping 通 -> T4 记录但事件不关（需两轮恢复确认）
    time.sleep(0.02)
    detector.process(conn, dev, _insert_status(conn, did, vrrp_state=backup))
    tl = conn.execute('SELECT * FROM event_timeline WHERE event_id=?', (ev['id'],)).fetchone()
    assert tl['t4_at'] and tl['recover_ms'] is not None and tl['total_ms'] is not None
    assert conn.execute("SELECT status FROM switch_events WHERE id=?", (ev['id'],)).fetchone()['status'] == 'pending'

    # 第二轮 ping 通 -> 事件关闭
    detector.process(conn, dev, _insert_status(conn, did, vrrp_state=backup))
    assert conn.execute("SELECT status FROM switch_events WHERE id=?", (ev['id'],)).fetchone()['status'] == 'closed'

    # 离线两轮 -> critical 事件；恢复两轮 -> 关闭
    for _ in range(2):
        detector.process(conn, dev, _insert_status(conn, did, online=0, error='timeout'))
    off = conn.execute("SELECT * FROM switch_events WHERE event_type='offline'").fetchone()
    assert off and off['status'] == 'pending'
    crit = conn.execute("SELECT * FROM alerts WHERE event_id=? AND level='critical'", (off['id'],)).fetchone()
    assert crit
    for _ in range(2):
        detector.process(conn, dev, _insert_status(conn, did, vrrp_state=backup))
    assert conn.execute("SELECT status FROM switch_events WHERE id=?", (off['id'],)).fetchone()['status'] == 'closed'

    conn.close()
    os.unlink(tmpdb)
    print('PASS: detector 状态机（两次确认/两次解除 + T1~T4 指标）')


if __name__ == '__main__':
    test_parsers()
    test_collect()
    test_detector()
    print('ALL PASS')
