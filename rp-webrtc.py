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
import os, sys, json, socket, subprocess, threading, signal, shutil, time, re, tempfile
import urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

IS_WIN = os.name == "nt"
EXE = ".exe" if IS_WIN else ""

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(HERE, "bin")
MEDIAMTX = os.path.join(BIN, "mediamtx" + EXE)
MTX_CFG = os.path.join(BIN, "rp-mediamtx.yml")
CLOUDFLARED = os.path.join(BIN, "cloudflared" + EXE)
PORT = 48592                 # control API (was 48591 for the old capture server)
RTSP = "rtsp://127.0.0.1:8554/screen"
WHEP_PORT = 8889
DISPLAY = os.environ.get("DISPLAY", ":1")   # Linux/X11 only; ignored on Windows
BITRATE = os.environ.get("DS_BITRATE", "12M")
FPS = os.environ.get("DS_FPS", "30")
# Tunnel mode: "auto" = pre-warm a cloudflared tunnel at boot + keep it warm (fast first
# share, works off-LAN). "off" = never tunnel (LAN-only; fastest, no public endpoint).
TUNNEL = os.environ.get("DS_TUNNEL", "auto").lower()

state = {"ff": None, "mtx": None, "geom": None, "cf": None, "cf_url": None}
lock = threading.Lock()


# --- Cloudflare TURN (NAT traversal for off-LAN viewers) -----------------------------
# Direct P2P with STUN alone fails on symmetric/CGNAT, so media never reaches a remote
# viewer (a connected-but-black tile). A TURN relay fixes it: when no direct path works,
# both ends relay the (DTLS-encrypted) media through Cloudflare. We mint SHORT-LIVED ICE
# credentials from a Cloudflare "TURN key" (free, ~1TB/mo); the relay can't decrypt the
# media. The key lives in a LOCAL secrets file or env vars and is never committed:
#   ~/.config/discordish/turn.json  ->  {"key_id": "...", "token": "..."}
#   or env:  DS_CF_TURN_KEY_ID, DS_CF_TURN_TOKEN
def _turn_secret():
    kid = os.environ.get("DS_CF_TURN_KEY_ID")
    tok = os.environ.get("DS_CF_TURN_TOKEN")
    if kid and tok:
        return kid, tok
    try:
        with open(os.path.join(os.path.expanduser("~"), ".config", "discordish", "turn.json")) as f:
            d = json.load(f)
        return d.get("key_id"), d.get("token")
    except Exception:
        return None, None


_turn = {"ice": None, "exp": 0}   # cached ICE servers + epoch expiry

# ICE servers when NO personal Cloudflare key is configured. STUN alone lets DIRECT P2P
# work whenever at least one peer has a friendly NAT; it does NOT relay, so a hard
# (symmetric/CGNAT) NAT on both ends still fails off-LAN. Reliable *anonymous* free TURN
# no longer exists (relaying costs bandwidth, so providers gate it behind a key), so we
# don't ship a relay by default. To plug one in, set DS_PUBLIC_TURN to a JSON array of
# RTCIceServer objects, e.g.:
#   DS_PUBLIC_TURN='[{"urls":["turn:my.relay:3478"],"username":"u","credential":"p"}]'
# For a robust no-config off-LAN path without any key, use the LL-HLS-over-tunnel mode.
def _public_ice():
    raw = os.environ.get("DS_PUBLIC_TURN")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            sys.stderr.write("[turn] bad DS_PUBLIC_TURN JSON; using STUN-only\n")
    return [{"urls": "stun:stun.l.google.com:19302"}]


