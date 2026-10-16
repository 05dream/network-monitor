"""系统日志查询接口（分页）"""
from flask import request

from ..auth import login_required
from ..db import get_db
from . import bp
from .devices import ok


@bp.get('/logs')
@login_required
def list_logs():
    page = max(request.args.get('page', 1, type=int), 1)
    size = min(max(request.args.get('size', 20, type=int), 1), 100)
    kw = (request.args.get('keyword') or '').strip()
    db = get_db()
    where, args = '', []
    if kw:
        where = 'WHERE username LIKE ? OR action LIKE ? OR detail LIKE ?'
        args = [f'%{kw}%'] * 3
    total = db.execute(f'SELECT COUNT(*) c FROM system_logs {where}', args).fetchone()['c']
    rows = db.execute(
        f'SELECT * FROM system_logs {where} ORDER BY id DESC LIMIT ? OFFSET ?',
        args + [size, (page - 1) * size]).fetchall()
    return ok({'total': total, 'page': page, 'size': size,
               'items': [dict(r) for r in rows]})
