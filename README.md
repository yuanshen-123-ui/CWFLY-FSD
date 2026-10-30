# CWFLY-FSD

CWFLY **FSD 协议服务器**（Python 3 + asyncio），供 EuroScope / 模拟飞行客户端连接。

## 特性
- FSD 协议完整命令：`#AA #AP #DA #DP @ % ' #TM #SB $FP $AM $CQ $CR $HO $HA #DL` 等
- 安全认证：SHA-256 口令 + `$CH/$SA` 挑战应答（`certificate.py`），兼容 legacy `#AP` 明文
- 广播优化：按视程裁剪（主位置 + 3 个 ATC 视程点取最近距离），未上报位置 fail-open
- **会话重连窗口**（默认 40s）：闪断重连恢复计划/ATIS/在线时长，不重复上下线
- 计划锁定：管制 `$AM` 修正后锁定，机组再提交需改起降机场才解锁
- 在线时长结算、命令长度守卫、`#DL` 心跳、ATIS 自动索取
- 气象：METAR 多源（aviationweather / VATSIM）+ OurAirports 站点库
- whazzup 生成、航迹记录（回放）、管理端口（3012）、服务器互联（3011）

## 快速开始
```bash
python -m venv .venv && . .venv/Scripts/activate   # Windows
pip install -r requirements.txt
cp fsd.conf.example fsd.conf        # 或 fsd-db.conf.example（MySQL 证书模式）
python main.py
```

## 端口
| 端口 | 用途 |
|---|---|
| 6809 | 客户端 FSD 协议 |
| 3011 | 服务器互联 |
| 3012 | 系统管理（管理口令登录） |

## 配置
- `fsd.conf`：单机证书文件模式（`certificates = cert.txt`）
- `fsd-db.conf`：共享 MySQL 证书模式（与网站共用 `User`/`Certificate`）
- 两个真实配置均不入库，只提交 `.example`

## 测试
```bash
python tests/test_session_reconnect.py   # 会话重连/心跳/视程/ATIS 共 21 项断言
```

## 注意
- 生产修改后**批量合并重启**，避免频繁闪断；`pm2 restart cwfly-fsd`
- 日志 `fsd.log` 运行期生成，不提交
---
## 相关仓库

| 仓库 | 内容 |
|---|---|
| [CWFLY-WEB](https://github.com/yuanshen-123-ui/CWFLY-WEB) | 网站前后端（Vue3 + Express/Prisma） |
| [CWFLY-FSD](https://github.com/yuanshen-123-ui/CWFLY-FSD) | FSD 服务器（Python asyncio，FSD 协议） |
| [CWFLY-ATC](https://github.com/yuanshen-123-ui/CWFLY-ATC) | 管制中心（ATC 席位/雷达/排班） |
| [CWFLY-BOT](https://github.com/yuanshen-123-ui/CWFLY-BOT) | QQ 机器人（OneBot / NapCat） |
| [CWFLY-DOCS](https://github.com/yuanshen-123-ui/CWFLY-DOCS) | 文档站（VitePress） |
| CWFLY-DEPLOY（私有） | 生产部署资产：Nginx 配置、运维脚本、SQL 迁移（私有仓库，不对外公开） | Nginx · Bash · MySQL |

> 本仓库由 CWFLY 主仓库于 2026-10-30 拆分而来，初始提交为当时快照（不含历史）。