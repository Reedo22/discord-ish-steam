#!/usr/bin/env bash
# Launch the sandboxed Flatpak Steam as the local Remote Play GUEST (second account).
# Runs alongside your native Steam (the HOST). Enables CEF remote debugging on port
# 8080 so tools/test_windowed.py (DS_CDP_PORT=8080) can drive the guest's stream window.
#
#   tools/guest_steam.sh            # launch the guest Steam
#
# First run bootstraps the full client + asks you to log into your SECOND account.
# Then: friend your main account, and on the host side start a share + invite this
# account. Accept here to watch.  CEF debug:  http://127.0.0.1:8080/json
set -e
APP=com.valvesoftware.Steam
DATA="$HOME/.var/app/$APP/.local/share/Steam"

# Ensure the Steam data dir exists (created on first launch) then drop the debug marker.
mkdir -p "$DATA"
: > "$DATA/.cef-enable-remote-debugging"
echo "[guest] CEF remote debugging marker at: $DATA/.cef-enable-remote-debugging"
echo "[guest] guest Steam CEF will be on http://127.0.0.1:8080  (host/native Steam is unaffected)"
echo "[guest] launching Flatpak Steam (second account)…"
exec flatpak run "$APP" "$@"
