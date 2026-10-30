# CWFLY-FSD · FSD 连线服务器

CWFLY 的 **FSD 协议服务器**，用 Python 3 + asyncio 实现，供 EuroScope / FSX / P3D / X-Plane / MSFS 等客户端连接。支持完整 FSD 命令集、证书认证、广播范围裁剪、会话重连、飞行计划锁定、在线时长结算、METAR 多源同步、whazzup 生成与航迹记录。

---

## 一、端口与服务

| 端口 | 用途 | 说明 |
|---|---|---|
| `6809` | 客户端 FSD 协议 | EuroScope / 模拟飞行客户端连接 |
| `3011` | 服务器互联 | 多 FSD 服务器之间同步在线列表与计划 |
| `3012` | 系统管理 | 管理口令登录（management 控制台） |

---

## 二、架构与模块

| 文件 | 职责 |
|---|---|
| `main.py` | 启动入口：加载配置 → 初始化证书/数据库/天气 → 启动各服务 |
| `server.py` | asyncio 服务器：监听三端口、连接准入、`clients` 注册表、保活、事件循环延迟监控、优雅关闭 |
| `client_handler.py` | 单客户端的协议状态机：命令分发、位置/计划/消息、在线快照、航迹记录、断线处理 |
| `protocol.py` | FSD 命令常量、错误码、等级常量 |
| `certificate.py` | 证书库：SHA-256 口令、`$CH/$SA` 挑战应答校验、MySQL 证书模式与 60s 热加载 |
| `auth.py` | 认证辅助（白名单/等级等） |
| `config.py` | 配置解析（`fsd.conf` / `fsd-db.conf`） |
| `db.py` | MySQL 访问：在线快照、计划落库、在线时长、航迹、审计 |
| `weather.py` | METAR 多源（aviationweather + VATSIM + 可选 metar-taf）、OurAirports 站点库、`metar.dat` 原子写入 |
| `whazzup.py` | 周期生成 `whazzup.txt`（供 Little Navmap 等第三方） |
| `management.py` | 3012 管理控制台：在线列表/踢人/广播/统计 |
| `server_link.py` | 服务器互联协议 |
| `tests/test_session_reconnect.py` | 会话重连/心跳/视程/ATIS 共 21 项离线断言 |

---

## 三、FSD 命令支持

| 命令 | 方向 | 说明 |
|---|---|---|
| `#AA` / `#AP` | C→S | ATC / 机组登录（legacy 明文口令，兼容旧客户端） |
| `$CH` / `$SA` | S→C / C→S | 安全认证挑战与应答（SHA-256 + HMAC） |
| `#DA` / `#DP` | C→S | 主动下线 |
| `@` | C→S | 机组位置（姿态 PBH、应答机、地速、修正高度） |
| `%` | C→S | 管制位置（频率、席位、视程） |
| `'` | C→S | 管制视程点（子可见范围点，最多 3 个） |
| `#TM` | C↔S | 文本消息（`@频率` / 点名 / `*` 全员 / `*S` 仅督导） |
| `$FP` | C→S | 飞行计划提交（含锁定语义） |
| `$AM` | C→S | 管制修正计划（触发锁定并广播） |
| `$CQ` / `$CR` | C↔S | 查询/应答（`SERVER FP` 查计划、`ATIS` 收集） |
| `$HO` / `$HA` / `#PC` | C↔S | 移交请求 / 接受 / 管制员广播 |
| `#SB` | C→S | SquawkBox 文本转发 |
| `$!!` | C→S | 踢人（仅 Supervisor/Administrator） |
| `#DL` | S→C | 心跳（风速扰动，默认 60s） |
| `$ER` | S→C | 错误码（长度不足、认证失败、呼号占用等） |

> 协议版本要求 `revision = 9`（不匹配直接拒绝并提示）。

---

## 四、核心机制

### 1）认证
- **Legacy**：`#AP` 明文口令 → 直接 SHA-256 比对 → SHA-256(明文) 比对 → **bcrypt 兜底**（主站同步账号）
- **安全认证**：`$CH` 下发挑战，客户端回 `$SA`（HMAC-SHA256(挑战, sha256(口令))）
- 证书来源两种：`cert.txt` 文件模式 或 **MySQL 证书模式**（`fsd-db.conf`，从 `User` 表读 `fsdPasswordHash` / `password` / `fsdLevel`），每 60s 热加载

### 2）广播范围裁剪
- 以视程（海里）裁剪：ATC↔ATC 取两者最大、ATC↔机组取 ATC 视程、机组↔机组取两者之和
- **多位置点**：主位置 + 3 个 ATC 视程点两两取最近距离
- **fail-open**：任一方尚未上报位置时全量接收（避免新连接被静默丢包）
- 上限保护：`MAX_VISUAL_RANGE = 3000` 海里

