# Screen-share WS+MSE universal fallback (Phase 1) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fragmented-MP4-over-WebSocket → MSE screen-share transport that rides the existing cloudflared tunnel, so any remote viewer (behind any NAT, no account/card/Tailscale) gets a ~0.5–1 s picture, with the viewer auto-falling-back to it from WebRTC.

**Architecture:** The host daemon (`rp-webrtc.py`) runs a second ffmpeg that encodes the same screen to fragmented MP4 on stdout. A new **media server** (a `ThreadingHTTPServer` on port 48890, bound to all interfaces) parses that fMP4 into init + fragments and broadcasts the fragments over a hand-rolled WebSocket to connected viewers; it also reverse-proxies `/screen/whep` to MediaMTX (8889) so WebRTC-over-tunnel still works. The cloudflared tunnel now targets 48890. The control API stays localhost-only on 48592. The plugin viewer gets a `wConnectWsMse()` MSE player and auto-fallback: try WebRTC, and if no live video in ~5 s, switch to the WS url.

**Tech Stack:** Python 3 stdlib (sockets, `http.server`, `hashlib`, `struct`, `base64`), ffmpeg + h264_nvenc/vaapi/qsv/x264, MediaMTX, cloudflared, browser MSE + WebSocket (Chromium/CEF, no external JS lib).

**Spec:** `docs/superpowers/specs/2026-06-16-screenshare-transports-design.md`

**Conventions in this repo:**
- No unit-test framework. "Tests" are verification commands: `python3 -c "import ast; ast.parse(...)"` for the daemon, the esprima venv at `/tmp/jschk` for `index.js`, `curl` against the daemon, a frame-brightness/`<video>` check, and a manual off-LAN run.
- Validate `index.js` after every change: `/tmp/jschk/bin/python -c "import esprima; esprima.parseModule(open('plugin/.millennium/Dist/index.js').read())"`. A parse error bricks the self-updater.
- Bump `var VERSION = N` in `index.js` whenever `index.js` changes (the self-updater key).
- The daemon runs as the systemd `--user` service `discordish-rp-webrtc`; reload with `systemctl --user restart discordish-rp-webrtc`. Force a MediaMTX respawn with `pkill -x mediamtx`.
- `pkill -f`/`pgrep -f` can match this shell (exit 144); use `pkill -x <exactname>`.
- The desktop's real X display is `:1` (verified).

---

## File structure

- **Modify `rp-webrtc.py`** — add: WebSocket helpers + broadcast registry; fMP4 box-pump; fMP4 capture command; the media `ThreadingHTTPServer` (WS + WHEP proxy); start/stop wiring in `start_capture`/`stop_capture`; retarget the tunnel to the media port; add `ws` to `/start`. (One file; it already owns all capture/serve logic.)
- **Modify `plugin/.millennium/Dist/index.js`** — add `wConnectWsMse()`; extend `sigPayload`/`parseSignal` with `t=` (type) and `ws=` (fallback url); host sends both; viewer auto-fallback; bump `VERSION`.
- **Create `tools/ws_probe.py`** — a tiny stdlib WebSocket client used only for verification (asserts the init segment + fragments arrive).

---

## Task 1: Lower the default bitrate to 6 Mbps

**Files:**
- Modify: `rp-webrtc.py` (the `BITRATE` constant, ~line 37)

- [ ] **Step 1: Change the default**

In `rp-webrtc.py`, change:
```python
BITRATE = os.environ.get("DS_BITRATE", "12M")
```
to:
```python
BITRATE = os.environ.get("DS_BITRATE", "6M")   # 1440p-friendly default; presets come in Phase 3
```

- [ ] **Step 2: Verify it parses**

Run: `python3 -c "import ast; ast.parse(open('rp-webrtc.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add rp-webrtc.py
git commit -m "screen share: default bitrate 12M -> 6M"
```

---

## Task 2: WebSocket helpers + broadcast registry

