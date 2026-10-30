# -*- coding: utf-8 -*-
"""whazzup 频率格式回归：ATC 行应输出 MHz（120.600），不能是 0.121。"""
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import whazzup as W  # noqa: E402


class _Cfg:
    whazzup_file = os.path.join(tempfile.gettempdir(), "whazzup_freq_test.txt")


class _Client:
    type = 1
    callsign = "ZBAA_APP"
    cid = "5966"
    realname = "Tester"
    frequency = 120.6
    lat = 40.0
    lon = 116.0
    altitude = 0
    groundspeed = 0
    protocol = "9"
    rating = 8
    transponder = 0
    facility_type = 5
    visual_range = 150
    plan = None
    start_time = 0


class _Handler:
    client = _Client()


class _Server:
    ident = "FSD-PY"
    config = _Cfg()
    clients = {"ZBAA_APP": _Handler()}
    location = "China"
    hostname = "localhost"
    name = "CWFLY FSD Server"

    class server_links:  # noqa: N801
        links = []


def main():
    W.CLIENT_ATC = 1
    gen = W.WhazzupGenerator.__new__(W.WhazzupGenerator)
    gen.server = _Server()
    gen._generate()
    txt = io.open(_Cfg.whazzup_file, encoding="utf-8").read()
    line = [ln for ln in txt.splitlines() if ln.startswith("ZBAA_APP")][0]
    ok = "120.600" in line
    print("  whazzup 行: " + line[:120])
    print(("  ✔ " if ok else "  ✘ ") + "ATC 频率为 120.600（不是 0.121）")
    os.remove(_Cfg.whazzup_file)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