### 3）会话重连窗口（40s）
异常断开（对端 FIN/RST、读超时、客户端主动下线）不立即注销，保留会话 40s：
- 窗口内同呼号 + 同 CID 重连 → **恢复原会话**（飞行计划、ATIS、在线时长、快照）
- 超时才终结：写 `offlineAt`、删快照、广播 `#DP/#DA`
- 管理口踢人 / 服务端停机 / 缓冲区攻击等 → 立即下线，不进入窗口

### 3.5）恢复会话的补播（≥10 秒）
- 断开 **< 10 秒** 内重连：静默恢复（保留计划/ATIS/在线时长），不向其他客户端广播上下线（避免网络抖动刷屏）
- 断开 **≥ 10 秒** 后重连：恢复会话的同时，向其他客户端补发一次 `#DA` + `#AA`（`#DP` + `#AP`），
  机器人 / 第三方客户端据此重新识别为「上线」并播报
- 阈值：`server.py` 的 `RESUME_ANNOUNCE_MIN = 10.0`（秒），可调整

### 4）飞行计划锁定
- 管制 `$AM` 修正后锁定（写入 `FlightPlan.locked` 并广播给所有在线管制）
- 机组再提交 `$FP`：起降机场未变 → 拒绝并提示联系管制员；起降机场已变 → 自动解锁后接受
- 会话彻底结束 / 重连窗口超时后解锁

### 5）在线时长与航迹
- 会话真正终结时结算：管制累加 `User.totalAtcTime`、机组累加 `User.totalPilotTime`
- 机组位置每 10s 采样写入 `FlightTrack`（供地图回放），30 天保留策略

### 6）气象 / whazzup / 管理
- METAR 三源合并（AWC → VATSIM → 可选 metar-taf），站点清单来自 OurAirports（缓存 7 天，348 个国内/港澳台机场）
- `whazzup.txt` 周期生成（标准格式，供第三方航图/流量工具）
- 3012 管理端口：在线列表、踢人、广播、统计（口令在 `fsd.conf [system] password`）

---

## 五、配置

两种模式，任选其一：

```ini
; fsd.conf —— 单机证书文件模式
[system]
clientport = 6809
serverport = 3011
systemport = 3012
certificates = cert.txt        ; 证书文件
whazzup = whazzup.txt
serverident = FSD-PY
servername = CWFLY FSD Server
serveremail = admin@cwflysim.cn
maxclients = 500
password =                     ; 管理端口口令（必填）
```

```ini
; fsd-db.conf —— MySQL 证书模式（与主站共库）
[system]
clientport = 6809
serverport = 3011
systemport = 3012
certificates = cert.txt
whazzup = whazzup.txt
serverident = FSD-PY
servername = CWFLY FSD Server
serveremail = admin@cwflysim.cn
serverhost = localhost
serverlocation = China
maxclients = 500

[database]
host = 127.0.0.1
port = 3306
name = cwfly
user =
password =

[connections]

[weather]
source = download
```

> 真实配置不入库：仓库只提交 `fsd.conf.example` / `fsd-db.conf.example`（口令已清空）。

---

## 六、运行

```bash
python -m venv .venv && . .venv/Scripts/activate    # Windows
pip install -r requirements.txt
cp fsd-db.conf.example fsd-db.conf                   # 或 fsd.conf.example
python main.py
```

### 测试

```bash
python tests/test_session_reconnect.py    # 21 项：重连窗口/心跳/视程最近距离/ATIS/视程点
python test_bcrypt_login.py               # 口令哈希与登录校验
python test_port.py                       # 端口连通
```

---

## 七、部署与重启纪律

- 进程：`pm2 restart cwfly-fsd`（**修改后合并重启**，避免频繁闪断；客户端会自动重连）
- 客户端闪断期间有 **40s 重连窗口**，短暂重启对在线机组影响可控
- 证书/口令变更：改 DB 后等 60s 热加载，无需重启
- 日志：`fsd.log`（运行期生成，不提交）

---

## 八、常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 客户端提示版本错误 | 协议 revision 必须为 9 |
| 一直“认证失败” | 口令错 / CID 不存在 / 账号被停用；安全认证走证书表，legacy 走 `fsdPasswordHash` 或 bcrypt |
| 看不到其他机组 | 视程裁剪生效：检查双方是否上报位置、视程设置 |
| 计划提交被拒 | 已被管制锁定（改起降机场可解锁），或呼号与登录呼号不一致（防冒用） |
| 天气为空 | 该机场不在公开 METAR 流（国内支线常见）；或 AWC/VATSIM 暂时不可达 |
| 管理端口连不上 | `fsd.conf [system] password` 未设置 |

---

## 九、相关仓库

| 仓库 | 内容 |
|---|---|
| [CWFLY-WEB](https://github.com/yuanshen-123-ui/CWFLY-WEB) | 网站前后端（计划提交/在线地图/回放） |
| [CWFLY-ATC](https://github.com/yuanshen-123-ui/CWFLY-ATC) | 管制中心 |
| [CWFLY-BOT](https://github.com/yuanshen-123-ui/CWFLY-BOT) | QQ 机器人（以 CID 0000 连接本服务器） |

