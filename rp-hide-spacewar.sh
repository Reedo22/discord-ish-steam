#!/usr/bin/env bash
# Declutter the Spacewar (SteamworksExample) Remote Play anchor window — SAFELY.
#
# IMPORTANT: the anchor must stay MAPPED and on-screen or the stream won't start.
#   - Minimizing it breaks the stream (presents no frames) — confirmed, do NOT do it.
#   - Moving it fully off-screen doesn't work either: KWin clamps it back on-screen.
# So the most we can safely do is remove it from the taskbar/pager and tuck it into a
# corner. To actually hide it from the VIEWER, stream a single monitor (the one without
# Spacewar) via the plugin's Monitor picker — that's the real fix.
export DISPLAY="${DISPLAY:-:1}"
command -v xdotool >/dev/null 2>&1 || { echo "rp-hide-spacewar: xdotool not installed"; exit 0; }

while true; do
  for w in $(xdotool search --sync --name "SteamworksExample" 2>/dev/null); do
    command -v wmctrl >/dev/null 2>&1 && wmctrl -i -r "$w" -b add,skip_taskbar,skip_pager 2>/dev/null
    # tuck into the bottom area of the primary monitor (kept on-screen + mapped so the
    # stream still works). NOT minimized. Adjust coords per your monitor layout.
    xdotool windowmove "$w" 3840 1700 2>/dev/null
  done
  sleep 2
done
