#!/usr/bin/env python3
# Stable Remote Play capture host for the Discord-ish screen share.
#
# Launched AS Spacewar (appid 480) via hijacked launch options, so Steam tracks
# THIS process as 480 and Remote Play Together streams the ffplay window it owns.
# It stays alive for the whole share; a tiny localhost control server lets the
# plugin switch which monitor/app is captured (and the resolution) on the fly by
# restarting only the capture child — the appid-480 process (this script) never
# exits, so the RPT session/group is never dropped and the friend needn't re-accept.
#
# Two capture kinds:
#   monitor  -> ffplay x11grab of a screen region (whole monitor).
#   window   -> gst ximagesrc xid=... | ffplay. Captures the window's COMPOSITED
#               backing pixmap (KWin/XComposite), so it works even when the app is
#               behind other windows — no occlusion. Some GL/ARGB windows can't be
#               XGetImage'd (BadMatch); those just won't show (pick another).
#
# Endpoints (GET, JSON):
#   /sources                 -> {monitors:[{name,geom,primary}], windows:[{id,x,y,w,h,title}]}
#   /set?geom=WxH+X+Y[&res=] -> capture that monitor region
#   /set?xid=0xID[&res=]     -> capture that window (occlusion-proof)
#   /res?v=WxH|none          -> set output resolution, re-apply current source
#   /ping                    -> {ok:true}
import sys, os, subprocess, threading, json, signal, shutil
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

PORT = 48591
DISPLAY = os.environ.get("DISPLAY", ":0")
state = {"proc": None, "res": "1920x1080", "kind": None, "value": None}
lock = threading.Lock()


def monitors():
    try:
        out = subprocess.run(["xrandr", "--query"], capture_output=True, text=True).stdout
    except Exception:
        return []
    mons = []
    for line in out.splitlines():
        if " connected" in line:
            parts = line.split()
            geom = next((p for p in parts if "x" in p and "+" in p), None)
            if geom:
                mons.append({"name": parts[0], "geom": geom, "primary": "primary" in line})
    return mons


def windows():
    if not shutil.which("wmctrl"):
        return []
    try:
        out = subprocess.run(["wmctrl", "-lG"], capture_output=True, text=True).stdout
    except Exception:
        return []
    wins = []
    for line in out.splitlines():
        f = line.split(None, 7)            # id desktop x y w h host title
        if len(f) >= 8 and f[7] not in ("Discord-ish Screen Share", "Desktop", "Plasma") and not f[7].startswith("Desktop @"):
            wins.append({"id": f[0], "x": int(f[2]), "y": int(f[3]),
                         "w": int(f[4]), "h": int(f[5]), "title": f[7]})
    return wins


def pick_view(geom):
    # place the ffplay window on a monitor whose X-origin differs from a captured
    # region, so monitor capture doesn't grab its own mirror. Falls back to 0,0.
    try:
        cx = int(geom.split("+")[1])
    except Exception:
        cx = 0
    for m in monitors():
        mx = int(m["geom"].split("+")[1])
        if mx != cx:
            return mx, int(m["geom"].split("+")[2])
    return 0, 0


def _ffplay_tail(vx, vy):
    return '-an -noborder -left %d -top %d -window_title "Discord-ish Screen Share"' % (vx, vy)


def stop_capture():
    p = state.get("proc")
    if p and p.poll() is None:
        try:
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
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception:
                pass
    state["proc"] = None


def _scale_filter_ffplay():
    return [] if state["res"] in (None, "none", "") else ["-vf", "scale=" + state["res"].replace("x", ":")]


def _scale_caps_gst():
    if state["res"] in (None, "none", ""):
        return ""
    w, h = state["res"].split("x")
    return "videoscale ! video/x-raw,width=%s,height=%s ! " % (w, h)


def start_capture(kind, value):
    with lock:
        stop_capture()
        state["kind"], state["value"] = kind, value
        if kind == "window":
            # occlusion-proof: gst grabs the window's composited pixmap, ffplay shows it
            # (ffplay is the surface Remote Play captures). Window pixmap != screen region,
            # so the ffplay window can sit anywhere without feedback.
            pipe = ("gst-launch-1.0 -q ximagesrc xid=%s use-damage=false ! videoconvert ! %smatroskamux streamable=true ! fdsink fd=1 "
                    "| ffplay -loglevel error -f matroska -i - %s") % (value, _scale_caps_gst(), _ffplay_tail(100, 100))
            state["proc"] = subprocess.Popen(pipe, shell=True, start_new_session=True)
        else:
            size = value.split("+")[0]
            gx, gy = value.split("+")[1], value.split("+")[2]
            vx, vy = pick_view(value)
            cmd = ["ffplay", "-loglevel", "error", "-f", "x11grab", "-framerate", "30",
                   "-video_size", size, "-i", "%s+%s,%s" % (DISPLAY, gx, gy)] + _scale_filter_ffplay() + \
                  ["-an", "-noborder", "-left", str(vx), "-top", str(vy), "-window_title", "Discord-ish Screen Share"]
            state["proc"] = subprocess.Popen(cmd, start_new_session=True)


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
            if "xid" in q:
                start_capture("window", q["xid"][0])
                self._send({"ok": True, "window": q["xid"][0]})
            elif "geom" in q:
                start_capture("monitor", q["geom"][0])
                self._send({"ok": True, "geom": q["geom"][0]})
            else:
                self._send({"ok": False})
        elif u.path == "/ping":
            self._send({"ok": True})
        else:
            self._send({"ok": False})


def main():
    geom = sys.argv[1] if len(sys.argv) > 1 else None
    if not geom:
        m = monitors()
        geom = m[0]["geom"] if m else "1920x1080+0+0"
    if len(sys.argv) > 2:
        state["res"] = sys.argv[2]
    start_capture("monitor", geom)
    srv = HTTPServer(("127.0.0.1", PORT), H)

    def shutdown(*a):
        # runs in the serve_forever thread — don't call srv.shutdown() (deadlocks);
        # kill the capture child group and exit hard. Steam SIGTERMs us on TerminateApp.
        stop_capture()
        os._exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    srv.serve_forever()


main()
