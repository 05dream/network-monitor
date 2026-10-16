# 企业核心网高可用路由监控与故障分析系统

![Python](https://img.shields.io/badge/Python-3.8+-blue?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-3.x-green?logo=flask&logoColor=white)
![Database](https://img.shields.io/badge/Database-SQLite-orange?logo=sqlite&logoColor=white)
![Frontend](https://img.shields.io/badge/Frontend-原生JS-yellow)
![License](https://img.shields.io/badge/License-MIT-purple)

基于 **VRRP + BFD + OSPF 联动**高可用架构的企业核心网监控系统：通过 telnet 登录 eNSP 仿真设备，定时采集 VRRP 主备状态、OSPF 邻居、接口与路由表，自动检测故障并**量化计算故障的完整时间线（T1~T4）**。

> 一次故障到底"多久被发现、多久完成收敛、多久业务恢复、总共中断多久"，本系统把它拆成四段可测量的数字并落库可视化。

本项目为毕业设计配套的监控软件，网络仿真部分（OSPF / VRRP / BFD 配置与故障场景测试）在 eNSP 中完成，本仓库只包含监控系统本体，不依赖任何商业设备或付费软件。

---

## 功能特性

| 模块 | 说明 |
|---|---|
| 实时监控 | 拓扑图 + 指标卡 + 设备状态表，默认 5 秒自动刷新 |
| 自动采集 | 后台调度线程按设备各自的采集间隔轮询，多设备独立配置 |
| 故障检测 | VRRP 主备切换检测、设备离线检测，**连续两轮确认防抖** |
| T1~T4 时间线 | T1=VRRP 主备互换 / T2=OSPF 全部 Full / T3=路由收敛 / T4=业务 ping 恢复 |
| 四项指标 | 检测耗时、收敛耗时、业务恢复耗时、总中断耗时（自动算并落库） |
| 告警闭环 | 分级告警、人工确认/关闭、业务恢复后自动关闭事件 |
| 拓扑管理 | 拖拽调位置、连线的增删、**JSON 导入/导出整套更换拓扑**（换设备不用改代码） |
| 故障演练 | 内置两个剧本，虚拟设备（`source='sim'`）走完整采集→检测链路，可一键清理 |
| 巡检报告 | 一键生成**自包含 HTML**（样式内联、SVG 图表），断网也能打开、可打印可存档 |
| 运行大屏 | 无侧边栏深色大字版，适合答辩投影 |
| 用户体系 | 管理员/查看者两级权限，支持自助注册，密码哈希存储 |
| 数据治理 | 采集历史保留 7 天、系统日志保留 30 天，自动清理 |

---

## 系统架构

```
┌──────────────────────────────────────────────────────────────┐
│                        eNSP 仿真网络                          │
│   R1 (VRRP Master) ── R2 (VRRP Backup) ── SW1/SW2/SW3/SW4    │
│         串口重定向 TCP：127.0.0.1:2000 ~ 2005                 │
└───────────────┬──────────────────────────────────────────────┘
                │ telnet（只发 \r，socket 自实现，无第三方库）
                ▼
┌──────────────────────────────────────────────────────────────┐
│  采集层 app/collector/                                        │
│  scheduler 调度线程 → collector 逐台采集 → parsers 正则解析   │
│  命令：display vrrp / ospf peer / interface / routing-table   │
└───────────────┬──────────────────────────────────────────────┘
                ▼
┌──────────────────────────────────────────────────────────────┐
│  检测层 detector.py（核心创新点）                             │
│  两轮确认状态机 → 识别主备切换 → 记录 T1~T4 → 计算四项指标    │
└───────────────┬──────────────────────────────────────────────┘
                ▼
┌──────────────────────────────────────────────────────────────┐
│  SQLite（monitor.db，9 张表）                                 │
│  users / devices / topology_links / device_status /           │
│  collect_tasks / switch_events / event_timeline / alerts /    │
│  system_logs                                                  │
└───────────────┬──────────────────────────────────────────────┘
                ▼
┌──────────────────────────────────────────────────────────────┐
│  展示层（Flask 蓝图 + Jinja2 + 原生 JS + 手写 SVG）           │
│  仪表盘 / 设备管理 / 设备详情 / 故障事件 / 拓扑 / 大屏 /      │
│  故障演练 / 巡检报告 / 用户管理 / 系统日志                    │
└──────────────────────────────────────────────────────────────┘
```

---

## 技术栈

- **后端**：Python 3.8+ / Flask（蓝图分模块）/ sqlite3 / socket 自写 telnet 客户端
- **前端**：Jinja2 模板 + 原生 JavaScript + 手写 SVG 图表与拓扑（**无前端框架、无 npm、无构建步骤**）
- **数据库**：SQLite（单文件 `monitor.db`，9 张业务表，启动时幂等迁移）
- **依赖**：仅 `flask`（见 `requirements.txt`）

---

## 目录结构

```
network-monitor/
├── run.py                      # 入口：建库/迁移 + 启动调度线程 + Flask 服务
├── requirements.txt
├── app/
│   ├── __init__.py             # create_app：注册蓝图、初始化数据库
│   ├── config.py               # 配置（SECRET_KEY 走环境变量或本地随机文件）
│   ├── schema.sql              # 建表脚本
│   ├── db.py                   # 连接管理、幂等迁移、种子账号、日志
│   ├── auth.py                 # 登录 / 注销 / 注册 + 权限装饰器
│   ├── pages.py                # 页面路由
│   ├── report.py               # 自包含 HTML 巡检报告生成
│   ├── api/                    # 后端接口（devices/events/topology/stats/users/report/drill/logs）
│   ├── collector/
│   │   ├── scheduler.py        # 采集调度线程（含 7 天/30 天清理）
│   │   ├── collector.py        # 单台设备采集流程（4 条 display 命令 + 可选 ping）
│   │   ├── telnet_client.py    # socket telnet 客户端（串口只发 \r）
│   │   ├── parsers.py          # 命令输出解析
│   │   ├── detector.py         # 故障检测状态机 + T1~T4（核心）
│   │   └── simulator.py        # 故障演练模拟器（基于 eNSP 真实基线）
│   └── web/
│       ├── templates/          # 页面模板
│       └── static/             # style.css / topo.js / charts.js
└── tests/                      # 5 组冒烟测试（无框架，纯断言脚本）
```

---

## 快速开始

```bash
# 1. 安装依赖（仅需 Flask）
pip install -r requirements.txt

# 2. 启动（会自动建库、生成初始账号、启动采集线程）
python run.py

# 3. 浏览器访问
http://127.0.0.1:5000
```

### 初始账号（**演示用，部署后请立即修改**）

| 用户名 | 默认密码 | 角色 |
|---|---|---|
| `admin` | `Admin@123` | 管理员：可改设备/拓扑/用户/事件，可演练与生成报告 |
| `viewer` | `Viewer@123` | 查看者：只读 |

> 登录页底部不显示账号密码提示；新用户可在登录页点「立即注册」自助注册（默认角色为 viewer 只读）。管理员可在「用户管理」页新增用户、重置密码、调整角色。密码以哈希方式存储在 `users` 表，不存明文。

### 通过环境变量覆盖默认配置（可选）

服务只监听 `127.0.0.1`，不对外网开放。部署到服务器或需要自定义时，可用以下环境变量：

| 环境变量 | 作用 | 未设置时 |
|---|---|---|
| `SECRET_KEY` | 会话签名密钥 | 首次启动随机生成，缓存到本地 `.secret_key`（不入库、不入 git） |
| `ADMIN_PASSWORD` | 覆盖初始管理员密码（仅首次建库时生效） | `Admin@123` |
| `VIEWER_PASSWORD` | 覆盖初始查看者密码（仅首次建库时生效） | `Viewer@123` |

Windows PowerShell 示例：

```powershell
$env:SECRET_KEY="换成你自己的随机串"
$env:ADMIN_PASSWORD="换成强密码"
python run.py
```

> 若需外网访问，请自行在 `run.py` 中调整 `host`，并务必设置上述环境变量、登录后立即改密。

---

## eNSP 环境准备

### 1. 装好 eNSP 及其依赖

eNSP 依赖 **VirtualBox**（必需）、**WinPcap**（或 Wireshark 自带的抓包驱动）。安装后**以管理员身份运行 eNSP**，否则设备无法启动。

### 2. 打开设备的串口并记下端口号

在 eNSP 中右键设备 → 勾选「串口」，界面会显示该设备的端口号（如 2000、2001……）。把这个号码填进系统的「设备管理 → 端口」，IP 一律填 `127.0.0.1`。

> **注意**：换设备或重新添加设备后**串口号会变**，必须同步更新，否则设备会全部离线。改完可用「测试连接」快速验证。

### 3. 拓扑与默认设备

本系统的默认设备表按下面的拓扑配置（可在「设备管理」中增删改）：

| 设备 | 角色 | 端口 | 说明 |
|---|---|---|---|
| R1 | 核心路由器 | 2000 | VRRP 主（VRID 10 / Vlanif10、VRID 30 / Vlanif30） |
| R2 | 核心路由器 | 2001 | VRRP 备 |
| SW1 | 汇聚交换机 | 2002 | 承载 VRRP 与 OSPF |
| SW2 ~ SW4 | 接入交换机 | 2003 ~ 2005 | 接入层 |

业务探测目标（`ping_target`）：`192.168.30.14`，用于 T4 业务恢复判定。

### 4. 采集命令

系统对每台设备执行以下**只读**命令（不会修改设备配置）：

```
display vrrp brief
display ospf peer brief
display interface brief
display ip routing-table
ping -c 3 <业务探测目标>        # 仅配了 ping_target 的设备
```

### 5. 串口的一个硬约束

eNSP 串口会把 `\n` 也当成一次回车，导致命令重复执行、回显与提示符错位、解析结果全乱。因此 `telnet_client.write_line()` **只发送 `\r`**，不能改成 `\r\n`。

---

## 主要页面

| 路径 | 页面 | 权限 |
|---|---|---|
| `/` | 运行仪表盘（指标卡 + 拓扑 + 状态表 + 最近事件） | 登录 |
| `/devices`、`/device/<id>` | 设备管理、设备详情（含指标趋势图） | 登录 |
| `/events` | 故障事件（告警卡 + T1~T4 时间线 + CSV 导出） | 登录 |
| `/drill` | 故障演练（两个剧本 + 基线来源） | 登录（启动演练需 admin） |
| `/reports` | 巡检报告（生成 / 预览 / 下载 / 删除） | 登录（生成需 admin） |
| `/screen` | 运行大屏（无侧边栏，5 秒刷新，适合投影） | 登录 |
| `/topology` | 拓扑管理（拖拽 / 连线 / JSON 导入导出） | admin |
| `/users` | 用户管理（增删改查、重置密码） | admin |
| `/logs` | 系统日志（操作记录 + 采集异常） | 登录 |

---

## 核心机制：T1~T4 时间线

系统对 VRRP 状态逐轮比对来判定故障，采用**连续两轮确认**的防抖策略：

| 时刻 | 含义 | 触发条件 |
|---|---|---|
| T1 | VRRP 主备角色互换确认 | 新角色连续两轮一致 |
| T2 | OSPF 恢复 | OSPF 邻居全部 Full |
| T3 | 路由收敛 | 路由数回到自动学习的基线；**或** OSPF 全 Full 且路由数与上一轮相同（兜底） |
| T4 | 业务恢复 | 业务 ping 连续两轮成功 |

指标计算：

```
detect_ms   = T1 - 故障发生时刻          # 检测耗时
converge_ms = T3 - T2                    # 收敛耗时
recover_ms  = T4 - T1                    # 业务恢复耗时
total_ms    = detect_ms + recover_ms     # 总中断耗时
```

设备离线只产生 `offline` 事件（离线时读不到 VRRP/OSPF，不建时间线）。

---

## 数据库

SQLite 单文件 `monitor.db`（首次运行自动创建），9 张业务表：

`users`（用户与角色）、`devices`（设备，含角色/坐标/来源/演练状态）、`topology_links`（拓扑连线）、`device_status`（每轮采集历史）、`collect_tasks`（采集任务）、`switch_events`（故障事件）、`event_timeline`（T1~T4 与四项指标）、`alerts`（告警）、`system_logs`（日志）。

表结构升级采用**幂等迁移**：启动时自动 `ALTER TABLE ADD COLUMN` 补齐新增列（见 `db.py` 的 `_MIGRATIONS`），历史采集数据完整保留。

---

## 测试

```bash
python -m tests.smoke_collector   # 采集中间件（自带 mock eNSP 服务端，无需 eNSP）
python -m tests.smoke_devices     # 设备管理接口与权限
python -m tests.smoke_drill       # 演练模拟器与检测链路
python -m tests.smoke_report      # 巡检报告生成与下载
python -m tests.smoke_e2e         # 端到端
```

> 测试会连接项目根目录的 `monitor.db`（会自动建库/建账号），并可能新增临时设备、生成报告文件。建议先在副本上跑，或先备份数据库。

---

## 常见问题

**设备一直显示离线？**
依次检查：① eNSP 里设备是否已「启动」；② 系统里的端口是否与 eNSP 当前串口号一致；③ 用「测试连接」看具体失败原因。

**采集回来数据是乱的、命令像执行了两次？**
行结束符问题。确认 `telnet_client.write_line()` 只发 `\r`。

**用界面上的「停用」开关能测故障吗？**
不能，那只是暂停采集。要测真实故障，必须在 eNSP 里停掉设备或断开链路。

**演练算出来的收敛耗时是 0？**
相位间隔太短会让 T2/T3 落在同一轮采集里。当前剧本相位间隔已设为 12 秒（≥ 2 倍采集间隔）。另外注意 T3 的完成条件是"OSPF 全 Full 且路由数稳定"，不是等原主设备的基线路由数（原主设备离线时那个数永远达不到）。

**数据库被占用？**
SQLite 同一时刻只允许一个写事务。后台采集线程与 Web 请求都会写库，所以「立即采集」只打标记由调度线程执行，避免并发写冲突。

**换掉 eNSP 拓扑后设备全离线？**
串口号变了。用「拓扑管理 → 导出 JSON」改好端口后再导入，或在「设备管理」里逐台改。

---

## 提交到 GitHub

仓库已通过 `.gitignore` 排除所有本地数据（数据库、密钥、报告、IDE 配置、`__pycache__`），首次推送前建议再确认一次：

```bash
# 确认清单里不含 monitor.db / .secret_key / reports/
git status --ignored

git init
git add .
git commit -m "init: 企业核心网高可用路由监控与故障分析系统"
git branch -M main
git remote add origin https://github.com/<你的用户名>/network-monitor.git
git push -u origin main
```

> 不想装 Git 客户端，也可以在 GitHub 网页 New repository 后用 **uploading an existing file** 拖拽上传（先确认本地数据文件没有被拖进去）。

---

## 开源许可证

本项目基于 [MIT License](LICENSE) 开源，可自由使用、修改与分发，请保留版权声明。

## 说明

- 本项目为毕业设计作品，用于学习与演示。
- 仓库**不包含**任何真实采集数据、数据库文件与设备口令。首次运行生成的 `monitor.db`、`.secret_key`、`reports/` 等本地文件已在 `.gitignore` 中排除，请勿提交。
