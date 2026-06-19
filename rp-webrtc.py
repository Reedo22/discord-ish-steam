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
import urllib.request, urllib.error, base64, hashlib, struct
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
MEDIA_PORT = 48890           # public-facing (tunneled): WS fMP4 + WHEP proxy. NOT the control API.
DISPLAY = os.environ.get("DISPLAY", ":1")   # Linux/X11 only; ignored on Windows
BITRATE = os.environ.get("DS_BITRATE", "6M")   # default; per-share override via /start?br=
FPS = os.environ.get("DS_FPS", "30")
MAX_H = os.environ.get("DS_MAX_H", "").strip()  # encode height cap; "" = native. /start?h= overrides live.
AUDIO = os.environ.get("DS_AUDIO", "1") != "0"  # capture desktop audio (loopback). /start?audio= overrides.
# Tunnel mode: "auto" = pre-warm a cloudflared tunnel at boot + keep it warm (fast first
# share, works off-LAN). "off" = never tunnel (LAN-only; fastest, no public endpoint).
TUNNEL = os.environ.get("DS_TUNNEL", "auto").lower()

state = {"ff": None, "mtx": None, "geom": None, "cf": None, "cf_url": None, "fmp4": None}
lock = threading.Lock()

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
        clients = list(ws_clients)
    dead = []
    for c in clients:
        try:
            c.sendall(frame)
        except Exception:
            dead.append(c)
    if dead:
        with ws_lock:
            for c in dead:
                ws_clients.discard(c)
        for c in dead:
            try:
                c.close()
            except Exception:
                pass


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


# H.264 encoder selection. We PROBE rather than guess: an encoder can be PRESENT but broken
# (e.g. an nvenc build newer than the installed driver, which errors at open). So we run a
# 1-frame test encode per candidate and use the first that actually works. Priority covers
# NVIDIA (nvenc), Intel (qsv), AMD (amf; vaapi also covers AMD/Intel on Linux). libx264 (CPU)
# is the guaranteed fallback. The choice is cached for the daemon's lifetime.
_enc_pick = {"name": None}


def _enc_in_args(name):
    """Capture/scale-side input args (the -vf chain) for a given encoder."""
    if name == "h264_vaapi":
        return ["-vaapi_device", "/dev/dri/renderD128", "-vf", "format=nv12,hwupload"]
    if name in ("h264_nvenc", "h264_qsv", "h264_amf"):
        return ["-vf", "format=nv12"]
    return ["-vf", "format=yuv420p"]   # libx264


