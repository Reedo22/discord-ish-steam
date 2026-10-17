#!/usr/bin/env python3
# Host-side WebRTC screen-share daemon for the discord-ish reskin.
#
# Replaces the old Steam-Remote-Play path entirely. The plugin (CEF) can't capture the
# screen (getDisplayMedia is blocked in Steam's webhelper) and can't exec, so this small
# localhost daemon does the work:
#   capture (X11: ffmpeg x11grab / gst ximagesrc; Wayland: xdg-desktop-portal ScreenCast
#   -> PipeWire -> gst pipewiresrc) -> HARDWARE encode (auto-detected:
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
import urllib.request, urllib.error, base64, hashlib, struct, collections, secrets, hmac
import xml.etree.ElementTree as ET
import ipaddress
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs, urljoin

IS_WIN = os.name == "nt"
EXE = ".exe" if IS_WIN else ""


def _console_fallback():
    """Under pythonw.exe (how the Windows logon task and the plugin's Lua backend start us,
    so no console window flashes up) sys.stdout/sys.stderr are None, and the first
    sys.stderr.write() crashed the daemon with exit 1 — found in the Windows test VM: the
    auto-start never actually stayed up. Send the log to a file instead (also the place to
    look when a friend's share misbehaves): %TEMP%\\rp-webrtc.log."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    path = os.path.join(tempfile.gettempdir(), "rp-webrtc.log")
    try:
        if os.path.getsize(path) > 5 * 1024 * 1024:
            os.replace(path, path + ".1")
    except OSError:
        pass
    try:
        f = open(path, "a", buffering=1, encoding="utf-8", errors="replace")
    except OSError:
        f = open(os.devnull, "w")
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f


_console_fallback()

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(HERE, "bin")
MEDIAMTX = os.path.join(BIN, "mediamtx" + EXE)
MTX_CFG = os.path.join(BIN, "rp-mediamtx.yml")
CLOUDFLARED = os.path.join(BIN, "cloudflared" + EXE)
# Which ffmpeg: see ffmpeg_bin(). bin/fetch-linux.sh bundles a current static build when
# the distro one is too old to carry Opus over RTMP (needs >= 7.1, which is what gives the
# WebRTC path its audio).
BUNDLED_FFMPEG = os.path.join(BIN, "ffmpeg" + EXE)
PORT = 48592                 # control API (was 48591 for the old capture server)
# Publish to MediaMTX over (enhanced) RTMP, loopback only. Not RTSP: ffmpeg < 7.1 has no
# AV1 RTP packetizer (MediaMTX answers 400), while enhanced-FLV AV1 works from ffmpeg 6.1.
RTMP_PORT = 19350
RTMP = "rtmp://127.0.0.1:%d/screen" % RTMP_PORT
PUBLISH = ["-f", "flv", "-flvflags", "no_duration_filesize", RTMP]
WHEP_PORT = 8889
MEDIA_PORT = 48890           # public-facing (tunneled): WS fMP4 + WHEP proxy. NOT the control API.
DISPLAY = os.environ.get("DISPLAY", ":1")   # Linux/X11 only; ignored on Windows


def _detect_wayland():
    """True when the desktop session is Wayland. The daemon usually runs as a systemd
    --user service whose env has NO WAYLAND_DISPLAY/XDG_SESSION_TYPE, so also look for
    the compositor's wayland-* socket in XDG_RUNTIME_DIR."""
    if IS_WIN:
        return False
    if os.environ.get("WAYLAND_DISPLAY"):
        return True
    if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
        return True
    run = os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()
    try:
        return any(f.startswith("wayland-") and not f.endswith(".lock")
                   for f in os.listdir(run))
    except Exception:
        return False


def _ensure_bus_env():
    """The portal lives on the session bus; a systemd --user service may lack the address var."""
    if IS_WIN or "DBUS_SESSION_BUS_ADDRESS" in os.environ:
        return
    bus_sock = os.path.join(os.environ.get("XDG_RUNTIME_DIR")
                            or "/run/user/%d" % os.getuid(), "bus")
    if os.path.exists(bus_sock):
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + bus_sock


_wl_seen = [False]


def is_wayland():
    """Session type, decided LAZILY (per call) rather than once at import. With linger on,
    systemd starts this daemon at boot — seconds BEFORE the compositor creates its wayland-0
    socket — so an import-time check latched X11 for the daemon's whole life and every share
    went down the x11grab path (black/XWayland-only on a Wayland desktop). Once Wayland has
    been seen it sticks; until then it's re-checked (a directory listing, cheap)."""
    if not _wl_seen[0] and _detect_wayland():
        _wl_seen[0] = True
        _ensure_bus_env()
    return _wl_seen[0]


_ensure_bus_env()
BITRATE = os.environ.get("DS_BITRATE", "6M")   # default; per-share override via /start?br=
FPS = os.environ.get("DS_FPS", "30")
MAX_H = os.environ.get("DS_MAX_H", "").strip()  # encode height cap; "" = native. /start?h= overrides live.
AUDIO = os.environ.get("DS_AUDIO", "1") != "0"  # capture desktop audio (loopback). /start?audio= overrides.
# Tunnel mode: "auto" = pre-warm a cloudflared tunnel at boot + keep it warm (fast first
# share, works off-LAN). "off" = never tunnel (LAN-only; fastest, no public endpoint).
TUNNEL = os.environ.get("DS_TUNNEL", "auto").lower()

state = {"ff": None, "mtx": None, "geom": None, "cf": None, "cf_url": None, "acodec": None,
         "wl_gst": None, "wl_ses": None, "cf_gen": 0, "audio": False, "err": None,
         "key": None, "upnp": {"ok": False, "why": "not tried yet"}}
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
fmp4_init = {"seg": None, "owner": None}   # owner = the stream that produced seg

# Per-client send queue depth, in fMP4 fragments. Fragments are ~100ms (-frag_duration
# 100000), so this is ~6s of video. A viewer that falls further behind than that is not
# coming back on its own (stalled TCP window, suspended laptop, dead tunnel), so we close
# it and let its plugin reconnect cleanly rather than keep buffering for it.
WS_QUEUE_MAX = 60


class WsClient:
    """One viewer socket with its OWN writer thread and a bounded queue.

    This exists so a slow viewer can never stall the capture. ws_broadcast used to
    sendall() to every client directly on the fmp4_pump thread; a viewer whose TCP window
    filled (congested link, cloudflared backpressure) blocked the pump for up to the
    socket timeout, which stopped draining ffmpeg's stdout, which blocked ffmpeg's write,
    which stalled the ENCODER — one bad viewer froze the share for everyone. Now the pump
    only appends to a deque and never blocks on the network.
    """

    def __init__(self, sock):
        self.sock = sock
        self.q = collections.deque()
        self.cv = threading.Condition()
        self.alive = True
        self.dropped = False
        threading.Thread(target=self._writer, daemon=True).start()

    def send(self, frame):
        """Non-blocking. Overflow closes the client instead of corrupting its stream:
        skipping fragments would leave MSE decoding a gap until the next keyframe, whereas
        a reconnect re-sends the init segment and resumes at a fragment boundary."""
        with self.cv:
            if not self.alive:
                return
            if len(self.q) >= WS_QUEUE_MAX:
                self.alive = False
                self.dropped = True
                self.q.clear()
                self.cv.notify()
                return
            self.q.append(frame)
            self.cv.notify()

    def _writer(self):
        while True:
            with self.cv:
                while self.alive and not self.q:
                    self.cv.wait()
                if not self.alive:
                    break
                frame = self.q.popleft()
            try:
                self.sock.sendall(frame)
            except Exception:
                break
        if self.dropped:
            sys.stderr.write("[ws] viewer fell >%d frags behind; dropped (it will reconnect)\n"
                             % WS_QUEUE_MAX)
        self.close()

    def close(self):
        with self.cv:
            self.alive = False
            self.q.clear()
            self.cv.notify()
        # shutdown() unblocks the handler thread's recv() so it can deregister us
        for fn in (lambda: self.sock.shutdown(socket.SHUT_RDWR), self.sock.close):
            try:
                fn()
            except Exception:
                pass


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
    """Hand one fMP4 fragment to every viewer. Never blocks: each WsClient owns its socket
    and drains its own queue, so the pump keeps reading ffmpeg no matter how slow a viewer
    is. (See WsClient — this used to sendall() inline and stall the encoder.)"""
    frame = _ws_frame(data)
    with ws_lock:
        clients = list(ws_clients)
    for c in clients:
        c.send(frame)
    dead = [c for c in clients if not c.alive]
    if dead:
        with ws_lock:
            for c in dead:
                ws_clients.discard(c)


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
    single WebSocket message so MSE sees clean fragment boundaries.

    When the stream ends (encoder exited or was killed) the init segment it published is
    withdrawn and its viewers are closed, so /ping stops reporting `streaming` for a dead
    encoder and viewers reconnect instead of sitting on a silent socket. A newer capture's
    init segment is left alone (identity check), since a restart kills the old encoder
    after the new pump may already have published."""
    try:
        _pump_boxes(stream)
    finally:
        with ws_lock:
            mine = fmp4_init["seg"] is not None and fmp4_init.get("owner") is stream
            if mine:
                fmp4_init["seg"] = None
                fmp4_init["owner"] = None
                clients = list(ws_clients)
                ws_clients.clear()
            else:
                clients = []
        for c in clients:
            c.close()


def _pump_boxes(stream):
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
            if size < 16:                  # malformed: a negative read silently desyncs us
                break
            body = _read_exact(stream, size - 16)
            box = header + ext + (body or b"")
        else:
            # size 0 ("box runs to EOF") and size < 8 are both invalid for a live fMP4
            # stream. Without this guard _read_exact() gets a negative n, returns b"",
            # and the parser walks off the box grid for the rest of the share.
            if size < 8:
                sys.stderr.write("[fmp4] bad box size %d (%r); resyncing stream\n"
                                 % (size, btype))
                break
            body = _read_exact(stream, size - 8)
            box = header + (body or b"")
        if body is None:
            break
        if btype in (b"ftyp", b"moov"):
            init += box
            if btype == b"moov":
                with ws_lock:
                    fmp4_init["seg"] = init
                    fmp4_init["owner"] = stream
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


def _mtx_config_path(drop=()):
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
    for key in drop:                   # keys an older MediaMTX doesn't know (see ensure_mediamtx)
        base = re.sub(r"(?m)^%s:.*\n?" % re.escape(key), "", base)
    gen = base.rstrip() + "\n\n" + "\n".join(lines) + "\n"
    try:
        out = os.path.join(tempfile.gettempdir(), "rp-mediamtx-gen.yml")
        with open(out, "w") as f:
            f.write(gen)
        return out
    except Exception:
        return MTX_CFG


# --- how viewers reach us ------------------------------------------------------------
# Media goes DIRECT whenever possible; Cloudflare is only a fallback (its terms forbid
# serving video through the CDN on non-Enterprise plans, and quick tunnels have no SLA):
#  * WebRTC media is UDP straight to WEBRTC_UDP on our public IP, which upnp_keep()
#    forwards on the router. The tunnel then carries only the tiny WHEP/SDP exchange.
#  * PUBLIC_URL (optional): an https origin that reverse-proxies to MEDIA_PORT — e.g. your
#    own domain through nginx with a real certificate. When set, signaling AND the WS/fMP4
#    fallback go there instead of a quick tunnel, so no video ever crosses Cloudflare.
#      ~/.config/discordish/public.json -> {"url": "https://share.example.com"}
#      or env DS_PUBLIC_URL
WEBRTC_UDP = 8189              # MediaMTX webrtcLocalUDPAddress (bin/rp-mediamtx.yml)
UPNP = os.environ.get("DS_UPNP", "1") != "0"


def public_url():
    u = os.environ.get("DS_PUBLIC_URL")
    if not u:
        try:
            with open(os.path.join(os.path.expanduser("~"), ".config", "discordish",
                                   "public.json")) as f:
                u = json.load(f).get("url")
        except Exception:
            u = None
    u = (u or "").strip().rstrip("/")
    return u if re.match(r"^https://[A-Za-z0-9.-]+(:\d+)?$", u) else None


def lan_whep(ip):
    """The share's WHEP endpoint on the LAN. Signaled alongside the public one because many
    home routers don't hairpin: a viewer in the SAME house can't reach the host's own public
    IP/domain, but can always reach this (WebRTC media then stays on the LAN)."""
    return "http://%s:%d/screen/whep?k=%s" % (ip, MEDIA_PORT, state.get("key") or "")


def share_urls(ip, tunnel=None):
    """(whep, ws, via) for the current share, with its access key. Priority: own https
    origin > quick tunnel > LAN address."""
    k = "?k=" + (state.get("key") or "")
    base = public_url()
    via = "public" if base else ("tunnel" if tunnel else "lan")
    base = base or tunnel
    if base:
        return base + "/screen/whep" + k, base.replace("https://", "wss://", 1) + "/screen/ws" + k, via
    return ("http://%s:%d/screen/whep%s" % (ip, MEDIA_PORT, k),
            "ws://%s:%d/screen/ws%s" % (ip, MEDIA_PORT, k), via)


# --- UPnP: forward our own WebRTC UDP port --------------------------------------------
# Without this, MediaMTX advertises <public-ip>:8189 but the router drops the packets, ICE
# fails for every off-LAN viewer and the share falls back to fMP4 THROUGH the tunnel.
# Routers in UPnP "secure mode" (e.g. TP-Link Deco) only accept mappings to the requesting
# host itself — which is exactly what this does — and may report success for a mapping they
# then drop, so every add is read back. Leases are renewed; mappings are removed on exit.
_UPNP_ST = ("urn:schemas-upnp-org:device:InternetGatewayDevice:1",
            "urn:schemas-upnp-org:service:WANIPConnection:1",
            "urn:schemas-upnp-org:service:WANPPPConnection:1")
_UPNP_WAN = ("urn:schemas-upnp-org:service:WANIPConnection:1",
             "urn:schemas-upnp-org:service:WANPPPConnection:1")
UPNP_LEASE = 3600


class UPnPError(Exception):
    def __init__(self, code, raw=""):
        super().__init__("UPnP error %s" % code)
        self.code = code


def _upnp_discover(timeout=3):
    """SSDP M-SEARCH -> the IGD's description URL (its path is randomised per boot)."""
    for st in _UPNP_ST:
        msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
               'MAN: "ssdp:discover"\r\nMX: 2\r\nST: %s\r\n\r\n' % st).encode()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            s.sendto(msg, ("239.255.255.250", 1900))
            while True:
                data, _ = s.recvfrom(65507)
                m = re.search(rb"(?im)^location:\s*(\S+)", data)
                if m:
                    return m.group(1).decode()
        except (socket.timeout, OSError):
            pass
        finally:
            s.close()
    return None


