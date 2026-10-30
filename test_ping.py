import os
import socket, time, hashlib, hmac

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(5)
s.connect(('127.0.0.1', 6809))

# Read challenge
data = s.recv(4096)
print('CHALLENGE:', data)

# Parse challenge
challenge = data.decode().strip().replace('$CH', '')
print(f'Parsed challenge: {challenge}')

# Compute HMAC like the Node.js tracker
password = os.environ.get("FSD_BOT_PASSWORD", "").encode()
pwd_hash = hashlib.sha256(password).digest()
h = hmac.new(challenge.encode(), pwd_hash, hashlib.sha256).hexdigest()
print(f'HMAC: {h}')

# Send $SA auth (use unique callsign to avoid "Callsign in use")
sa_msg = f'$SATEST-PING:1:000001:{h}:1:9:TestPing:0\r\n'
s.send(sa_msg.encode())
time.sleep(1)

# Read auth response
try:
    resp = s.recv(4096)
    print('AUTH RESPONSE:', resp)
except Exception as e:
    print('NO AUTH RESPONSE:', e)

# Now send ping
print('Sending PING...')
s.send(b'$PICALLSIGN:*\r\n')
time.sleep(1)

# Read pong response
try:
    resp = s.recv(4096)
    print('PONG RESPONSE:', resp)
except Exception as e:
    print('NO PONG RESPONSE:', e)
s.close()
