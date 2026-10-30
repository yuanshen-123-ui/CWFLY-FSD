import socket
s = socket.socket()
s.settimeout(5)
try:
    s.connect(("38.76.182.6", 6809))
    print("OK: connected to port 6809")
    data = s.recv(1024)
    print("Got:", data[:80])
except Exception as e:
    print("Error:", e)
finally:
    s.close()