**Files:**
- Modify: `rp-webrtc.py` (add a new section after the `state`/`lock` block, near line 44)

- [ ] **Step 1: Add the WS framing + broadcast code**

Add these imports to the existing import line if missing: `base64, hashlib, struct` (struct is already imported). Add `base64, hashlib`:
```python
import os, sys, json, socket, subprocess, threading, signal, shutil, time, re, tempfile
import urllib.request, urllib.error, base64, hashlib, struct
```

Then add this block right after `lock = threading.Lock()`:
```python
# --- fragmented-MP4 over WebSocket (the universal, tunnel-friendly transport) ---------
# A remote viewer behind a hard NAT can't get WebRTC media (UDP) through the cloudflared
# tunnel, but it CAN pull fragmented MP4 over a WebSocket (TCP) and play it via MSE. We
# hand-roll a minimal server->client binary WebSocket (RFC 6455) so the daemon stays
# stdlib-only. ws_clients holds the live viewer sockets; fmp4_init holds the latest init
# segment (ftyp+moov) so a newly-connected viewer can start immediately.
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
ws_clients = set()
ws_lock = threading.Lock()
fmp4_init = {"seg": None}


def _ws_accept(key):
    return base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()


def _ws_frame(payload, opcode=0x2):          # 0x2 = binary, FIN set, unmasked (server->client)
    n = len(payload)
    if n < 126:
        hdr = bytes([0x80 | opcode, n])
    elif n < 65536:
        hdr = bytes([0x80 | opcode, 126]) + struct.pack(">H", n)
    else:
        hdr = bytes([0x80 | opcode, 127]) + struct.pack(">Q", n)
    return hdr + payload


def ws_broadcast(data):
    frame = _ws_frame(data)
    with ws_lock:
        dead = []
        for c in ws_clients:
            try:
                c.sendall(frame)
            except Exception:
                dead.append(c)
        for c in dead:
            ws_clients.discard(c)
            try:
                c.close()
            except Exception:
                pass
```

- [ ] **Step 2: Verify it parses**

