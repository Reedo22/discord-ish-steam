#!/usr/bin/env python3
"""Inject (or refresh) theme/friends.custom.css into the live docked window
for instant preview. Bakes nothing — just a <style id=reskin-preview> tag."""
import sys, json, asyncio, urllib.request, pathlib
import websockets

PORT = 36377
TITLE_SUBSTR = sys.argv[1] if len(sys.argv) > 1 else "Friends List -"
CSS = pathlib.Path(__file__).resolve().parent.parent / "theme" / "friends.custom.css"

def find():
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json") as r:
        for t in json.load(r):
            if TITLE_SUBSTR.lower() in (t.get("title", "")).lower() and t.get("webSocketDebuggerUrl"):
                return t
    return None

async def main():
    t = find()
    if not t:
        print(f"NO target matching {TITLE_SUBSTR!r}", file=sys.stderr); sys.exit(2)
    css = CSS.read_text()
    expr = (
        "(() => {"
        "  let s = document.getElementById('reskin-preview');"
        "  if (!s) { s = document.createElement('style'); s.id='reskin-preview';"
        "            document.head.appendChild(s); }"
        f"  s.textContent = {json.dumps(css)};"
        "  return 'injected ' + s.textContent.length + ' chars into ' + document.title;"
        "})()"
    )
    async with websockets.connect(t["webSocketDebuggerUrl"], max_size=64*1024*1024) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                  "params": {"expression": expr, "returnByValue": True}}))
        while True:
            m = json.loads(await ws.recv())
            if m.get("id") == 1:
                print(m.get("result", {}).get("result", {}).get("value", m)); return

asyncio.run(main())
