from flask import Blueprint

bp = Blueprint('api', __name__, url_prefix='/api')

from . import devices, events, logs, stats, topology, users, drill, report  # noqa: E402,F401
