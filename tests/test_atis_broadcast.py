# -*- coding: utf-8 -*-
"""离线单测：EuroScope 通过 $CR 上报 ATIS 后，其他客户端（机器人）应收到 $AX 广播。

不连接任何网络端口，不触发机器人消息。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import client_handler  # noqa: E402
from client_handler import Client, ClientHandler  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✔ " if cond else "  ✘ ") + name + (("  " + str(extra)) if extra else ""))


class FakeServer:
    def __init__(self):
        self.clients = {}

    def get_client(self, callsign):
        return self.clients.get(callsign)


class Recorder:
    """模拟另一个在线客户端（机器人）"""

    def __init__(self, callsign, cid, ctype):
        self.client = Client(cid, callsign, ctype, 1, "Recorder", "9")
        self.callsign = callsign
        self.cid = cid
        self.client_type = ctype
        self.received = []

    async def send(self, data):
        self.received.append(data)


def bare_handler(srv, callsign, cid, ctype):
    h = ClientHandler.__new__(ClientHandler)
    h.server = srv
    h.client = Client(cid, callsign, ctype, 8, "Tester", "9")
    h.callsign = callsign
    h.cid = cid
    h.client_type = ctype
    h._closed = False
    h.writer = None
    return h


async def main():
    srv = FakeServer()
    atc = bare_handler(srv, "ZBAA_APP", "5966", client_handler.CLIENT_ATC)
    bot = Recorder("CW-BOT", "0000", client_handler.CLIENT_PILOT)
    peer = Recorder("ZBAA_TWR", "1234", client_handler.CLIENT_ATC)
    srv.clients = {"ZBAA_APP": atc, "CW-BOT": bot, "ZBAA_TWR": peer}

    text = "ZBAA ATIS A\nRWY36L\nWIND 18005KT"
    parts = ["ZBAA_APP", "SERVER", "ATIS", "T", text]
    await atc._handle_cr(parts)
    await asyncio.sleep(0.05)

    check("管制员的 ATIS 已存储", atc.client.atis == text, repr(atc.client.atis))
    bot_msgs = [m for m in bot.received if m.startswith("$AX")]
    check("机器人收到 $AX 广播", len(bot_msgs) == 1, bot_msgs[:1])
    if bot_msgs:
        m = bot_msgs[0]
        check("$AX 以 ATC 呼号开头", m.startswith("$AXZBAA_APP:"), m[:40])
        check("$AX 目标是机器人", m.split(":")[1] == "CW-BOT", m.split(":")[1])
        check("多行用 ^ 分隔", "ZBAA ATIS A^RWY36L^WIND 18005KT" in m, m[:80])
    peer_msgs = [m for m in peer.received if m.startswith("$AX")]
    check("其他管制也收到 $AX", len(peer_msgs) == 1)

    # 回归：非 ATIS 的 $CR 仍按原逻辑转发，不应被 ATIS 分支吞掉
    peer.received.clear()
    await atc._handle_cr(["ZBAA_TWR", "ZBAA_APP", "hello"])
    await asyncio.sleep(0.05)
    check("普通 $CR 仍转发给目标客户端",
          any(m.startswith("$CR") for m in peer.received), peer.received[:1])

    print()
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
