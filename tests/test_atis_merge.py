# -*- coding: utf-8 -*-
"""离线单测：ATIS 分段聚合 + 防抖广播。

场景：EuroScope 分两段上报（如 25 字符 + 9 字符）。
期望：client.atis 为完整两行；其他客户端只收到 1 条聚合后的 $AX；$AR 请求返回完整 ATIS。
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


class Bare(ClientHandler):
    """复用真实 ClientHandler，但替换 send 为记录（不连网络）"""

    def __init__(self, srv, callsign, cid, ctype, rating=8):
        self.server = srv
        self.client = Client(cid, callsign, ctype, rating, "Tester", "9")
        self.callsign = callsign
        self.cid = cid
        self.client_type = ctype
        self._closed = False
        self.writer = None
        self.received = []

    async def send(self, data):
        self.received.append(data)


async def main():
    srv = FakeServer()
    atc = Bare(srv, "ZBAA_APP", "5966", client_handler.CLIENT_ATC)
    bot = Bare(srv, "CW-BOT", "0000", client_handler.CLIENT_PILOT, rating=1)
    srv.clients = {"ZBAA_APP": atc, "CW-BOT": bot}

    seg1 = "ZBAA ATIS INFO A, RWY36L"
    seg2 = "WIND 18005KT"
    await atc._handle_cr(["ZBAA_APP", "SERVER", "ATIS", "T", seg1])
    await asyncio.sleep(0.2)
    await atc._handle_cr(["ZBAA_APP", "SERVER", "ATIS", "T", seg2])

    check("防抖期内未广播", not [m for m in bot.received if m.startswith("$AX")], bot.received[:1])

    await asyncio.sleep(1.2)
    atis = atc.client.atis
    check("两段已聚合", atis == seg1 + "\n" + seg2, repr(atis))
    ax = [m for m in bot.received if m.startswith("$AX")]
    check("只广播 1 次 $AX", len(ax) == 1, len(ax))
    if ax:
        check("$AX 为完整 ATIS", "ZBAA ATIS INFO A, RWY36L^WIND 18005KT" in ax[0], ax[0][:80])

    # $AR：机器人主动请求，应立即拿到完整 ATIS
    bot.received.clear()
    await bot._handle_atis_req(["CW-BOT", "ZBAA_APP"])
    ax2 = [m for m in bot.received if m.startswith("$AX")]
    check("$AR 返回完整 ATIS", bool(ax2) and "^WIND 18005KT" in ax2[0], ax2[:1])

    # 新一轮 ATIS（间隔 > 1.5s）：不应叠加旧内容
    await asyncio.sleep(1.6)
    await atc._handle_cr(["ZBAA_APP", "SERVER", "ATIS", "T", "ZBAA ATIS INFO B"])
    check("新一轮 ATIS 重置而非叠加",
          atc.client.atis == "ZBAA ATIS INFO B", repr(atc.client.atis))

    print()
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
