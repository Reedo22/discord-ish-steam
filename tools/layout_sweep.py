#!/usr/bin/env python3
"""Render Steam's friends/chat window at many sizes and check the reskin's layout.

  DS_CDP_PORT=28080 python3 tools/layout_sweep.py [outdir]

Uses CDP device-metrics emulation on the live "Friends List" page, so no real window is
resized. CSS width is what drives layout, so 1280 CSS px at 150% scaling is the same test as
a 1920 px screen. For each size it runs the checks in CHECK_JS, writes a screenshot, and
exits non-zero if anything failed.
"""
import asyncio, base64, json, os, sys, urllib.request
import websockets

PORT = int(os.environ.get("DS_CDP_PORT", "8080"))
SIZES = [(400, 500), (520, 600), (640, 480), (800, 600), (1024, 768), (1280, 720),
         (1366, 768), (1600, 900), (1920, 1080), (2560, 1440), (3440, 1440), (3840, 2160), (700, 1300)]

CHECK_JS = r"""
(function () {
  var W = innerWidth, H = innerHeight, bad = [];
  function box(sel) { var e = document.querySelector(sel); if (!e) return null;
    var r = e.getBoundingClientRect(); return r.width && r.height ? r : null; }
  function inView(name, sel) { var r = box(sel);
    if (!r) { bad.push(name + " missing"); return null; }
    if (r.left < -1 || r.top < -1 || r.right > W + 1 || r.bottom > H + 1)
      bad.push(name + " off-screen " + JSON.stringify([r.left|0, r.top|0, r.right|0, r.bottom|0]));
    return r; }
  function overlap(a, b) { return a && b && a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1; }
  if (document.documentElement.scrollWidth > W + 1) bad.push("page scrolls sideways " + document.documentElement.scrollWidth);
  var ta = inView("message box", ".chatEntry textarea");
  var send = inView("send", ".chatEntry button[type=submit]");
  var emo = inView("emoji", '.chatEntry button[title="Emoticon Picker"]');
  var att = inView("attach", '.chatEntry button[title="Send Special"]');
  var voice = inView("voice", ".chatEntry .ChatMessageEntryVoice button");
  var close = inView("close button", ".ChatTabs .closeButton");
  inView("chat title", ".ChatTabs .ChatTab");
  var rail = box(".friendsListContainer");
  if (rail) { inView("theme button", ".ds-theme-btn"); inView("add friend", ".friendsTabButtonsContainer .addFriendButton"); }
  if (ta && ta.width < 80) bad.push("message box only " + (ta.width|0) + "px wide");
  var row = [["message box", ta], ["send", send], ["emoji", emo], ["attach", att], ["voice", voice]];
  for (var i = 0; i < row.length; i++) for (var j = i + 1; j < row.length; j++)
    if (overlap(row[i][1], row[j][1])) bad.push(row[i][0] + " overlaps " + row[j][0]);
  [emo, att, voice].forEach(function (r, i) { if (r && Math.abs(r.width - r.height) > 2) bad.push(["emoji","attach","voice"][i] + " not round " + (r.width|0) + "x" + (r.height|0)); });
  if (emo && voice && Math.abs((emo.top + emo.bottom) - (voice.top + voice.bottom)) > 3) bad.push("voice not aligned with emoji");
  var title = box(".ChatTabs .ChatTab"), ctl = box(".ChatTabs .window-controls");
  if (overlap(title, ctl)) bad.push("chat title under window controls");
  // anything of ours wider than the window
  document.querySelectorAll("[class*=ds-]").forEach(function (e) { var r = e.getBoundingClientRect();
    if (r.width && r.right > W + 1) bad.push(e.className.split(" ")[0] + " past right edge"); });
  return JSON.stringify({ W: W, H: H, rail: rail ? rail.width|0 : 0, bad: bad });
})()
"""

PANEL_JS = r"""
(function () {
  var b = document.querySelector(".ds-theme-btn"); if (!b) return JSON.stringify({bad: ["no theme button"]});
  if (!document.querySelector(".ds-theme-panel")) b.click();
  var p = document.querySelector(".ds-theme-panel"), r = p.getBoundingClientRect(), bad = [];
  if (r.left < 0 || r.right > innerWidth) bad.push("appearance panel wider than window");
  if (r.top < 0 || r.bottom > innerHeight) bad.push("appearance panel taller than window (scrolls: " + (p.scrollHeight > p.clientHeight) + ")");
  var done = p.querySelector(".ds-stream-go"); var d = done.getBoundingClientRect();
  if (p.scrollHeight <= p.clientHeight && (d.bottom > innerHeight || d.top < 0)) bad.push("Done button unreachable");
  return JSON.stringify({ bad: bad });
})()
"""


async def main(outdir):
    os.makedirs(outdir, exist_ok=True)
    targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json"))
    t = next(t for t in targets if "Friends List" in t.get("title", ""))
    failures = 0
    async with websockets.connect(t["webSocketDebuggerUrl"], max_size=1 << 27) as ws:
        n = 0
        async def call(method, **params):
            nonlocal n
            n += 1
            await ws.send(json.dumps({"id": n, "method": method, "params": params}))
            while True:
                m = json.loads(await ws.recv())
                if m.get("id") == n:
                    return m.get("result", {})
        async def ev(js):
            r = await call("Runtime.evaluate", expression=js, returnByValue=True)
            return json.loads(r["result"]["value"])
        for w, h in SIZES:
            await call("Emulation.setDeviceMetricsOverride", width=w, height=h, deviceScaleFactor=1, mobile=False)
            await asyncio.sleep(0.8)
            res = await ev(CHECK_JS)
            shot = await call("Page.captureScreenshot", format="png")
            open(os.path.join(outdir, f"{w}x{h}.png"), "wb").write(base64.b64decode(shot["data"]))
            panel = await ev(PANEL_JS)
            await asyncio.sleep(0.4)
            shot = await call("Page.captureScreenshot", format="png")
            open(os.path.join(outdir, f"{w}x{h}-panel.png"), "wb").write(base64.b64decode(shot["data"]))
            await ev("(function(){var p=document.querySelector('.ds-theme-panel');if(p)p.remove();return '{}'})()")
            bad = res["bad"] + panel["bad"]
            failures += bool(bad)
            print(f"{w}x{h} (rail {res.get('rail')}px): " + ("ok" if not bad else "; ".join(bad)))
        await call("Emulation.clearDeviceMetricsOverride")
    return failures


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "layout-sweep")) else 0)
