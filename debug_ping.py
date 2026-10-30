#!/usr/bin/env python3
"""模拟后端 fsdTracker 连接，测试 fsd-py 是否回应 $PI ping。"""
import socket
import time

HOST, PORT = "127.0.0.1", 6809

def recv_all(sock, timeout=3):
    sock.settimeout(timeout)
    data = b""
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    except socket.timeout:
        pass
    return data

s = socket.create_connection((HOST, PORT), timeout=5)
ch = recv_all(s, timeout=2)
print("challenge:", ch[:30])

# 后端认证 $SA (CW-MAP/0000/***)
import os
import hashlib, hmac as hmac_mod
password = os.environ.get("FSD_BOT_PASSWORD", "").encode()
challenge = ch[3:].strip()
pwdHash = hashlib.sha256(password).digest()
hmac_resp = hmac_mod.new(challenge, pwdHash, hashlib.sha256).hexdigest()
s.sendall(f"$SACW-MAP:1:0000:{hmac_resp}:1:9:CW-MAP:0\r\n".encode())
time.sleep(2)
data = recv_all(s, timeout=2)
print("auth response:", data[:100])

# 发 ping
print("sending $PICW-MAP:*")
s.sendall(b"$PICW-MAP:*\r\n")
time.sleep(2)
resp = recv_all(s, timeout=3)
print("ping response:", resp[:100])
s.close()