def _enc_video_args(name):
    """Codec-specific low-latency flags (bitrate/maxrate/bufsize appended by the caller)."""
    if name == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-tune", "ull"]
    if name == "h264_amf":             # AMD (esp. Windows; vaapi covers AMD on Linux)
        return ["-c:v", "h264_amf", "-usage", "lowlatency", "-rc", "cbr"]
    if name == "h264_qsv":             # Intel Quick Sync
        return ["-c:v", "h264_qsv", "-preset", "veryfast"]
    if name == "h264_vaapi":           # AMD/Intel on Linux
        return ["-c:v", "h264_vaapi"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency"]


def _probe_encoder(name):
    """1-frame test encode — True iff this encoder actually works on this machine/driver."""
    try:
        cmd = (["ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=black:s=256x144:r=30"]
               + _enc_in_args(name) + ["-frames:v", "1", "-c:v", name, "-f", "null", "-"])
        return subprocess.run(cmd, capture_output=True, timeout=20).returncode == 0
    except Exception:
        return False


def _working_encoder():
    """First hardware H.264 encoder that PROBES OK, else libx264. Cached per daemon run."""
    if _enc_pick["name"]:
        return _enc_pick["name"]
    avail = _ffmpeg_encoders()
    order = []
    if shutil.which("nvidia-smi"):
        order.append("h264_nvenc")
    order += ["h264_qsv", "h264_amf"]          # Intel QSV, AMD AMF (Windows-friendly)
    if os.path.exists("/dev/dri/renderD128"):
        order.append("h264_vaapi")             # AMD/Intel on Linux
    for name in order:
        if name in avail and _probe_encoder(name):
            _enc_pick["name"] = name
            sys.stderr.write("[enc] using %s\n" % name)
            return name
    _enc_pick["name"] = "libx264"
    sys.stderr.write("[enc] no working hardware encoder; using libx264 (CPU)\n")
    return "libx264"


def pick_encoder():
    """Return (name, in_args, enc_args) for the RTSP/WHEP (WebRTC) capture. H.264 always.
    1s GOP (fast share startup), no B-frames (low latency)."""
    name = _working_encoder()
    common = ["-g", str(int(FPS)), "-bf", "0"]
    rate = ["-b:v", BITRATE, "-maxrate", BITRATE, "-bufsize", "6M"]
    return (name, _enc_in_args(name), _enc_video_args(name) + rate + common)


def _apply_scale(in_args):
    """ALWAYS inject a normalizing CPU scale into the -vf chain: force EVEN width+height and
    cap the height. H.264/NVENC reject odd dimensions and just output BLACK (e.g. a 1599x899
    window, or an odd monitor mode), so we round both dims down to even. If DS_MAX_H / `h=` is
    set we also cap to that height (downscale only — never upscale a smaller source like 900p);
    unset = a huge cap, i.e. native size but still evened."""
    h = MAX_H if MAX_H.isdigit() else "100000"
    sc = "scale=-2:2*trunc(min(ih\\,%s)/2)" % h
    out = list(in_args)
    if "-vf" in out:
        i = out.index("-vf"); out[i + 1] = sc + "," + out[i + 1]
    else:
        out = ["-vf", sc] + out
    return out


# fragmented-MP4 output muxer args (appended AFTER video[+audio] codec args).
FMP4_OUT = ["-movflags", "+frag_keyframe+empty_moov+default_base_moof",
            "-frag_duration", "100000", "-f", "mp4", "pipe:1"]


def _fmp4_enc_args():
    """Video input + video-codec args for MSE fragmented-MP4: High@5.2 (covers up to 4K so the
    browser codec string is fixed), no B-frames, 1 s GOP. Returns (in_args, video_enc_args);
    the caller appends audio codec + FMP4_OUT. Same probed encoder as WebRTC."""
    name = _working_encoder()
    in_args = _apply_scale(_enc_in_args(name))
    common = ["-profile:v", "high", "-level", "5.2", "-bf", "0", "-g", str(int(FPS)),
              "-b:v", BITRATE, "-maxrate", BITRATE, "-bufsize", "2M"]
    return in_args, _enc_video_args(name) + common


def _pulse_monitor():
    """Linux: the monitor source of the default output sink (native desktop-audio loopback —
    NOT a virtual device). Returns the source name, or None."""
    try:
        sink = subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True, timeout=3).stdout.strip()
        if sink:
            return sink + ".monitor"
    except Exception:
        pass
    try:
        out = subprocess.run(["pactl", "list", "short", "sources"], capture_output=True, text=True, timeout=3).stdout
        for line in out.splitlines():
            p = line.split()
            if len(p) >= 2 and p[1].endswith(".monitor"):
                return p[1]
    except Exception:
        pass
    return None


def _audio_input():
    """ffmpeg input args for desktop audio, with NO virtual device. Returns (args, win_pcm)
    where win_pcm=True means a WASAPI-loopback thread must feed PCM to the encoder's stdin.
    Falls back to [] (video-only) if audio is off/unavailable — never breaks the video."""
    if not AUDIO:
        return [], False
    if IS_WIN:
        dev = _wasapi_device()
        if not dev:
            sys.stderr.write("[audio] no WASAPI loopback device (pyaudiowpatch missing?) -> video only\n")
            return [], False
        # ffmpeg MUST be told the loopback's NATIVE rate/channels — the feeder opens the device
        # at those and a mismatch garbles (or, if we forced 48k/2ch on a 44.1k/6ch device,
        # p.open() throws and we'd get permanent silence).
        return (["-f", "s16le", "-ar", str(dev["rate"]), "-ac", str(dev["ch"]),
                 "-thread_queue_size", "1024", "-i", "pipe:0"], True)
    mon = _pulse_monitor()
    if not mon:
        sys.stderr.write("[audio] no pulse monitor found -> video only\n")
        return [], False
    return (["-f", "pulse", "-thread_queue_size", "1024", "-i", mon], False)


def _wasapi_device():
    """Windows: resolve the loopback device for the default output sink and its NATIVE format.
    Returns {"index","rate","ch"} or None. The rate/channels here MUST match what ffmpeg is
    told (see _audio_input) — WASAPI loopback only opens at the device's own rate/channels."""
    try:
        import pyaudiowpatch as pa
    except Exception:
        return None
    try:
        p = pa.PyAudio()
        loop = None
        try:
            wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
            dflt = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
            for d in p.get_loopback_device_info_generator():
                if dflt["name"] in d["name"]:
                    loop = d
                    break
        except Exception:
            pass
        if loop is None:
            for d in p.get_loopback_device_info_generator():
                loop = d
                break
        if loop is None:
            p.terminate(); return None
        info = {"index": int(loop["index"]),
                "rate": int(round(loop.get("defaultSampleRate", 48000) or 48000)),
                "ch": int(loop.get("maxInputChannels", 2) or 2)}
        p.terminate()
        return info
    except Exception as e:
        sys.stderr.write("[audio] WASAPI device probe failed (%s)\n" % e)
        return None


