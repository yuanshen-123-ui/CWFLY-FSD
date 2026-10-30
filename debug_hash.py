"""调试工具：打印环境变量中口令的 SHA-256（用于核对 FSD 口令哈希）。

口令从环境变量读取，不要把真实口令写进脚本：
    ADMIN_PASSWORD / FSD_BOT_PASSWORD
"""
import hashlib
import os

found = False
for name in ("ADMIN_PASSWORD", "FSD_BOT_PASSWORD"):
    pwd = os.environ.get(name)
    if not pwd:
        continue
    found = True
    print("%-18s : %s" % (name, hashlib.sha256(pwd.encode()).hexdigest()))
if not found:
    raise SystemExit("请设置 ADMIN_PASSWORD / FSD_BOT_PASSWORD 环境变量")
