"""Test connecting to FSD with website password (bcrypt verification)."""
import socket, time

cid = "5966"
password = input("Enter website password for CID 5966: ")

s = socket.socket()
s.settimeout(10)
s.connect(("38.76.182.6", 6809))

# Read challenge
data = s.recv(4096)
print("Challenge received")

# Skip challenge-response, send legacy #AA (ATC login)
# Format: callsign::realname:cid:password:rating:revision
msg = f"#AA{CID}_TWR::Test:{cid}:{password}:1:9\r\n"
s.send(msg.encode())
time.sleep(1)

try:
    resp = s.recv(4096)
    print("Response:", resp.decode().strip())
    if b"$ER" in resp:
        print("❌ LOGIN FAILED")
    else:
        print("✅ LOGIN SUCCESS")
except Exception as e:
    print("Error:", e)
s.close()
