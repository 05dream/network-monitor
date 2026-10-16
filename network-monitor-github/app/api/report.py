"""巡检报告接口：预览 / 生成落盘 / 历史列表 / 下载 / 删除。

报告是自包含 HTML 文件（内联 CSS + 服务端 SVG），存到 REPORT_DIR 目录，
可以离线打开、打印、拷走存档，因此这里只做目录读写，不建数据库表。
"""
import os
import re
from datetime import datetime

from flask import Response, current_app, g, request, send_from_directory

from ..auth import admin_required, login_required
from ..db import get_db, log_action
from ..report import render_report
from . import bp
from .devices import err, ok

# 文件名白名单：既校验命名格式，也防止路径穿越
_NAME_RE = re.compile(r'^report_\d{8}_\d{6}_\d{1,3}h\.html$')


def _hours(source=None):
    raw = (source or {}).get('hours') if isinstance(source, dict) else source
    try:
        return min(max(int(raw), 1), 720)
    except (TypeError, ValueError):
        return 24


def _dir():
    d = current_app.config['REPORT_DIR']
    os.makedirs(d, exist_ok=True)
    return d


@bp.get('/report/preview')
@login_required
def report_preview():
    """直接返回报告 HTML，供新窗口/iframe 预览（不落盘）"""
    return Response(render_report(get_db(), _hours(request.args.get('hours'))),
                    mimetype='text/html')


@bp.post('/report/generate')
@admin_required
def report_generate():
    """生成报告并落盘，返回文件名与访问地址"""
    hours = _hours(request.get_json(silent=True))
    name = f'report_{datetime.now():%Y%m%d_%H%M%S}_{hours}h.html'
    try:
        with open(os.path.join(_dir(), name), 'w', encoding='utf-8') as f:
            f.write(render_report(get_db(), hours))
    except OSError as e:
        return err(f'报告写入失败：{type(e).__name__}: {e}')
    log_action('生成巡检报告', f'生成巡检报告 {name}（统计周期 {hours} 小时）', g.user['username'])
    return ok({'name': name, 'hours': hours, 'url': f'/api/report/download/{name}'})


@bp.get('/report/list')
@login_required
def report_list():
    """历史报告列表（直接列目录，报告不做数据库登记）"""
    d = _dir()
    items = []
    for fn in sorted(os.listdir(d), reverse=True):
        path = os.path.join(d, fn)
        if not _NAME_RE.match(fn) or not os.path.isfile(path):
            continue
        st = os.stat(path)
        items.append({
            'name': fn, 'size': st.st_size,
            'created_at': datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
        })
    return ok(items)


@bp.get('/report/download/<name>')
@login_required
def report_download(name):
    """预览/下载报告；加 ?dl=1 触发附件下载"""
    if not _NAME_RE.match(name):
        return err('文件名不合法', 404)
    return send_from_directory(_dir(), name, as_attachment=bool(request.args.get('dl')),
                              mimetype='text/html')


@bp.delete('/report/<name>')
@admin_required
def report_delete(name):
    if not _NAME_RE.match(name):
        return err('文件名不合法', 404)
    path = os.path.join(_dir(), name)
    if not os.path.isfile(path):
        return err('报告不存在', 404)
    os.remove(path)
    log_action('删除巡检报告', f'删除巡检报告 {name}', g.user['username'])
    return ok()
