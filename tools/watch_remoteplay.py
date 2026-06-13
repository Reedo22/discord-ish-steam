#!/usr/bin/env python3
"""Poll Steam's Remote Play state and log every change (to learn the desktop-
stream flow live). Run in background; tail the output."""
import json, time, subprocess, re, urllib.request, sys

def port():
    out = subprocess.check_output(["pgrep", "-af", "steamwebhelper"], text=True)
    m = re.search(r"remote-debugging-port=(\d+)", out)
    return int(m.group(1)) if m else 36377

EXPR = r"""(() => {
  const rps = window.RemotePlayStore_SteamUI;
  const safe = (f) => { try { return rps && rps[f] ? rps[f]() : null; } catch(e){ return 'err'; } };
  let groupId = null;
  try {
    const rp = window.SteamClient.RemotePlay;
    if (rp.GetRemotePlayTogetherGroupIDForOverlayPID)
      groupId = rp.GetRemotePlayTogetherGroupIDForOverlayPID(0);
  } catch(e){}
  let nStreams = 0;
  try { const cs = rps && rps.remoteClientStreams; nStreams = cs ? (cs.length!=null?cs.length:Object.keys(cs).length) : 0; } catch(e){}
  return JSON.stringify({
    desktop: safe('BIsStreamingRemoteDesktop'),
    rptGame: safe('BIsStreamingRemotePlayTogetherGame'),
    streams: nStreams,
    group: groupId,
  });
})()"""

def find_ws(p):
    with urllib.request.urlopen(f"http://127.0.0.1:{p}/json") as r:
        for t in json.load(r):
            if t.get("title") == "SharedJSContext" and t.get("webSocketDebuggerUrl"):
                return t["webSocketDebuggerUrl"]
    return None

import websockets, asyncio
async def sample(ws_url):
    async with websockets.connect(ws_url, max_size=8*1024*1024) as ws:
        await ws.send(json.dumps({"id":1,"method":"Runtime.evaluate",
            "params":{"expression":EXPR,"returnByValue":True}}))
        while True:
            m = json.loads(await ws.recv())
            if m.get("id")==1:
                return m["result"]["result"].get("value")

last = None
print("watching remote play… (state changes only)", flush=True)
while True:
    try:
        ws = find_ws(port())
        val = asyncio.run(sample(ws))
        if val != last:
            print(time.strftime("%H:%M:%S"), val, flush=True)
            last = val
    except Exception as e:
        print(time.strftime("%H:%M:%S"), "poll err:", str(e)[:60], flush=True)
    time.sleep(2)
