#!/usr/bin/env bash
# discord-ish-steam — Linux installer.
#  - links the Millennium plugin + writes the theme into quickcss
#  - enables the plugin in Millennium's config.json
#  - installs the screen-share daemon (rp-webrtc.py) as a systemd --user service so it
#    auto-starts on login and restarts if it dies.
# CSS source of truth = theme/friends.custom.css.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# Millennium v3 reads quick.css (older builds read quickcss.css) — write both.
QUICKCSS="$HOME/.config/millennium/quick.css"
QUICKCSS_OLD="$HOME/.config/millennium/quickcss.css"
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
# X11: wmctrl lists windows, x11-xserver-utils provides xrandr (lists monitors).
# Wayland: gstreamer pipewire plugin + python dbus/gi drive the xdg-desktop-portal
# ScreenCast path (the compositor's own picker replaces wmctrl/xrandr enumeration).
  local apt=(ffmpeg python3 gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-pipewire python3-dbus python3-gi wmctrl x11-xserver-utils)
  local dnf=(ffmpeg python3 gstreamer1-plugins-base gstreamer1-plugins-good gstreamer1-plugins-bad-free pipewire-gstreamer python3-dbus python3-gobject wmctrl xrandr)
  local pac=(ffmpeg python gst-plugins-base gst-plugins-good gst-plugins-bad gst-plugin-pipewire python-dbus python-gobject wmctrl xorg-xrandr)
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
cp "$QUICKCSS" "$QUICKCSS_OLD"
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
# Captures a monitor/window (X11: ffmpeg x11grab / gst ximagesrc; Wayland: ScreenCast
# portal -> pipewire; NVENC/VAAPI/QSV encode), publishes via MediaMTX, serves WebRTC/WHEP
# to the viewer's plugin. Auto-starts + auto-restarts.
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
# on-failure, NOT always: the daemon now self-guards on its control port and exits 0 when
# another instance already owns it (the plugin's Lua backend also starts one on every Steam
# launch). With Restart=always that clean exit would respawn every RestartSec forever.
Restart=on-failure
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
if [[ "${XDG_SESSION_TYPE:-}" == "wayland" ]]; then
  # Wayland: everything goes through the ScreenCast portal -> pipewire -> gstreamer.
  command -v gst-launch-1.0 >/dev/null 2>&1 || { echo "  ! gstreamer missing — Wayland capture won't work at all." >&2; ok=0; }
  gst-inspect-1.0 pipewiresrc >/dev/null 2>&1 || { echo "  ! gst pipewiresrc missing — install gstreamer1.0-pipewire (Wayland capture needs it)." >&2; ok=0; }
  python3 -c 'import dbus, gi' >/dev/null 2>&1 || { echo "  ! python3-dbus / python3-gi missing — daemon can't talk to the screencast portal." >&2; ok=0; }
else
  command -v gst-launch-1.0 >/dev/null 2>&1 || echo "  ! gstreamer missing — per-window (occlusion-proof) capture won't work; monitor capture still will." >&2
  command -v xrandr         >/dev/null 2>&1 || { echo "  ! xrandr missing — daemon can't list MONITORS, so you can't start a share. Install: x11-xserver-utils" >&2; ok=0; }
  command -v wmctrl         >/dev/null 2>&1 || echo "  ! wmctrl missing — daemon can't list app WINDOWS (per-app share); monitor share still works." >&2
fi
[[ -x "$HERE/bin/mediamtx" ]] || { echo "  ! bin/mediamtx missing — screen share won't work." >&2; ok=0; }
[[ $ok = 1 ]] && echo "Capture stack OK (ffmpeg + mediamtx present)."

echo "RESTART Steam to load the plugin + clean-boot the CSS."
