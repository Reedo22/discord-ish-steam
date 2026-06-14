#!/usr/bin/env python3
# Stable Remote Play capture host for the Discord-ish screen share (cross-platform).
#
# A long-lived control server that owns the capture window Remote Play streams.
# It stays alive for the whole share so the RPT session is never dropped; the
# plugin switches monitor / app / resolution live over a localhost HTTP API by
# restarting only the ffplay (capture) child.
#
# Linux:  launched AS Spacewar (appid 480) via a launch-options hijack; the JS
#         side creates the RPT invite. Monitor = ffplay x11grab; app = gst
#         ximagesrc xid | ffplay (occlusion-proof via XComposite/KWin).
# Windows: launched as a non-Steam shortcut; on start it runs RemotePlayWhatever
#         (-a 480 -i <sid>) to force the RPT session + invite. Monitor + app both
#         use ffplay -f gdigrab (-i desktop region / -i title=<title>).
#
# Args:  <geom|primary|secondary>  <res WxH|none>  [invite_steamid64]
# API (GET, JSON):
#   /sources                 -> {monitors:[{name,geom,primary}], windows:[{id,title,...}]}
#   /set?geom=WxH+X+Y[&res=] -> capture a monitor region
#   /set?win=<id>[&res=]     -> capture a window (id = X11 xid on Linux, title on Windows)
#   /res?v=WxH|none          -> set output resolution, re-apply current source
#   /ping                    -> {ok:true}
import sys, os, subprocess, threading, json, signal, shutil

IS_WIN = sys.platform.startswith("win")
PORT = 48591
DISPLAY = os.environ.get("DISPLAY", ":0")
RPW = os.environ.get("RPW_PATH", "")          # RemotePlayWhatever path (Windows)
state = {"proc": None, "res": "1920x1080", "kind": None, "value": None}
lock = threading.Lock()

from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs


# ---- source enumeration ----------------------------------------------------
def monitors():
    if IS_WIN:
        import ctypes
        from ctypes import wintypes
        out = []
        MEP = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.POINTER(wintypes.RECT), ctypes.c_double)

        def cb(hMon, hdc, lprc, data):
            r = lprc.contents
            out.append({"name": "Display %d" % (len(out) + 1),
                        "geom": "%dx%d+%d+%d" % (r.right - r.left, r.bottom - r.top, r.left, r.top),
                        "primary": (r.left == 0 and r.top == 0)})
            return 1
        ctypes.windll.user32.EnumDisplayMonitors(0, 0, MEP(cb), 0)
        return out
    try:
        xr = subprocess.run(["xrandr", "--query"], capture_output=True, text=True).stdout
    except Exception:
        return []
    out = []
    for line in xr.splitlines():
        if " connected" in line:
            parts = line.split()
            geom = next((p for p in parts if "x" in p and "+" in p), None)
            if geom:
                out.append({"name": parts[0], "geom": geom, "primary": "primary" in line})
    return out


def windows():
    if IS_WIN:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        out = []
        EWP = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_ulong, ctypes.c_long)

        def cb(hwnd, lparam):
            if not user32.IsWindowVisible(hwnd):
                return 1
            n = user32.GetWindowTextLengthW(hwnd)
            if not n:
                return 1
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            title = buf.value
            if not title or title in ("Discord-ish Screen Share", "Program Manager"):
                return 1
            r = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            w, h = r.right - r.left, r.bottom - r.top
            if w < 80 or h < 80:
                return 1
            out.append({"id": title, "title": title, "x": r.left, "y": r.top, "w": w, "h": h})  # id = title (gdigrab key)
            return 1
        user32.EnumWindows(EWP(cb), 0)
        return out
    if not shutil.which("wmctrl"):
        return []
    try:
        wm = subprocess.run(["wmctrl", "-lG"], capture_output=True, text=True).stdout
    except Exception:
        return []
    out = []
    for line in wm.splitlines():
        f = line.split(None, 7)            # id desktop x y w h host title
        if len(f) >= 8 and f[7] not in ("Discord-ish Screen Share",) and not f[7].startswith("Desktop @") and f[7] not in ("Plasma", "Desktop"):
            out.append({"id": f[0], "title": f[7], "x": int(f[2]), "y": int(f[3]), "w": int(f[4]), "h": int(f[5])})
    return out


def resolve_geom(g):
    # accept "primary"/"secondary" keywords or an explicit WxH+X+Y
    if g and "+" in g:
        return g
    mons = monitors()
    if not mons:
        return "1920x1080+0+0"
    if g == "secondary" and len(mons) > 1:
        return mons[1]["geom"]
    prim = next((m for m in mons if m.get("primary")), mons[0])
    return prim["geom"]