def _upnp_control(desc_url):
    ns = "{urn:schemas-upnp-org:device-1-0}"
    with urllib.request.urlopen(desc_url, timeout=6) as r:
        root = ET.fromstring(r.read())
    for svc in root.iter(ns + "service"):
        st = svc.findtext(ns + "serviceType") or ""
        if st in _UPNP_WAN:
            return urljoin(desc_url, svc.findtext(ns + "controlURL") or ""), st
    raise UPnPError("no-wan-service")


def _upnp_soap(ctl, st, action, args=()):
    body = "".join("<%s>%s</%s>" % (k, v, k) for k, v in args)
    env = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
           ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
           '<u:%s xmlns:u="%s">%s</u:%s></s:Body></s:Envelope>' % (action, st, body, action))
    req = urllib.request.Request(ctl, data=env.encode(), headers={
        "Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": '"%s#%s"' % (st, action)})
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
        m = re.search(rb"<errorCode>(\d+)</errorCode>", raw)
        raise UPnPError(m.group(1).decode() if m else str(e.code))
    out = {}
    for el in ET.fromstring(raw).iter():
        tag = el.tag.split("}")[-1]
        if tag.startswith("New"):
            out[tag] = el.text
    return out


def _private_ip(ip):
    try:
        a, b = [int(x) for x in ip.split(".")[:2]]
    except Exception:
        return True
    return a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168) \
        or (a == 100 and 64 <= b <= 127) or a == 127


def upnp_map(port=WEBRTC_UDP, proto="UDP"):
    """Forward <proto>/<port> on the router to this machine. Returns a status dict:
    {"ok", "ext_ip", "why"} — never raises."""
    try:
        desc = _upnp_discover()
        if not desc:
            return {"ok": False, "why": "no UPnP router found (UPnP off?)"}
        ctl, st = _upnp_control(desc)
        me = lan_ip()
        ext = None
        try:
            ext = _upnp_soap(ctl, st, "GetExternalIPAddress").get("NewExternalIPAddress")
        except Exception:
            pass
        if ext and _private_ip(ext):
            # double NAT / carrier-grade NAT: a mapping here can't make us reachable
            return {"ok": False, "ext_ip": ext,
                    "why": "router's WAN address %s is private (double NAT / CGNAT)" % ext}
        args = [("NewRemoteHost", ""), ("NewExternalPort", port), ("NewProtocol", proto),
                ("NewInternalPort", port), ("NewInternalClient", me), ("NewEnabled", 1),
                ("NewPortMappingDescription", "discord-ish screen share"),
                ("NewLeaseDuration", UPNP_LEASE)]
        try:
            _upnp_soap(ctl, st, "AddPortMapping", args)
        except UPnPError as e:
            if e.code != "725":                 # 725 = router only supports permanent leases
                raise
            args[-1] = ("NewLeaseDuration", 0)
            _upnp_soap(ctl, st, "AddPortMapping", args)
        got = _upnp_soap(ctl, st, "GetSpecificPortMappingEntry",
                         [("NewRemoteHost", ""), ("NewExternalPort", port), ("NewProtocol", proto)])
        if got.get("NewInternalClient") != me:
            return {"ok": False, "ext_ip": ext,
                    "why": "router kept %s/%s pointed at %s, not us"
                           % (port, proto, got.get("NewInternalClient") or "nothing")}
        state["upnp_ctl"] = (ctl, st)
        return {"ok": True, "ext_ip": ext, "why": "forwarded %s/%s" % (port, proto)}
    except UPnPError as e:
        why = {"718": "port %d already forwarded to another device" % port,
               "606": "router refused (UPnP read-only?)"}.get(e.code, "router error %s" % e.code)
        return {"ok": False, "why": why}
    except Exception as e:
        return {"ok": False, "why": "UPnP failed (%s)" % e}


