"""端到端联调：python -m tests.smoke_e2e

mock eNSP（可切换正常/故障场景）-> scheduler._collect_one 全链路
-> 验证事件/时间线/告警 -> test_client 验证全部查询 API 与页面
"""
import os
import socket
import sqlite3
import sys
import threading
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app
from app.collector import scheduler
from app.db import init_db

STATE = {'vrrp': 'Master', 'ping_ok': True}

MASTER = '10    Master       Vlanif10                 Normal   192.168.10.254'
BACKUP = '10    Backup       Vlanif10                 Normal   192.168.10.254'


def responses():
    return {
        'display vrrp brief': f'\nVRID  State        Interface                Type     Virtual IP\n{MASTER if STATE["vrrp"] == "Master" else BACKUP}\n',
        'display ospf peer brief': '\n Area Id          Interface                        Neighbor id      State\n 0.0.0.0          GigabitEthernet0/0/0              2.2.2.2          Full\n',
        'display interface brief': '\nInterface                   PHY   Protocol InUti OutUti   inErrors  outErrors\nGigabitEthernet0/0/0        up    up        0%     0%          0          0\n',
        'display ip routing-table': '\nDestination/Mask    Proto   Pre  Cost      Flags NextHop         Interface\n\n        1.1.1.1/32  Direct  0    0           D   127.0.0.1       LoopBack0\n      10.0.12.0/24  Direct  0    0           D   10.0.12.1       GigabitEthernet0/0/0\n',
        'ping -c 3 192.168.30.14': ('\n    3 packet(s) received\n' if STATE['ping_ok'] else '\n    0 packet(s) received\n'),
    }


def server(sock):
    while True:
        conn, _ = sock.accept()
        conn.sendall(bytes([255, 251, 1]))
        buf = b''
        try:
            while True:
                data = conn.recv(1024)
                if not data:
                    break
                buf += data
                # 客户端只发 \r 作为行结束符（eNSP 串口把 \n 也当回车会导致输出错位），这里按 \r 拆行
                while b'\r' in buf:
                    line, buf = buf.split(b'\r', 1)
                    cmd = line.strip().decode('utf-8', 'ignore')
                    if not cmd:
                        conn.sendall(b'\r\n<R1>')
                    else:
                        conn.sendall(b'\r\n' + responses().get(cmd, '').encode() + b'\r\n<R1>')
        except OSError:
            pass
        conn.close()


def main():
    import tempfile
    app = create_app()
    fd, tmpdb = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    app.config['DATABASE'] = tmpdb
    with app.app_context():
        init_db()

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', 0))
    srv.listen(5)
    port = srv.getsockname()[1]
    threading.Thread(target=server, args=(srv,), daemon=True).start()

    conn = sqlite3.connect(tmpdb)
    conn.row_factory = sqlite3.Row
    conn.execute("INSERT INTO devices (name, host, port, ping_target, collect_interval) "
                 "VALUES ('R1', '127.0.0.1', ?, '192.168.30.14', 1)", (port,))
    did = conn.execute('SELECT id FROM devices').fetchone()['id']
    conn.execute('INSERT INTO collect_tasks (device_id) VALUES (?)', (did,))
    conn.commit()

    def round_():
        with app.app_context():
            dev = dict(conn.execute('SELECT * FROM devices WHERE id=?', (did,)).fetchone())
            scheduler._collect_one(app, conn, dev)
        time.sleep(0.02)

    # 两轮正常：基线学习
    round_(); round_()
    dev = conn.execute('SELECT * FROM devices WHERE id=?', (did,)).fetchone()
    assert dev['route_count_baseline'] == 2, '基线应自动学习为2'
    assert conn.execute('SELECT COUNT(*) c FROM switch_events').fetchone()['c'] == 0

    # 故障：VRRP 切到 Backup 且业务不通，两轮后确认事件
    STATE['vrrp'] = 'Backup'; STATE['ping_ok'] = False
    round_(); round_()
    ev = conn.execute("SELECT * FROM switch_events WHERE event_type='vrrp_switch'").fetchone()
    assert ev and ev['status'] == 'pending', '故障两轮后应确认VRRP切换事件'
    tl = conn.execute('SELECT * FROM event_timeline WHERE event_id=?', (ev['id'],)).fetchone()
    assert tl['t1_at'] and tl['detect_ms'] is not None

    # 恢复：两轮后 T2/T3/T4 齐，再一轮关闭
    STATE['ping_ok'] = True
    round_(); round_(); round_()
    tl = conn.execute('SELECT * FROM event_timeline WHERE event_id=?', (ev['id'],)).fetchone()
    assert tl['t2_at'] and tl['t3_at'] and tl['t4_at'], dict(tl)
    assert tl['converge_ms'] is not None and tl['recover_ms'] is not None and tl['total_ms'] is not None
    assert conn.execute('SELECT status FROM switch_events WHERE id=?', (ev['id'],)).fetchone()['status'] == 'closed'
    n_status = conn.execute('SELECT COUNT(*) c FROM device_status WHERE device_id=?', (did,)).fetchone()['c']
    assert n_status == 7, f'应有7轮状态: {n_status}'
    print('PASS: 全链路采集->状态机->时间线->关闭')

    # API + 页面验证
    client = app.test_client()
    client.post('/login', data={'username': 'admin', 'password': 'Admin@123'})
    checks = [
        ('GET', '/api/dashboard', 200),
        ('GET', f'/api/devices/{did}/detail', 200),
        ('GET', f'/api/devices/{did}/history', 200),
        ('GET', '/api/events', 200),
        ('GET', '/api/events?status=closed&type=vrrp_switch', 200),
        ('GET', '/api/alerts', 200),
        ('GET', '/api/logs?page=1&size=10', 200),
        ('GET', '/', 200),
        ('GET', f'/device/{did}', 200),
        ('GET', '/events', 200),
        ('GET', '/logs', 200),
    ]
    for method, url, expect in checks:
        r = getattr(client, method.lower())(url)
        assert r.status_code == expect, f'{url} -> {r.status_code}'
        if url.startswith('/api/'):
            assert r.get_json()['code'] == 0, f'{url} code!=0'

    # CSV 导出
    r = client.get('/api/events/export')
    assert r.status_code == 200 and 'text/csv' in r.content_type
    assert '总中断ms'.encode('utf-8') in r.data and b'vrrp_switch' in r.data

    # 告警确认
    aid = conn.execute("SELECT id FROM alerts WHERE status='pending' LIMIT 1").fetchone()
    if aid:
        r = client.post(f"/api/alerts/{aid['id']}/confirm")
        assert r.get_json()['code'] == 0
        assert conn.execute('SELECT status FROM alerts WHERE id=?', (aid['id'],)).fetchone()['status'] == 'confirmed'

    # 仪表盘数据内容
    d = client.get('/api/dashboard').get_json()['data']
    assert d['devices_total'] == 1 and d['devices_online'] == 1 and d['vrrp_backups'] == 1, d

    conn.close()
    os.unlink(tmpdb)
    print('PASS: API / 页面 / CSV导出 / 告警确认 / 仪表盘数据')
    print('ALL PASS')


if __name__ == '__main__':
    main()
