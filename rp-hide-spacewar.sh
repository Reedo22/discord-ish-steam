#!/usr/bin/env bash
# Keep Steam's Spacewar (SteamworksExample) window out of sight. The "Remote Play"
# screen-share uses Spacewar (AppID 480) purely as a Remote Play Together anchor —
# we never want to SEE it, and since RPT streams the whole desktop the friend would
# otherwise watch the asteroids demo. This watcher minimizes the window whenever it
# appears. Spacewar is only ever launched by our share, so minimizing it on sight is
# safe. Started on login by install.sh (autostart entry) and immediately on install.
# X11 only (xdotool). No-op + exit cleanly if xdotool is missing.
export DISPLAY="${DISPLAY:-:1}"

if ! command -v xdotool >/dev/null 2>&1; then
  echo "rp-hide-spacewar: xdotool not installed — install it (the installer does: sudo apt install xdotool)" >&2
  exit 0
fi

while true; do
  # --sync blocks until at least one matching window exists, so this isn't a busy
  # loop when Spacewar isn't running. Minimize every match (re-minimizing is a no-op).
  for w in $(xdotool search --sync --name "SteamworksExample" 2>/dev/null); do
    xdotool windowminimize "$w" 2>/dev/null
  done
  sleep 2
done