Run: `python3 -c "import ast; ast.parse(open('rp-webrtc.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Verify the accept-key against the RFC example**

Run:
```bash
python3 - <<'EOF'
import base64, hashlib
GUID="258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
def acc(k): return base64.b64encode(hashlib.sha1((k+GUID).encode()).digest()).decode()
assert acc("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=", "bad accept"
print("accept-key OK")
EOF
```
Expected: `accept-key OK` (this is the canonical RFC 6455 test vector)

- [ ] **Step 4: Commit**

```bash
git add rp-webrtc.py
git commit -m "screen share: minimal server->client WebSocket framing + broadcast registry"
```

---

## Task 3: fMP4 box-pump (split init from fragments, broadcast each fragment)

**Files:**
- Modify: `rp-webrtc.py` (add after the WS block from Task 2)

- [ ] **Step 1: Add the reader + box parser**

```python
def _read_exact(stream, n):
    buf = b""
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def fmp4_pump(stream):
    """Read a fragmented-MP4 byte stream box-by-box. ftyp+moov form the MSE init segment
    (stored for new clients); each moof+mdat pair is one media fragment broadcast as a
    single WebSocket message so MSE sees clean fragment boundaries."""
    init = b""
    have_init = False
    pending_moof = None
    while True:
        header = _read_exact(stream, 8)
        if not header:
            break
        size = struct.unpack(">I", header[:4])[0]
        btype = header[4:8]
        if size == 1:                      # 64-bit extended size
            ext = _read_exact(stream, 8)
            if ext is None:
                break
            size = struct.unpack(">Q", ext)[0]
            body = _read_exact(stream, size - 16)
            box = header + ext + (body or b"")
        else:
            body = _read_exact(stream, size - 8)
            box = header + (body or b"")
        if body is None:
            break
        if btype in (b"ftyp", b"moov"):
            init += box
            if btype == b"moov":
                with ws_lock:
                    fmp4_init["seg"] = init
                have_init = True
        elif btype == b"moof":
            pending_moof = box
        elif btype == b"mdat":
            if have_init and pending_moof is not None:
                ws_broadcast(pending_moof + box)
                pending_moof = None
        # any other box (styp, sidx, free, ...) is ignored
```

- [ ] **Step 2: Verify it parses**

Run: `python3 -c "import ast; ast.parse(open('rp-webrtc.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Unit-test the box parser against a real fMP4 from ffmpeg**

Run (generates 1 s of fragmented MP4, feeds it through the parser, asserts an init segment with `moov` and at least one fragment with `moof`):
```bash
python3 - <<'EOF'
import subprocess, struct, importlib.util, io
# tiny fragmented mp4 (test source, no display needed)
data = subprocess.run(
    ["ffmpeg","-hide_banner","-loglevel","error","-f","lavfi","-i","testsrc=size=320x240:rate=30",
     "-t","1","-c:v","libx264","-profile:v","high","-pix_fmt","yuv420p","-g","30","-bf","0",
     "-movflags","+frag_keyframe+empty_moov+default_base_moof","-frag_duration","100000",
     "-f","mp4","pipe:1"], capture_output=True).stdout
assert data, "ffmpeg produced nothing"
frags = []
init = {"seg": None}
# minimal stand-in for the daemon globals so we can import fmp4_pump in isolation
import types
g = {"struct": struct, "ws_lock": __import__("threading").Lock(), "fmp4_init": init,
     "ws_broadcast": lambda d: frags.append(d)}
src = open("rp-webrtc.py").read()
# pull just _read_exact + fmp4_pump definitions
import re
m = "\n".join(re.findall(r"(?ms)^def _read_exact.*?\n\n\n", src)) + \
    "\n".join(re.findall(r"(?ms)^def fmp4_pump.*?\n\n\n", src))
exec(m, g)
g["fmp4_pump"](io.BytesIO(data))
assert init["seg"] and b"moov" in init["seg"], "no init segment"
assert frags and b"moof" in frags[0], "no fragments"
print("box parser OK: init=%d bytes, %d fragments" % (len(init["seg"]), len(frags)))
EOF
```
Expected: `box parser OK: init=… bytes, N fragments` (N ≥ 1)

- [ ] **Step 4: Commit**

```bash
git add rp-webrtc.py
git commit -m "screen share: fMP4 box-pump (init + per-fragment WS broadcast)"
```

---

## Task 4: fMP4 capture command (feeds the pump)

**Files:**
- Modify: `rp-webrtc.py` (add a capture helper; reuse `pick_encoder` input args)

- [ ] **Step 1: Add the fMP4 encoder-args + capture launcher**

Add after `pick_encoder` (so it can reuse it):
```python
def _fmp4_enc_args():
    """H.264 args tuned for MSE fragmented-MP4: High@5.2 (covers up to 4K so the browser
    codec string is fixed), no B-frames, 1 s GOP, small fragments for low latency."""
    name, in_args, _ = pick_encoder()
    common = ["-profile:v", "high", "-level", "5.2", "-bf", "0", "-g", str(int(FPS)),
              "-b:v", BITRATE, "-maxrate", BITRATE, "-bufsize", "2M"]
    frag = ["-movflags", "+frag_keyframe+empty_moov+default_base_moof",
            "-frag_duration", "100000", "-f", "mp4", "pipe:1"]
    if name == "h264_nvenc":
        return in_args, ["-c:v", "h264_nvenc", "-preset", "p4", "-tune", "ll"] + common + frag
    if name == "h264_vaapi":
        return in_args, ["-c:v", "h264_vaapi"] + common + frag
    if name == "h264_qsv":
        return in_args, ["-c:v", "h264_qsv"] + common + frag
    return ["-vf", "format=yuv420p"], ["-c:v", "libx264", "-preset", "veryfast",
            "-tune", "zerolatency"] + common + frag


def start_fmp4(geom=None, win=None):
    """Start the fragmented-MP4 encode for the WS path and pump it to ws_broadcast.
    Runs alongside the RTSP/WHEP capture so the viewer can use either transport."""
    in_args, enc_args = _fmp4_enc_args()
    env = _cap_env()
    if IS_WIN and win:
        cap_in = ["-f", "gdigrab", "-draw_mouse", "1", "-framerate", FPS, "-i", "title=%s" % win]
    elif win and not IS_WIN:
        # Linux per-window: reuse the gst xid pipe -> h264, but for fMP4 we re-grab via ffmpeg
        # x11grab of the window's geometry is not occlusion-proof; Phase 1 supports monitor
        # geom for the WS path and falls back to full-monitor for per-window shares.
        m = monitors(); g = (next((x for x in m if x["primary"]), m[0])["geom"] if m else "1920x1080+0+0")
        return start_fmp4(geom=g)
    else:
        mm = re.match(r"(\d+)x(\d+)\+(\d+)\+(\d+)", geom or "")
        if not mm:
            return False
        w, h, x, y = mm.groups()
        if IS_WIN:
            cap_in = ["-f", "gdigrab", "-draw_mouse", "1", "-framerate", FPS,
                      "-offset_x", x, "-offset_y", y, "-video_size", "%sx%s" % (w, h), "-i", "desktop"]
        else:
            cap_in = ["-f", "x11grab", "-draw_mouse", "1", "-framerate", FPS,
                      "-video_size", "%sx%s" % (w, h), "-i", "%s+%s,%s" % (DISPLAY, x, y)]
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning"] + cap_in + in_args + enc_args
    with ws_lock:
        fmp4_init["seg"] = None
    p = _spawn(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    state["fmp4"] = p
    threading.Thread(target=fmp4_pump, args=(p.stdout,), daemon=True).start()
    return True


def stop_fmp4():
    _kill(state.get("fmp4"))
    state["fmp4"] = None
    with ws_lock:
        fmp4_init["seg"] = None
        for c in list(ws_clients):
            try:
                c.close()
            except Exception:
                pass
        ws_clients.clear()
```

Add `"fmp4": None` to the `state` dict initialization:
```python
state = {"ff": None, "mtx": None, "geom": None, "cf": None, "cf_url": None, "fmp4": None}
```

- [ ] **Step 2: Verify it parses**

Run: `python3 -c "import ast; ast.parse(open('rp-webrtc.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add rp-webrtc.py
git commit -m "screen share: fMP4 capture launcher (monitor geom) feeding the WS pump"
```

---

## Task 5: Media server (WebSocket endpoint + WHEP reverse-proxy) on port 48890

**Files:**
- Modify: `rp-webrtc.py` (add a media `ThreadingHTTPServer`; start it in `main`)

- [ ] **Step 1: Add the media server and its handler**

Add near the top with the other ports:
```python
MEDIA_PORT = 48890           # public-facing (tunneled): WS fMP4 + WHEP proxy. NOT the control API.
```

Add this handler + launcher (place above `main`):
```python
from http.server import ThreadingHTTPServer


class MediaH(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _proxy_whep(self):
        # Forward the WHEP signaling (POST SDP offer) to MediaMTX; media itself is separate UDP.
        body = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        try:
            req = urllib.request.Request("http://127.0.0.1:8889" + self.path, data=body,
                                         method=self.command,
                                         headers={"Content-Type": self.headers.get("Content-Type", "application/sdp")})
            with urllib.request.urlopen(req, timeout=10) as r:
                ans = r.read(); code = r.status; ctype = r.headers.get("Content-Type", "application/sdp")
        except urllib.error.HTTPError as e:
            ans = e.read(); code = e.code; ctype = "text/plain"
        except Exception as e:
            ans = str(e).encode(); code = 502; ctype = "text/plain"
        self.send_response(code)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(ans)))
        self.end_headers()
        try:
            self.wfile.write(ans)
        except Exception:
            pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        if "/whep" in self.path:
            self._proxy_whep()
        else:
            self.send_response(404); self.end_headers()

    def do_GET(self):
        if self.path.startswith("/screen/ws"):
            key = self.headers.get("Sec-WebSocket-Key")
            if not key:
                self.send_response(400); self.end_headers(); return
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", _ws_accept(key))
            self.end_headers()
            sock = self.connection
            with ws_lock:
                seg = fmp4_init["seg"]
            if seg:
                try:
                    sock.sendall(_ws_frame(seg))
                except Exception:
                    return
            with ws_lock:
                ws_clients.add(sock)
            try:
                while True:                     # block this thread until the client disconnects
                    if not sock.recv(4096):
                        break
            except Exception:
                pass
            finally:
                with ws_lock:
                    ws_clients.discard(sock)
            return
        if "/whep" in self.path:                # MediaMTX WHEP may probe with GET
            self._proxy_whep(); return
        self.send_response(404); self.end_headers()


def start_media_server():
    srv = ThreadingHTTPServer(("0.0.0.0", MEDIA_PORT), MediaH)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
```

In `main()`, start the media server before `srv.serve_forever()`:
```python
    start_media_server()
```
(place it just after the control `HTTPServer` is created and the prewarm thread is started)

- [ ] **Step 2: Verify it parses**

Run: `python3 -c "import ast; ast.parse(open('rp-webrtc.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add rp-webrtc.py
git commit -m "screen share: media server on :48890 (WS fMP4 endpoint + WHEP reverse-proxy)"
```

---

## Task 6: Wire fMP4 into start/stop, retarget the tunnel, add `ws` to /start

**Files:**
- Modify: `rp-webrtc.py` — `start_capture`, `stop_capture`, `ensure_tunnel`, the `/start` handler

- [ ] **Step 1: Start/stop the fMP4 encode alongside the RTSP capture**

In `start_capture`, right after `ensure_mediamtx()`, add:
```python
        start_fmp4(geom=geom, win=win)
```
In `stop_capture`, add `stop_fmp4()`:
```python
def stop_capture():
    _kill(state.get("ff"))
    state["ff"] = None
    stop_fmp4()
```

- [ ] **Step 2: Point the tunnel at the media port (so WS + WHEP are both reachable)**

In `ensure_tunnel`, change the tunnel target from `WHEP_PORT` to `MEDIA_PORT`:
```python
    p = _spawn([CLOUDFLARED, "tunnel", "--no-autoupdate", "--url",
                "http://localhost:%d" % MEDIA_PORT],
               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
```

- [ ] **Step 3: Add the `ws` url to the /start response**

In the `/start` branch of the control handler, build a ws url from the tunnel (wss when tunneled, else ws to LAN media port) and include it:
```python
            ws_base = (tun.replace("https://", "wss://") if tun
                       else "ws://%s:%d" % (ip, MEDIA_PORT))
            out_ws = ws_base + "/screen/ws"
            # WHEP now rides the media-port proxy too:
            out_whep = (tun + "/screen/whep") if tun else ("http://%s:%d/screen/whep" % (ip, MEDIA_PORT))
            self._send({"ok": ok, "whep": out_whep, "ws": out_ws, "lan_whep": lan_whep,
                        "tunnel": bool(tun), "path": "screen", "geom": geom,
                        "encoder": pick_encoder()[0], "ice": turn_ice()})
```
(Replace the existing `out_whep`/`self._send(...)` lines in that branch with the above. `lan_whep` stays as defined earlier in the handler.)

- [ ] **Step 4: Verify it parses**

Run: `python3 -c "import ast; ast.parse(open('rp-webrtc.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 5: Restart the daemon and smoke-test /start**

```bash
systemctl --user restart discordish-rp-webrtc
sleep 1
pkill -x mediamtx 2>/dev/null; sleep 1
curl -s --max-time 8 "http://127.0.0.1:48592/start?geom=3840x2160%2B3840%2B0" | python3 -m json.tool
```
Expected: JSON with `"ok": true`, a `"ws": "wss://….trycloudflare.com/screen/ws"`, and a `"whep"` url. Leave the share running for Task 7.

- [ ] **Step 6: Commit**

```bash
git add rp-webrtc.py
git commit -m "screen share: run fMP4 with capture, tunnel media port, return ws url from /start"
```

---

## Task 7: Verification tool — pull the WS stream and assert it's real

**Files:**
- Create: `tools/ws_probe.py`

- [ ] **Step 1: Write the stdlib WebSocket probe client**

```python
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
# read handshake response headers
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
```

- [ ] **Step 2: Run it against the live share from Task 6**

Run: `python3 tools/ws_probe.py ws://127.0.0.1:48890/screen/ws`
Expected: `WS stream OK: 5 msgs, … bytes, moov+moof present`

If it hangs or fails: check the fMP4 ffmpeg is alive (`pgrep -af ffmpeg | grep mp4`), and run its command by hand with `-loglevel info` to see encoder errors (the daemon hides stderr).

- [ ] **Step 3: Stop the test share**

Run: `curl -s http://127.0.0.1:48592/stop`

- [ ] **Step 4: Commit**

```bash
git add tools/ws_probe.py
git commit -m "screen share: ws_probe.py verification client for the fMP4 WebSocket"
```

---

## Task 8: Viewer — `wConnectWsMse()` MSE player

**Files:**
- Modify: `plugin/.millennium/Dist/index.js` (add next to `wConnectShare`, ~line 232)

- [ ] **Step 1: Add the MSE player**

Add after `wConnectShare`:
```javascript
  // Viewer (universal fallback): play fragmented-MP4 streamed over a WebSocket via MSE.
  // Works for any remote viewer through the cloudflared tunnel (no WebRTC/UDP needed).
  function wConnectWsMse(wsUrl, v) {
    try {
      if (!window.MediaSource) { console.warn("[ds] MSE unavailable"); return; }
      if (v.__ws) { try { v.__ws.close(); } catch (e) {} v.__ws = null; }
      var ms = new MediaSource();
      v.src = URL.createObjectURL(ms);
      var sb = null, queue = [];
      function pump() {
        if (!sb || sb.updating || !queue.length) return;
        var chunk = queue[0];
        try { sb.appendBuffer(chunk); queue.shift(); }
        catch (e) {
          if (e && e.name === "QuotaExceededError" && v.buffered.length) {
            try { sb.remove(0, Math.max(0, v.currentTime - 2)); } catch (e2) {}
            // updateend (from remove) re-fires pump; leave the chunk queued to retry
          } else { queue.shift(); }
        }
      }
      ms.addEventListener("sourceopen", function () {
        try {
          sb = ms.addSourceBuffer('video/mp4; codecs="avc1.640034"');   // High@5.2, covers up to 4K
          sb.mode = "sequence";
          sb.addEventListener("updateend", pump);
        } catch (e) { console.warn("[ds] addSourceBuffer", e); return; }
        var ws = new WebSocket(wsUrl);
        ws.binaryType = "arraybuffer";
        v.__ws = ws;
        ws.onmessage = function (ev) {
          queue.push(new Uint8Array(ev.data));
          if (queue.length > 240) queue.splice(0, queue.length - 240);   // bound memory
          pump();
          try {                                       // ride the live edge (keep latency low)
            if (v.buffered.length) {
              var end = v.buffered.end(v.buffered.length - 1);
              if (end - v.currentTime > 1.2) v.currentTime = end - 0.2;
            }
          } catch (e) {}
          if (v.paused && v.play) v.play().catch(function () {});
        };
        ws.onerror = function (e) { console.warn("[ds] ws err", e); };
      });
    } catch (e) { console.warn("[ds] wConnectWsMse", e); }
  }
```

- [ ] **Step 2: Validate JS**

Run: `/tmp/jschk/bin/python -c "import esprima; esprima.parseModule(open('plugin/.millennium/Dist/index.js').read()); print('OK')"`
(If the venv is gone: `python3 -m venv /tmp/jschk && /tmp/jschk/bin/pip -q install esprima`)
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add plugin/.millennium/Dist/index.js
git commit -m "screen share: wConnectWsMse MSE player for the WS fMP4 fallback"
```

---

## Task 9: Signal format + viewer auto-fallback

**Files:**
- Modify: `plugin/.millennium/Dist/index.js` — `sigPayload`/`parseSignal` (~line 692), the host auto-signal (~line 496), the viewer-side tile connect (`window.__ds_view_url` handling, ~line 622), and the poll handler (~line 724); bump `VERSION`.

- [ ] **Step 1: Extend the signal payload to carry type + ws url**

Replace `sigPayload`/`parseSignal` with:
```javascript
  function sigPayload(url, ice, ws) {
    var p = url;
    if (ice && ice.length) { try { p += "|ice=" + btoa(JSON.stringify(ice)); } catch (e) {} }
    if (ws) p += "|ws=" + encodeURIComponent(ws);
    return p;
  }
  function parseSignal(payload) {
    var out = { url: payload, ice: null, ws: null };
    var iceI = payload.indexOf("|ice=");
    var wsI = payload.indexOf("|ws=");
    var cut = Math.min(iceI < 0 ? payload.length : iceI, wsI < 0 ? payload.length : wsI);
    out.url = payload.slice(0, cut);
    if (iceI >= 0) {
      var end = (wsI > iceI) ? wsI : payload.length;
      try { out.ice = JSON.parse(atob(payload.slice(iceI + 5, end))); } catch (e) {}
    }
    if (wsI >= 0) { try { out.ws = decodeURIComponent(payload.slice(wsI + 4)); } catch (e) {} }
    return out;
  }
```

- [ ] **Step 2: Host sends both the WebRTC url and the WS fallback url**

In `wStartShare`'s `.then`, also stash the ws url:
```javascript
        window.__ds_share_ice = (j && j.ice) || null;
        window.__ds_share_ws = (j && j.ws) || null;
```
In the share-button auto-signal call (currently `sendSignal(doc, sigPayload(window.__ds_share_url, window.__ds_share_ice))`), pass the ws url:
```javascript
          if (window.__ds_share_url) { try { sendSignal(doc, sigPayload(window.__ds_share_url, window.__ds_share_ice, window.__ds_share_ws)); } catch (e) {} }
```

- [ ] **Step 3: Viewer stores the ws url from the signal**

In `pollSignals`, where it sets `window.__ds_view_url`, also set the ws url:
```javascript
          if (payload === "stop") { window.__ds_view_url = null; window.__ds_view_ice = null; window.__ds_view_ws = null; }
          else { var p = parseSignal(payload); window.__ds_view_url = p.url; window.__ds_view_ice = p.ice; window.__ds_view_ws = p.ws; }
```

- [ ] **Step 4: Auto-fallback when WebRTC brings no video in ~5 s**

Find where the viewer tile calls `wConnectShare(window.__ds_view_url, vid)` (the two call sites around lines 635/638). Wrap each so it arms a fallback. Add this helper just above `wConnectWsMse`:
```javascript
  // Try WebRTC; if no live video arrives within ~5s and we have a ws fallback url, switch.
  function wConnectAuto(url, v) {
    if (v.__dsAutoTimer) { clearTimeout(v.__dsAutoTimer); v.__dsAutoTimer = null; }
    wConnectShare(url, v);
    var ws = window.__ds_view_ws;
    if (ws) {
      v.__dsAutoTimer = setTimeout(function () {
        if (!(v.srcObject && v.srcObject.active) && (!v.videoWidth)) {
          try { if (v.__pc) { v.__pc.close(); v.__pc = null; } } catch (e) {}
          wConnectWsMse(ws, v);
        }
      }, 5000);
    }
  }
```
Then replace the two `wConnectShare(window.__ds_view_url, …)` tile call sites with `wConnectAuto(window.__ds_view_url, …)` (keep the same `<video>` argument each used).

- [ ] **Step 5: Bump VERSION**

Change `var VERSION = 45;` to `var VERSION = 46;`.

- [ ] **Step 6: Validate JS**

Run: `/tmp/jschk/bin/python -c "import esprima; esprima.parseModule(open('plugin/.millennium/Dist/index.js').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 7: Commit**

```bash
git add plugin/.millennium/Dist/index.js
git commit -m "screen share: signal carries ws fallback url + viewer WebRTC->WS auto-fallback (v46)"
```

---

## Task 10: End-to-end off-LAN verification

**Files:** none (manual verification)

- [ ] **Step 1: Push so the viewer self-updates**

```bash
git push origin master
```

- [ ] **Step 2: Put the viewer off-LAN**

On the laptop (or a second machine) signed into a **second Steam account friended to your main one**, get it **off your LAN**: tether to a phone hotspot, or run a full-tunnel VPN. (On the same Wi-Fi it would use the direct LAN path and not exercise the fallback.) Restart Steam so the plugin self-updates to v46.

- [ ] **Step 3: Share and confirm**

From the desktop, start a screen share to that friend. Expected on the laptop: within ~5 s the call tile shows your screen (not black) — it tried WebRTC, failed (no relay), and fell back to WS+MSE over the tunnel.

- [ ] **Step 4: Confirm which path / rough latency**

On the desktop, confirm a viewer attached: `python3 tools/ws_probe.py wss://<your-trycloudflare-host>/screen/ws` should also pull frames (use the `ws` url from `/url`). Eyeball latency by sharing a window with a running clock/stopwatch — target ~0.5–1 s.

- [ ] **Step 5: Record the result**

If it works, note it. If black persists, gather evidence before fixing (per systematic-debugging): is the fMP4 ffmpeg alive? does `ws_probe.py` pull frames locally? does the laptop reach the tunnel host (`curl -sI https://<host>/screen/ws`)? Fix the failing layer, don't guess.

---

## Self-review notes

- **Spec coverage (Phase 1 rows):** WS+MSE universal transport → Tasks 2–8; auto-fallback → Task 9; tunnel carries it → Task 6; bitrate default → Task 1; verification incl. off-LAN → Tasks 7,10. Tailscale tier, settings panel, TURN provider-agnostic entry, and the cost limiter are **Phase 2/3** (separate plans) and intentionally out of this plan.
- **Known Phase-1 simplification:** Linux **per-window** shares use full-monitor capture for the WS path (Task 4) — occlusion-proof per-window WS capture is deferred; WebRTC per-window still works as today. Also two simultaneous encodes (RTSP + fMP4) run during a share; teeing a single capture is a later optimization (noted in the spec).
- **Type consistency:** `wConnectWsMse(wsUrl, v)`, `wConnectAuto(url, v)`, `sigPayload(url, ice, ws)`, `parseSignal(...) -> {url,ice,ws}`, daemon `start_fmp4`/`stop_fmp4`/`fmp4_pump`/`ws_broadcast`/`fmp4_init`/`MEDIA_PORT` are used consistently across tasks.
- **Codec risk:** MSE uses fixed `avc1.640034` (High@5.2) and the encoder is forced `-profile:v high -level 5.2`. If a specific encoder ignores the level and MSE rejects the buffer, the fallback is to parse the `avcC` box in `fmp4_init` and send the exact codec string to the viewer (note for execution if Task 8 verification shows a SourceBuffer error).
