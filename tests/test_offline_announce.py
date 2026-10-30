# -*- coding: utf-8 -*-
"""离线单测：下线提前播报（断开 ≥OFFLINE_ANNOUNCE_DELAY 未恢复 → 广播 #DA/#DP）。"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import client_handler  # noqa: E402
import server as server_mod  # noqa: E402
from client_handler import Client  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✔ " if cond else "  ✘ ") + name + (("  " + str(extra)) if extra else ""))


class Rec:
    def __init__(self, callsign, cid, ctype):
        self.client = Client(cid, callsign, ctype, 1, "Rec", "9")
        self.callsign = callsign
        self.cid = cid
        self.client_type = ctype
        self.sent = []

    async def send(self, data):
        self.sent.append(data)


class H(Rec):
    def __init__(self, callsign, cid, ctype):
        super().__init__(callsign, cid, ctype)
        self.flightlog_id = 0
        self.peer = ("127.0.0.1", 1)
        self._closed = False
        self.writer = None


def make_srv():
    srv = object.__new__(server_mod.FSDServer)
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
    old_delay = server_mod.OFFLINE_ANNOUNCE_DELAY
    server_mod.OFFLINE_ANNOUNCE_DELAY = 0.3  # 测试加速
    try:
        print("=== 断开 0.3s（模拟 10s）仍未恢复：应广播下线 ===")
        srv = make_srv()
        atc = H("ZBAA_APP", "5966", client_handler.CLIENT_ATC)
        srv.clients["ZBAA_APP"] = atc
        bot = Rec("CW-BOT", "0000", client_handler.CLIENT_PILOT)
        srv.clients["CW-BOT"] = bot

        ok = srv.mark_for_reconnect(atc, "connection_reset")
        check("进入重连窗口", ok is True)
        await asyncio.sleep(0.6)
        da = [m for m in bot.sent if m.startswith("#DA")]
        check("机器人收到下线广播 #DA", len(da) == 1, bot.sent[:2])
        check("下线广播带呼号", bool(da) and "ZBAA_APP" in da[0], da[:1])

        print()
        print("=== 断开 0.1s 就恢复：不应广播下线 ===")
        srv2 = make_srv()
        atc2 = H("ZBAA_TWR", "1234", client_handler.CLIENT_ATC)
        srv2.clients["ZBAA_TWR"] = atc2
        bot2 = Rec("CW-BOT", "0000", client_handler.CLIENT_PILOT)
        srv2.clients["CW-BOT"] = bot2
        srv2.mark_for_reconnect(atc2, "connection_reset")
        # 模拟恢复：清掉 pending 并把会话放回在线表
        srv2.pending_sessions.pop("ZBAA_TWR", None)
        srv2.clients["ZBAA_TWR"] = atc2
        await asyncio.sleep(0.6)
        check("恢复后没有下线广播", not [m for m in bot2.sent if m.startswith("#DA")], bot2.sent[:2])
    finally:
        server_mod.OFFLINE_ANNOUNCE_DELAY = old_delay

    print()
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
