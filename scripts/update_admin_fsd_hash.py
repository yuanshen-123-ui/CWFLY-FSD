"""为管理员(CID 0000)与机器人(CID 000001)设置 FSD 口令哈希。

口令一律从环境变量读取，不要写进脚本/仓库：
    ADMIN_PASSWORD      管理员 FSD 口令
    FSD_BOT_PASSWORD    机器人 FSD 口令
数据库连接同样从环境变量读取，缺省为本地开发默认值。

用法：
    ADMIN_PASSWORD=*** FSD_BOT_PASSWORD=*** python update_admin_fsd_hash.py
"""
import hashlib
import os

import pymysql

passwords = {}
admin_pwd = os.environ.get("ADMIN_PASSWORD")
bot_pwd = os.environ.get("FSD_BOT_PASSWORD")
if admin_pwd:
    passwords["0000"] = admin_pwd
if bot_pwd:
    passwords["000001"] = bot_pwd
if not passwords:
    raise SystemExit("请设置 ADMIN_PASSWORD / FSD_BOT_PASSWORD 环境变量")

conn = pymysql.connect(
    host=os.environ.get("DB_HOST", "127.0.0.1"),
    port=int(os.environ.get("DB_PORT", "3306")),
    user=os.environ.get("DB_USER", "root"),
    password=os.environ.get("DB_PASSWORD", "password"),
    database=os.environ.get("DB_NAME", "cwfly"),
    charset="utf8mb4",
)
with conn.cursor() as cur:
    for cid, pwd in passwords.items():
        sha = hashlib.sha256(pwd.encode()).hexdigest()
        cur.execute("UPDATE `User` SET fsdPasswordHash = %s WHERE cid = %s", (sha, cid))
        print(f"Updated CID {cid}: rows={cur.rowcount}")
conn.commit()
conn.close()
print("Done!")
