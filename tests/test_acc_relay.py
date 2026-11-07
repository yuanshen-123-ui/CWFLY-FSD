# -*- coding: utf-8 -*-
"""离线单测：swift Aircraft Configuration（$CQ ... ACC）转发。

对应 swift 源码 src/core/fsd/fsdclient.cpp：
  - 增量广播  $CQ<from>:@94836:ACC:{"config":{...}}
  - 点对点    $CQ<from>:<callsign>:ACC:{"request":"full"}
服务端只按 $CQ 语义路由：@94836 广播给所有在线机组，指定呼号只发目标。
不连接任何网络端口，不触发机器人消息。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fsd import client_handler  # noqa: E402
from fsd.client_handler import Client, ClientHandler  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (("  " + str(extra)) if extra else ""))


class FakeServer:
    def __init__(self):
        self.clients = {}

    def get_client(self, callsign):
        return self.clients.get(callsign)


class Recorder:
    """模拟另一在线客户端，收集 send() 内容。"""

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


def acc_lines(rec):
    return [m for m in rec.received if m.startswith("$CQ") and ":ACC:" in m]


async def main():
    srv = FakeServer()
    sender = bare_handler(srv, "CES1234", "1234", client_handler.CLIENT_PILOT)
    sender.received = []

    async def _sender_send(data):
        sender.received.append(data)

    sender.send = _sender_send
    pilot2 = Recorder("CCA1234", "2345", client_handler.CLIENT_PILOT)
    atc = Recorder("ZBAA_APP", "5966", client_handler.CLIENT_ATC)
    srv.clients = {"CES1234": sender, "CCA1234": pilot2, "ZBAA_APP": atc}

    # 1) swift 增量配置广播：{"config":{...}}，JSON 里含冒号
    payload = '{"config":{"gear_down":true,"lights":{"nav_on":true},"time":18:07:28}}'
    parts = ["CES1234", "@94836", "ACC", payload]
    await sender._handle_cq(parts)

    got = acc_lines(pilot2)
    check("广播：另一机组收到 1 条 ACC", len(got) == 1, got[:1])
    if got:
        m = got[0]
        check("广播：发送者呼号保持为 CES1234", m.startswith("$CQCES1234:@94836:ACC:"), m[:48])
        check("广播：payload 中的冒号原样保留", payload in m, m[-80:])
        check("广播：行以 \\r\\n 结尾", m.endswith("\r\n"))
    check("广播：ATC 客户端不应收到 ACC", len(acc_lines(atc)) == 0, acc_lines(atc)[:1])
    check("广播：不回发给发送者本身", len(acc_lines(sender)) == 0, acc_lines(sender)[:1])

    # 2) 点对点 ACC（请求 {"request":"full"}）只发目标
    pilot2.received.clear()
    atc.received.clear()
    req = ["CES1234", "CCA1234", "ACC", '{"request":"full"}']
    await sender._handle_cq(req)
    check("点对点：目标收到 1 条 ACC", len(acc_lines(pilot2)) == 1, acc_lines(pilot2)[:1])
    check("点对点：ATC 不收到", len(acc_lines(atc)) == 0)
    check("点对点：目标呼号正确", acc_lines(pilot2)[0].startswith("$CQCES1234:CCA1234:ACC:"))

    # 3) 应答包（带 isFullJson）从另一机组回到请求方
    sender.received.clear()
    responder = bare_handler(srv, "CCA1234", "2345", client_handler.CLIENT_PILOT)
    srv.clients["CCA1234"] = responder
    resp = ["CCA1234", "CES1234", "ACC",
            '{"config":{"gear_down":false,"isFullJson":true}}']
    await responder._handle_cq(resp)
    got_resp = [m for m in sender.received if m.startswith("$CQ") and ":ACC:" in m]
    check("应答：点对点回给请求方成功", len(got_resp) == 1, got_resp[:1])
    check("应答：应答包内容完整", "isFullJson" in got_resp[0] if got_resp else False)
    srv.clients["CCA1234"] = pilot2  # 恢复广播接收者

    # 4) 超长 payload 丢弃
    pilot2.received.clear()
    huge = '{"config":{"x":"' + ("A" * 9000) + '"}}'
    await sender._handle_cq(["CES1234", "@94836", "ACC", huge])
    check("超长 payload 被丢弃", len(acc_lines(pilot2)) == 0)

    # 5) 令牌桶限流：连续 40 条，最多转发 30 条（突发容量）
    pilot2.received.clear()
    sender2 = bare_handler(srv, "CCA9999", "9999", client_handler.CLIENT_PILOT)
    srv.clients["CCA9999"] = sender2
    for i in range(40):
        await sender2._handle_cq(["CCA9999", "@94836", "ACC", '{"config":{"i":%d}}' % i])
    check("限流：突发 40 条仅转发 <=30 条", len(acc_lines(pilot2)) <= 30, len(acc_lines(pilot2)))
    check("限流：至少转发 1 条", len(acc_lines(pilot2)) >= 1)

    # 6) 回归：未知 CQ 类型仍然静默忽略（不报错、不转发）
    pilot2.received.clear()
    await sender._handle_cq(["CES1234", "@94836", "ZZZ", "whatever"])
    check("回归：未知 CQ 类型不转发", len(acc_lines(pilot2)) == 0)

    print()
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