def pick_view(geom):
    try:
        cx = int(geom.split("+")[1])
    except Exception:
        cx = 0
    for m in monitors():
        try:
            mx = int(m["geom"].split("+")[1])
        except Exception:
            continue
        if mx != cx:
            return mx, int(m["geom"].split("+")[2])
    return 0, 0


# ---- capture ---------------------------------------------------------------
def _scale_ffplay():
    return [] if state["res"] in (None, "none", "") else ["-vf", "scale=" + state["res"].replace("x", ":")]


def _scale_caps_gst():
    if state["res"] in (None, "none", ""):
        return ""
    w, h = state["res"].split("x")
    return "videoscale ! video/x-raw,width=%s,height=%s ! " % (w, h)


def stop_capture():
    p = state.get("proc")
    if p and p.poll() is None:
        try:
            if IS_WIN:
                p.terminate()
            else:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except Exception:
            try:
                p.terminate()
            except Exception:
                pass
        try:
            p.wait(timeout=2)
        except Exception:
            try:
                if IS_WIN:
                    p.kill()
                else:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception:
                pass
    state["proc"] = None


def _popen(cmd, shell=False):
    if IS_WIN:
        return subprocess.Popen(cmd, shell=shell)
    return subprocess.Popen(cmd, shell=shell, start_new_session=True)


def start_capture(kind, value):
    with lock:
        stop_capture()
        state["kind"], state["value"] = kind, value
        if IS_WIN:
            tail = ["-an", "-noborder", "-window_title", "Discord-ish Screen Share"]
            if kind == "window":
                cmd = ["ffplay", "-loglevel", "error", "-f", "gdigrab", "-framerate", "30",
                       "-i", "title=" + value] + _scale_ffplay() + tail
            else:
                size = value.split("+")[0]
                gx, gy = value.split("+")[1], value.split("+")[2]
                cmd = ["ffplay", "-loglevel", "error", "-f", "gdigrab", "-framerate", "30",
                       "-offset_x", gx, "-offset_y", gy, "-video_size", size, "-i", "desktop"] + _scale_ffplay() + tail
            state["proc"] = _popen(cmd)
        else:
            if kind == "window":
                pipe = ("gst-launch-1.0 -q ximagesrc xid=%s use-damage=false ! videoconvert ! %smatroskamux streamable=true ! fdsink fd=1 "
                        "| ffplay -loglevel error -f matroska -i - -an -noborder -left 100 -top 100 -window_title \"Discord-ish Screen Share\"") % (value, _scale_caps_gst())
                state["proc"] = _popen(pipe, shell=True)
            else:
                size = value.split("+")[0]
                gx, gy = value.split("+")[1], value.split("+")[2]
                vx, vy = pick_view(value)
                cmd = ["ffplay", "-loglevel", "error", "-f", "x11grab", "-framerate", "30",
                       "-video_size", size, "-i", "%s+%s,%s" % (DISPLAY, gx, gy)] + _scale_ffplay() + \
                      ["-an", "-noborder", "-left", str(vx), "-top", str(vy), "-window_title", "Discord-ish Screen Share"]
                state["proc"] = _popen(cmd)


# ---- control server --------------------------------------------------------
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/sources":
            self._send({"monitors": monitors(), "windows": windows()})
        elif u.path == "/res":
            state["res"] = q.get("v", ["1920x1080"])[0]
            if state.get("kind"):
                start_capture(state["kind"], state["value"])
            self._send({"ok": True, "res": state["res"]})
        elif u.path == "/set":
            if "res" in q:
                state["res"] = q["res"][0]
            if "win" in q:
                start_capture("window", q["win"][0])
                self._send({"ok": True, "window": q["win"][0]})
            elif "geom" in q:
                start_capture("monitor", resolve_geom(q["geom"][0]))
                self._send({"ok": True, "geom": q["geom"][0]})
            else:
                self._send({"ok": False})
        elif u.path == "/ping":
            self._send({"ok": True})
        else:
            self._send({"ok": False})


def main():
    geom = resolve_geom(sys.argv[1] if len(sys.argv) > 1 else "primary")
    if len(sys.argv) > 2:
        state["res"] = sys.argv[2]
    invite = sys.argv[3] if len(sys.argv) > 3 else ""

    # Windows: force the RPT session + invite via RemotePlayWhatever (Linux does
    # this from the plugin via the launch hijack + native CreateInviteAndSession).
    if IS_WIN and invite and invite != "none" and RPW and os.path.exists(RPW):
        try:
            subprocess.Popen([RPW, "-a", "480", "-i", invite])
        except Exception:
            pass

    start_capture("monitor", geom)
    srv = HTTPServer(("127.0.0.1", PORT), H)

    def shutdown(*a):
        stop_capture()
        os._exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    srv.serve_forever()


main()
