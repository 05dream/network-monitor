import os
import secrets

BASE_DIR = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))


def _secret_key():
    """会话签名密钥：优先读环境变量；没有则首次运行随机生成并缓存在本地 .secret_key 文件里（已被 .gitignore 排除）"""
    env = os.environ.get('SECRET_KEY')
    if env:
        return env
    path = os.path.join(BASE_DIR, '.secret_key')
    try:
        with open(path, encoding='utf-8') as f:
            key = f.read().strip()
        if key:
            return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(key)
    except OSError:
        pass
    return key


class Config:
    SECRET_KEY = _secret_key()
    DATABASE = os.path.join(BASE_DIR, 'monitor.db')
    SCHEMA = os.path.join(BASE_DIR, 'app', 'schema.sql')
    # 采集异常时 telnet 单次命令超时（秒）
    TELNET_TIMEOUT = 10
    # 数据保留：采集历史保留天数（系统日志固定 30 天）
    DATA_RETENTION_DAYS = 7
    # 巡检报告落盘目录（自包含 HTML，可离线打开/打印/存档）
    REPORT_DIR = os.path.join(BASE_DIR, 'reports')
