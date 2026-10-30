"""List users with fsdLevel >= 1 (OBS and above)."""
import pymysql

conn = pymysql.connect(host="127.0.0.1", port=3306, user="root",
                       password="password", database="cwfly", charset="utf8mb4")
with conn.cursor() as cur:
    cur.execute("""
        SELECT cid, username, fsdLevel, fsdType, rating,
               CASE fsdLevel
                   WHEN 0 THEN 'SUSPENDED'
                   WHEN 1 THEN 'OBS'
                   WHEN 2 THEN 'S1'
                   WHEN 3 THEN 'S2'
                   WHEN 4 THEN 'S3'
                   WHEN 5 THEN 'C1'
                   WHEN 6 THEN 'C2'
                   WHEN 7 THEN 'C3'
                   WHEN 8 THEN 'I1'
                   WHEN 9 THEN 'I2'
                   WHEN 10 THEN 'I3'
                   WHEN 11 THEN 'SUP'
                   WHEN 12 THEN 'ADM'
                   ELSE CAST(fsdLevel AS CHAR)
               END AS level_name,
               createdAt
        FROM `User`
        WHERE fsdLevel >= 1
        ORDER BY fsdLevel DESC, cid
    """)
    rows = cur.fetchall()
    if not rows:
        print("没有找到 fsdLevel >= 1 的用户")
    else:
        print(f"{'CID':<8} {'用户名':<16} {'等级':<8} {'等级名':<10} {'FSD类型':<8} {'注册时间'}")
        print("-" * 70)
        for r in rows:
            print(f"{r[0]:<8} {r[1]:<16} {r[2]:<8} {r[4]:<10} {r[3]:<8} {r[5]}")
    print(f"\n共 {len(rows)} 人")
conn.close()
