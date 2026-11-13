# 临时 FSD 实例（160.202.241.240）

> 2026-11-07 旧服务器 SSH 被锁期间的临时迁移。**旧服务器（38.76.182.6）上的 FSD 仍在运行**，DNS 未切换前不受影响。

## 基础信息

| 项 | 值 |
|---|---|
| 主机 | `160.202.241.240`（Ubuntu 24.04，root 可 key 登录） |
| 目录 | `/opt/cwfly-fsd-cwfly`（新加固版代码，`fsd/` 包结构） |
| systemd | `cwfly-fsd-cwfly.service`（enabled，Restart=always） |
| 客户端端口 | `6809`（公网，ufw 已放行） |
| 服务器互联 | `3011`（仅 127.0.0.1） |
| 管理控制台 | `3012`（仅 127.0.0.1） |
| 日志 | `journalctl -u cwfly-fsd-cwfly` |

## 数据来源（重要）

- 该实例**不连 MySQL**（`fsd.conf` 的 `[database]` 留空 → DB 功能静默禁用）。
- 证书来自本机 PostgreSQL `cwfly` 库（`ts-postgres` 容器）中的 **User 快照**：22 个账号 + 随机口令的 `000000` 管理员，共 23 条，写入 `cert.txt`（600）。
- 因此：**快照之后新注册的用户、或已修改过密码的用户，可能无法登录**；等旧服务器恢复后需要重新同步 `cert.txt`（或改用 DB 模式）。
- 无 DB 时：在线列表/位置/whazzup/认证正常；飞行计划落库、在线日志、证书热加载暂停。

## 密码/凭据文件（root-only）

- 管理台（3012）口令：`/root/cwfly-fsd-secrets/fsd-tmp-secrets.txt`
- 管理员 `000000` 的 FSD 口令：`/opt/cwfly-fsd-cwfly/.fsd-admin-000000`
- 配置：`/opt/cwfly-fsd-cwfly/fsd.conf`（600）

## 切换 DNS（待办）

客户端用的是 `fsd.cwflysim.cn`（当前 A = `38.76.182.6`）。临时切到本机需要把该 A 记录改为 `160.202.241.240`（DNSPod：`ns3.dnsv2.com` / `ns4.dnsv2.com`），TTL 建议 60–120 秒。

切换前客户端仍连旧服务器；切换后：
- 旧服务上的在线用户会掉线；
- 主站「在线数据/计划下发」仍指向旧 FSD，**两边不同步**（临时现象）。

## 回滚

```bash
# 停临时实例
systemctl disable --now cwfly-fsd-cwfly.service

# 恢复 target 上原有的 CWTS 备份 FSD（可选）
cp -a /opt/cwfly-fsd /opt/cwfly-fsd.bak-<TS>   # 已有备份
systemctl enable --now cwfly-fsd.service

# 旧服务器 38.76.182.6 的 FSD 一直未动，DNS 改回即可
```

## 已知限制

1. `[weather] source = none`（target 的 DNS 对 NOAA 域名解析异常），临时实例**没有 METAR/天气**。
2. 无 MySQL：航班计划/在线日志不落库。
3. 证书快照为 2026-08 左右数据，需后续同步。
4. 原 target 上的 `cwfly-fsd.service`（CWTS 备份，最后客户端活动 2026-09-28）已 stop/disable，备份在 `/opt/cwfly-fsd.bak-*`。