def _wasapi_loopback_feed(proc):
    """Windows: capture the default output's WASAPI loopback at its NATIVE rate/channels and
    pipe s16 PCM to the encoder's stdin. On any error, write silence so ffmpeg's pipe:0 input
    never stalls (a stalled audio input would freeze the whole stream)."""
    dev = _wasapi_device()
    rate = dev["rate"] if dev else 48000
    ch = dev["ch"] if dev else 2
    frames = 1024
    silence = b"\x00" * (frames * ch * 2)   # s16 = 2 bytes/sample
    try:
        import pyaudiowpatch as pa
        p = pa.PyAudio()
        stream = p.open(format=pa.paInt16, channels=ch, rate=rate, frames_per_buffer=frames,
                        input=True, input_device_index=(dev["index"] if dev else None))
        sys.stderr.write("[audio] WASAPI loopback @ %dHz %dch\n" % (rate, ch))
        while proc.poll() is None:
            try:
                data = stream.read(frames, exception_on_overflow=False)
            except Exception:
                data = silence
            try:
                proc.stdin.write(data)
            except Exception:
                break
    except Exception as e:
        sys.stderr.write("[audio] WASAPI loopback failed (%s); feeding silence\n" % e)
        while proc.poll() is None:
            try:
                proc.stdin.write(silence)
                time.sleep(0.02)
            except Exception:
                break


def start_fmp4(geom=None, win=None, cam=None):
    """Start the fragmented-MP4 encode for the WS path and pump it to ws_broadcast.
    Runs alongside the RTSP/WHEP capture so the viewer can use either transport.
    Muxes desktop audio (AAC) when available — Linux pulse monitor / Windows WASAPI loopback."""
    in_args, venc = _fmp4_enc_args()
    env = _cap_env()
    if cam:
        cap_in = _camera_cap_in(cam)
    elif IS_WIN and win:
        cap_in = ["-f", "gdigrab", "-draw_mouse", "1", "-framerate", FPS, "-i", "title=%s" % win]
    elif win and not IS_WIN:
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
    audio_in, win_pcm = _audio_input()
    state["audio"] = bool(audio_in)   # actual audio state (may be False even if AUDIO on)
    # with a 2nd (audio) input, map explicitly and encode AAC; "-async 1" nudges A/V sync.
    aud_out = (["-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac", "-b:a", "128k", "-async", "1"]
               if audio_in else [])
    cmd = (["ffmpeg", "-hide_banner", "-loglevel", "warning"]
           + cap_in + audio_in + in_args + venc + aud_out + FMP4_OUT)
    with ws_lock:
        fmp4_init["seg"] = None
    # Route ffmpeg's stderr to a log file (not DEVNULL) so a black-screen failure is
    # actually diagnosable — otherwise the encoder fails silently and we're blind.
    logp = os.path.join(tempfile.gettempdir(), "rp-fmp4.log")
    try:
        errf = open(logp, "wb")
        errf.write(("[cmd] " + " ".join(cmd) + "\n").encode()); errf.flush()
    except Exception:
        errf = subprocess.DEVNULL
    p = _spawn(cmd, stdout=subprocess.PIPE, stderr=errf,
               stdin=(subprocess.PIPE if win_pcm else subprocess.DEVNULL), env=env)
    state["fmp4"] = p
    if win_pcm:
        threading.Thread(target=_wasapi_loopback_feed, args=(p,), daemon=True).start()
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


def cameras():
    """Webcams available for camera-share. Linux=v4l2 (/dev/video*), Windows=dshow video
    devices. De-duped by name (Linux exposes metadata nodes at higher indices with the same
    name; keep the lowest = the capture node)."""
    out = []
    if IS_WIN:
        try:
            r = subprocess.run(["ffmpeg", "-hide_banner", "-f", "dshow",
                                "-list_devices", "true", "-i", "dummy"],
                               capture_output=True, text=True, timeout=8)
            for line in r.stderr.splitlines():
                m = re.search(r'"([^"]+)"\s*\((video)\)', line)
                if m:
                    out.append({"id": m.group(1), "name": m.group(1)})
        except Exception:
            pass
        return out
    try:
        import glob
        seen = set()
        for dev in sorted(glob.glob("/dev/video*"),
                          key=lambda d: int(re.sub(r"\D", "", d) or 0)):
            name = dev
            n = re.sub(r"\D", "", dev)
            try:
                with open("/sys/class/video4linux/video%s/name" % n) as f:
                    name = f.read().strip()
            except Exception:
                pass
            if name in seen:
                continue
            seen.add(name)
            out.append({"id": dev, "name": name})
    except Exception:
        pass
    return out


