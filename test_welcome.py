"""Test EuroScope connection - check for welcome message."""
import socket, time

s = socket.socket()
s.settimeout(10)
s.connect(("38.76.182.6", 6809))

# Read and ignore challenge
chal = s.recv(4096)

# Send #AA as EuroScope does
msg = "#AAES_TEST::EuroScope Tester:5966:cwfly2024:1:9\r\n"
s.send(msg.encode())
time.sleep(2)

# Read all available data
try:
    s.settimeout(3)
    while True:
        data = s.recv(4096)
        if not data:
            break
        print("GOT:", data.decode().strip())
except socket.timeout:
    print("(no more data)")

s.close()
