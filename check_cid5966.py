import pymysql
conn = pymysql.connect(host='127.0.0.1', port=3306, user='root',
    password='password', database='cwfly', charset='utf8mb4')
with conn.cursor() as cur:
    cur.execute("SELECT cid, username, fsdPasswordHash, LENGTH(fsdPasswordHash), fsdLevel FROM `User` WHERE cid='5966'")
    r = cur.fetchone()
    if r:
        print(f'CID: {r[0]}, Name: {r[1]}, Hash len: {r[3]}, Level: {r[4]}')
        print(f'Hash: {r[2][:30]}...' if r[2] else 'Hash: EMPTY')
    else:
        print('User not found')
conn.close()
