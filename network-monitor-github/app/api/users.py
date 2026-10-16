"""用户管理接口（仅管理员）"""
from flask import g, request
from werkzeug.security import generate_password_hash

from ..auth import admin_required
from ..db import get_db, log_action
from . import bp
from .devices import err, ok


@bp.get('/users')
@admin_required
def list_users():
    rows = get_db().execute(
        'SELECT id, username, role, created_at FROM users ORDER BY id').fetchall()
    return ok([dict(r) for r in rows])


@bp.post('/users')
@admin_required
def create_user():
    d = request.get_json(silent=True) or request.form
    username = (d.get('username') or '').strip()
    password = d.get('password') or ''
    role = d.get('role') or 'viewer'
    if not username or len(username) > 30:
        return err('用户名必填且不超过30字符')
    if role not in ('admin', 'viewer'):
        return err('角色只能是 admin 或 viewer')
    if len(password) < 6:
        return err('密码至少6位')
    db = get_db()
    if db.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone():
        return err('用户名已存在')
    cur = db.execute('INSERT INTO users (username, password_hash, role) VALUES (?,?,?)',
                     (username, generate_password_hash(password), role))
    db.commit()
    log_action('新增用户', f'新增用户 {username}（{role}）', g.user['username'])
    return ok({'id': cur.lastrowid})


@bp.put('/users/<int:uid>')
@admin_required
def update_user(uid):
    """修改用户：重置密码 / 调整角色（两者可选其一或同时）"""
    db = get_db()
    u = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
    if not u:
        return err('用户不存在', 404)
    d = request.get_json(silent=True) or request.form
    role = d.get('role') or u['role']
    password = d.get('password') or ''
    if role not in ('admin', 'viewer'):
        return err('角色只能是 admin 或 viewer')
    if u['id'] == g.user['id'] and role != 'admin':
        return err('不能降级当前登录账号')
    if password and len(password) < 6:
        return err('密码至少6位')
    if password:
        db.execute('UPDATE users SET role=?, password_hash=? WHERE id=?',
                   (role, generate_password_hash(password), uid))
    else:
        db.execute('UPDATE users SET role=? WHERE id=?', (role, uid))
    db.commit()
    log_action('修改用户',
               f'修改用户 {u["username"]}（角色 {role}{"，已重置密码" if password else ""}）',
               g.user['username'])
    return ok()


@bp.delete('/users/<int:uid>')
@admin_required
def delete_user(uid):
    if uid == g.user['id']:
        return err('不能删除当前登录账号')
    db = get_db()
    u = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
    if not u:
        return err('用户不存在', 404)
    if u['role'] == 'admin':
        admins = db.execute("SELECT COUNT(*) c FROM users WHERE role='admin'").fetchone()['c']
        if admins <= 1:
            return err('系统至少需要保留一个管理员')
    db.execute('DELETE FROM users WHERE id=?', (uid,))
    db.commit()
    log_action('删除用户', f'删除用户 {u["username"]}', g.user['username'])
    return ok()
