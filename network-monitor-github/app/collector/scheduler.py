"""后台采集调度：单线程轮询，按设备各自 collect_interval 到期采集。

- 每秒扫描一次启用中的设备，到期则串行采集（3~5 台核心设备规模足够）
- 单台设备异常只记日志，不影响其他设备
- 采集结果写 device_status，更新 collect_tasks 计数，交给 detector 做事件检测
- 路由条目基线 route_count_baseline 为 0 时自动学习
"""
import sqlite3
import threading
import time
from datetime import datetime

from ..db import log_action
from . import collector, detector, simulator

_started = False
_lock = threading.Lock()
_manual = set()   # 请求立即采集的设备 id（API 线程写入，调度线程消费并移除）


def request_collect(did):
    """请求调度线程下一轮立即采集指定设备（跨线程只做标记，避免写锁冲突）"""
    _manual.add(did)


def _cleanup(app, conn):
    """数据保留清理：删过期采集历史与系统日志"""
    days = app.config.get('DATA_RETENTION_DAYS', 7)
    conn.execute("DELETE FROM device_status WHERE collected_at < "
                 "datetime('now','localtime',?)", (f'-{days} days',))
    conn.execute("DELETE FROM system_logs WHERE created_at < "
                 "datetime('now','localtime','-30 days')")
    conn.commit()
    log_action('数据清理', f'已清理 {days} 天前采集历史及 30 天前系统日志')


def _now_str():
    t = datetime.now()
    return t.strftime('%Y-%m-%d %H:%M:%S.') + f'{t.microsecond // 1000:03d}'


def _collect_one(app, conn, dev):
    # 时间戳先取：演练模拟器要靠它算"已运行秒数"，真实设备也统一以本轮开始时刻为采集时间
    ts = _now_str()
    if dev.get('source') == 'sim':
        # 演练设备：数据由模拟器按剧本产出，绝不走 telnet
        res = simulator.collect(dev, ts)
    else:
        res = collector.collect_device(dev, timeout=app.config['TELNET_TIMEOUT'])
    cur = conn.execute(
        'INSERT INTO device_status (device_id, collected_at, online, vrrp_state, '
        'ospf_neighbors, ospf_full, ospf_state, interfaces, route_count, biz_ping_ok, error) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        (dev['id'], ts, res['online'], res['vrrp_state'], res['ospf_neighbors'],
         res['ospf_full'], res['ospf_state'], res['interfaces'], res['route_count'],
         res['biz_ping_ok'], res['error']))
    fail = 0 if res['online'] else 1
    conn.execute(
        'UPDATE collect_tasks SET last_run_at=?, run_count=run_count+1, '
        'fail_count=fail_count+?, last_result=? WHERE device_id=?',
        (ts, fail, '正常' if res['online'] else res['error'][:200], dev['id']))
    if res['online'] and not dev['route_count_baseline'] and res['route_count'] > 0:
        conn.execute('UPDATE devices SET route_count_baseline=? WHERE id=?',
                     (res['route_count'], dev['id']))
        # 与采集同一连接同一事务写日志，避免 SQLite 多连接写锁冲突
        conn.execute('INSERT INTO system_logs (username, action, detail) VALUES (?,?,?)',
                     ('system', '基线学习',
                      f"设备 {dev['name']} 路由条目基线自动设为 {res['route_count']}"))
        dev = dict(dev)
        dev['route_count_baseline'] = res['route_count']
    cur_row = dict(res)
    cur_row['id'] = cur.lastrowid
    cur_row['collected_at'] = ts
    detector.process(conn, dev, cur_row)
    conn.commit()


def _loop(app):
    next_run = {}
    next_cleanup = time.time() + 60        # 启动1分钟后来一次清理，之后每6小时一次
    with app.app_context():
        while True:
            try:
                conn = sqlite3.connect(app.config['DATABASE'])
                conn.row_factory = sqlite3.Row
                if time.time() >= next_cleanup:
                    try:
                        _cleanup(app, conn)
                    except Exception as e:
                        log_action('数据清理', f'清理失败: {type(e).__name__}: {e}')
                    next_cleanup = time.time() + 6 * 3600
                devs = conn.execute(
                    'SELECT d.* FROM devices d JOIN collect_tasks t ON t.device_id = d.id '
                    "WHERE d.enabled = 1 AND t.status = 'running'").fetchall()
                now = time.time()
                for dev in devs:
                    manual = dev['id'] in _manual
                    if manual:
                        _manual.discard(dev['id'])
                    elif now < next_run.get(dev['id'], 0):
                        continue
                    try:
                        _collect_one(app, conn, dict(dev))
                    except Exception as e:      # 单台异常不扩散
                        try:
                            conn.rollback()     # 释放可能残留的写事务，避免日志写锁冲突
                        except Exception:
                            pass
                        log_action('采集异常', f"设备 {dev['name']}: {type(e).__name__}: {e}")
                    next_run[dev['id']] = time.time() + dev['collect_interval']
                conn.close()
            except Exception as e:
                log_action('调度异常', f'{type(e).__name__}: {e}')
            time.sleep(1)


def start(app):
    """启动后台调度线程（幂等）"""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, args=(app,), daemon=True, name='collector').start()
    with app.app_context():
        log_action('系统启动', '后台采集调度线程已启动')
