import os
import sqlite3

from flask import current_app, g, has_request_context
from werkzeug.security import generate_password_hash


def get_db():
    """获取当前请求的数据库连接（按请求复用）"""
    if 'db' not in g:
        g.db = sqlite3.connect(current_app.config['DATABASE'])
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys = ON')
    return g.db


def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


# 幂等迁移清单：(表, 列, 定义)。老库启动时自动 ALTER 补列，新库建表已含这些列则为空操作
_MIGRATIONS = [
    ('devices', 'role', "TEXT DEFAULT 'other'"),
    ('devices', 'pos_x', 'REAL'),
    ('devices', 'pos_y', 'REAL'),
    ('devices', 'topo_visible', 'INTEGER DEFAULT 1'),
    ('devices', 'source', "TEXT DEFAULT 'telnet'"),
    ('devices', 'drill_state', "TEXT DEFAULT ''"),
]


def migrate(db):
    """为已存在的老库补齐新增列（SQLite 的 ADD COLUMN 必须带默认值，这里全部满足）"""
    added = []
    for table, col, spec in _MIGRATIONS:
        cols = {r['name'] for r in db.execute(f'PRAGMA table_info({table})').fetchall()}
        if col in cols:
            continue
        db.execute(f'ALTER TABLE {table} ADD COLUMN {col} {spec}')
        added.append(f'{table}.{col}')
    if added:
        # 此处不能用 log_action：同一连接事务未提交，另开连接会撞写锁
        db.execute('INSERT INTO system_logs (username, action, detail) VALUES (?,?,?)',
                   ('system', '数据库迁移', '新增列：' + ', '.join(added)))
    db.commit()


def init_db():
    """按 schema.sql 建表 + 补迁移 + 写入种子用户（幂等，可重复执行）"""
    db = sqlite3.connect(current_app.config['DATABASE'])
    db.row_factory = sqlite3.Row
    with open(current_app.config['SCHEMA'], encoding='utf-8') as f:
        db.executescript(f.read())
    # 种子用户：仅用于本地演示；部署时可用环境变量 ADMIN_PASSWORD / VIEWER_PASSWORD 覆盖
    seed_users = [
        ('admin', os.environ.get('ADMIN_PASSWORD', 'Admin@123'), 'admin'),
        ('viewer', os.environ.get('VIEWER_PASSWORD', 'Viewer@123'), 'viewer'),
    ]
    for username, pwd, role in seed_users:
        exists = db.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
        if not exists:
            db.execute('INSERT INTO users (username, password_hash, role) VALUES (?,?,?)',
                       (username, generate_password_hash(pwd), role))
    db.commit()
    migrate(db)
    db.close()


def log_action(action, detail='', username='system'):
    """写系统日志（供各模块调用，自动适配请求内/后台线程两种场景）"""
    if has_request_context():
        db = get_db()
        db.execute('INSERT INTO system_logs (username, action, detail) VALUES (?,?,?)',
                   (username, action, detail))
        db.commit()
        return
    # 后台线程等无请求上下文的场景，独立开短连接
    db = sqlite3.connect(current_app.config['DATABASE'])
    db.execute('INSERT INTO system_logs (username, action, detail) VALUES (?,?,?)',
               (username, action, detail))
    db.commit()
    db.close()


def init_app(app):
    app.teardown_appcontext(close_db)
    # 每次启动都执行：建表用 IF NOT EXISTS、种子用户与补列迁移均幂等，老库可安全升级
    with app.app_context():
        init_db()
