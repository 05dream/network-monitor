-- ============================================================
-- 企业核心网高可用路由监控与故障分析系统 - 数据库结构
-- 开题报告要求的7张表 + 系统日志表
-- ============================================================

-- 用户表（管理员 admin / 普通查看者 viewer）
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'viewer',   -- admin / viewer
    created_at    TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 设备表
CREATE TABLE IF NOT EXISTS devices (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,                -- 设备名，如 R1
    host                TEXT NOT NULL,                -- eNSP串口重定向填 127.0.0.1
    port                INTEGER NOT NULL,             -- eNSP串口号，如 2000
    username            TEXT DEFAULT '',              -- VTY登录用户名（串口方式可空）
    password            TEXT DEFAULT '',              -- VTY登录密码
    collect_interval    INTEGER DEFAULT 5,            -- 采集间隔（秒）
    ping_target         TEXT DEFAULT '',              -- 业务连通性探测目标（T4用），如 192.168.30.14
    route_count_baseline INTEGER DEFAULT 0,           -- 路由条目基线（T3恢复判断用，自动学习）
    enabled             INTEGER DEFAULT 1,            -- 1启用 0停用
    role                TEXT DEFAULT 'other',         -- 拓扑角色 core/aggregation/access/host/other（仅布局用）
    pos_x               REAL,                         -- 拓扑图节点坐标，NULL 时前端自动布局
    pos_y               REAL,
    topo_visible        INTEGER DEFAULT 1,            -- 是否在拓扑图显示（与 enabled 独立）
    source              TEXT DEFAULT 'telnet',        -- 采集来源：telnet 真实设备 / sim 演练模拟设备
    drill_state         TEXT DEFAULT '',              -- 演练状态 JSON（source=sim 时有值）：剧本/设备标识/开始毫秒
    created_at          TEXT DEFAULT (datetime('now', 'localtime'))
);

-- 拓扑连线表（设备之间的链路，仅用于拓扑图展示）
CREATE TABLE IF NOT EXISTS topology_links (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    from_id    INTEGER NOT NULL REFERENCES devices(id),
    to_id      INTEGER NOT NULL REFERENCES devices(id),
    label      TEXT DEFAULT '',                       -- 链路标签，如 Eth-Trunk1
    style      TEXT DEFAULT 'link',                   -- eth-trunk / trunk / access / link
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(from_id, to_id),
    CHECK(from_id <> to_id)
);
CREATE INDEX IF NOT EXISTS idx_links_from ON topology_links(from_id);
CREATE INDEX IF NOT EXISTS idx_links_to ON topology_links(to_id);

-- 设备状态表（每轮采集存一条历史）
CREATE TABLE IF NOT EXISTS device_status (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id      INTEGER NOT NULL REFERENCES devices(id),
    collected_at   TEXT DEFAULT (datetime('now', 'localtime')),
    online         INTEGER DEFAULT 0,        -- 1在线 0离线
    vrrp_state     TEXT DEFAULT '',          -- JSON: [{vrid,state,interface,vip}]
    ospf_neighbors INTEGER DEFAULT 0,
    ospf_full      INTEGER DEFAULT 0,
    ospf_state     TEXT DEFAULT '',          -- JSON: [{neighbor,interface,state}]
    interfaces     TEXT DEFAULT '',          -- JSON: [{name,phy,protocol}]
    route_count    INTEGER DEFAULT 0,
    biz_ping_ok    INTEGER DEFAULT -1,       -- 业务ping结果 1通 0不通 -1未测
    error          TEXT DEFAULT ''           -- 本轮采集异常说明
);
CREATE INDEX IF NOT EXISTS idx_status_device_time ON device_status(device_id, collected_at);

-- 采集任务表
CREATE TABLE IF NOT EXISTS collect_tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   INTEGER NOT NULL REFERENCES devices(id),
    status      TEXT DEFAULT 'running',      -- running / paused
    last_run_at TEXT,
    last_result TEXT DEFAULT '',
    run_count   INTEGER DEFAULT 0,
    fail_count  INTEGER DEFAULT 0
);

-- 切换事件表
CREATE TABLE IF NOT EXISTS switch_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id  INTEGER REFERENCES devices(id),
    event_type TEXT NOT NULL,      -- vrrp_switch / ospf_change / offline / recover
    title      TEXT NOT NULL,
    started_at TEXT DEFAULT (datetime('now', 'localtime')),
    ended_at   TEXT,
    status     TEXT DEFAULT 'pending',   -- pending待确认 / confirmed已确认 / closed已关闭
    detail     TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_time ON switch_events(started_at);

-- 时间线记录表（T1~T4，创新点）
CREATE TABLE IF NOT EXISTS event_timeline (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER NOT NULL REFERENCES switch_events(id),
    t1_at       TEXT,    -- T1 VRRP主备角色互换
    t2_at       TEXT,    -- T2 OSPF邻居恢复Full
    t3_at       TEXT,    -- T3 路由表条目恢复
    t4_at       TEXT,    -- T4 业务ping恢复
    detect_ms   INTEGER, -- 检测时间   = T1 - 故障发生
    converge_ms INTEGER, -- 路由收敛   = T3 - T2
    recover_ms  INTEGER, -- 业务恢复   = T4 - T1
    total_ms    INTEGER  -- 总中断时间 = T4 - 故障发生
);

-- 告警记录表
CREATE TABLE IF NOT EXISTS alerts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id    INTEGER REFERENCES devices(id),
    event_id     INTEGER REFERENCES switch_events(id),
    level        TEXT DEFAULT 'warning',     -- info / warning / critical
    message      TEXT NOT NULL,
    status       TEXT DEFAULT 'pending',     -- pending / confirmed
    created_at   TEXT DEFAULT (datetime('now', 'localtime')),
    confirmed_by TEXT,
    confirmed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status);

-- 系统日志表（操作日志 + 采集异常日志）
CREATE TABLE IF NOT EXISTS system_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    username   TEXT DEFAULT 'system',
    action     TEXT NOT NULL,
    detail     TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_logs_time ON system_logs(created_at);
