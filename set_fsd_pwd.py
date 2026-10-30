"""Set FSD password hash for a user (so they can connect via EuroScope)."""
import hashlib
import pymysql
import sys

cid = sys.argv[1] if len(sys.argv) > 1 else "5966"
password = sys.argv[2] if len(sys.argv) > 2 else "test1234"

sha = hashlib.sha256(password.encode()).hexdigest()
print(f"Setting fsdPasswordHash for CID {cid}")
print(f"Password: {password}")
print(f"SHA256:   {sha}")

conn = pymysql.connect(host="127.0.0.1", port=3306, user="root",
                       password="password", database="cwfly", charset="utf8mb4")
with conn.cursor() as cur:
    cur.execute("UPDATE `User` SET fsdPasswordHash = %s WHERE cid = %s", (sha, cid))
    print(f"Updated: {cur.rowcount} row(s)")
conn.commit()
conn.close()

# Also update FSD's cert.txt for immediate effect
with open("/opt/cwfly/fsd-py/cert.txt", "r") as f:
    lines = f.readlines()
# Remove old entry for this CID if exists
lines = [l for l in lines if not l.startswith(cid + " ")]
# Add new entry
lines.append(f"{cid} {sha} 12 1785145302 mysql\n")
with open("/opt/cwfly/fsd-py/cert.txt", "w") as f:
    f.writelines(lines)
print("Also updated cert.txt")
