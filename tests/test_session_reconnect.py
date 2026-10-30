"""离线单测：会话重连状态机 / 4 点视程最近距离 / #DL 心跳（不依赖真实端口）。"""
import asyncio
import sys

sys.path.insert(0, '.')

from server import FSDServer, RECONNECT_GRACE, RECONNECTABLE_REASONS  # noqa: E402
import client_handler  # noqa: E402
from client_handler import Client, ClientHandler  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✔ " if cond else "  ✘ ") + name + (("  " + extra) if extra else ""))


class FakeWriter:
    def close(self):
        pass

    def is_closing(self):
        return False


def make_handler(callsign, cid, client, sent=None):
    h = object.__new__(ClientHandler)
    h.callsign = callsign
    h.cid = cid
    h.client = client
    h.client_type = client.type if client else None
    h._closed = True
    h.writer = FakeWriter()
    h.peer = ("127.0.0.1", 1234)
    h.flightlog_id = 0
    h._sent = sent if sent is not None else []
    h.send = lambda data: _fake_send(h, data)
    return h


async def _fake_send(h, data):
    h._sent.append(data)


async def main():
    print("== 1) 会话重连窗口 ==")
    srv = object.__new__(FSDServer)
    srv.clients = {}
    srv.pending_sessions = {}
    srv.client_list = []
    srv.locked_plans = set()
    srv.stats = {}
    srv.config = type("C", (), {"get": lambda self, s, k, d=None: d})()
    finalized = []
    srv.unregister_client = lambda h: finalized.append(h.callsign)

    c = Client("1234", "TEST123", client_handler.CLIENT_PILOT, 1, "Tester", 9, 0)
    c.plan = "FAKE_PLAN"
    h = make_handler("TEST123", "1234", c)
    srv.clients["TEST123"] = h
    srv.client_list.append(c)

    # 1.1 不可恢复的原因 → 拒绝进入窗口
    check("缓冲区攻击等不可恢复原因不进入窗口",
          srv.mark_for_reconnect(h, "buffer_overflow") is False)
    check("管理口 kill 不进入窗口", srv.mark_for_reconnect(h, "management_kill") is False)
    check("服务端停机不进入窗口", srv.mark_for_reconnect(h, "server_shutdown") is False)

    # 1.2 可恢复原因 → 进入窗口
    ok = srv.mark_for_reconnect(h, "peer_closed")
    check("peer_closed 进入重连窗口", ok is True)
    check("会话从在线表摘除但仍在 pending", "TEST123" not in srv.clients and "TEST123" in srv.pending_sessions)
    check("client_list 暂时保留（客户端仍显示在线）", c in srv.client_list)
    check("重复调用幂等", srv.mark_for_reconnect(h, "read_timeout") is True)
    check("计划保留在会话对象里（不会被清理丢计划）",
          srv.pending_sessions["TEST123"]["client"].plan == "FAKE_PLAN")
    task = srv.pending_sessions["TEST123"]["task"]
    check("窗口任务已创建", task is not None)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # 1.3 已被新会话顶替的旧会话 → 拒绝进入窗口
    h_new = make_handler("TEST123", "1234", c)
    srv.clients["TEST123"] = h_new
    srv.pending_sessions.pop("TEST123", None)
    check("被顶替的旧会话不进入窗口", srv.mark_for_reconnect(h, "peer_closed") is False)
    srv.clients.pop("TEST123", None)

    # 1.4 超时终结 → 调用 unregister 并清空 pending
    srv.mark_for_reconnect(h, "peer_closed")
    srv._finalize_pending_session("TEST123")
    check("超时后正式注销", finalized == ["TEST123"] and "TEST123" not in srv.pending_sessions)
    try:
        await asyncio.sleep(0)
    except Exception:
        pass

    print("== 2) 4 点视程最近距离 ==")
    from client_handler import ClientHandler as CH
    a = Client("1", "ATC1", client_handler.CLIENT_ATC, 5, "A", 9, 0)
    a.pos_valid = True
    a.lat, a.lon = 30.0, 120.0          # 主位置
    a.vis_points = [(31.0, 121.0), (32.0, 122.0), (0.0, 0.0)]  # 第 3 点无效
    b = Client("2", "PILOT1", client_handler.CLIENT_PILOT, 1, "B", 9, 0)
    b.pos_valid = True
    b.lat, b.lon = 31.05, 121.05        # 只与 A 的第 1 个视程点接近
    d_main = CH._nearest_distance_nm(a, b)
    # 主位置(30,120)→(31.05,121.05) 约 90nm；视程点(31,121)→(31.05,121.05) 约 4nm
    check("最近距离取视程点（<20nm）", d_main < 20, "d=%.1fnm" % d_main)
    a2 = Client("3", "ATC2", client_handler.CLIENT_ATC, 5, "A2", 9, 0)
    a2.pos_valid = True
    a2.lat, a2.lon = 40.0, 130.0
    check("远距离仍正确（>100nm）", CH._nearest_distance_nm(a2, b) > 100)
    a3 = Client("4", "ATC3", client_handler.CLIENT_ATC, 5, "A3", 9, 0)
    a3.pos_valid = False
    a3.vis_points = []
    check("未上报位置时返回 0（fail-open 依赖项）", CH._nearest_distance_nm(a3, b) == 0.0)
    check("单一主位置比较仍与旧行为一致",
          abs(CH._nearest_distance_nm(a, b) - 4.2) < 3.0)

    print("== 3) #DL 心跳广播 ==")
    srv2 = object.__new__(FSDServer)
    srv2.stats = {}
    srv2.config = type("C", (), {"get": lambda self, s, k, d=None: "CWFLY FSD"})()
    h1 = make_handler("CES1", "1", Client("1", "CES1", client_handler.CLIENT_PILOT, 1, "A", 9, 0))
    h2 = make_handler("ZSHA_CTR", "2", Client("2", "ZSHA_CTR", client_handler.CLIENT_ATC, 5, "B", 9, 0))
    srv2.clients = {"CES1": h1, "ZSHA_CTR": h2}
    await srv2.broadcast_heartbeat()
    await asyncio.sleep(0.05)
    lines = [l for h in (h1, h2) for l in h._sent if l.startswith("#DL")]
    check("每个客户端收到 1 条 #DL", len(lines) == 2, "got=%d" % len(lines))
    check("#DL 格式为 <名称>:*:<d1>:<d2>",
          bool(lines) and lines[0].count(":") == 3 and lines[0].endswith("\r\n"), repr(lines[0]) if lines else "")
    check("心跳计数增加", srv2.stats.get("heartbeats_sent") == 1)

    print("== 4) ATIS / 视程点处理 ==")
    from client_handler import ClientHandler as CH2
    atc_client = Client("9", "ZSHA_CTR", client_handler.CLIENT_ATC, 5, "C", 9, 0)
    hg = make_handler("ZSHA_CTR", "9", atc_client)
    hg.client_type = client_handler.CLIENT_ATC
    hg.server = type("S", (), {"get_client": lambda self, cs: None, "stats": {}})()
    await hg._handle_subvis(["ZSHA_CTR", "1", "31.5", "121.5"])
    check("视程点写入 vis_points[1]",
          hg.client.vis_points and abs(hg.client.vis_points[1][0] - 31.5) < 1e-9,
          str(hg.client.vis_points))
    await hg._handle_subvis(["ZSHA_CTR", "9", "1", "1"])
    check("非法索引被忽略（不越界）", len(hg.client.vis_points) == 3)
    await hg._handle_cr(["ZSHA_CTR", "SERVER", "ATIS", "T", "ZSHA_CTR Shanghai Control"])
    check("$CR 收集 ATIS 文本", hg.client.atis == "ZSHA_CTR Shanghai Control", hg.client.atis)

    print()
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项:", FAIL)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
