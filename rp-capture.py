#!/usr/bin/env python3
# Stable Remote Play capture host for the Discord-ish screen share.
#
# Launched AS Spacewar (appid 480) via hijacked launch options, so Steam tracks
# THIS process as 480 and Remote Play Together streams the ffplay window it owns.
# It stays alive for the whole share; a tiny localhost control server lets the
# plugin switch which monitor/app is captured (and the resolution) on the fly by
# restarting only the ffplay child — the appid-480 process (this script) never
# exits, so the RPT session/group is never dropped and the friend needn't re-accept.
#
# Endpoints (GET, JSON):
#   /sources                  -> {monitors:[{name,geom,primary}], windows:[{id,x,y,w,h,title}]}
#   /set?geom=WxH+X+Y[&res=WxH|none]  -> capture that region (a monitor or a window), restart ffplay
#   /res?v=WxH|none           -> set output resolution for the next /set
#   /ping                     -> {ok:true}
import sys, os, subprocess, threading, json, signal, shutil
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

PORT = 48591
DISPLAY = os.environ.get("DISPLAY", ":0")
state = {"proc": None, "res": "1920x1080", "geom": None}
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
        if len(f) >= 8 and f[7] != "Discord-ish Screen Share":
            wins.append({"id": f[0], "x": int(f[2]), "y": int(f[3]),
                         "w": int(f[4]), "h": int(f[5]), "title": f[7]})
    return wins


def pick_view(geom):
    # place the ffplay window on a monitor whose X-origin differs from the captured
    # region, so we don't capture our own mirror (feedback). Falls back to 0,0.
    try:
        cx = int(geom.split("+")[1])
    except Exception:
        cx = 0
    for m in monitors():
        mx = int(m["geom"].split("+")[1])
        if mx != cx:
            return mx, int(m["geom"].split("+")[2])
    return 0, 0


def stop_capture():
    p = state.get("proc")
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=2)
        except Exception:
            p.kill()
    state["proc"] = None


def start_capture(geom):
    with lock:
        stop_capture()
        state["geom"] = geom
        size = geom.split("+")[0]
        gx, gy = geom.split("+")[1], geom.split("+")[2]
        vx, vy = pick_view(geom)
        vf = [] if state["res"] in (None, "none", "") else ["-vf", "scale=" + state["res"].replace("x", ":")]
        cmd = ["ffplay", "-loglevel", "error", "-f", "x11grab", "-framerate", "30",
               "-video_size", size, "-i", "%s+%s,%s" % (DISPLAY, gx, gy)] + vf + \
              ["-an", "-noborder", "-left", str(vx), "-top", str(vy),
               "-window_title", "Discord-ish Screen Share"]
        state["proc"] = subprocess.Popen(cmd)


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
            if state["geom"]:
                start_capture(state["geom"])      # re-apply with new resolution
            self._send({"ok": True, "res": state["res"]})
        elif u.path == "/set":
            geom = q.get("geom", [None])[0]
            if "res" in q:
                state["res"] = q["res"][0]
            if geom:
                start_capture(geom)
            self._send({"ok": bool(geom), "geom": geom})
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
    start_capture(geom)
    srv = HTTPServer(("127.0.0.1", PORT), H)

    def shutdown(*a):
        # Don't call srv.shutdown() here — this handler runs in the same thread as
        # serve_forever(), so shutdown() would deadlock. Kill the ffplay child and
        # exit hard; Steam SIGTERMs us on TerminateApp.
        stop_capture()
        os._exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    srv.serve_forever()


main()
