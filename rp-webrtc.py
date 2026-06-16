#!/usr/bin/env python3
# Host-side WebRTC screen-share daemon for the discord-ish reskin.
#
# Replaces the old Steam-Remote-Play path entirely. The plugin (CEF) can't capture the
# screen (getDisplayMedia is blocked in Steam's webhelper) and can't exec, so this small
# localhost daemon does the work:
#   capture (ffmpeg x11grab, a chosen monitor) -> HARDWARE encode (auto-detected:
#   NVENC / VAAPI / QSV, else libx264) -> publish to MediaMTX -> MediaMTX serves WebRTC
#   (WHEP) which the VIEWER's plugin plays in a <video> tile. Viewers only decode H.264
#   (universal) so any GPU/OS can watch.
#
# Control API (localhost; the plugin fetches these — Chromium allows https->127.0.0.1):
#   GET /sources              -> {monitors:[{name,geom,primary}], encoder:"h264_nvenc"|...}
#   GET /start?geom=WxH+X+Y    -> start capturing that monitor region; {ok, whep, path}
#   GET /stop                  -> stop the capture
#   GET /url                   -> {whep: "http://<lan-ip>:8889/screen/whep", lan_ip}
#   GET /ping                  -> {ok:true, running:bool}
#
# MediaMTX is started on demand from ./bin/mediamtx with ./bin/rp-mediamtx.yml.
import os, sys, json, socket, subprocess, threading, signal, shutil, time, re
import urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(HERE, "bin")
MEDIAMTX = os.path.join(BIN, "mediamtx")
MTX_CFG = os.path.join(BIN, "rp-mediamtx.yml")
CLOUDFLARED = os.path.join(BIN, "cloudflared")
PORT = 48592                 # control API (was 48591 for the old capture server)
RTSP = "rtsp://127.0.0.1:8554/screen"
WHEP_PORT = 8889
DISPLAY = os.environ.get("DISPLAY", ":1")
BITRATE = os.environ.get("DS_BITRATE", "12M")
FPS = os.environ.get("DS_FPS", "30")

state = {"ff": None, "mtx": None, "geom": None, "cf": None, "cf_url": None}
lock = threading.Lock()


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close()
        return ip
    except Exception:
        return "127.0.0.1"


_pubip = [None]
def public_ip():
    if _pubip[0]:
        return _pubip[0]
    for url in ("https://api.ipify.org", "https://ifconfig.me"):
        try:
            _pubip[0] = urllib.request.urlopen(url, timeout=6).read().decode().strip()
            if _pubip[0]:
                break
        except Exception:
            pass
    return _pubip[0]


def _ffmpeg_encoders():
    try:
        return subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    except Exception:
        return ""


def pick_encoder():
    """Return (name, in_args, enc_args) auto-detecting the best available H.264 encoder.
    Viewers only decode H.264, so we always emit H.264."""
    enc = _ffmpeg_encoders()
    common = ["-g", "60", "-bf", "0"]
    # NVIDIA
    if shutil.which("nvidia-smi") and "h264_nvenc" in enc:
        return ("h264_nvenc", ["-vf", "format=nv12"],
                ["-c:v", "h264_nvenc", "-preset", "p4", "-tune", "ll",
                 "-b:v", BITRATE, "-maxrate", BITRATE, "-bufsize", "6M"] + common)
    # AMD / Intel via VAAPI
    if os.path.exists("/dev/dri/renderD128") and "h264_vaapi" in enc:
        return ("h264_vaapi",
                ["-vaapi_device", "/dev/dri/renderD128", "-vf", "format=nv12,hwupload"],
                ["-c:v", "h264_vaapi", "-b:v", BITRATE, "-maxrate", BITRATE] + common)
    # Intel QSV
    if "h264_qsv" in enc:
        return ("h264_qsv", ["-vf", "format=nv12"],
                ["-c:v", "h264_qsv", "-b:v", BITRATE] + common)
    # CPU fallback
    return ("libx264", ["-vf", "format=yuv420p"],
            ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
             "-b:v", BITRATE, "-maxrate", BITRATE, "-bufsize", "6M"] + common)


