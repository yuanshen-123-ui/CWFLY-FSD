# -*- coding: utf-8 -*-
"""离线单测：会话恢复补播（断开 >=10s 才补发 #DA+#AA；小于阈值不补发）。"""
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


class Recorder:
    def __init__(self, callsign, cid, ctype):
        self.client = Client(cid, callsign, ctype, 1, "Recorder", "9")
        self.callsign = callsign
        self.cid = cid
        self.client_type = ctype
        self.sent = []

    async def send(self, data):
        self.sent.append(data)


class FakeHandler(Recorder):
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


async def scenario(gap_seconds):
    """构造一次恢复：gap_seconds 前断线（写入 pending），然后新连接恢复。"""
    srv = make_srv()
    old_client = Client("5966", "ZBAA_APP", client_handler.CLIENT_ATC, 8, "yuanshen", "9")
    old_handler = FakeHandler("ZBAA_APP", "5966", client_handler.CLIENT_ATC)
    old_handler.client = old_client
    srv.clients["ZBAA_APP"] = old_handler
    srv.client_list.append(old_client)

    peer = Recorder("CW-BOT", "0000", client_handler.CLIENT_PILOT)
    srv.clients["CW-BOT"] = peer

    # 写入重连窗口：expires_at = now + GRACE - gap ⇒ 已断开 gap 秒（窗口剩余 GRACE-gap）
    srv.pending_sessions["ZBAA_APP"] = {
        "handler": old_handler,
        "client": old_client,
        "task": None,
        "reason": "connection_reset",
        "expires_at": time.time() + server_mod.RECONNECT_GRACE - gap_seconds,
    }

    new_client = Client("5966", "ZBAA_APP", client_handler.CLIENT_ATC, 8, "yuanshen", "9")
    new_handler = FakeHandler("ZBAA_APP", "5966", client_handler.CLIENT_ATC)
    new_handler.client = new_client
    srv.register_client(new_handler)
    await asyncio.sleep(0.05)
    return peer.sent


async def main():
    print("=== 断开 20s 后恢复：应向其他客户端补发 #DA + #AA ===")
    sent = await scenario(20)
    check("相邻客户端收到 #DA", any(m.startswith("#DAZBAA_APP") for m in sent), sent[:2])
    check("相邻客户端收到 #AA", any(m.startswith("#AAZBAA_APP") for m in sent), sent[:2])
    check("先移除后新增",
          sent and sent[0].startswith("#DA") and sent[1].startswith("#AA"), sent[:2])

    print()
    print("=== 断开 2s 后恢复（抖动）：不补发，避免刷屏 ===")
    sent2 = await scenario(2)
    check("未补发 #DA/#AA", len(sent2) == 0, sent2)

    print()
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
