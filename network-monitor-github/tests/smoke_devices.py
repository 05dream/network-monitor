"""设备管理 API 冒烟测试：python -m tests.smoke_devices"""
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app

app = create_app()
client = app.test_client()


def login(username, password):
    return client.post('/login', data={'username': username, 'password': password},
                       follow_redirects=False)


def main():
    # 未登录访问 API 应 401
    r = client.get('/api/devices')
    assert r.status_code == 401, f'未登录应401: {r.status_code}'

    # viewer 登录：可读不可写
    login('viewer', 'Viewer@123')
    r = client.get('/api/devices')
    assert r.status_code == 200 and r.get_json()['code'] == 0, 'viewer应可读设备列表'
    r = client.post('/api/devices', json={'name': 'X', 'host': '127.0.0.1', 'port': 2099})
    assert r.status_code == 403, f'viewer写操作应403: {r.status_code}'
    client.get('/logout')

    # admin 登录
    login('admin', 'Admin@123')

    # 参数校验：坏IP / 坏端口
    r = client.post('/api/devices', json={'name': 'R9', 'host': '999.1.1.1', 'port': 2000})
    assert r.status_code == 400, '坏IP应400'
    r = client.post('/api/devices', json={'name': 'R9', 'host': '127.0.0.1', 'port': 70000})
    assert r.status_code == 400, '坏端口应400'

    # 新增
    r = client.post('/api/devices', json={
        'name': 'TEST-R1', 'host': '127.0.0.1', 'port': 2000,
        'collect_interval': 5, 'ping_target': '192.168.30.14'})
    body = r.get_json()
    assert r.status_code == 200 and body['code'] == 0, f'新增失败: {body}'
    did = body['data']['id']

    # 列表能查到，且 collect_tasks 已建
    body = client.get('/api/devices?keyword=TEST').get_json()
    assert len(body['data']) == 1 and body['data'][0]['task_status'] == 'running', '列表/任务异常'

    # 修改
    r = client.put(f'/api/devices/{did}', json={
        'name': 'TEST-R1B', 'host': '127.0.0.1', 'port': 2001, 'collect_interval': 10})
    assert r.get_json()['code'] == 0, '修改失败'
    body = client.get('/api/devices?keyword=TEST-R1B').get_json()
    assert body['data'][0]['port'] == 2001 and body['data'][0]['collect_interval'] == 10, '修改未生效'

    # 启停
    body = client.patch(f'/api/devices/{did}/toggle').get_json()
    assert body['data']['enabled'] == 0, '停用失败'
    body = client.get('/api/devices?keyword=TEST-R1B').get_json()
    assert body['data'][0]['task_status'] == 'paused', '任务应paused'
    client.patch(f'/api/devices/{did}/toggle')

    # 删除
    r = client.delete(f'/api/devices/{did}')
    assert r.get_json()['code'] == 0, '删除失败'
    body = client.get('/api/devices?keyword=TEST-R1B').get_json()
    assert len(body['data']) == 0, '删除后仍可查到'

    # 页面 GET
    assert client.get('/devices').status_code == 200, '设备页应200'

    print('PASS: 设备管理 API 全部冒烟用例通过')


if __name__ == '__main__':
    main()