def _camera_cap_in(cam):
    """ffmpeg input args to capture a webcam. We DON'T force input size/fps — webcams only
    offer specific modes and forcing an unsupported one errors out; let it negotiate native,
    then _apply_scale evens/caps the output. Linux=v4l2 (/dev/videoN), Windows=dshow."""
    if IS_WIN:
        return ["-f", "dshow", "-i", "video=%s" % cam]
    return ["-f", "v4l2", "-i", cam]


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
                "http://localhost:%d" % MEDIA_PORT],
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
    stop_fmp4()


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


def start_capture(geom=None, win=None, cam=None):
    with lock:
        stop_capture()
        ensure_mediamtx()
        start_fmp4(geom=geom, win=win, cam=cam)
        env = _cap_env()
        if cam:
            # webcam -> RTSP/WHEP path. Same ffmpeg encode as a monitor, camera input.
            name, in_args, enc_args = pick_encoder()
            cmd = (["ffmpeg", "-hide_banner", "-loglevel", "warning"]
                   + _camera_cap_in(cam) + _apply_scale(in_args) + enc_args
                   + ["-f", "rtsp", "-rtsp_transport", "tcp", RTSP])
            state["ff"] = _spawn(cmd, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, env=env)
            state["geom"] = "cam:" + cam
            return True
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
            self._send({"monitors": monitors(), "windows": windows(),
                        "cameras": cameras(), "encoder": pick_encoder()[0]})
        elif u.path == "/start":
            win = q.get("win", [None])[0]
            geom = q.get("geom", [None])[0]
            cam = q.get("cam", [None])[0]
            if not win and not geom and not cam:
                mons = monitors(); geom = (next((m for m in mons if m["primary"]), mons[0])["geom"] if mons else "1920x1080+0+0")
            # live encode overrides (UI dropdowns) — set before (re)starting the capture.
            global BITRATE, MAX_H, FPS, AUDIO
            br = (q.get("br", [None])[0] or "").strip()
            if re.match(r"^\d+(\.\d+)?[MmKk]?$", br):
                BITRATE = br
            hh = (q.get("h", [None])[0] or "").strip().lower()
            if hh.isdigit():
                MAX_H = hh
            elif hh in ("auto", "native", "0"):
                MAX_H = ""
            fps = (q.get("fps", [None])[0] or "").strip()
            if fps.isdigit() and 1 <= int(fps) <= 60:
                FPS = fps
            au = (q.get("audio", [None])[0] or "").strip().lower()
            if au in ("0", "false", "off"):
                AUDIO = False
            elif au in ("1", "true", "on"):
                AUDIO = True
            ok = start_capture(geom=geom, win=win, cam=cam)
            # local=1 -> skip the tunnel (LAN-only, faster). default -> public https tunnel.
            tun = None if q.get("local", ["0"])[0] == "1" else ensure_tunnel()
            ws_base = (tun.replace("https://", "wss://") if tun
                       else "ws://%s:%d" % (ip, MEDIA_PORT))
            out_ws = ws_base + "/screen/ws"
            out_whep = (tun + "/screen/whep") if tun else ("http://%s:%d/screen/whep" % (ip, MEDIA_PORT))
            self._send({"ok": ok, "whep": out_whep, "ws": out_ws, "lan_whep": lan_whep,
                        "tunnel": bool(tun), "path": "screen", "geom": geom,
                        "encoder": pick_encoder()[0], "ice": turn_ice(),
                        "h": MAX_H or "auto", "br": BITRATE, "fps": FPS,
                        "audio": bool(state.get("audio"))})
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


from http.server import ThreadingHTTPServer


class MediaH(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _proxy_whep(self):
        if not self.path.startswith("/screen/whep") or ".." in self.path:
            self.send_response(404); self.end_headers(); return
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
        if self.path.startswith("/screen/whep"):
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
            sock.settimeout(15)
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
                while True:
                    try:
                        if not sock.recv(4096):
                            break
                    except socket.timeout:
                        continue
                    except Exception:
                        break
            finally:
                with ws_lock:
                    ws_clients.discard(sock)
            return
        if self.path.startswith("/screen/whep"):
            self._proxy_whep(); return
        self.send_response(404); self.end_headers()


def start_media_server():
    srv = ThreadingHTTPServer(("0.0.0.0", MEDIA_PORT), MediaH)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


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
    start_media_server()
    srv.serve_forever()


main()
