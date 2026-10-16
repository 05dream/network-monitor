"""巡检报告冒烟：python -m tests.smoke_report

- render_report 输出自包含 HTML（内联 style、服务端 SVG、设备名、结论与建议）
- /api/report/generate 落盘、/api/report/list 列出、/api/report/download 预览
- 非法文件名被拒、viewer 无权生成/删除
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app
from app.db import init_db
from app.report import render_report


def _now():
    from datetime import datetime
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S.000')


def _prepare():
    app = create_app()
    fd, tmpdb = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    repdir = tempfile.mkdtemp(suffix='-reports')
    app.config['DATABASE'] = tmpdb
    app.config['REPORT_DIR'] = repdir
    with app.app_context():
        init_db()
    conn = sqlite3.connect(tmpdb)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO devices (name, host, port, role, pos_x, pos_y, topo_visible, enabled, "
        "route_count_baseline) VALUES ('CORE1', '127.0.0.1', 2000, 'core', 300, 80, 1, 1, 10)")
    did = conn.execute('SELECT id FROM devices').fetchone()['id']
    conn.execute('INSERT INTO collect_tasks (device_id, status, run_count, fail_count, '
                 'last_run_at, last_result) VALUES (?,?,?,?,?,?)',
                 (did, 'running', 10, 0, _now(), '正常'))
    for i in range(5):
        conn.execute(
            'INSERT INTO device_status (device_id, collected_at, online, vrrp_state, '
            'ospf_neighbors, ospf_full, ospf_state, interfaces, route_count, biz_ping_ok, error) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (did, _now(), 1,
             '[{"vrid": 10, "state": "Master", "interface": "Vlanif10", "vip": "192.168.10.254"}]',
             2, 2, '[]', '[]', 10 + i, 1, ''))
    cur = conn.execute(
        "INSERT INTO switch_events (device_id, event_type, title, started_at, ended_at, status) "
        "VALUES (?,?,?,?,?,?)",
        (did, 'vrrp_switch', 'CORE1 VRRP 主备角色切换', _now(), _now(), 'closed'))
    eid = cur.lastrowid
    conn.execute('INSERT INTO event_timeline (event_id, t1_at, t2_at, t3_at, t4_at, '
                 'detect_ms, converge_ms, recover_ms, total_ms) VALUES (?,?,?,?,?,?,?,?,?)',
                 (eid, _now(), _now(), _now(), _now(), 1200, 800, 3400, 4600))
    conn.execute('INSERT INTO alerts (device_id, event_id, level, message, status) '
                 "VALUES (?,?,?,?,?)", (did, eid, 'warning', 'CORE1 VRRP 主备切换', 'pending'))
    conn.commit()
    return app, tmpdb, repdir, conn, did


def test_render(conn):
    html = render_report(conn, 24)
    assert html.startswith('<!DOCTYPE html>'), '报告应为完整 HTML 文档'
    for token in ('<style>', '</style>', '<svg', 'CORE1', '三、结论与建议',
                  '五、故障事件与时间线', 'VRID10 Master'):
        assert token in html, f'报告缺少内容：{token}'
    # 自包含：不应引用任何外部资源
    assert 'src="http' not in html and 'href="http' not in html, '报告不应包含外部链接'
    assert 'style.css' not in html, '报告样式应内联，不引用外部 css'
    print('PASS: render_report（自包含 SVG 报告 + 内联样式 + 结论）')


def test_api(app):
    client = app.test_client()
    # 未登录：401
    assert client.get('/api/report/list').status_code == 401, '未登录应 401'

    # viewer 可预览、不可生成/删除
    client.post('/login', data={'username': 'viewer', 'password': 'Viewer@123'})
    assert client.get('/api/report/preview?hours=24').status_code == 200, 'viewer 可预览'
    assert client.post('/api/report/generate', json={'hours': 24}).status_code == 403, 'viewer 不可生成'
    client.get('/logout')

    # admin 生成 + 列表 + 下载
    client.post('/login', data={'username': 'admin', 'password': 'Admin@123'})
    r = client.post('/api/report/generate', json={'hours': 24})
    body = r.get_json()
    assert r.status_code == 200 and body['code'] == 0, f'生成失败: {body}'
    name = body['data']['name']
    assert os.path.isfile(os.path.join(app.config['REPORT_DIR'], name)), '报告未落盘'

    items = client.get('/api/report/list').get_json()['data']
    assert any(it['name'] == name for it in items), '列表未包含新报告'

    r = client.get(f'/api/report/download/{name}')
    assert r.status_code == 200 and b'<svg' in r.data, '下载/预览失败'
    r.close()                                   # 关闭响应以释放文件句柄（Windows 删除前必须）
    r = client.get(f'/api/report/download/{name}?dl=1')
    assert 'attachment' in r.headers.get('Content-Disposition', ''), 'dl=1 应触发附件下载'
    r.close()

    # 非法文件名被拒
    assert client.get('/api/report/download/report_bad.txt').status_code == 404, '非法文件名应 404'
    assert client.delete('/api/report/passwd').status_code == 404, '非法文件名删除应 404'

    # 删除刚生成的报告
    assert client.delete(f'/api/report/{name}').get_json()['code'] == 0, '删除失败'
    assert not client.get('/api/report/list').get_json()['data'], '删除后列表应为空'
    print('PASS: 报告接口（预览/生成/列表/下载/删除/权限/非法文件名）')


def test_pages(app):
    client = app.test_client()
    client.post('/login', data={'username': 'admin', 'password': 'Admin@123'})
    assert client.get('/reports').status_code == 200, '报告页应 200'
    assert client.get('/drill').status_code == 200, '演练页应 200'
    # 大屏是独立模板：没有侧边栏，但要有拓扑画布
    r = client.get('/screen')
    assert r.status_code == 200, '大屏页应 200'
    html = r.get_data(as_text=True)
    assert 'class="sidebar"' not in html, '大屏不应包含侧边栏'
    assert 'id="topo"' in html, '大屏应包含拓扑画布'
    # 回归：原有两个页面仍可访问
    assert client.get('/').status_code == 200, '仪表盘应 200'
    assert client.get('/topology').status_code == 200, '拓扑管理页应 200'
    print('PASS: 页面路由（/reports /screen 无侧边栏 /drill + 仪表盘/拓扑无回归）')


def main():
    app, tmpdb, repdir, conn, did = _prepare()
    try:
        test_render(conn)
        test_api(app)
        test_pages(app)
    finally:
        conn.close()
        os.unlink(tmpdb)
        for fn in os.listdir(repdir):
            os.unlink(os.path.join(repdir, fn))
        os.rmdir(repdir)
    print('ALL PASS: 巡检报告')


if __name__ == '__main__':
    main()