def monitors():
    out = []
    try:
        xr = subprocess.run(["xrandr", "--query"], capture_output=True, text=True,
                            env={**os.environ, "DISPLAY": DISPLAY}).stdout
        for line in xr.splitlines():
            if " connected" in line:
                parts = line.split()
                geom = next((p for p in parts if re.match(r"\d+x\d+\+\d+\+\d+", p)), None)
                if geom:
                    out.append({"name": parts[0], "geom": geom, "primary": "primary" in line})
    except Exception:
        pass
    return out


def windows():
    """App windows with on-screen geometry, so 'share an app' = capture that region.
    (Reuses the x11grab pipeline; the window must stay visible/un-occluded.)"""
    out = []
    try:
        wm = subprocess.run(["wmctrl", "-lG"], capture_output=True, text=True,
                            env={**os.environ, "DISPLAY": DISPLAY}).stdout
        for line in wm.splitlines():
            f = line.split(None, 7)            # id desktop x y w h host title
            if len(f) < 8:
                continue
            desk, x, y, w, h, title = f[1], f[2], f[3], f[4], f[5], f[7]
            if desk == "-1":                   # skip desktop/panels
                continue
            if title.startswith("Desktop @") or title in ("Plasma", "Desktop") or "—" == title:
                continue
            if int(w) < 120 or int(h) < 100:
                continue
            out.append({"id": f[0], "title": title, "geom": "%sx%s+%s+%s" % (w, h, x, y)})
    except Exception:
        pass
    return out


def ensure_mediamtx():
    if state["mtx"] and state["mtx"].poll() is None:
        return
    if not os.path.exists(MEDIAMTX):
        return
    # already running externally?
    try:
        if subprocess.run(["pgrep", "-x", "mediamtx"], capture_output=True).returncode == 0:
            return
    except Exception:
        pass
    # Advertise BOTH the LAN interface IPs AND the public IP as ICE candidates: same-LAN
    # viewers use the local one, off-LAN viewers use the public one (via STUN hole-punch).
    # We do NOT drop local — forcing public-only breaks same-LAN because most home routers
    # don't NAT-hairpin (a LAN client can't reach its own public IP).
    env = dict(os.environ)
    pip = public_ip()
    if pip:
        env["MTX_WEBRTCADDITIONALHOSTS"] = pip
    state["mtx"] = subprocess.Popen([MEDIAMTX, MTX_CFG], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True, env=env)
    time.sleep(1.5)


def stop_tunnel():
    p = state.get("cf")
    if p and p.poll() is None:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM); p.wait(timeout=3)
        except Exception:
            try: os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception: pass
    state["cf"] = None; state["cf_url"] = None


def ensure_tunnel():
    # Start a Cloudflare quick tunnel exposing ONLY MediaMTX's WebRTC port, so off-LAN
    # viewers get a valid-HTTPS WHEP url (no mixed-content, no proxy). Tunnel carries the
    # tiny signaling only; media is P2P (STUN). Torn down on /stop. Random one-time URL.
    if state.get("cf") and state["cf"].poll() is None and state.get("cf_url"):
        return state["cf_url"]
    if not os.path.exists(CLOUDFLARED):
        return None
    stop_tunnel()
    p = subprocess.Popen([CLOUDFLARED, "tunnel", "--no-autoupdate", "--url",
                          "http://localhost:%d" % WHEP_PORT],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         start_new_session=True, text=True)
    state["cf"] = p

    def reader():
        try:
            for line in p.stdout:
                m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
                if m and not state.get("cf_url"):
                    state["cf_url"] = m.group(0)
        except Exception:
            pass
    threading.Thread(target=reader, daemon=True).start()
    for _ in range(60):                       # wait up to ~15s for the URL
        if state.get("cf_url"):
            break
        time.sleep(0.25)
    return state.get("cf_url")


def stop_capture():
    p = state.get("ff")
    if p and p.poll() is None:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM); p.wait(timeout=3)
        except Exception:
            try: os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception: pass
    state["ff"] = None


