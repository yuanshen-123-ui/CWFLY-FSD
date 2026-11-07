# FSD `ACC`（Aircraft Configuration）— swift 协议说明与实现

> 来源：swift 源码 `swift-project/pilotclient`，`src/core/fsd/`
> - `enums.h`：`ClientQueryType::AircraftConfig`、`Capabilities::AircraftConfig (1<<9)`
> - `serializer.cpp`：`AircraftConfig <-> "ACC"`、`Capabilities::AircraftConfig <-> "ACCONFIG"`
> - `fsdclient.cpp`：发送/接收逻辑（见下方行号）

## 报文（标准 `$CQ` 查询包）

```
$CQ<from>:<to>:ACC:<json>
```

| 字段 | 说明 |
|---|---|
| `from` | 发送者呼号（服务端转发时应保持原发送者） |
| `to` | `@94836` = 广播给所有机组（客户端自行按距离过滤）；或具体呼号 = 点对点 |
| `ACC` | Query Type，`ClientQueryType::AircraftConfig` |
| `json` | 紧凑 JSON（swift 会做 `\uXXXX` 转义），可能包含 `:`，解析时须从第 4 个字段起用 `:` 重建 |

### 三种 JSON

| 用途 | 示例 | swift 代码 |
|---|---|---|
| 增量广播（常态，部件变化时） | `{"config":{<incremental parts>}}` | `sendIncrementalAircraftConfig()` L1207-1235，目标 `@94836` |
| 点对点请求完整配置 | `{"request":"full"}` | `sendClientQueryAircraftConfig()` L543-548 / `JsonPackets::aircraftConfigRequest()` L2899 |
| 请求的应答（完整配置） | `{"config":{...,"isFullJson":true}}` | `handleClientQuery()` L1636-1665 |

swift 收到非请求的 ACC 后，会先 `isAircraftInRange()` 过滤（L1667），因此服务端广播给所有机组是安全的。

## 服务端行为（`fsd/client_handler.py`）

- `_handle_cq` 增加 `ACC` 分支 → `_relay_aircraft_config`：
  - `to == "@94836"`：转发给所有在线 **PILOT**（排除发送者与 ATC）
  - `to == 具体呼号`：只转发给目标客户端
  - `to` 为其它 `@xxx`：忽略
  - 不解析 JSON，原样重建 `$CQ<from>:<to>:ACC:<payload>`
- 保护：
  - payload 上限 8192 字节（超长丢弃并记日志）
  - 令牌桶：突发 30 条、5 条/秒补充（防刷屏/放大）
- 单元测试：`tests/test_acc_relay.py`（15 项）

## 已知差异（与本服务端相关）

- 本服务端的握手质询为 EuroScope 风格 `$CH<challenge>`（配合 `$SA`）；
  swift 的质询是 `$ZC` / `$ZR`，因此 swift 会把 `$CH` 记成未知包并忽略，**不影响明文 `#AP` 登录**。
- swift 的 `#AP` 第二个字段固定是接收方 `SERVER`（`AddPilot` 构造 `MessageBase(callsign, "SERVER")`），
  不是客户端类型、也不涉及提权。
