#!/usr/bin/env python3
"""Minimal Chrome DevTools Protocol client for Steam's CEF (port 36377).

Usage:
  cdp.py list                       # list targets
  cdp.py eval <title-substr> <js>   # Runtime.evaluate JS in the matching target, print result
"""
import sys, json, asyncio, urllib.request, subprocess, re
import websockets

def _detect_port():
    # Explicit override (e.g. the sandboxed guest Steam on CEF's marker-file port 8080,
    # which has no remote-debugging-port= flag in its cmdline for pgrep to find).
    import os
    if os.environ.get("DS_CDP_PORT"):
        return int(os.environ["DS_CDP_PORT"])
    try:
        out = subprocess.check_output(["pgrep", "-af", "steamwebhelper"], text=True)
        m = re.search(r"remote-debugging-port=(\d+)", out)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return 36377

PORT = _detect_port()
BASE = f"http://127.0.0.1:{PORT}"

def targets():
    with urllib.request.urlopen(f"{BASE}/json") as r:
        return json.load(r)

def find(substr):
    substr = substr.lower()
    for t in targets():
        hay = (t.get("title", "") + " " + t.get("url", "")).lower()
        if substr in hay and t.get("webSocketDebuggerUrl"):
            return t
    return None

async def evaluate(ws_url, expression):
    # No Origin header sent -> avoids --remote-allow-origins rejection.
    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
        await ws.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True, "awaitPromise": True},
        }))
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("id") == 1:
                return msg

def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "list":
        for t in targets():
            print(f"{t.get('type'):8} | {t.get('title','?')[:50]:50} | {t.get('url','')[:60]}")
        return
    if len(sys.argv) >= 4 and sys.argv[1] == "eval":
        t = find(sys.argv[2])
        if not t:
            print(f"NO TARGET matching {sys.argv[2]!r}", file=sys.stderr)
            sys.exit(2)
        print(f"# target: {t['title']}", file=sys.stderr)
        res = asyncio.run(evaluate(t["webSocketDebuggerUrl"], sys.argv[3]))
        r = res.get("result", {}).get("result", {})
        if "value" in r:
            v = r["value"]
            print(v if isinstance(v, str) else json.dumps(v, indent=2))
        else:
            print(json.dumps(res, indent=2))
        return
    print(__doc__)
    sys.exit(1)

if __name__ == "__main__":
    main()
