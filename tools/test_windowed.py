#!/usr/bin/env python3
"""Linchpin experiment for the "don't fullscreen the watcher's stream" feature.

RUN THIS ON THE WATCHER MACHINE *WHILE A REMOTE PLAY STREAM IS ACTIVE* (i.e. you
have accepted a share and are looking at the fullscreen stream window).

It sets the streaming client's windowed flag + a window position/size and applies
the config, then reports the active-stream state. Watch the stream window:
  - if it pops out of fullscreen into a 1280x720 window near the top-left,
    window placement is read INDEPENDENT of the locked "client config enabled"
    gate -> the feature is fully buildable (force windowed + position into the
    call UI region).
  - if nothing changes, placement requires the gate we can't flip from CEF, and
    we fall back to a call-UI tile + the user toggling fullscreen manually
    (clientCaps.can_toggle_fullscreen is true).

  python3 tools/test_windowed.py            # apply windowed 1280x720 @ (100,100)
  python3 tools/test_windowed.py reset       # restore fullscreen (windowed=false)
"""
import sys
from cdp import find, evaluate  # reuse the minimal CDP client in this dir
import asyncio

WINDOWED = sys.argv[1] != "reset" if len(sys.argv) > 1 else True

EXPR = """(() => {
  const RP = window.SteamClient.RemotePlay;
  const s = window.RemotePlayStore_SteamUI;
  const cc = s.m_clientConfig;
  const want = %s;
  try {
    cc.set_windowed(want);
    if (want) { cc.set_window_width(1280); cc.set_window_height(720);
                cc.set_window_position_x(100); cc.set_window_position_y(100); }
    RP.SetStreamingClientConfig(cc);
    RP.SetStreamingClientConfigEnabled(true);   // best-effort; gate may reset
  } catch (e) { return JSON.stringify({error: e.message}); }
  let nStreams = 0, names = [];
  try {
    const cs = s.m_remoteClientStreams;
    if (cs) { const arr = Array.isArray(cs) ? cs : Object.values(cs);
              nStreams = arr.length; }
  } catch (e) {}
  return JSON.stringify({
    appliedWindowed: cc.toObject().windowed,
    window: [cc.toObject().window_width, cc.toObject().window_height,
             cc.toObject().window_position_x, cc.toObject().window_position_y],
    gateEnabled: s.m_settings.bRemotePlayClientConfigEnabled,
    canToggleFullscreen: s.m_clientCaps.toObject().can_toggle_fullscreen,
    activeStreams: nStreams,
    streamingSessionID: s.m_settings.unStreamingSessionID,
  });
})()""" % ("true" if WINDOWED else "false")


def main():
    t = find("SharedJSContext")
    if not t:
        print("SharedJSContext target not found — is Steam running with the debug port?")
        return
    out = asyncio.run(evaluate(t["webSocketDebuggerUrl"], EXPR))
    val = out.get("result", {}).get("result", {}).get("value", out)
    print(("RESET (fullscreen)" if not WINDOWED else "APPLY (windowed 1280x720 @100,100)") + " ->")
    print(val)


if __name__ == "__main__":
    main()