def upnp_keep():
    """Background: keep WEBRTC_UDP forwarded (renew well inside the lease)."""
    if not UPNP:
        state["upnp"] = {"ok": False, "why": "disabled (DS_UPNP=0)"}
        return
    while True:
        st = upnp_map()
        if st != state.get("upnp"):
            sys.stderr.write("[upnp] %s\n" % st["why"])
        state["upnp"] = st
        time.sleep(UPNP_LEASE // 3 if st["ok"] else 300)


def upnp_release(port=WEBRTC_UDP, proto="UDP"):
    c = state.get("upnp_ctl")
    if not c:
        return
    try:
        _upnp_soap(c[0], c[1], "DeletePortMapping",
                   [("NewRemoteHost", ""), ("NewExternalPort", port), ("NewProtocol", proto)])
    except Exception:
        pass


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


def _open_log(name, header):
    """Open a capture log (in the temp dir) for a child's stderr, so a black-screen failure
    is diagnosable instead of the encoder failing silently. Caller must _close_log() it
    once the children are spawned — they hold their own copy of the fd."""
    try:
        f = open(os.path.join(tempfile.gettempdir(), name), "wb")
        f.write(header.encode()); f.flush()
        return f
    except Exception:
        return subprocess.DEVNULL


def _close_log(f):
    if f is not subprocess.DEVNULL:
        try:
            f.close()
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


_ffbin = []


def _hw_ok(binary):
    """Does this ffmpeg have ANY working hardware H.264 encoder here?"""
    for name in _candidates("h264"):
        try:
            if subprocess.run([binary, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                               "-i", "color=c=black:s=256x144:r=30"] + _enc_in_args(name)
                              + ["-frames:v", "1", "-c:v", name, "-f", "null", "-"],
                              capture_output=True, timeout=20).returncode == 0:
                return True
        except Exception:
            pass
    return False


def ffmpeg_bin():
    """The bundled ffmpeg normally wins — but a static build can be NEWER than the GPU
    driver supports (FFmpeg 9.0's NVENC needs driver >= 610), silently dropping every share
    to CPU x264. GPU encoding beats WebRTC audio, so if the bundled build has no hardware
    encoder and the system one does, use the system one. Decided once per daemon run."""
    if not _ffbin:
        pick = "ffmpeg"
        if os.path.exists(BUNDLED_FFMPEG):
            pick = BUNDLED_FFMPEG
            if not _hw_ok(BUNDLED_FFMPEG) and shutil.which("ffmpeg") and _hw_ok("ffmpeg"):
                sys.stderr.write("[enc] bundled ffmpeg can't use the GPU encoder here; "
                                 "using the system ffmpeg (WebRTC audio needs ffmpeg >= 7.1)\n")
                pick = "ffmpeg"
        _ffbin.append(pick)
    return _ffbin[0]


def _ffmpeg_encoders():
    try:
        return subprocess.run([ffmpeg_bin(), "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    except Exception:
        return ""


# Encoder selection. We PROBE rather than guess: an encoder can be PRESENT but broken (e.g.
# an nvenc build newer than the installed driver, which errors at open). So we run a 1-frame
# test encode per candidate and use the first that actually works. Two codecs:
#   av1  — preferred: measured ~40-50% fewer bits than H.264 for the same SSIM on gameplay,
#          and sharper scrolling text (tests/bench notes in the README). Hardware only:
#          NVENC (RTX 40+), QSV (Arc), AMF (RX 7000+), VAAPI. Viewers decode it anywhere
#          (Chromium ships a software AV1 decoder; Steam's CEF is Chromium 126).
#   h264 — the fallback when the host has no AV1 encoder; libx264 (CPU) is the last resort.
# Choices are cached per codec; a CPU fallback is re-probed after ENC_RETRY_S.
_enc_pick = {}                 # codec -> {"name", "t"}
# A libx264 fallback is only cached this long before hardware is re-probed. At boot (linger
# starts us before the GPU driver/session is fully up) the NVENC probe can fail once, and a
# lifetime cache then left every share on the CPU encoder until the daemon was restarted.
ENC_RETRY_S = 120
CODEC = os.environ.get("DS_CODEC", "auto").lower()   # auto | av1 | h264; /start?codec= overrides
# MSE codec strings the viewer needs (High@5.2 / AV1 Main L5.1 8-bit: both cover 4K60).
MSE_CODEC = {"h264": "avc1.640034", "av1": "av01.0.13M.08"}


def _codec_of(name):
    return "av1" if (name or "").startswith("av1") else "h264"


def _enc_in_args(name):
    """Capture/scale-side input args (the -vf chain) for a given encoder."""
    if name.endswith("_vaapi"):
        return ["-vaapi_device", "/dev/dri/renderD128", "-vf", "format=nv12,hwupload"]
    if name.endswith(("_nvenc", "_qsv", "_amf")):
        return ["-vf", "format=nv12"]
    return ["-vf", "format=yuv420p"]   # libx264


def _enc_video_args(name):
    """Codec-specific low-latency flags (bitrate/maxrate/bufsize appended by the caller).
    NVENC: p5 + tune ll + spatial AQ, chosen by measurement (SSIM/PSNR on gameplay and
    scrolling-code clips): +0.4..0.9 dB over the old p4/ull at the same bitrate, still >2x
    realtime at 4K60. p7 measured no better than p5."""
    if name in ("h264_nvenc", "av1_nvenc"):
        return ["-c:v", name, "-preset", "p5", "-tune", "ll", "-rc", "cbr", "-spatial-aq", "1"]
    if name in ("h264_amf", "av1_amf"):    # AMD (esp. Windows; vaapi covers AMD on Linux)
        return ["-c:v", name, "-usage", "lowlatency", "-rc", "cbr"]
    if name in ("h264_qsv", "av1_qsv"):    # Intel Quick Sync / Arc
        return ["-c:v", name, "-preset", "veryfast"]
    if name.endswith("_vaapi"):            # AMD/Intel on Linux
        return ["-c:v", name]
    return ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency"]


def _profile_args(name):
    """Profile/level pinned to match MSE_CODEC (the viewer's MediaSource codec string)."""
    if _codec_of(name) == "h264":
        return ["-profile:v", "high", "-level", "5.2"]
    return []


def _probe_encoder(name):
    """1-frame test encode — True iff this encoder actually works on this machine/driver."""
    try:
        cmd = ([ffmpeg_bin(), "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=black:s=256x144:r=30"]
               + _enc_in_args(name) + ["-frames:v", "1", "-c:v", name, "-f", "null", "-"])
        return subprocess.run(cmd, capture_output=True, timeout=20).returncode == 0
    except Exception:
        return False


def _candidates(codec):
    order = []
    if shutil.which("nvidia-smi"):
        order.append(codec + "_nvenc")
    order += [codec + "_qsv", codec + "_amf"]     # Intel QSV/Arc, AMD AMF (Windows-friendly)
    if os.path.exists("/dev/dri/renderD128"):
        order.append(codec + "_vaapi")             # AMD/Intel on Linux
    return order


def _working_encoder(codec=None):
    """Working encoder for the requested codec ('auto' = AV1 when the host can encode it in
    hardware, else H.264). Hardware picks are cached for the daemon's lifetime; the libx264
    fallback is re-probed after ENC_RETRY_S. Returns an ffmpeg encoder name."""
    codec = (codec or CODEC or "auto").lower()
    if codec in ("auto", "av1"):
        av1 = _hw_encoder("av1")
        if av1:
            return av1
    return _hw_encoder("h264") or "libx264"


def _hw_encoder(codec):
    c = _enc_pick.get(codec)
    if c and (c["name"] or time.time() - c["t"] < ENC_RETRY_S):
        return c["name"]
    avail = _ffmpeg_encoders()
    for name in _candidates(codec):
        if name in avail and _probe_encoder(name):
            _enc_pick[codec] = {"name": name, "t": time.time()}
            sys.stderr.write("[enc] %s: using %s\n" % (codec, name))
            return name
    if not c:
        sys.stderr.write("[enc] %s: no working hardware encoder (re-probing in %ds)\n"
                         % (codec, ENC_RETRY_S))
    _enc_pick[codec] = {"name": None, "t": time.time()}
    return None


def pick_encoder():
    """Return (name, in_args, enc_args) for the WebRTC (MediaMTX) capture. No B-frames (low
    latency); 2 s GOP — measured +0.85 dB SSIM at 3 Mbps over 1 s (fewer keyframe-quality
    pulses), at the cost of up to 1 s extra wait for a joining viewer's first keyframe.
    Lost packets are retransmitted by MediaMTX (NACK), so the GOP isn't the loss-recovery
    path. VBV = 1 s of bitrate so keyframes aren't starved."""
    name = _working_encoder()
    common = ["-g", str(2 * int(FPS)), "-bf", "0"]
    rate = ["-b:v", BITRATE, "-maxrate", BITRATE, "-bufsize", BITRATE]
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


# --- desktop audio WITHOUT Steam -----------------------------------------------------
# Capturing the whole desktop mix also captures the CALL: the friend's voice coming out of
# Steam gets sent back to them a second later inside the share (echo), and if both people
# share, each share re-captures the other's -> a screeching feedback loop (found on a real
# two-way test). Discord excludes its own audio; we exclude Steam's (voice chat + incoming
# shares both play inside steamwebhelper). Extra process names: DS_AUDIO_EXCLUDE=a,b.
AUDIO_EXCLUDE = {"steam", "steamwebhelper", "steam.exe", "steamwebhelper.exe"} | {
    x.strip().lower() for x in os.environ.get("DS_AUDIO_EXCLUDE", "").split(",") if x.strip()}


class _PcmBuffer:
    """Drains a byte stream (pw-record stdout) on a thread; exposes the
    get_read_available()/read() pair _paced_pcm_feed expects (s16, `ch` channels)."""

    def __init__(self, stream, ch):
        self.buf = bytearray()
        self.bpf = ch * 2
        self.cv = threading.Lock()
        threading.Thread(target=self._pump, args=(stream,), daemon=True).start()

    def _pump(self, stream):
        while True:
            try:
                chunk = stream.read1(65536) if hasattr(stream, "read1") else stream.read(4096)
            except Exception:
                break
            if not chunk:
                break
            with self.cv:
                self.buf += chunk
                if len(self.buf) > 48000 * self.bpf:      # >1 s backlog: drop the oldest
                    del self.buf[:len(self.buf) - 48000 * self.bpf]

    def get_read_available(self):
        with self.cv:
            return len(self.buf) // self.bpf

    def read(self, n, exception_on_overflow=False):
        with self.cv:
            take = min(n, len(self.buf) // self.bpf) * self.bpf
            out = bytes(self.buf[:take])
            del self.buf[:take]
        return out


def pipewire_ok():
    return not IS_WIN and all(shutil.which(t) for t in ("pw-record", "pw-dump", "pw-link"))


class PipeWireAppMix:
    """Linux/PipeWire: a private recording node (node.autoconnect=false, so nothing routes
    into it by default) with every application's playback stream linked in EXCEPT Steam's.
    Apps keep playing to the speakers as usual — links only add a second destination; no
    virtual device, no rerouting. Rescans every second for apps that start/stop playing.
    PCM reaches ffmpeg on an extra pipe fd (stdin may already carry raw video from gst)."""
    NODE = "ds-share-audio"
    RATE, CH = 48000, 2
    # app channel -> our stereo inputs (mono/centre to both; LFE dropped; unknown to both)
    DOWNMIX = {"FL": ("FL",), "FR": ("FR",), "MONO": ("FL", "FR"), "FC": ("FL", "FR"),
               "RL": ("FL",), "SL": ("FL",), "RR": ("FR",), "SR": ("FR",), "LFE": ()}

    def __init__(self):
        self.r, self.w = os.pipe()
        self.args = ["-f", "s16le", "-ar", str(self.RATE), "-ac", str(self.CH),
                     "-thread_queue_size", "1024", "-i", "pipe:%d" % self.r]
        self.pass_fds = (self.r,)
        self.stdin = False
        self.rec = None
        self.alive = True
        self.linked = set()

    def start(self, proc, t0=None):
        os.close(self.r)                      # ffmpeg holds its own copy now
        self.rec = subprocess.Popen(
            ["pw-record", "-P", '{ node.name = "%s" node.autoconnect = false '
             'media.role = "Production" }' % self.NODE,
             "--rate", str(self.RATE), "--channels", str(self.CH), "--format", "s16", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        out = os.fdopen(self.w, "wb", buffering=0)
        buf = _PcmBuffer(self.rec.stdout, self.CH)
        threading.Thread(target=self._link_loop, args=(proc,), daemon=True).start()

        def feed():
            _paced_pcm_feed(proc, buf, self.RATE, self.CH, 480, write=out.write, t0=t0)
            self.stop()
        threading.Thread(target=feed, daemon=True).start()
        sys.stderr.write("[audio] PipeWire app mix (excluding %s)\n" % ", ".join(sorted(AUDIO_EXCLUDE)))

    def stop(self):
        self.alive = False
        if self.rec and self.rec.poll() is None:
            self.rec.kill()
        try:
            os.close(self.w)
        except OSError:
            pass

    @staticmethod
    def plan(graph, node_name=NODE, exclude=AUDIO_EXCLUDE):
        """From a pw-dump graph: the (out_port, in_port) links that should exist."""
        nodes, ports, links = {}, {}, set()
        for o in graph:
            info = o.get("info") or {}
            props = info.get("props") or {}
            t = o.get("type", "")
            if t.endswith(":Node"):
                nodes[o["id"]] = props
            elif t.endswith(":Port") and props.get("node.id") is not None:
                ports.setdefault(props["node.id"], []).append(
                    (o["id"], info.get("direction"), props.get("audio.channel")))
            elif t.endswith(":Link"):
                links.add((info.get("output-port-id"), info.get("input-port-id")))
        ours = [n for n, p in nodes.items() if p.get("node.name") == node_name]
        if not ours:
            return [], links
        ins = {ch: pid for pid, d, ch in ports.get(ours[0], []) if d == "input"}
        want = []
        for nid, p in nodes.items():
            if p.get("media.class") != "Stream/Output/Audio":
                continue
            binary = (p.get("application.process.binary") or "").lower()
            name = (p.get("application.name") or "").lower()
            if binary in exclude or name in exclude:
                continue
            for pid, d, ch in ports.get(nid, []):
                if d == "output":
                    want += [(pid, ins[c]) for c in PipeWireAppMix.DOWNMIX.get(ch, ("FL", "FR"))
                             if c in ins]
        return want, links

    def _link_loop(self, proc):
        while self.alive and proc.poll() is None:
            try:
                graph = _json_docs(subprocess.run(["pw-dump"], capture_output=True,
                                                  timeout=5).stdout)
                want, links = self.plan(graph)
                for out_p, in_p in want:
                    if (out_p, in_p) not in links:
                        subprocess.run(["pw-link", str(out_p), str(in_p)],
                                       capture_output=True, timeout=5)
            except Exception as e:
                sys.stderr.write("[audio] pipewire link scan failed: %s\n" % e)
            time.sleep(1)


def _json_docs(raw):
    """pw-dump's output as ONE list of objects. It sometimes prints several JSON documents
    back to back (seen when the graph changes mid-dump: "Extra data: line 7 column 1"),
    which json.loads rejects outright, so read them all and flatten."""
    text = (raw or b"").decode("utf-8", "replace")
    dec, i, out = json.JSONDecoder(), 0, []
    while True:
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            return out
        doc, i = dec.raw_decode(text, i)
        out.extend(doc if isinstance(doc, list) else [doc])


class WasapiExcludeSteam:
    """Windows 10 2004+/11: WASAPI *process* loopback in EXCLUDE mode targeting Steam's
    webhelper process tree (see _steam_audio_pid) — every app's audio except Steam's,
    no virtual device. Raw COM via ctypes (pyaudiowpatch can only do whole-device loopback).
    Exposes get_read_available()/read() for _paced_pcm_feed, like _PcmBuffer."""
    RATE, CH = 48000, 2

    def __init__(self, exclude_pid):
        self.pid = exclude_pid
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.alive = True
        self.ready = threading.Event()
        self.error = None
        threading.Thread(target=self._run, daemon=True).start()
        self.ready.wait(5)
        if self.error or not self.ready.is_set():
            self.alive = False
            raise RuntimeError(self.error or "process loopback activation timed out")

    def get_read_available(self):
        with self.lock:
            return len(self.buf) // (2 * self.CH)

    def read(self, n, exception_on_overflow=False):
        with self.lock:
            take = min(n, len(self.buf) // (2 * self.CH)) * 2 * self.CH
            out = bytes(self.buf[:take])
            del self.buf[:take]
        return out

    def stop(self):
        self.alive = False

    def _run(self):
        try:
            self._capture()
        except Exception as e:                     # incl. ctypes setup on a non-Windows box
            self.error = str(e) or repr(e)
            self.ready.set()

    def _capture(self):
        import ctypes
        from ctypes import wintypes, POINTER, c_void_p, byref, c_uint32, c_uint64, c_int64

        class GUID(ctypes.Structure):
            _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD),
                        ("d", ctypes.c_ubyte * 8)]

            @staticmethod
            def of(s):
                import uuid
                return GUID.from_buffer_copy(uuid.UUID(s).bytes_le)

        IID_IUnknown = GUID.of("00000000-0000-0000-C000-000000000046")
        IID_IAgileObject = GUID.of("94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90")
        IID_Handler = GUID.of("41D949AB-9862-444A-80F6-C261334DA5EB")
        IID_IAudioClient = GUID.of("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2")
        IID_IAudioCaptureClient = GUID.of("C8ADBD64-E71E-48a0-A4DE-185C395CD317")

        class LOOPBACK_PARAMS(ctypes.Structure):
            _fields_ = [("TargetProcessId", wintypes.DWORD), ("ProcessLoopbackMode", ctypes.c_int)]

        class ACTIVATION_PARAMS(ctypes.Structure):
            _fields_ = [("ActivationType", ctypes.c_int), ("ProcessLoopbackParams", LOOPBACK_PARAMS)]

        class BLOB(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.ULONG), ("pBlobData", c_void_p)]

        class PROPVARIANT(ctypes.Structure):
            _fields_ = [("vt", wintypes.USHORT), ("r1", wintypes.WORD), ("r2", wintypes.WORD),
                        ("r3", wintypes.WORD), ("blob", BLOB)]

        class WAVEFORMATEX(ctypes.Structure):
            _fields_ = [("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD),
                        ("nSamplesPerSec", wintypes.DWORD), ("nAvgBytesPerSec", wintypes.DWORD),
                        ("nBlockAlign", wintypes.WORD), ("wBitsPerSample", wintypes.WORD),
                        ("cbSize", wintypes.WORD)]

        HRESULT = ctypes.HRESULT
        ole32 = ctypes.OleDLL("ole32")
        mmdev = ctypes.OleDLL("Mmdevapi")
        k32 = ctypes.WinDLL("kernel32")

        def method(ptr, index, restype, *argtypes):
            vtbl = ctypes.cast(ctypes.cast(ptr, POINTER(c_void_p))[0], POINTER(c_void_p))
            return ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)(vtbl[index])

        try:
            ole32.CoInitializeEx(None, 0)                     # COINIT_MULTITHREADED
            done = threading.Event()
            result = {}
            # the completion handler: a minimal agile COM object (IUnknown + ActivateCompleted)
            QI = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(GUID), POINTER(c_void_p))
            REF = ctypes.WINFUNCTYPE(wintypes.ULONG, c_void_p)
            DONE = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_void_p)

            def qi(this, riid, out):
                r = riid.contents
                if any(bytes(r) == bytes(g) for g in (IID_IUnknown, IID_IAgileObject, IID_Handler)):
                    out[0] = this
                    return 0
                out[0] = None
                return -2147467262                            # E_NOINTERFACE

            def completed(this, op):
                try:
                    hr = HRESULT
                    act = wintypes.LONG()
                    unk = c_void_p()
                    method(op, 3, ctypes.c_long, POINTER(wintypes.LONG), POINTER(c_void_p))(
                        op, byref(act), byref(unk))
                    result["hr"], result["client"] = act.value, unk
                except Exception as e:
                    result["hr"], result["err"] = -1, str(e)
                done.set()
                return 0

            fns = (QI(qi), REF(lambda this: 1), REF(lambda this: 1), DONE(completed))
            vtbl = (c_void_p * 4)(*[ctypes.cast(f, c_void_p) for f in fns])
            obj = ctypes.pointer(ctypes.pointer(vtbl))
            self._keep = (fns, vtbl, obj)                     # keep alive for COM

            params = ACTIVATION_PARAMS(1, LOOPBACK_PARAMS(self.pid, 1))   # PROCESS_LOOPBACK, EXCLUDE tree
            pv = PROPVARIANT(65, 0, 0, 0, BLOB(ctypes.sizeof(params), ctypes.cast(byref(params), c_void_p)))  # VT_BLOB
            op = c_void_p()
            mmdev.ActivateAudioInterfaceAsync(ctypes.c_wchar_p("VAD\\Process_Loopback"),
                                              byref(IID_IAudioClient), byref(pv),
                                              ctypes.cast(obj, c_void_p), byref(op))
            if not done.wait(5) or result.get("hr", -1) != 0:
                raise RuntimeError("process loopback activation failed (hr=%s %s)"
                                   % (result.get("hr"), result.get("err", "")))
            client = result["client"]
            fmt = WAVEFORMATEX(1, self.CH, self.RATE, self.RATE * 2 * self.CH, 2 * self.CH, 16, 0)
            # LOOPBACK | EVENTCALLBACK | AUTOCONVERTPCM | SRC_DEFAULT_QUALITY
            flags = 0x00020000 | 0x00040000 | 0x80000000 | 0x08000000
            method(client, 3, HRESULT, ctypes.c_int, wintypes.DWORD, c_int64, c_int64,
                   POINTER(WAVEFORMATEX), c_void_p)(client, 0, flags, 200000, 0, byref(fmt), None)
            ev = k32.CreateEventW(None, False, False, None)
            method(client, 13, HRESULT, c_void_p)(client, ev)
            cap = c_void_p()
            method(client, 14, HRESULT, POINTER(GUID), POINTER(c_void_p))(
                client, byref(IID_IAudioCaptureClient), byref(cap))
            get_buf = method(cap, 3, HRESULT, POINTER(c_void_p), POINTER(c_uint32),
                             POINTER(wintypes.DWORD), POINTER(c_uint64), POINTER(c_uint64))
            release = method(cap, 4, HRESULT, c_uint32)
            next_size = method(cap, 5, HRESULT, POINTER(c_uint32))
            method(client, 10, HRESULT)(client)               # Start
            self.ready.set()
            sys.stderr.write("[audio] WASAPI process loopback: everything except Steam (webhelper pid %d)\n"
                             % self.pid)
            data, n, fl = c_void_p(), c_uint32(), wintypes.DWORD()
            while self.alive:
                k32.WaitForSingleObject(ev, 20)
                while True:
                    size = c_uint32()
                    next_size(cap, byref(size))
                    if not size.value:
                        break
                    get_buf(cap, byref(data), byref(n), byref(fl), None, None)
                    nbytes = n.value * 2 * self.CH
                    chunk = (b"\x00" * nbytes if fl.value & 0x2       # AUDCLNT_BUFFERFLAGS_SILENT
                             else ctypes.string_at(data, nbytes))
                    release(cap, n)
                    with self.lock:
                        self.buf += chunk
                        if len(self.buf) > self.RATE * 2 * self.CH:   # >1 s backlog
                            del self.buf[:len(self.buf) - self.RATE * 2 * self.CH]
            method(client, 11, HRESULT)(client)               # Stop
        except Exception as e:
            self.error = str(e)
            self.ready.set()


def _steam_audio_pid():
    """PID whose process tree plays Steam's audio (voice chat + incoming shares): the MAIN
    steamwebhelper.exe — the one without a --type= switch; Chromium's audio service is its
    child. NOT steam.exe: measured in the Windows VM, process loopback treats the webhelper
    as outside steam.exe's tree even though steam.exe is its parent (excluding steam.exe
    left the call audio in the share; excluding the webhelper removed it)."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='steamwebhelper.exe'\" | "
             "Where-Object { $_.CommandLine -notmatch '--type=' } | "
             "Select-Object -First 1 -ExpandProperty ProcessId"],
            capture_output=True, text=True, timeout=15, creationflags=0x08000000).stdout.strip()
        return int(out) if out.isdigit() else None
    except Exception:
        return None


class _WinExcludeSteamAudio:
    """Windows share audio = everything except Steam, fed into ffmpeg's stdin."""
    RATE, CH = WasapiExcludeSteam.RATE, WasapiExcludeSteam.CH

    def __init__(self, cap):
        self.cap = cap
        self.args = ["-f", "s16le", "-ar", str(self.RATE), "-ac", str(self.CH),
                     "-thread_queue_size", "1024", "-i", "pipe:0"]
        self.pass_fds, self.stdin = (), True

    def start(self, proc, t0=None):
        threading.Thread(target=_paced_pcm_feed, args=(proc, self.cap, self.RATE, self.CH, 480),
                         kwargs={"t0": t0}, daemon=True).start()

    def stop(self):
        self.cap.stop()


class _SimpleAudio:
    """An ffmpeg-native audio input (pulse monitor) — nothing to start or stop."""
    def __init__(self, args):
        self.args, self.pass_fds, self.stdin = args, (), False

    def start(self, proc, t0=None):
        pass

    def stop(self):
        pass


class _WasapiAudio:
    """Windows WASAPI loopback fed into ffmpeg's stdin by _wasapi_loopback_feed."""
    def __init__(self, args):
        self.args, self.pass_fds, self.stdin = args, (), True

    def start(self, proc, t0=None):
        threading.Thread(target=_wasapi_loopback_feed, args=(proc, t0), daemon=True).start()

    def stop(self):
        pass


def audio_source():
    """Desktop-audio source for a share, or None (video only). Never breaks the video."""
    if not AUDIO:
        return None
    if IS_WIN:
        if os.environ.get("DS_AUDIO_MODE", "") != "all":
            try:
                pid = _steam_audio_pid()
                if not pid:
                    sys.stderr.write("[audio] Steam's webhelper not found; share audio may "
                                     "include the call\n")
                return _WinExcludeSteamAudio(WasapiExcludeSteam(pid or os.getpid()))
            except Exception as e:
                sys.stderr.write("[audio] process loopback unavailable (%s); capturing the WHOLE "
                                 "desktop mix (the call will echo into the share)\n" % e)
        args, _ = _audio_input()
        return _WasapiAudio(args) if args else None
    if pipewire_ok() and os.environ.get("DS_AUDIO_MODE", "") != "all":
        return PipeWireAppMix()
    args, _ = _audio_input()
    if args:
        sys.stderr.write("[audio] no PipeWire tools: capturing the WHOLE desktop mix "
                         "(the call will echo into the share)\n")
    return _SimpleAudio(args) if args else None


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


def _wasapi_loopback_feed(proc, t0=None):
    """Windows: capture the default output's WASAPI loopback at its NATIVE rate/channels and
    pipe s16 PCM to the encoder's stdin.

    WASAPI loopback delivers NO data at all while nothing is playing — read() just blocks.
    ffmpeg won't start ANY output until every input has produced a frame, so a share
    started on a quiet desktop never began, video included (found in the Windows test VM).
    So this paces itself against the wall clock: real audio when the device has some,
    otherwise silence for exactly the time that passed. aresample=async=1 absorbs drift."""
    dev = _wasapi_device()
    rate = dev["rate"] if dev else 48000
    ch = dev["ch"] if dev else 2
    frames = 480 * rate // 48000 or 480                  # ~10 ms
    stream = None
    try:
        import pyaudiowpatch as pa
        p = pa.PyAudio()
        stream = p.open(format=pa.paInt16, channels=ch, rate=rate, frames_per_buffer=frames,
                        input=True, input_device_index=(dev["index"] if dev else None))
        sys.stderr.write("[audio] WASAPI loopback @ %dHz %dch\n" % (rate, ch))
    except Exception as e:
        sys.stderr.write("[audio] WASAPI loopback failed (%s); feeding silence\n" % e)
    _paced_pcm_feed(proc, stream, rate, ch, frames, t0=t0)


def _paced_pcm_feed(proc, stream, rate, ch, frames, clock=time.monotonic, sleep=time.sleep,
                    write=None, t0=None):
    """Write PCM to proc.stdin (or `write`) in real time: whatever `stream` has available,
    topped up with silence whenever the source falls behind the clock (no data = silence)."""
    write = write or proc.stdin.write
    bpf = ch * 2                                       # s16 bytes per frame
    # t0 = when VIDEO capture started (see _start_tee): the first write then back-fills the
    # gap with silence, so audio timestamps line up with video from frame one. Starting the
    # audio clock at its own (later) start left audio ~180 ms behind video, and ffmpeg's
    # muxer held every video packet back to interleave with it: measured 244 ms -> 62 ms.
    t0 = clock() if t0 is None else t0
    sent = 0                                           # frames written so far
    while proc.poll() is None:
        due = int((clock() - t0) * rate) - sent        # frames the clock says we owe
        data = b""
        try:
            avail = stream.get_read_available() if stream is not None else 0
            if avail >= frames:
                data = stream.read(min(avail, frames * 4), exception_on_overflow=False)
        except Exception:
            data = b""
        if not data and due >= frames:
            data = b"\x00" * (due * bpf)              # quiet desktop: fill the gap
        if data:
            try:
                write(data)
            except Exception:
                break
            sent += len(data) // bpf
        else:
            sleep(0.005)


def stop_fmp4():
    """Forget the WS side of the share: drop the init segment and close every viewer."""
    with ws_lock:
        fmp4_init["seg"] = None
        clients = list(ws_clients)
        ws_clients.clear()
    for c in clients:                       # close outside the lock: close() joins on its cv
        c.close()


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


# System overlays / helper windows / virtual devices that are never useful to share
# (matched case-insensitively as substrings of the title/device name). This is why
# things like the NVIDIA overlay used to clutter the source picker.
_SRC_DENY = (
    "nvidia geforce overlay",
    "nvidia broadcast",
    "geforce experience",
    "nvidia container",
    "program manager",
    "windows input experience",
    "windows shell experience host",
    "microsoft text input application",
    "xbox game bar",
    "game bar",
)


def _is_junk_source(name):
    t = (name or "").strip().lower()
    if not t:
        return True
    return any(k in t for k in _SRC_DENY)


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
        if not title or _is_junk_source(title):
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


def _x11_monitors():
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


def _wayland_monitors():
    """Monitor list on Wayland — kscreen-doctor (KDE) first, xrandr-via-XWayland as a
    fallback (XWayland mirrors the outputs). The geom here is only a UI label and the
    restore-token key: the PORTAL supplies the actual capture region."""
    out = []
    try:
        j = json.loads(subprocess.run(["kscreen-doctor", "-j"], capture_output=True,
                                      text=True, timeout=5).stdout)
        for o in j.get("outputs", []):
            if not o.get("enabled"):
                continue
            pos, size = o.get("pos") or {}, o.get("size") or {}
            out.append({"name": o.get("name", "?"),
                        "geom": "%dx%d+%d+%d" % (size.get("width", 0), size.get("height", 0),
                                                 pos.get("x", 0), pos.get("y", 0)),
                        "primary": o.get("priority") == 1})
    except Exception:
        pass
    if not out:
        out = _x11_monitors()
    if not out:
        # no enumerator at all — still shareable: the portal picker chooses the screen
        out = [{"name": "Screen (system picker)", "geom": "0x0+0+0", "primary": True}]
    return out


def monitors():
    if IS_WIN:
        try:
            return _win_monitors()
        except Exception:
            return []
    return _wayland_monitors() if is_wayland() else _x11_monitors()


def windows():
    """App windows with on-screen geometry, so 'share an app' = capture that window.
    Linux: occlusion-proof via gst ximagesrc xid. Windows: gdigrab -i title= (the id IS
    the title) — captures the window's DC, tolerant of being partly behind others."""
    if IS_WIN:
        try:
            return _win_windows()
        except Exception:
            return []
    if is_wayland():
        # Wayland can't enumerate other apps' windows — the compositor's portal picker
        # does the choosing. One pseudo-entry keeps the UI's "Windows" group working.
        return [{"id": "portal", "title": "App window… (system picker)", "geom": ""}]
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
            if _is_junk_source(title):
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
            r = subprocess.run([ffmpeg_bin(), "-hide_banner", "-f", "dshow",
                                "-list_devices", "true", "-i", "dummy"],
                               capture_output=True, text=True, timeout=8)
            for line in r.stderr.splitlines():
                m = re.search(r'"([^"]+)"\s*\((video)\)', line)
                if m and not _is_junk_source(m.group(1)):
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


# --- Wayland capture: xdg-desktop-portal ScreenCast -> PipeWire ----------------------
# On Wayland, x11grab/ximagesrc see nothing (or only XWayland windows): the compositor
# owns the pixels. The sanctioned path is the ScreenCast portal — the daemon asks for a
# monitor/window over D-Bus, the COMPOSITOR shows its own picker dialog (only the first
# time: we save the portal's restore_token so re-shares skip it), and frames arrive on a
# PipeWire node that gstreamer's pipewiresrc reads. Needs python3-dbus + python3-gi (a
# GLib loop delivers the portal's async Response signals) and gstreamer1.0-pipewire.
# cancel: set by /stop so a share waiting on the compositor's picker can be abandoned
# (start_capture holds `lock` for the whole wait, up to minutes, so /stop would otherwise
# block behind it and the Stop button would appear dead).
_portal = {"bus": None, "ctr": 0, "cancel": threading.Event()}
_TOKENS = os.path.join(os.path.expanduser("~"), ".config", "discordish",
                       "screencast-tokens.json")


def _tokens_load():
    try:
        with open(_TOKENS) as f:
            return json.load(f)
    except Exception:
        return {}


def _tokens_save(key, tok):
    d = _tokens_load()
    d[key] = tok
    try:
        os.makedirs(os.path.dirname(_TOKENS), exist_ok=True)
        with open(_TOKENS, "w") as f:
            json.dump(d, f)
        os.chmod(_TOKENS, 0o600)
    except Exception:
        pass


def _portal_bus():
    if _portal["bus"]:
        return _portal["bus"]
    import dbus, dbus.mainloop.glib
    from gi.repository import GLib
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    _portal["bus"] = dbus.SessionBus()
    threading.Thread(target=GLib.MainLoop().run, daemon=True).start()
    return _portal["bus"]


def _sv(d):
    """Portal option dicts are 'a{sv}' (string -> variant). dbus-python guesses the
    signature from value types, and an ALL-STRING dict is mis-guessed as 'a{ss}' — which
    the portal rejects. Force 'sv' and wrap bare python strs as dbus.String so the
    signature is always correct regardless of which options are present."""
    import dbus
    out = dbus.Dictionary(signature="sv")
    for k, v in d.items():
        out[k] = dbus.String(v) if isinstance(v, str) else v
    return out


def _portal_call(bus, method, args, opts, timeout):
    """Call a portal Request-style method and wait for its Response signal (subscribed
    on the predictable request path BEFORE calling, per the portal spec, so the response
    can't be missed). Returns the results dict; raises on timeout/cancel/failure."""
    _portal["ctr"] += 1
    token = "ds%d_%d" % (os.getpid(), _portal["ctr"])
    sender = bus.get_unique_name()[1:].replace(".", "_")
    path = "/org/freedesktop/portal/desktop/request/%s/%s" % (sender, token)
    ev = threading.Event()
    out = {}

    def on_response(code, results):
        out["code"] = int(code)
        out["results"] = results
        ev.set()

    match = bus.add_signal_receiver(on_response, signal_name="Response",
                                    dbus_interface="org.freedesktop.portal.Request",
                                    bus_name="org.freedesktop.portal.Desktop", path=path)
    o = dict(opts)
    o["handle_token"] = token
    try:
        method(*(list(args) + [_sv(o)]))
        deadline = time.time() + timeout
        while not ev.wait(0.25):
            if _portal["cancel"].is_set():
                raise RuntimeError("portal request cancelled by user")
            if time.time() > deadline:
                raise RuntimeError("portal request timed out")
    finally:
        match.remove()
    if out.get("code") != 0:
        raise RuntimeError("portal request %s"
                           % ("cancelled by user" if out.get("code") == 1 else "failed"))
    return out.get("results") or {}


class PortalSession:
    """One ScreenCast portal session (a monitor or a window). start() -> pipewire node
    id; open_remote() -> a fresh portal-scoped pipewire fd for one consumer pipeline;
    close() ends the session (the node dies with it)."""
    SCREEN, WINDOW = 1, 2                      # portal source-type bitmask

    def __init__(self, kind, key):
        self.kind = kind
        self.key = key                          # restore-token key ("mon:<geom>" / "win")
        self.session = None
        self.node = None
        self.size = None
        self.fds = []
        self._sc = None

    def start(self):
        import dbus
        bus = _portal_bus()
        obj = bus.get_object("org.freedesktop.portal.Desktop",
                             "/org/freedesktop/portal/desktop")
        sc = dbus.Interface(obj, "org.freedesktop.portal.ScreenCast")
        _portal["ctr"] += 1
        r = _portal_call(bus, sc.CreateSession, [],
                         {"session_handle_token": "ds%d_%d" % (os.getpid(), _portal["ctr"])}, 10)
        self.session = str(r["session_handle"])
        try:
            props = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
            cmodes = int(props.Get("org.freedesktop.portal.ScreenCast",
                                   "AvailableCursorModes"))
        except Exception:
            cmodes = 2
        # cursor EMBEDDED (burned into frames, like x11grab -draw_mouse) when supported;
        # persist_mode 2 = compositor remembers the choice across daemon restarts.
        opts = {"types": dbus.UInt32(self.kind), "multiple": False,
                "cursor_mode": dbus.UInt32(2 if cmodes & 2 else 1),
                "persist_mode": dbus.UInt32(2)}
        tok = _tokens_load().get(self.key)
        if tok:
            opts["restore_token"] = tok
        _portal_call(bus, sc.SelectSources, [dbus.ObjectPath(self.session)], opts, 15)
        # Start blocks on the compositor's picker dialog the FIRST time (the restore
        # token skips it afterwards) — give the user a couple of minutes to click.
        r = _portal_call(bus, sc.Start, [dbus.ObjectPath(self.session), ""], {}, 180)
        if r.get("restore_token"):
            _tokens_save(self.key, str(r["restore_token"]))
        streams = r.get("streams")
        if not streams:
            raise RuntimeError("portal returned no streams")
        self.node = int(streams[0][0])
        try:
            # stream size from the portal (LOGICAL px on KDE — uniform scale, so the
            # ASPECT is exact; used to shape the window-share canvas)
            sz = (streams[0][1] or {}).get("size")
            self.size = (int(sz[0]), int(sz[1])) if sz else None
        except Exception:
            self.size = None
        self._sc = sc
        return self.node

    def open_remote(self):
        import dbus
        fd = self._sc.OpenPipeWireRemote(dbus.ObjectPath(self.session), _sv({})).take()
        self.fds.append(fd)
        return fd

    def close(self):
        import dbus
        try:
            bus = _portal_bus()
            so = bus.get_object("org.freedesktop.portal.Desktop", self.session)
            dbus.Interface(so, "org.freedesktop.portal.Session").Close()
        except Exception:
            pass
        for fd in self.fds:
            try:
                os.close(fd)
            except Exception:
                pass
        self.fds = []


def _wl_canvas(geom, win, src=None):
    """Fixed output canvas for the Wayland pipe (raw video over a pipe can't change
    size mid-stream, so gst scales everything to this). Its ASPECT must match the
    source — the caps can't pin pixel-aspect-ratio (that stalls pipewiresrc caps
    negotiation with KWin forever), so an aspect mismatch would STRETCH, not letterbox.
    Monitor shares use the monitor's native mode; window shares the portal-reported
    stream size (src). MAX_H caps the height (downscale only); always even dims
    (encoders reject odd and emit black)."""
    w = h = 0
    m = re.match(r"(\d+)x(\d+)\+", geom or "")
    if not win and m:
        w, h = int(m.group(1)), int(m.group(2))
    elif src:
        w, h = src
    if not w or not h:
        mons = monitors()
        mm = re.match(r"(\d+)x(\d+)\+",
                      (next((x for x in mons if x.get("primary")), mons[0])["geom"]
                       if mons else "") or "")
        w, h = (int(mm.group(1)), int(mm.group(2))) if mm else (1920, 1080)
    if MAX_H.isdigit() and h > int(MAX_H):
        w = int(round(w * int(MAX_H) / h))
        h = int(MAX_H)
    return max(2, (w // 2) * 2), max(2, (h // 2) * 2)


_gst_conv = []


def _gst_convert():
    """Scale + colour-convert stage. Multi-threaded: the default single-threaded
    videoscale ! videoconvert topped out at ~34 fps turning a 4K desktop into 1440p NV12
    (one core pegged), so 60 fps shares silently ran at half rate. videoconvertscale
    (GStreamer >= 1.22) does both in one pass across all cores."""
    if not _gst_conv:
        try:
            have = subprocess.run(["gst-inspect-1.0", "videoconvertscale"],
                                  capture_output=True, timeout=10).returncode == 0
        except Exception:
            have = False
        _gst_conv.append("videoconvertscale n-threads=0" if have
                         else "videoscale n-threads=0 ! videoconvert n-threads=0")
    return _gst_conv[0]


_ff_caps = {}


def opus_over_rtmp():
    """Can this ffmpeg mux Opus into (enhanced) FLV? ffmpeg >= 7.1 can; then audio is ONE
    Opus encode feeding both transports — WebRTC (Opus is the only audio browsers take
    over WebRTC) and the fMP4/MSE path (Chromium plays Opus in MP4). Older ffmpeg: AAC on
    the fMP4 path only, and WebRTC viewers get video without sound."""
    if "opus_flv" not in _ff_caps:
        try:
            _ff_caps["opus_flv"] = subprocess.run(
                [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                 "anullsrc=r=48000:cl=stereo", "-t", "0.1", "-c:a", "libopus", "-f", "flv",
                 "-"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=20).returncode == 0
        except Exception:
            _ff_caps["opus_flv"] = False
    return _ff_caps["opus_flv"]


def _audio_out_args():
    if opus_over_rtmp():
        # lowdelay + 10 ms frames: measured 124 -> 96 ms glass-to-glass vs default 20 ms frames
        return "opus", ["-c:a", "libopus", "-b:a", "128k", "-ar", "48000", "-ac", "2",
                        "-application", "lowdelay", "-frame_duration", "10"]
    return "aac", ["-c:a", "aac", "-b:a", "128k"]


def _start_tee(video_in, label, scale=True, stdin=None, pass_fds=(), extra=None, video_t0=None):
    """THE capture pipeline, for every source: ONE ffmpeg that captures (or reads raw video
    from gst on stdin), does ONE video encode (+ one audio encode) and fans the result out
    with the tee muxer to BOTH transports:
      * RTMP -> MediaMTX -> WebRTC/WHEP (video, + Opus audio when opus_over_rtmp())
      * fragmented MP4 on stdout -> fmp4_pump -> WebSocket/MSE viewers (video + audio)
    The non-Wayland paths used to run TWO captures + TWO encodes (one per transport), which
    doubled GPU/CPU load on the host and left the WebRTC copy without audio."""
    src = audio_source()
    audio_in = src.args if src else []
    state["audio"] = bool(audio_in)
    acodec, aargs = _audio_out_args()
    state["acodec"] = acodec if audio_in else None
    name, in_args, enc_args = pick_encoder()
    if scale:
        in_args = _apply_scale(in_args)
    rtmp_sel = "v,a" if (audio_in and acodec == "opus") else "v"
    mp4o = "f=mp4:movflags=+frag_keyframe+empty_moov+default_base_moof:frag_duration=100000"
    # onfail=ignore: a MediaMTX hiccup must not take the WS path down with it
    tee = ("[onfail=ignore:select='%s':f=flv:flvflags=no_duration_filesize]%s|[select='%s':%s]pipe:1"
           % (rtmp_sel, RTMP, "v,a" if audio_in else "v", mp4o))
    ff = ([ffmpeg_bin(), "-hide_banner", "-loglevel", "warning"] + video_in + audio_in
          + ["-map", "0:v:0"]
          + (["-map", "1:a:0"] + aargs + ["-af", "aresample=async=1"] if audio_in else [])
          + in_args + enc_args
          # global_header: tee's mp4 slave needs the codec extradata up front; the profile
          # is pinned to match the viewer's MSE codec string (MSE_CODEC).
          + _profile_args(name) + ["-flags", "+global_header",
                                   # write packets as they come instead of holding video to
                                   # interleave with audio (timestamps are aligned already)
                                   "-max_interleave_delta", "1000", "-f", "tee", tee])
    with ws_lock:
        fmp4_init["seg"] = None
    errf = _open_log("rp-capture.log", (extra or "") + "[ff ] %s\n" % " ".join(ff))
    video_t0 = video_t0 or time.monotonic()      # a direct capture starts with ffmpeg itself
    try:
        p = _spawn(ff, stdin=(subprocess.PIPE if (src and src.stdin) else (stdin or subprocess.DEVNULL)),
                   stdout=subprocess.PIPE, stderr=errf, env=_cap_env(),
                   pass_fds=tuple(pass_fds) + (src.pass_fds if src else ()))
    finally:
        _close_log(errf)
    state["ff"] = p
    state["asrc"] = src
    if src:
        src.start(p, t0=video_t0)
    threading.Thread(target=fmp4_pump, args=(p.stdout,), daemon=True).start()
    sys.stderr.write("[cap] sharing %s (%s%s)\n" % (label, name, ", %s audio" % acodec if audio_in else ""))
    return True


def _start_raw_tee(gst_src, w, h, label, pass_fds=()):
    """gst captures + normalizes (CFR at FPS, scale to a fixed w x h canvas, NV12) and pipes
    RAW video into _start_tee's ffmpeg. Used for sources only gst can read: the Wayland
    portal (pipewiresrc) and X11 per-window (ximagesrc xid)."""
    fps = int(FPS)
    gst = ("%s ! videorate ! video/x-raw,framerate=%d/1 ! %s ! "
           "video/x-raw,format=NV12,width=%d,height=%d ! fdsink fd=1"
           % (gst_src, fps, _gst_convert(), w, h))
    errf = _open_log("rp-gst.log", "[gst] gst-launch-1.0 -q %s\n" % gst)
    t_video = time.monotonic()
    try:
        g = _spawn(["gst-launch-1.0", "-q"] + gst.split(), stdout=subprocess.PIPE,
                   stderr=errf, pass_fds=tuple(pass_fds), env=_cap_env())
    finally:
        _close_log(errf)
    state["wl_gst"] = g
    video_in = ["-f", "rawvideo", "-pix_fmt", "nv12", "-video_size", "%dx%d" % (w, h),
                "-framerate", str(fps), "-thread_queue_size", "512", "-i", "pipe:0"]
    ok = _start_tee(video_in, "%s -> %dx%d@%d" % (label, w, h, fps), scale=False, stdin=g.stdout,
                    video_t0=t_video)
    g.stdout.close()
    return ok


def _start_wayland_capture(win=None, geom=None):
    """Wayland share: xdg-desktop-portal ScreenCast -> PipeWire node -> gst pipewiresrc,
    then the shared raw-video tee (_start_raw_tee)."""
    ses = PortalSession(PortalSession.WINDOW if win else PortalSession.SCREEN,
                        "win" if win else "mon:%s" % (geom or "primary"))
    try:
        node = ses.start()
        fd = ses.open_remote()
    except Exception as e:
        sys.stderr.write("[wl] portal screencast failed: %s\n" % e)
        state["err"] = ("Screen picker was cancelled" if "cancel" in str(e)
                        else "System screen-share request failed (%s)" % e)
        ses.close()
        # Drop the cached session bus: if xdg-desktop-portal restarted under us the
        # old connection is wedged (stale signal subscriptions), and only a fresh bus
        # recovers — otherwise every later share would hang too, needing a daemon
        # restart. Next /start rebuilds it via _portal_bus().
        _portal["bus"] = None
        return False
    w, h = _wl_canvas(geom, win, ses.size)
    # keepalive-time re-pushes the last frame when the compositor goes damage-idle
    # (static screen) so videorate/CFR — and the viewer — never stall. NO
    # pixel-aspect-ratio in the caps: pinning it stalls negotiation with KWin forever
    # (the canvas matches the source aspect instead — see _wl_canvas).
    src = "pipewiresrc fd=%d path=%d do-timestamp=true keepalive-time=500" % (fd, node)
    state["wl_ses"] = ses
    state["geom"] = "win:portal" if win else (geom or "portal")
    return _start_raw_tee(src, w, h, "%s via portal node %d"
                          % ("window" if win else (geom or "screen"), node), pass_fds=(fd,))


def _start_x11_window(xid):
    """X11 per-window share: gst ximagesrc grabs the window's own buffer by XID (occlusion-
    proof), then the shared raw-video tee. Feeding BOTH transports from this one capture
    matters: the WS/fMP4 path used to fall back to capturing the whole PRIMARY MONITOR for
    window shares, so a remote viewer saw everything on that screen, not just the window."""
    geom = next((wn["geom"] for wn in windows() if wn["id"].lower() == xid.lower()), "")
    m = re.match(r"(\d+)x(\d+)\+", geom)
    w, h = _wl_canvas("", True, (int(m.group(1)), int(m.group(2))) if m else None)
    state["geom"] = "win:" + xid
    return _start_raw_tee("ximagesrc xid=%s use-damage=false show-pointer=true" % xid,
                          w, h, "window %s" % xid)


def _port_open(port, host="127.0.0.1"):
    try:
        socket.create_connection((host, port), 0.1).close()
        return True
    except Exception:
        return False


def _kill_stray_mediamtx():
    """A mediamtx we didn't spawn this run: a leftover of a previous daemon (Windows keeps
    children alive when the parent dies) still running an OLD config — e.g. without the
    RTMP ingest — would hold the WebRTC ports and silently break every share."""
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/F", "/IM", "mediamtx.exe"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.run(["pkill", "-f", "^" + re.escape(MEDIAMTX)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.5)
    except Exception:
        pass


def ensure_mediamtx():
    if state["mtx"] and state["mtx"].poll() is None:
        return
    if not os.path.exists(MEDIAMTX):
        return
    if _port_open(RTMP_PORT):
        return                                # one of ours is already up with the RTMP ingest
    _kill_stray_mediamtx()
    # Advertise BOTH the LAN interface IPs AND the public IP as ICE candidates: same-LAN
    # viewers use the local one, off-LAN viewers use the public one (reachable directly
    # once upnp_keep() has forwarded WEBRTC_UDP). We do NOT drop local — forcing public-only
    # breaks same-LAN because most home routers don't NAT-hairpin.
    env = dict(os.environ)
    pip = public_ip()
    if pip:
        env["MTX_WEBRTCADDITIONALHOSTS"] = pip
    # MediaMTX refuses to start on a config key it doesn't know, and installs keep whatever
    # mediamtx binary they first fetched — so a key added for newer versions (moq: no) would
    # kill sharing on an older one. Retry without the keys it names.
    drop = []
    for _ in range(4):
        logf = _open_log("rp-mediamtx.log", "")
        try:
            p = _spawn([MEDIAMTX, _mtx_config_path(drop)], stdout=logf, stderr=subprocess.STDOUT, env=env)
        finally:
            _close_log(logf)
        state["mtx"] = p
        for _ in range(50):                   # up to ~5s for the ingest port
            if _port_open(RTMP_PORT) or p.poll() is not None:
                break
            time.sleep(0.1)
        if p.poll() is None:
            return
        try:
            with open(os.path.join(tempfile.gettempdir(), "rp-mediamtx.log"), errors="replace") as f:
                m = re.search(r'unknown field "?([A-Za-z0-9_]+)"?', f.read())
        except OSError:
            m = None
        if not m or m.group(1) in drop:
            sys.stderr.write("[mtx] mediamtx exited (rc=%s); see rp-mediamtx.log\n" % p.returncode)
            return
        sys.stderr.write("[mtx] this mediamtx doesn't know %r; starting without it\n" % m.group(1))
        drop.append(m.group(1))


# Serializes tunnel (re)builds: /start and the watchdog can both call ensure_tunnel, and
# without this each spawned its own cloudflared — the loser was orphaned (never killed,
# not even at daemon exit) and its reader could publish ITS hostname over the winner's.
tunnel_lock = threading.Lock()
# cloudflared prints its API endpoint (https://api.trycloudflare.com/tunnel) in error lines
# when it can't register — e.g. at boot before the network is up — which the old pattern
# took for the tunnel URL and handed to viewers. Real quick-tunnel hosts are multi-word.
_CF_URL_RE = re.compile(r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com")


def stop_tunnel():
    _kill(state.get("cf"))
    state["cf"] = None; state["cf_url"] = None


def ensure_tunnel():
    with tunnel_lock:
        return _ensure_tunnel()


def _ensure_tunnel():
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
                m = _CF_URL_RE.search(line)
                if m and state.get("cf") is p and not state.get("cf_url"):
                    state["cf_url"] = m.group(0)
        except Exception:
            pass
    threading.Thread(target=reader, daemon=True).start()
    for _ in range(60):                       # wait up to ~15s for the URL
        if state.get("cf_url"):
            break
        time.sleep(0.25)
    return state.get("cf_url")


def _tunnel_watchdog():
    """Quick tunnels are free and unmanaged — cloudflared can exit mid-share (rate limit,
    edge hiccup, network blip). Nothing else notices: ensure_tunnel only re-checks on the
    next /start, so the viewer sits reconnecting to a dead hostname forever. Poll it and
    rebuild. The replacement gets a NEW random hostname, so we bump `cf_gen`; the sharer's
    plugin watches that via /ping and re-signals the fresh URL over chat."""
    if TUNNEL == "off":
        return
    while True:
        time.sleep(5)
        p = state.get("cf")
        if p is None or p.poll() is None:
            continue
        sys.stderr.write("[cf] quick tunnel exited (rc=%s); rebuilding\n" % p.returncode)
        old = state.get("cf_url")
        with tunnel_lock:
            if state.get("cf") is not p:          # someone else already replaced it
                continue
            state["cf"] = None
            state["cf_url"] = None
        try:
            url = ensure_tunnel()
        except Exception as e:
            sys.stderr.write("[cf] rebuild failed (%s); retrying\n" % e)
            continue
        if url:
            state["cf_gen"] = state.get("cf_gen", 0) + 1
            sys.stderr.write("[cf] tunnel back up: %s (was %s)\n" % (url, old))


def capture_alive():
    """Is the current share's capture process still running?"""
    p = state.get("ff")
    return p is not None and p.poll() is None


def stop_capture():
    _kill(state.get("ff"))
    state["ff"] = None
    if state.get("asrc"):
        state["asrc"].stop()
        state["asrc"] = None
    _kill(state.get("wl_gst"))
    state["wl_gst"] = None
    ses = state.get("wl_ses")
    state["wl_ses"] = None
    if ses:
        ses.close()
    state["geom"] = None
    state["audio"] = False
    stop_fmp4()


def start_capture(geom=None, win=None, cam=None):
    with lock:
        state["err"] = None
        state["key"] = secrets.token_urlsafe(12)   # new share -> links to the old one die
        _portal["cancel"].clear()
        stop_capture()
        ensure_mediamtx()
        if is_wayland() and not cam:
            # (cameras are plain v4l2 and take the direct path below on any session)
            return _start_wayland_capture(win=win, geom=geom)
        if win and not IS_WIN:
            return _start_x11_window(win)
        if cam:
            state["geom"] = "cam:" + cam
            return _start_tee(_camera_cap_in(cam), "camera %s" % cam)
        if win:
            # Windows per-window: gdigrab by title grabs the window's device context, so it
            # keeps working when partly covered (not when minimized).
            state["geom"] = "win:" + win
            return _start_tee(["-f", "gdigrab", "-draw_mouse", "1", "-framerate", FPS,
                               "-i", "title=%s" % win], "window %r" % win)
        # geom = WxH+X+Y -> monitor / region capture
        m = GEOM_RE.match(geom or "")   # offsets can be negative (monitor left of primary)
        if not m:
            return False
        w, h, x, y = m.groups()
        if IS_WIN:
            cap_in = ["-f", "gdigrab", "-draw_mouse", "1", "-framerate", FPS,
                      "-offset_x", x, "-offset_y", y, "-video_size", "%sx%s" % (w, h),
                      "-i", "desktop"]
        else:
            cap_in = ["-f", "x11grab", "-draw_mouse", "1", "-framerate", FPS,
                      "-video_size", "%sx%s" % (w, h), "-i", "%s+%s,%s" % (DISPLAY, x, y)]
        state["geom"] = geom
        return _start_tee(cap_in, "monitor %s" % geom)


# --- control-API request guards ------------------------------------------------------
# The control port is loopback-only, but loopback is reachable from EVERY web page open in
# any browser on this machine, and it used to answer them all (CORS *). A random site could
# /start a share and read the public tunnel URL back from /ping (= watch your screen), or
# drive the X11 window path's shell pipeline. Only Steam's own pages may use it now; non-
# browser clients (curl, the Lua backend, tests) send no Origin and are still allowed.
_ALLOWED_ORIGINS = re.compile(
    r"^https://(steamloopback\.host|([a-z0-9-]+\.)*steampowered\.com|"
    r"([a-z0-9-]+\.)*steamcommunity\.com)$")
_EXTRA_ORIGINS = {o.strip() for o in os.environ.get("DS_ALLOW_ORIGINS", "").split(",")
                  if o.strip()}
GEOM_RE = re.compile(r"^(\d{1,5})x(\d{1,5})\+(-?\d{1,5})\+(-?\d{1,5})$")
XID_RE = re.compile(r"^0x[0-9a-fA-F]{1,8}$")


def host_is_loopback(host):
    """The control API's Host header: 127.0.0.1 / localhost / [::1] (any port). It's the NAME
    that defeats DNS rebinding: a rebound request carries the attacker's hostname."""
    return bool(re.match(r"^(127\.0\.0\.1|localhost|\[::1\])(:\d{1,5})?$", (host or "").strip().lower()))


def origin_allowed(origin, fetch_site=None):
    """Origin header present -> must be a Steam page. Absent -> refuse only what a browser
    marks cross-site (an <img>/<script> GET from another site carries no Origin but does
    carry Sec-Fetch-Site); a non-browser client has neither header."""
    if origin:
        return bool(_ALLOWED_ORIGINS.match(origin)) or origin in _EXTRA_ORIGINS
    return (fetch_site or "none") in ("none", "same-origin")


def is_private_host(host):
    """A LAN/loopback/link-local/CGNAT (Tailscale) IP literal. Hostnames don't count."""
    try:
        ip = ipaddress.ip_address((host or "").strip("[]"))
    except ValueError:
        return False
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or (ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10")))


def whep_target_ok(target):
    """The viewer's WHEP proxy forwards an SDP offer to the HOST's endpoint, whose URL
    arrives in a chat message, i.e. from another person. Only forward to something shaped
    like OUR share endpoint: https anywhere, plain http only to a LAN address (a plain-text
    handshake must not cross the internet), path .../screen/whep, no credentials."""
    try:
        t = urlparse(target or "")
        host = t.hostname
    except Exception:
        return False
    if not host or t.username or t.password or len(target) > 2048:
        return False
    if not (t.scheme == "https" or (t.scheme == "http" and is_private_host(host))):
        return False
    return t.path.rstrip("/").endswith("/screen/whep")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The WHEP proxy must not follow redirects: a vetted URL could otherwise bounce the
    request to an internal address the check above would have refused."""
    def redirect_request(self, *a, **k):
        return None


_no_redirect_opener = urllib.request.build_opener(_NoRedirect)
MAX_SDP = 64 * 1024          # an SDP offer is a few KB; refuse anything absurd
MAX_WS_VIEWERS = 8


def _body(handler):
    """Request body up to MAX_SDP, or None if missing/oversized/malformed."""
    try:
        n = int(handler.headers.get("Content-Length", 0) or 0)
    except ValueError:
        return None
    if n < 0 or n > MAX_SDP:
        return None
    return handler.rfile.read(n)


def validate_source(geom, win, cam):
    """Reject malformed /start sources before they reach a capture command line. Returns
    an error string, or None when the request is fine."""
    if geom and not GEOM_RE.match(geom):
        return "bad geom"
    if win and not IS_WIN and not is_wayland() and not XID_RE.match(win):
        return "bad window id"
    if cam and cam not in {c["id"] for c in cameras()}:
        return "unknown camera"
    return None


class H(BaseHTTPRequestHandler):
    timeout = 20

    def log_message(self, *a): pass

    def _cors(self):
        o = self.headers.get("Origin")
        self.send_header("Access-Control-Allow-Origin", o if o and origin_allowed(o) else "null")
        self.send_header("Vary", "Origin")

    def _send(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _guard(self):
        # Host must be loopback: a web page that DNS-rebinds its own name to 127.0.0.1 makes
        # same-origin requests (no Origin header) that the origin check alone would let in.
        if not host_is_loopback(self.headers.get("Host")):
            sys.stderr.write("[ctl] refused %s %s for Host %r (DNS rebinding?)\n" % (
                self.command, urlparse(self.path).path, self.headers.get("Host")))
            self._send({"ok": False, "error": "forbidden host"}, 403)
            return False
        if origin_allowed(self.headers.get("Origin"), self.headers.get("Sec-Fetch-Site")):
            return True
        sys.stderr.write("[ctl] refused %s %s from origin=%r site=%r\n" % (
            self.command, urlparse(self.path).path, self.headers.get("Origin"),
            self.headers.get("Sec-Fetch-Site")))
        self._send({"ok": False, "error": "forbidden origin"}, 403)
        return False

    def do_GET(self):
        if not self._guard():
            return
        u = urlparse(self.path); q = parse_qs(u.query)
        ip = lan_ip()
        if u.path == "/sources":
            self._send({"monitors": monitors(), "windows": windows(),
                        "cameras": cameras(), "encoder": pick_encoder()[0]})
        elif u.path == "/start":
            win = q.get("win", [None])[0]
            geom = q.get("geom", [None])[0]
            cam = q.get("cam", [None])[0]
            if not win and not geom and not cam:
                mons = monitors(); geom = (next((m for m in mons if m["primary"]), mons[0])["geom"] if mons else "1920x1080+0+0")
            bad = validate_source(geom, win, cam)
            if bad:
                self._send({"ok": False, "error": bad}, 400)
                return
            # live encode overrides (UI dropdowns) — set before (re)starting the capture.
            global BITRATE, MAX_H, FPS, AUDIO
            br = (q.get("br", [None])[0] or "").strip()
            if re.match(r"^\d+(\.\d+)?[MmKk]$", br):
                BITRATE = br
            hh = (q.get("h", [None])[0] or "").strip().lower()
            if hh.isdigit():
                MAX_H = hh
            elif hh in ("auto", "native", "0"):
                MAX_H = ""
            fps = (q.get("fps", [None])[0] or "").strip()
            if fps.isdigit() and 1 <= int(fps) <= 60:
                FPS = fps
            global CODEC
            cd = (q.get("codec", [None])[0] or "").strip().lower()
            if cd in ("auto", "av1", "h264"):
                CODEC = cd
            au = (q.get("audio", [None])[0] or "").strip().lower()
            if au in ("0", "false", "off"):
                AUDIO = False
            elif au in ("1", "true", "on"):
                AUDIO = True
            # The same share requested again moments after it started (a double-click, or two
            # loaded copies of the plugin both handling one click): keep the running capture.
            # Restarting tore down the Wayland portal stream mid-negotiation and the new capture
            # got corrupt frames and died (seen live: 2-3 /starts within 2 s, share never came up).
            sig = (geom, win, cam, BITRATE, MAX_H, FPS, CODEC, AUDIO)
            dup = (state.get("start_sig") == sig and capture_alive()
                   and time.monotonic() - state.get("start_t", 0) < 8)
            try:
                if dup:
                    sys.stderr.write("[ctl] duplicate /start ignored (share already starting)\n")
                    ok = True
                else:
                    ok = start_capture(geom=geom, win=win, cam=cam)
                    state["start_sig"], state["start_t"] = sig, time.monotonic()
            except Exception as e:
                sys.stderr.write("[ctl] start failed: %r\n" % e)
                state["err"] = "capture failed to start (%s)" % e
                ok = False
            # local=1 -> skip the tunnel (LAN-only, faster). An own https origin
            # (public_url) replaces the tunnel entirely.
            tun = (None if q.get("local", ["0"])[0] == "1" or public_url()
                   else ensure_tunnel())
            out_whep, out_ws, via = share_urls(ip, tun)
            enc = pick_encoder()[0]
            up = state.get("upnp") or {}
            self._send({"ok": ok, "whep": out_whep, "ws": out_ws, "via": via,
                        "lan_whep": lan_whep(ip),
                        "tunnel": bool(tun), "path": "screen", "geom": geom,
                        "encoder": enc, "codec": _codec_of(enc),
                        "mse": MSE_CODEC[_codec_of(enc)], "ice": turn_ice(),
                        "direct": bool(up.get("ok")), "upnp": up.get("why"),
                        "h": MAX_H or "auto", "br": BITRATE, "fps": FPS,
                        "audio": bool(state.get("audio")),
                        "acodec": state.get("acodec"),
                        "error": None if ok else (state.get("err") or "capture failed to start")})
        elif u.path == "/stop":
            # stop the capture but KEEP the tunnel warm for the next share (instant restart)
            _portal["cancel"].set()          # abandon a share still waiting on the picker
            with lock: stop_capture()
            self._send({"ok": True})
        elif u.path == "/url":
            tun = state.get("cf_url")
            self._send({"whep": share_urls(ip, tun)[0], "lan_ip": ip,
                        "tunnel": bool(tun), "ice": turn_ice()})
        elif u.path == "/ping":
            # cf_gen/ws let the SHARER's plugin notice the watchdog swapped in a new quick
            # tunnel (new random hostname) and re-signal the viewer, instead of leaving it
            # reconnecting to the dead one.
            tun = state.get("cf_url")
            # "streaming" = the WS/fMP4 side has produced an init segment (the encoder is
            # actually emitting media, not just running).
            with ws_lock:
                streaming = fmp4_init["seg"] is not None
            self._send({"ok": True,
                        "running": capture_alive(),
                        "sharing": state.get("geom") is not None,
                        "streaming": streaming,
                        "viewers": len(ws_clients),
                        "cf_gen": state.get("cf_gen", 0),
                        "tunnel": bool(tun),
                        "ws": share_urls(ip, tun)[1],
                        "whep": share_urls(ip, tun)[0],
                        "lan_whep": lan_whep(ip),
                        "direct": bool((state.get("upnp") or {}).get("ok"))})
        else:
            self._send({"ok": False})

    def do_OPTIONS(self):
        # CORS preflight (application/sdp POST from the https Steam page triggers it)
        if not self._guard():
            return
        self.send_response(204)
        self._cors()
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
        if not self._guard():
            return
        u = urlparse(self.path); q = parse_qs(u.query)
        if u.path != "/whep":
            self.send_response(404); self.end_headers(); return
        target = q.get("target", [None])[0]
        if not whep_target_ok(target):
            self.send_response(400); self._cors(); self.end_headers()
            self.wfile.write(b"bad or missing target"); return
        body = _body(self)
        if body is None:
            self.send_response(413); self._cors(); self.end_headers(); return
        try:
            req = urllib.request.Request(target, data=body, method="POST",
                headers={"Content-Type": self.headers.get("Content-Type", "application/sdp")})
            r = _no_redirect_opener.open(req, timeout=12)
            ans, ct, code = r.read(), r.headers.get("Content-Type", "application/sdp"), r.status
        except urllib.error.HTTPError as e:
            ans, ct, code = e.read(), "application/sdp", e.code
        except Exception as e:
            self.send_response(502); self._cors()
            self.end_headers(); self.wfile.write(str(e).encode()); return
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self._cors()
        self.send_header("Content-Length", str(len(ans)))
        self.end_headers(); self.wfile.write(ans)


from http.server import ThreadingHTTPServer


def key_ok(path):
    """The media port is reachable from the internet (tunnel / own domain / LAN), so every
    request must carry the current share's key — handed only to the friend, via chat."""
    want = state.get("key")
    got = parse_qs(urlparse(path).query).get("k", [""])[0]
    return bool(want) and hmac.compare_digest(got.encode(), want.encode())


class MediaH(BaseHTTPRequestHandler):
    timeout = 20             # idle/slow clients can't pin a thread forever (slowloris)

    def log_message(self, *a):
        pass

    def _proxy_whep(self):
        path = urlparse(self.path).path
        if path != "/screen/whep" or not key_ok(self.path):
            self.send_response(404); self.end_headers(); return
        body = _body(self)
        if body is None:
            self.send_response(413); self.end_headers(); return
        try:
            req = urllib.request.Request("http://127.0.0.1:%d/screen/whep" % WHEP_PORT, data=body,
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
        if urlparse(self.path).path == "/screen/ws":
            if not key_ok(self.path):
                self.send_response(404); self.end_headers(); return
            key = self.headers.get("Sec-WebSocket-Key")
            if not key:
                self.send_response(400); self.end_headers(); return
            with ws_lock:
                full = len(ws_clients) >= MAX_WS_VIEWERS
            if full:             # a leaked link can't turn your upload into a broadcast
                self.send_response(503); self.end_headers(); return
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", _ws_accept(key))
            self.end_headers()
            sock = self.connection
            sock.settimeout(15)
            try:                         # fragments are latency-critical; don't let Nagle sit on them
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except Exception:
                pass
            client = WsClient(sock)
            # Queue the init segment BEFORE publishing the client, so the writer thread
            # always emits ftyp+moov ahead of any fragment ws_broadcast hands it.
            with ws_lock:
                seg = fmp4_init["seg"]
            if seg:
                client.send(_ws_frame(seg))
            with ws_lock:
                ws_clients.add(client)
            try:
                while client.alive:
                    try:
                        if not sock.recv(4096):
                            break
                    except socket.timeout:
                        continue
                    except Exception:
                        break
            finally:
                with ws_lock:
                    ws_clients.discard(client)
                client.close()
            return
        if self.path.startswith("/screen/whep"):
            self._proxy_whep(); return
        self.send_response(404); self.end_headers()


def start_media_server():
    srv = ThreadingHTTPServer(("0.0.0.0", MEDIA_PORT), MediaH)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _run(cmd, timeout=40):
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return p.returncode, (p.stdout or b""), (p.stderr or b"")
    except Exception as e:
        return -1, b"", str(e).encode()


def _ffprobe_streams(path):
    rc, out, err = _run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name",
                         "-of", "csv=p=0", path], timeout=15)
    return out.decode(errors="replace").strip().replace("\n", " | ") if rc == 0 else "ffprobe n/a"


def _win_audio_rms():
    """Capture ~1s of WASAPI loopback and return RMS level — ~0 = silence (nothing playing or
    capture broken), >50ish = real audio. Answers 'does desktop-audio capture actually work'."""
    import math
    dev = _wasapi_device()
    if not dev:
        return None, "no loopback device"
    try:
        import pyaudiowpatch as pa
        p = pa.PyAudio()
        s = p.open(format=pa.paInt16, channels=dev["ch"], rate=dev["rate"],
                   frames_per_buffer=1024, input=True, input_device_index=dev["index"])
        raw = b""
        for _ in range(max(1, int(dev["rate"] / 1024))):
            raw += s.read(1024, exception_on_overflow=False)
        s.close(); p.terminate()
        n = len(raw) // 2
        if not n:
            return 0.0, "no samples"
        sm = struct.unpack("<%dh" % n, raw[:n * 2])
        rms = math.sqrt(sum(x * x for x in sm) / n)
        return rms, "%dHz %dch" % (dev["rate"], dev["ch"])
    except Exception as e:
        return None, "capture error: %s" % e


def selftest():
    """Run over SSH (`python rp-webrtc.py --selftest`) to test the capture/encode/audio pipeline
    end-to-end and print PASS/FAIL. Does NOT need the server running. The viewer/NC side lives in
    Steam's CEF and can't be tested from here."""
    L = lambda *a: print(*a, flush=True)
    L("=== rp-webrtc self-test ===")
    L("OS=%s  python=%s  workdir=%s" % (sys.platform, sys.version.split()[0], HERE))
    rc, out, err = _run([ffmpeg_bin(), "-version"], timeout=10)
    L("ffmpeg: %s" % (out.decode(errors="replace").splitlines()[0] if rc == 0 else "MISSING/ERROR " + err.decode(errors="replace")[:160]))
    L("mediamtx: %s" % ("ok" if os.path.exists(MEDIAMTX) else "MISSING " + MEDIAMTX))
    L("cloudflared: %s" % ("ok" if os.path.exists(CLOUDFLARED) else "missing (off-LAN sharing only)"))
    # sources
    try: mons = monitors()
    except Exception as e: mons = []; L("monitors() ERROR: %s" % e)
    try: wins = windows()
    except Exception as e: wins = []; L("windows() ERROR: %s" % e)
    try: cams = cameras()
    except Exception as e: cams = []; L("cameras() ERROR: %s" % e)
    L("sources: %d monitors, %d windows, %d cameras" % (len(mons), len(wins), len(cams)))
    for m in mons: L("   monitor %s %s%s" % (m["name"], m["geom"], " *primary" if m.get("primary") else ""))
    for c in cams: L("   camera  %s" % c["name"])
    # encoders
    avail = _ffmpeg_encoders()
    for name in ["h264_nvenc", "h264_qsv", "h264_amf", "h264_vaapi", "libx264"]:
        if name in avail:
            L("   probe %-12s %s" % (name, "WORKS" if _probe_encoder(name) else "fails"))
    L("chosen encoder: %s" % _working_encoder())
    # real capture + encode (primary monitor, 2s) — the actual share pipeline
    geom = (next((m for m in mons if m.get("primary")), mons[0])["geom"] if mons else None)
    if is_wayland():
        L("session: WAYLAND -> capture via xdg-desktop-portal ScreenCast + PipeWire")
        try:
            import dbus, gi  # noqa: F401
            L("   python dbus/gi: OK")
        except Exception as e:
            L("   python dbus/gi: MISSING (%s) -> install python3-dbus python3-gi" % e)
        pw = subprocess.run(["gst-inspect-1.0", "pipewiresrc"],
                            capture_output=True).returncode == 0
        L("   gst pipewiresrc: %s" % ("OK" if pw else "MISSING -> install gstreamer1.0-pipewire"))
        toks = _tokens_load()
        L("   saved portal tokens: %s" % (", ".join(sorted(toks)) if toks
                                          else "none (first share shows the system picker once)"))
        L("capture+encode: SKIP (portal capture needs an interactive picker; start a share to test)")
    elif geom:
        mm = GEOM_RE.match(geom); w, h, x, y = mm.groups()
        outp = os.path.join(tempfile.gettempdir(), "rp-selftest.mp4")
        if IS_WIN:
            cap = ["-f", "gdigrab", "-framerate", "15", "-offset_x", x, "-offset_y", y,
                   "-video_size", "%sx%s" % (w, h), "-i", "desktop"]
        else:
            cap = ["-f", "x11grab", "-framerate", "15", "-video_size", "%sx%s" % (w, h),
                   "-i", "%s+%s,%s" % (DISPLAY, x, y)]
        name, in_args, enc_args = pick_encoder()
        cmd = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y"] + cap + _apply_scale(in_args) + enc_args + ["-t", "2", outp]
        rc, o, e = _run(cmd, timeout=60)
        if rc == 0 and os.path.exists(outp) and os.path.getsize(outp) > 1000:
            L("capture+encode: PASS (%d bytes, streams: %s)" % (os.path.getsize(outp), _ffprobe_streams(outp)))
        else:
            L("capture+encode: FAIL rc=%d :: %s" % (rc, e.decode(errors="replace")[:400]))
            L("   cmd was: %s" % " ".join(cmd))
    else:
        L("capture+encode: SKIP (no monitor)")
    # audio
    if IS_WIN:
        # What a share actually uses: WASAPI process loopback (no packages needed).
        try:
            pid = _steam_audio_pid()
            cap = WasapiExcludeSteam(pid or os.getpid())
            time.sleep(1.0)
            n = cap.get_read_available(); raw = cap.read(n); cap.stop()
            sm = struct.unpack("<%dh" % (len(raw) // 2), raw) if raw else ()
            rms = (sum(x * x for x in sm) / len(sm)) ** 0.5 if sm else 0.0
            L("audio: process loopback OK (%s), %d frames/s, RMS=%.0f -> %s" % (
                "excluding Steam pid %d" % pid if pid else "Steam not running",
                n, rms, "SILENT (nothing playing?)" if rms < 5 else "CAPTURING"))
        except Exception as ex:
            L("audio: process loopback UNAVAILABLE (%s) -> whole-desktop fallback" % ex)
            try:
                import pyaudiowpatch  # noqa: F401
                rms, info = _win_audio_rms()
                L("   fallback WASAPI loopback: %s" % (info if rms is None else "%s RMS=%.0f" % (info, rms)))
            except Exception as ex2:
                L("   fallback: pyaudiowpatch MISSING (%s)" % ex2)
    else:
        L("audio: pulse monitor = %s" % (_pulse_monitor() or "NONE"))
    if not IS_WIN:
        ai, win_pcm = _audio_input()
        L("audio input args: %s" % (" ".join(ai) if ai else "[] (none -> video only)"))
    L("=== self-test done ===")


def _already_running():
    """True if another instance already owns the control port. Lets any number of things
    (systemd service, Windows logon task, the plugin's Lua backend, a manual run) try to
    start us without stepping on each other — the duplicate just exits 0 quietly instead of
    dying on EADDRINUSE with a traceback."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/ping" % PORT, timeout=2) as r:
            return json.load(r).get("ok") is True
    except Exception:
        return False


def main():
    if _already_running():
        print("rp-webrtc already running on 127.0.0.1:%d - nothing to do" % PORT)
        return
    # Threaded: a share start can block briefly (Wayland portal negotiation, encoder
    # spin-up), and a single-threaded server would stall /ping and the whole plugin
    # behind it. start_capture holds its own lock, so concurrent /start is still safe.
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    except OSError as e:
        # lost a startup race with another instance between the check and the bind
        print("rp-webrtc: control port %d busy (%s); assuming another instance won" % (PORT, e))
        return
    srv.daemon_threads = True
    def shutdown(*a):
        # Don't BLOCK on the lock — if a /start is mid-flight we still want to exit
        # promptly (else systemd waits out its stop timeout and SIGKILLs us).
        got = lock.acquire(timeout=2)
        try:
            stop_capture(); stop_tunnel(); _kill(state.get("mtx")); upnp_release()
        except Exception:
            pass
        finally:
            if got:
                lock.release()
        os._exit(0)
    signal.signal(signal.SIGTERM, shutdown); signal.signal(signal.SIGINT, shutdown)
    print("rp-webrtc daemon on 127.0.0.1:%d (encoder=%s, lan=%s, tunnel=%s, capture=%s)"
          % (PORT, pick_encoder()[0], lan_ip(), TUNNEL,
             "portal/pipewire" if is_wayland() else ("gdigrab" if IS_WIN else "x11")))
    # Pre-warm the slow bits in the background so the FIRST share is fast: MediaMTX (so it's
    # already listening) and the cloudflared tunnel (so its https URL is ready). Both are
    # otherwise built lazily on the first /start, which is what made sharing slow to begin.
    def prewarm():
        try: ensure_mediamtx()
        except Exception: pass
        if TUNNEL != "off" and not public_url():      # own domain -> no tunnel needed
            try: ensure_tunnel()
            except Exception: pass
    threading.Thread(target=prewarm, daemon=True).start()
    threading.Thread(target=_tunnel_watchdog, daemon=True).start()
    threading.Thread(target=upnp_keep, daemon=True).start()
    start_media_server()
    srv.serve_forever()


if __name__ == "__main__":
    if "--selftest" in sys.argv or "--diag" in sys.argv:
        selftest()
    else:
        main()
