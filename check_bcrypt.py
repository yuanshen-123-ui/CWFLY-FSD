"""Check user's bcrypt password hash in database."""
import pymysql

conn = pymysql.connect(host="127.0.0.1", port=3306, user="root",
                       password="password", database="cwfly", charset="utf8mb4")
with conn.cursor() as cur:
    cur.execute("SELECT cid, fsdPasswordHash, LENGTH(`password`) AS pwlen, "
                "LEFT(`password`, 40) AS pwstart FROM `User` WHERE cid='5966'")
    r = cur.fetchone()
    print(f"CID: {r[0]}")
    print(f"SHA256 hash: '{r[1]}' (len={len(r[1]) if r[1] else 0})")
    print(f"BCrypt hash len: {r[2]}")
    print(f"BCrypt starts: {r[3]}...")
conn.close()

# Also test that bcrypt is importable
import bcrypt
print(f"bcrypt module OK (version: {bcrypt.__version__ if hasattr(bcrypt, '__version__') else 'present'})")
