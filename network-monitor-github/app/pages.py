from flask import Blueprint, g, render_template

from .auth import login_required

bp = Blueprint('pages', __name__)


@bp.route('/')
@login_required
def dashboard():
    return render_template('dashboard.html')


@bp.route('/devices')
@login_required
def devices():
    return render_template('devices.html')


@bp.route('/device/<int:did>')
@login_required
def device_detail(did):
    return render_template('device_detail.html', device_id=did)


@bp.route('/topology')
@login_required
def topology():
    if g.user['role'] != 'admin':
        from flask import redirect, url_for
        return redirect(url_for('pages.dashboard'))
    return render_template('topology.html')


@bp.route('/drill')
@login_required
def drill():
    if g.user['role'] != 'admin':
        from flask import redirect, url_for
        return redirect(url_for('pages.dashboard'))
    return render_template('drill.html')


@bp.route('/screen')
@login_required
def screen():
    """全屏数据大屏：独立模板（不带侧边栏），用于答辩投屏"""
    return render_template('screen.html')


@bp.route('/reports')
@login_required
def reports():
    return render_template('reports.html')


@bp.route('/events')
@login_required
def events():
    return render_template('events.html')


@bp.route('/logs')
@login_required
def logs():
    return render_template('logs.html')


@bp.route('/users')
@login_required
def users():
    if g.user['role'] != 'admin':
        from flask import redirect, url_for
        return redirect(url_for('pages.dashboard'))
    return render_template('users.html')
