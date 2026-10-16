import functools

from flask import (Blueprint, flash, g, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_db, log_action

bp = Blueprint('auth', __name__)


def login_required(view):
    """必须登录才能访问"""
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            if request.path.startswith('/api/'):
                return {'code': 401, 'msg': '未登录'}, 401
            return redirect(url_for('auth.login', next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    """仅管理员可操作"""
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            if request.path.startswith('/api/'):
                return {'code': 401, 'msg': '未登录'}, 401
            return redirect(url_for('auth.login', next=request.path))
        if g.user['role'] != 'admin':
            if request.path.startswith('/api/'):
                return {'code': 403, 'msg': '需要管理员权限'}, 403
            flash('需要管理员权限')
            return redirect(url_for('pages.dashboard'))
        return view(*args, **kwargs)
    return wrapped


@bp.before_app_request
def load_logged_in_user():
    user_id = session.get('user_id')
    if user_id is None:
        g.user = None
    else:
        g.user = get_db().execute(
            'SELECT id, username, role FROM users WHERE id=?', (user_id,)).fetchone()


@bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
        if user and check_password_hash(user['password_hash'], password):
            session.clear()
            session['user_id'] = user['id']
            log_action('登录系统', f'用户 {username} 登录成功', username)
            return redirect(request.args.get('next') or url_for('pages.dashboard'))
        log_action('登录失败', f'用户 {username} 密码错误或不存在', username)
        flash('用户名或密码错误')
    return render_template('login.html')


@bp.route('/register', methods=['GET', 'POST'])
def register():
    """公开注册：账号密码写入 users 表，新账号默认为查看者（viewer，只读）"""
    if g.user is not None:
        return redirect(url_for('pages.dashboard'))
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        password2 = request.form.get('password2') or ''
        db = get_db()
        if not 2 <= len(username) <= 30 or ' ' in username:
            flash('用户名需为 2~30 个字符且不含空格')
        elif len(password) < 6:
            flash('密码至少 6 位')
        elif password != password2:
            flash('两次输入的密码不一致')
        elif db.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone():
            flash('用户名已存在，请换一个')
        else:
            cur = db.execute('INSERT INTO users (username, password_hash, role) VALUES (?,?,?)',
                             (username, generate_password_hash(password), 'viewer'))
            db.commit()
            session.clear()
            session['user_id'] = cur.lastrowid
            log_action('注册账号', f'新用户 {username} 注册成功（查看者）', username)
            return redirect(url_for('pages.dashboard'))
    return render_template('register.html')


@bp.route('/logout')
def logout():
    username = g.user['username'] if g.user else ''
    log_action('退出系统', f'用户 {username} 退出', username or 'system')
    session.clear()
    return redirect(url_for('auth.login'))
