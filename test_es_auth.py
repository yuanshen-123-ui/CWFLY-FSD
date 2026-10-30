"""Test EuroScope-style #AA auth against FSD server."""
import socket, time

s = socket.socket()
s.settimeout(10)
s.connect(("38.76.182.6", 6809))

# Read challenge (ignore it, EuroScope does the same)
challenge = s.recv(4096)
print("Challenge:", challenge.decode().strip())

# Send #AA like EuroScope does
# Format: #AA<callsign>::<realname>:<cid>:<password>:<rating>:<revision>
# EuroScope sends: #AACALLSIGN::REALNAME:CID:PASSWORD:RATING:9
msg = "#AAES_TEST::EuroScope Tester:5966:cwfly2024:1:9\r\n"
s.send(msg.encode())
time.sleep(1)

try:
    resp = s.recv(4096)
    print("Response:", resp.decode().strip())
    if b"$ER" in resp:
        print("❌ AUTH FAILED")
    else:
        print("✅ AUTH SUCCESS")
except Exception as e:
    print("Timeout/Error:", e)

s.close()