def start_capture(geom):
    with lock:
        stop_capture()
        ensure_mediamtx()
        # geom = WxH+X+Y -> ffmpeg -video_size WxH -i :D+X,Y
        m = re.match(r"(\d+)x(\d+)\+(\d+)\+(\d+)", geom)
        if not m:
            return False
        w, h, x, y = m.groups()
        name, in_args, enc_args = pick_encoder()
        cmd = (["ffmpeg", "-hide_banner", "-loglevel", "warning",
                "-f", "x11grab", "-draw_mouse", "1", "-framerate", FPS,
                "-video_size", "%sx%s" % (w, h), "-i", "%s+%s,%s" % (DISPLAY, x, y)]
               + in_args + enc_args
               + ["-f", "rtsp", "-rtsp_transport", "tcp", RTSP])
        state["ff"] = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, start_new_session=True,
                                       env={**os.environ, "DISPLAY": DISPLAY})
        state["geom"] = geom
        return True


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        ip = lan_ip()
        whep = "http://%s:%d/screen/whep" % (ip, WHEP_PORT)
        lan_whep = whep
        if u.path == "/sources":
            self._send({"monitors": monitors(), "windows": windows(), "encoder": pick_encoder()[0]})
        elif u.path == "/start":
            geom = q.get("geom", [None])[0]
            if not geom:
                mons = monitors(); geom = (next((m for m in mons if m["primary"]), mons[0])["geom"] if mons else "1920x1080+0+0")
            ok = start_capture(geom)
            # local=1 -> skip the tunnel (LAN-only, faster). default -> public https tunnel.
            tun = None if q.get("local", ["0"])[0] == "1" else ensure_tunnel()
            out_whep = (tun + "/screen/whep") if tun else lan_whep
            self._send({"ok": ok, "whep": out_whep, "lan_whep": lan_whep, "tunnel": bool(tun),
                        "path": "screen", "geom": geom, "encoder": pick_encoder()[0]})
        elif u.path == "/stop":
            with lock: stop_capture(); stop_tunnel()
            self._send({"ok": True})
        elif u.path == "/url":
            tun = state.get("cf_url")
            self._send({"whep": (tun + "/screen/whep") if tun else lan_whep, "lan_ip": ip, "tunnel": bool(tun)})
        elif u.path == "/ping":
            self._send({"ok": True, "running": bool(state.get("ff") and state["ff"].poll() is None)})
        else:
            self._send({"ok": False})

    def do_OPTIONS(self):
        # CORS preflight (application/sdp POST from the https Steam page triggers it)
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_POST(self):
        # VIEWER-side WHEP proxy: the plugin (secure https context) can't POST to a
        # remote http:// WHEP endpoint (mixed content), but it CAN POST to 127.0.0.1.
        # So the viewer fetches us at /whep?target=<host-whep-url> and we forward the
        # SDP offer to the host and return the SDP answer. (Host on LAN reachable; for
        # internet the target is the host's https tunnel URL.)
        u = urlparse(self.path); q = parse_qs(u.query)
        if u.path != "/whep":
            self.send_response(404); self.end_headers(); return
        target = q.get("target", [None])[0]
        if not target:
            self.send_response(400); self.end_headers(); self.wfile.write(b"missing target"); return
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            req = urllib.request.Request(target, data=body, method="POST",
                headers={"Content-Type": self.headers.get("Content-Type", "application/sdp")})
            r = urllib.request.urlopen(req, timeout=12)
            ans, ct, code = r.read(), r.headers.get("Content-Type", "application/sdp"), r.status
        except urllib.error.HTTPError as e:
            ans, ct, code = e.read(), "application/sdp", e.code
        except Exception as e:
            self.send_response(502); self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers(); self.wfile.write(str(e).encode()); return
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(ans)))
        self.end_headers(); self.wfile.write(ans)


def main():
    srv = HTTPServer(("127.0.0.1", PORT), H)
    def shutdown(*a):
        with lock: stop_capture(); stop_tunnel()
        os._exit(0)
    signal.signal(signal.SIGTERM, shutdown); signal.signal(signal.SIGINT, shutdown)
    print("rp-webrtc daemon on 127.0.0.1:%d (encoder=%s, lan=%s)" % (PORT, pick_encoder()[0], lan_ip()))
    srv.serve_forever()


main()
