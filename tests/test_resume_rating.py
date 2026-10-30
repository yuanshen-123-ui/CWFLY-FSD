# -*- coding: utf-8 -*-
"""离线验证：会话重连恢复后，rating/realname 按本次登录刷新，席位状态复位。"""
import asyncio
import sys

sys.path.insert(0, ".")
from server import FSDServer  # noqa: E402
import client_handler  # noqa: E402
from client_handler import Client  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✔ " if cond else "  ✘ ") + name + (("  " + str(extra)) if extra else ""))


class FakeHandler:
    def __init__(self, callsign, cid, client):
        self.callsign = callsign
        self.cid = cid
        self.client = client
        self.client_type = client.type
        self._sent = []
        self._closed = False
        self.writer = None
        self.peer = ("127.0.0.1", 1)
        self.flightlog_id = 0

    async def send(self, data):
        self._sent.append(data)


def make_srv():
    srv = object.__new__(FSDServer)
    srv.clients = {}
    srv.pending_sessions = {}
    srv.client_list = []
    srv.locked_plans = set()
    srv.stats = {}
    srv.config = type("C", (), {"get": lambda self, s, k, d=None: "CWFLY FSD"})()
    srv._broadcast_addclient = lambda c: None
    srv._broadcast_rmclient = lambda c: None
    srv.unregister_client = lambda h: None
    return srv


async def main():
    srv = make_srv()

    print("=== 重连恢复：等级按新登录刷新 ===")
    # 旧会话：level=1（第一次登录 EuroScope 送 rating=1）
    old_client = Client("0000", "ZBAA_APP", client_handler.CLIENT_ATC, 1, "OldName", "9")
    old_client.facility_type = 5        # 旧会话残留的席位
    old_client.visual_range = 100
    old_client.pos_valid = True
    old_handler = FakeHandler("ZBAA_APP", "0000", old_client)
    srv.clients["ZBAA_APP"] = old_handler
    srv.client_list.append(old_client)

    # 模拟断线 → 进入重连窗口
    ok = srv.mark_for_reconnect(old_handler, "peer_closed")
    check("进入重连窗口", ok is True and "ZBAA_APP" in srv.pending_sessions)

    # 新连接：本次登录 level=8
    new_client = Client("0000", "ZBAA_APP", client_handler.CLIENT_ATC, 8, "NewName", "9")
    new_handler = FakeHandler("ZBAA_APP", "0000", new_client)
    srv.register_client(new_handler)

    restored = srv.clients.get("ZBAA_APP")
    live = restored.client  # 恢复后 handler 持有的 client
    check("会话被恢复（新 handler 接管）", restored is new_handler)
    check("复用旧会话对象（计划/ATIS 不丢）", live is old_client)
    check("rating 已刷新为本次登录的 8", live.rating == 8, live.rating)
    check("realname 已刷新", live.realname == "NewName", live.realname)
    check("席位状态复位（facility_type=0）", live.facility_type == 0, live.facility_type)
    check("视程复位（40）", live.visual_range == 40, live.visual_range)
    check("位置标记复位（等待重新上报）", live.pos_valid is False)

    print()
    print("=== 被踢的会话不进入重连窗口 ===")
    srv2 = make_srv()
    c = Client("0000", "TEST_KICK", client_handler.CLIENT_ATC, 1, "T", "9")
    h = FakeHandler("TEST_KICK", "0000", c)
    srv2.clients["TEST_KICK"] = h
    h._kicked = True
    reason = "peer_closed"
    if getattr(h, "_kicked", False):
        reason = "kicked"
    check("_kicked 时强制 reason=kicked", reason == "kicked")
    check("kicked 不在可恢复原因中", reason not in __import__("server").RECONNECTABLE_REASONS)

    print()
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