def turn_ice(ttl=86400):
    """ICE servers (STUN + Cloudflare TURN) as RTCIceServer dicts, cached until ~10 min
    before expiry. Falls back to plain STUN if no key is set or minting fails (LAN still
    works; a remote viewer behind a hard NAT may not)."""
    now = time.time()
    if _turn["ice"] and now < _turn["exp"] - 600:
        return _turn["ice"]
    kid, tok = _turn_secret()
    if not kid or not tok:
        pub = _public_ice()
        _turn["ice"] = pub; _turn["exp"] = now + ttl   # no personal key -> free public relay
        return pub
    try:
        url = "https://rtc.live.cloudflare.com/v1/turn/keys/%s/credentials/generate" % kid
        req = urllib.request.Request(
            url, data=json.dumps({"ttl": ttl}).encode(),
            headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            j = json.load(r)
        ice = j.get("iceServers")
        servers = ice if isinstance(ice, list) else [ice]   # CF returns one object; normalize
        _turn["ice"] = servers; _turn["exp"] = now + ttl
        sys.stderr.write("[turn] minted Cloudflare ICE credentials (ttl %ds)\n" % ttl)
        return servers
    except Exception as e:
        sys.stderr.write("[turn] mint failed (%s); falling back to public relay\n" % e)
        pub = _public_ice()
        _turn["ice"] = pub; _turn["exp"] = now + 300   # retry the key sooner
        return pub


def _mtx_config_path():
    """Write a MediaMTX config = the template with its webrtcICEServers2 block replaced by
    the current ICE servers (so MediaMTX gathers TURN relay candidates too). Returns the
    path MediaMTX should load; falls back to the static template on any error."""
    try:
        with open(MTX_CFG) as f:
            base = f.read()
    except Exception:
        return MTX_CFG
    lines = ["webrtcICEServers2:"]
    for s in turn_ice():
        if not isinstance(s, dict):
            continue
        urls = s.get("urls")
        if isinstance(urls, str):
            urls = [urls]
        for u in (urls or []):
            lines.append("  - url: %s" % u)
            if s.get("username") is not None:
                lines.append("    username: %s" % json.dumps(s.get("username")))
                lines.append("    password: %s" % json.dumps(s.get("credential") or ""))
    # drop the template's static webrtcICEServers2 block, then append the generated one
    base = re.sub(r"(?ms)^webrtcICEServers2:.*?(?=^\S|\Z)", "", base)
    gen = base.rstrip() + "\n\n" + "\n".join(lines) + "\n"
    try:
        out = os.path.join(tempfile.gettempdir(), "rp-mediamtx-gen.yml")
        with open(out, "w") as f:
            f.write(gen)
        return out
    except Exception:
        return MTX_CFG


# --- cross-platform process helpers --------------------------------------------------
# Linux: start each child in its own session so we can kill the whole pipeline group.
# Windows: new process group; terminate() (+ taskkill for any children) tears it down.
def _spawn(cmd, **kw):
    if IS_WIN:
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000  # +NO_WINDOW
    else:
        kw["start_new_session"] = True
    return subprocess.Popen(cmd, **kw)


def _kill(p):
    if not p or p.poll() is not None:
        return
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        p.wait(timeout=3)
    except Exception:
        try:
            if IS_WIN:
                p.kill()
            else:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except Exception:
            pass


def _cap_env():
    """Env for capture children. Linux pins DISPLAY; Windows uses the desktop session."""
    if IS_WIN:
        return dict(os.environ)
    return {**os.environ, "DISPLAY": DISPLAY}


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


def _kbps():
    b = BITRATE.strip().lower()
    if b.endswith("m"): return int(float(b[:-1]) * 1000)
    if b.endswith("k"): return int(float(b[:-1]))
    try: return int(int(b) / 1000)
    except Exception: return 12000


def pick_gst_encoder():
    """Encoder fragment for gstreamer window capture (occlusion-proof, via ximagesrc xid).
    Mirrors pick_encoder() priority but in GStreamer element syntax."""
    try:
        insp = subprocess.run(["gst-inspect-1.0"], capture_output=True, text=True).stdout
    except Exception:
        insp = ""
    kb = _kbps()
    if "nvh264enc" in insp:
        return ("nvh264enc", "videoconvert ! video/x-raw,format=NV12 ! "
                "nvh264enc preset=low-latency-hq bitrate=%d gop-size=30 rc-mode=cbr" % kb)
    if "vah264enc" in insp:
        return ("vah264enc", "videoconvert ! vah264enc bitrate=%d key-int-max=30" % kb)
    if "x264enc" in insp:
        return ("x264enc", "videoconvert ! video/x-raw,format=I420 ! "
                "x264enc tune=zerolatency speed-preset=veryfast bitrate=%d key-int-max=30" % kb)
    return (None, None)


def pick_encoder():
    """Return (name, in_args, enc_args) auto-detecting the best available H.264 encoder.
    Viewers only decode H.264, so we always emit H.264."""
    enc = _ffmpeg_encoders()
    # 1s keyframe interval (was 2s): WebRTC/WHEP can't show a new viewer anything until
    # the next keyframe, so a shorter GOP = faster share startup. -bf 0 = no B-frames (low latency).
    common = ["-g", str(int(FPS)), "-bf", "0"]
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


def _win_monitors():
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    out = []
    PROC = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC,
                              ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def cb(hmon, hdc, lprc, lparam):
        mi = MONITORINFO(); mi.cbSize = ctypes.sizeof(MONITORINFO)
        user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        r = mi.rcMonitor
        out.append({"name": "DISPLAY%d" % (len(out) + 1),
                    "geom": "%dx%d+%d+%d" % (r.right - r.left, r.bottom - r.top, r.left, r.top),
                    "primary": bool(mi.dwFlags & 1)})   # MONITORINFOF_PRIMARY
        return 1

    user32.EnumDisplayMonitors(0, 0, PROC(cb), 0)
    return out


def _win_windows():
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    out = []
    PROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value
        if not title:
            return True
        # skip tool windows (palettes, tray helpers)
        if user32.GetWindowLongW(hwnd, -20) & 0x00000080:   # GWL_EXSTYLE & WS_EX_TOOLWINDOW
            return True
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w, h = rect.right - rect.left, rect.bottom - rect.top
        if w < 120 or h < 100:
            return True
        # On Windows the capture handle is the title (gdigrab uses -i title=...).
        out.append({"id": title, "title": title,
                    "geom": "%dx%d+%d+%d" % (w, h, rect.left, rect.top)})
        return True

    user32.EnumWindows(PROC(cb), 0)
    return out


def monitors():
    if IS_WIN:
        try:
            return _win_monitors()
        except Exception:
            return []
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
    """App windows with on-screen geometry, so 'share an app' = capture that window.
    Linux: occlusion-proof via gst ximagesrc xid. Windows: gdigrab -i title= (the id IS
    the title) — captures the window's DC, tolerant of being partly behind others."""
    if IS_WIN:
        try:
            return _win_windows()
        except Exception:
            return []
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
        if IS_WIN:
            tl = subprocess.run(["tasklist", "/FI", "IMAGENAME eq mediamtx.exe"],
                                capture_output=True, text=True).stdout
            if "mediamtx.exe" in tl:
                return
        elif subprocess.run(["pgrep", "-x", "mediamtx"], capture_output=True).returncode == 0:
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
    state["mtx"] = _spawn([MEDIAMTX, _mtx_config_path()], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, env=env)
    # poll the RTSP port instead of a fixed 1.5s sleep — return the moment it's listening
    for _ in range(40):                       # up to ~4s
        try:
            s = socket.create_connection(("127.0.0.1", 8554), 0.1); s.close(); break
        except Exception:
            time.sleep(0.1)


def stop_tunnel():
    _kill(state.get("cf"))
    state["cf"] = None; state["cf_url"] = None


def ensure_tunnel():
    # Start a Cloudflare quick tunnel exposing ONLY MediaMTX's WebRTC port, so off-LAN
    # viewers get a valid-HTTPS WHEP url (no mixed-content, no proxy). Tunnel carries the
    # tiny signaling only; media is P2P (STUN). KEPT WARM across shares (pre-warmed at boot
    # + reused) so a share never pays the ~2-4s handshake; torn down only on daemon stop.
    if TUNNEL == "off":
        return None
    if state.get("cf") and state["cf"].poll() is None and state.get("cf_url"):
        return state["cf_url"]
    if not os.path.exists(CLOUDFLARED):
        return None
    stop_tunnel()
    p = _spawn([CLOUDFLARED, "tunnel", "--no-autoupdate", "--url",
                "http://localhost:%d" % WHEP_PORT],
               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
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
    _kill(state.get("ff"))
    state["ff"] = None


def _start_win_window(title):
    """Windows per-window capture: ffmpeg gdigrab by window title. gdigrab grabs the
    window's device context, so it keeps working when the window is partly behind
    others (it must not be minimized). GPU-encode straight to RTSP."""
    name, in_args, enc_args = pick_encoder()
    cmd = (["ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-f", "gdigrab", "-draw_mouse", "1", "-framerate", FPS,
            "-i", "title=%s" % title]
           + in_args + enc_args
           + ["-f", "rtsp", "-rtsp_transport", "tcp", RTSP])
    state["ff"] = _spawn(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         env=_cap_env())
    state["geom"] = "win:" + title
    return True


def start_capture(geom=None, win=None):
    with lock:
        stop_capture()
        ensure_mediamtx()
        env = _cap_env()
        if win:
            if IS_WIN:
                return _start_win_window(win)
            # Linux: occlusion-proof single-window capture — grab the window's own buffer
            # by XID (gstreamer ximagesrc xid), GPU-encode, pipe h264/mpegts to ffmpeg.
            gname, genc = pick_gst_encoder()
            if not genc:
                return False
            xid = win if str(win).startswith("0x") else "0x%x" % int(win)
            # nobuffer + tiny analyze window so the stream goes live in ~1-2s instead of
            # ffmpeg's default ~5s mpegts probe (the viewer connects almost immediately).
            pipe = ("gst-launch-1.0 -q ximagesrc xid=%s use-damage=false ! %s ! "
                    "h264parse ! mpegtsmux alignment=7 ! fdsink fd=1 2>/dev/null | "
                    "ffmpeg -hide_banner -loglevel warning -fflags nobuffer "
                    "-probesize 4096 -analyzeduration 200000 -i - -c copy "
                    "-f rtsp -rtsp_transport tcp %s") % (xid, genc, RTSP)
            state["ff"] = _spawn(["bash", "-c", pipe], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, env=env)
            state["geom"] = "win:" + xid
            return True
        # geom = WxH+X+Y -> monitor / region capture
        m = re.match(r"(\d+)x(\d+)\+(\d+)\+(\d+)", geom or "")
        if not m:
            return False
        w, h, x, y = m.groups()
        name, in_args, enc_args = pick_encoder()
        if IS_WIN:
            # Windows: gdigrab the desktop at the monitor's offset+size.
            cap_in = ["-f", "gdigrab", "-draw_mouse", "1", "-framerate", FPS,
                      "-offset_x", x, "-offset_y", y, "-video_size", "%sx%s" % (w, h),
                      "-i", "desktop"]
        else:
            # Linux: x11grab a monitor region -> :D+X,Y
            cap_in = ["-f", "x11grab", "-draw_mouse", "1", "-framerate", FPS,
                      "-video_size", "%sx%s" % (w, h), "-i", "%s+%s,%s" % (DISPLAY, x, y)]
        cmd = (["ffmpeg", "-hide_banner", "-loglevel", "warning"]
               + cap_in + in_args + enc_args
               + ["-f", "rtsp", "-rtsp_transport", "tcp", RTSP])
        state["ff"] = _spawn(cmd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, env=env)
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
            win = q.get("win", [None])[0]
            geom = q.get("geom", [None])[0]
            if not win and not geom:
                mons = monitors(); geom = (next((m for m in mons if m["primary"]), mons[0])["geom"] if mons else "1920x1080+0+0")
            ok = start_capture(geom=geom, win=win)
            # local=1 -> skip the tunnel (LAN-only, faster). default -> public https tunnel.
            tun = None if q.get("local", ["0"])[0] == "1" else ensure_tunnel()
            out_whep = (tun + "/screen/whep") if tun else lan_whep
            self._send({"ok": ok, "whep": out_whep, "lan_whep": lan_whep, "tunnel": bool(tun),
                        "path": "screen", "geom": geom, "encoder": pick_encoder()[0],
                        "ice": turn_ice()})
        elif u.path == "/stop":
            # stop the capture but KEEP the tunnel warm for the next share (instant restart)
            with lock: stop_capture()
            self._send({"ok": True})
        elif u.path == "/url":
            tun = state.get("cf_url")
            self._send({"whep": (tun + "/screen/whep") if tun else lan_whep, "lan_ip": ip,
                        "tunnel": bool(tun), "ice": turn_ice()})
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
    print("rp-webrtc daemon on 127.0.0.1:%d (encoder=%s, lan=%s, tunnel=%s)"
          % (PORT, pick_encoder()[0], lan_ip(), TUNNEL))
    # Pre-warm the slow bits in the background so the FIRST share is fast: MediaMTX (so it's
    # already listening) and the cloudflared tunnel (so its https URL is ready). Both are
    # otherwise built lazily on the first /start, which is what made sharing slow to begin.
    def prewarm():
        try: ensure_mediamtx()
        except Exception: pass
        if TUNNEL != "off":
            try: ensure_tunnel()
            except Exception: pass
    threading.Thread(target=prewarm, daemon=True).start()
    srv.serve_forever()


main()
