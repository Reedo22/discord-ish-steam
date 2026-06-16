#!/usr/bin/env python3
"""Deploy the live plugin index.js into a running Steam CEF context via CDP.
Resets the boot guard and evals the bundled source (stripped of ES export),
so window.__ds_VERSION reflects the freshly-loaded copy. Robust to large
payloads by sending over the websocket with json.dumps (no shell argv limit).

Usage: deploy_js.py [target-substr]   (default: SharedJSContext)
"""
import sys, json, asyncio, urllib.request, pathlib, subprocess, re
import websockets

def _detect_port():
    try:
        out = subprocess.check_output(["pgrep", "-af", "steamwebhelper"], text=True)
        m = re.search(r"remote-debugging-port=(\d+)", out)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return 36377

PORT = _detect_port()
TARGET = sys.argv[1] if len(sys.argv) > 1 else "SharedJSContext"
JS = pathlib.Path(__file__).resolve().parent.parent / "plugin" / ".millennium" / "Dist" / "index.js"

def find():
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json") as r:
        for t in json.load(r):
            if TARGET.lower() in (t.get("title", "")).lower() and t.get("webSocketDebuggerUrl"):
                return t
    return None

async def main():
    t = find()
    if not t:
        print(f"NO target matching {TARGET!r}", file=sys.stderr); sys.exit(2)
    src = JS.read_text()
    src = re.sub(r"(?m)^export\s+default[\s\S]*$", "", src)
    expr = (
        "(() => {"
        "  try { window.__DISCORDISH_BOOTED__ = false; } catch(e){}"
        f"  (0, eval)({json.dumps(src)});"
        "  return 'deployed v' + (window.__ds_VERSION || '?') + ' into ' + document.title;"
        "})()"
    )
    async with websockets.connect(t["webSocketDebuggerUrl"], max_size=64*1024*1024) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                  "params": {"expression": expr, "returnByValue": True}}))
        while True:
            m = json.loads(await ws.recv())
            if m.get("id") == 1:
                res = m.get("result", {})
                if res.get("exceptionDetails"):
                    print("EXCEPTION:", json.dumps(res["exceptionDetails"])[:400], file=sys.stderr); sys.exit(1)
                print(res.get("result", {}).get("value", m)); return

asyncio.run(main())
