#!/usr/bin/env python3
# Verification client: connect to the daemon's fMP4 WebSocket, read a few messages,
# assert the init segment (moov) and at least one media fragment (moof) arrive.
# Usage: python3 tools/ws_probe.py ws://127.0.0.1:48890/screen/ws
import sys, socket, base64, os, struct
from urllib.parse import urlparse

url = urlparse(sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:48890/screen/ws")
host, port = url.hostname, url.port or 80
s = socket.create_connection((host, port), 5)
key = base64.b64encode(os.urandom(16)).decode()
s.sendall(("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
           "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
           % (url.path, host, key)).encode())
buf = b""
while b"\r\n\r\n" not in buf:
    buf += s.recv(1)
assert b"101" in buf.split(b"\r\n")[0], "no 101 upgrade: %r" % buf.split(b"\r\n")[0]

def read_frame():
    h = s.recv(2)
    ln = h[1] & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", s.recv(2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", s.recv(8))[0]
    data = b""
    while len(data) < ln:
        data += s.recv(ln - len(data))
    return data

msgs = [read_frame() for _ in range(5)]
blob = b"".join(msgs)
assert b"moov" in blob, "no init segment (moov) received"
assert b"moof" in blob, "no media fragment (moof) received"
print("WS stream OK: %d msgs, %d bytes, moov+moof present" % (len(msgs), len(blob)))
s.close()
