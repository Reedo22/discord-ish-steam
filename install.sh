#!/usr/bin/env bash
# discord-ish-steam — Linux installer.
#  - links the Millennium plugin + writes the theme into quickcss
#  - enables the plugin in Millennium's config.json
#  - installs the screen-share daemon (rp-webrtc.py) as a systemd --user service so it
#    auto-starts on login and restarts if it dies.
# CSS source of truth = theme/friends.custom.css.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
QUICKCSS="$HOME/.config/millennium/quickcss.css"
CONFIG="$HOME/.config/millennium/config.json"
PLUGINS="$HOME/.local/share/millennium/plugins"

if [[ ! -d "$(dirname "$QUICKCSS")" ]]; then
  echo "Millennium config dir not found at $(dirname "$QUICKCSS") — is Millennium installed?" >&2
  exit 1
fi

# --- theme (quickcss baseline; the plugin also live-fetches the latest CSS on boot) ---
{
  echo "/* Quick CSS file created by Millennium */"
  echo "/* === discord-ish steam reskin (managed by steam-reskin/install.sh) === */"
  cat "$HERE/theme/friends.custom.css"
} > "$QUICKCSS"
echo "Wrote $(wc -l < "$QUICKCSS") lines to $QUICKCSS"

# --- plugin ---
if [[ -d "$PLUGINS" ]]; then
  ln -sfn "$HERE/plugin" "$PLUGINS/discordish-chat"
  echo "Linked plugin -> $PLUGINS/discordish-chat"
  python3 - "$CONFIG" <<'PY'
import json, sys
cfg_path = sys.argv[1]
with open(cfg_path) as f: cfg = json.load(f)
enabled = cfg.setdefault("plugins", {}).setdefault("enabledPlugins", [])
if "discordish-chat" not in enabled:
    enabled.append("discordish-chat")
    with open(cfg_path, "w") as f: json.dump(cfg, f, indent=2)
    print("Enabled discordish-chat in", cfg_path)
else:
    print("discordish-chat already enabled")
PY
fi

# --- screen-share daemon (rp-webrtc.py) as a systemd --user service ---
# Captures a monitor/window (ffmpeg x11grab / gst ximagesrc + NVENC/VAAPI/QSV), publishes
# via MediaMTX, serves WebRTC/WHEP to the viewer's plugin. Auto-starts + auto-restarts.
DISPLAY_VAL="${DISPLAY:-:1}"
SVC_DIR="$HOME/.config/systemd/user"
mkdir -p "$SVC_DIR"
cat > "$SVC_DIR/discordish-rp-webrtc.service" <<SVC
[Unit]
Description=discord-ish-steam screen-share daemon (rp-webrtc)
After=graphical-session.target

[Service]
Type=simple
Environment=DISPLAY=$DISPLAY_VAL
WorkingDirectory=$HERE
ExecStart=/usr/bin/python3 $HERE/rp-webrtc.py
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
SVC
if command -v systemctl >/dev/null 2>&1; then
  systemctl --user daemon-reload
  systemctl --user enable --now discordish-rp-webrtc.service && \
    echo "Installed + started discordish-rp-webrtc.service"
  # keep the user service alive across logout/before GUI login
  loginctl enable-linger "$USER" >/dev/null 2>&1 || true
else
  echo "systemd --user not available; run the daemon manually: python3 $HERE/rp-webrtc.py" >&2
fi

# --- host-side capture prerequisites ---
command -v ffmpeg       >/dev/null 2>&1 || echo "  ! ffmpeg not found — screen-share capture needs it (apt install ffmpeg)." >&2
command -v gst-launch-1.0 >/dev/null 2>&1 || echo "  ! gstreamer not found — per-window (occlusion-proof) capture needs it (apt install gstreamer1.0-tools gstreamer1.0-plugins-{good,bad,ugly})." >&2
[[ -x "$HERE/bin/mediamtx" ]] || echo "  ! bin/mediamtx missing — download the Linux build from github.com/bluenviron/mediamtx/releases into bin/." >&2

echo "RESTART Steam to load the plugin + clean-boot the CSS."
