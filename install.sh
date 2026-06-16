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
  echo "Millennium not found at $(dirname "$QUICKCSS")." >&2
  echo "Install it first:  curl -fsSL \"https://steambrew.app/install.sh\" | bash" >&2
  echo "then run Steam once and re-run this installer." >&2
  exit 1
fi

# --- system dependencies (ffmpeg + gstreamer for capture, python3 for the daemon) ---
install_deps() {
  local apt=(ffmpeg python3 gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad wmctrl)
  local dnf=(ffmpeg python3 gstreamer1-plugins-base gstreamer1-plugins-good gstreamer1-plugins-bad-free wmctrl)
  local pac=(ffmpeg python gst-plugins-base gst-plugins-good gst-plugins-bad wmctrl)
  if   command -v apt-get >/dev/null 2>&1; then echo "Installing deps (apt, needs sudo)…"; sudo apt-get install -y "${apt[@]}" || echo "  ! apt install failed — install manually: ${apt[*]}" >&2
  elif command -v dnf     >/dev/null 2>&1; then echo "Installing deps (dnf, needs sudo)…"; sudo dnf install -y "${dnf[@]}" || echo "  ! dnf install failed" >&2
  elif command -v pacman  >/dev/null 2>&1; then echo "Installing deps (pacman, needs sudo)…"; sudo pacman -S --needed --noconfirm "${pac[@]}" || echo "  ! pacman install failed" >&2
  else echo "  ! Unknown package manager — install manually: ffmpeg python3 gstreamer + plugins wmctrl" >&2; fi
}
install_deps

# --- host binaries (MediaMTX + cloudflared) ---
bash "$HERE/bin/fetch-linux.sh" || echo "  ! binary fetch failed — see bin/fetch-linux.sh" >&2

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

# --- verify the capture stack is in place ---
ok=1
command -v ffmpeg         >/dev/null 2>&1 || { echo "  ! ffmpeg still missing — screen-share capture won't work." >&2; ok=0; }
command -v gst-launch-1.0 >/dev/null 2>&1 || echo "  ! gstreamer missing — per-window (occlusion-proof) capture won't work; monitor capture still will." >&2
[[ -x "$HERE/bin/mediamtx" ]] || { echo "  ! bin/mediamtx missing — screen share won't work." >&2; ok=0; }
[[ $ok = 1 ]] && echo "Capture stack OK (ffmpeg + mediamtx present)."

echo "RESTART Steam to load the plugin + clean-boot the CSS."
