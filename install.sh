#!/usr/bin/env bash
# Persist the Discord-ish theme into Millennium's quickcss (layers on top of the
# active theme, survives Steam restarts). CSS source of truth = theme/friends.custom.css.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
QUICKCSS="$HOME/.config/millennium/quickcss.css"

if [[ ! -d "$(dirname "$QUICKCSS")" ]]; then
  echo "Millennium config dir not found at $(dirname "$QUICKCSS") — is Millennium installed?" >&2
  exit 1
fi

{
  echo "/* Quick CSS file created by Millennium */"
  echo "/* === discord-ish steam reskin (managed by steam-reskin/install.sh) === */"
  cat "$HERE/theme/friends.custom.css"
} > "$QUICKCSS"
echo "Wrote $(wc -l < "$QUICKCSS") lines to $QUICKCSS"

# --- plugin (voice-to-top-bar + placeholder; needs JS) ---
PLUGINS="$HOME/.local/share/millennium/plugins"
CONFIG="$HOME/.config/millennium/config.json"
if [[ -d "$PLUGINS" ]]; then
  ln -sfn "$HERE/plugin" "$PLUGINS/discordish-chat"
  echo "Linked plugin -> $PLUGINS/discordish-chat"
  # enable it in config.json (idempotent)
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

# --- RemotePlayWhatever (low-latency Remote Play screen share) ---
# Hosts the RPT session under Spacewar (AppID 480) and sends the invite; the
# capture script (stream-capture.sh) calls it. Fetched here so it's not a
# committed 14MB binary. Skip silently if already present.
RPW="$HERE/bin/remoteplaywhatever-x86_64.AppImage"
RPW_URL="https://github.com/m4dEngi/RemotePlayWhatever/releases/download/0.2.14-alpha/remoteplaywhatever-x86_64.AppImage"
if [[ ! -x "$RPW" ]]; then
  mkdir -p "$HERE/bin"
  if command -v curl >/dev/null; then
    echo "Fetching RemotePlayWhatever…"
    curl -fsSL -o "$RPW" "$RPW_URL" && chmod +x "$RPW" && echo "  -> $RPW" \
      || echo "  ! failed to download RemotePlayWhatever — Remote Play share won't work until it's at $RPW" >&2
  else
    echo "  ! curl not found — download RemotePlayWhatever manually to $RPW" >&2
  fi
fi
command -v ffplay >/dev/null || echo "  ! ffplay (ffmpeg) not on PATH — needed for the screen-capture mirror." >&2

# --- Spacewar-hider for the Remote Play share (xdotool watcher) ---
# The Remote Play path uses Spacewar (480) as an invisible RPT anchor; this keeps
# its window minimized so the whole-desktop stream doesn't show the demo.
# Remote Play helpers: xdotool (Spacewar window-hider), wmctrl (app list),
# gstreamer (occlusion-proof app capture via ximagesrc).
if ! command -v xdotool >/dev/null || ! command -v wmctrl >/dev/null || ! command -v gst-launch-1.0 >/dev/null; then
  echo "Installing xdotool + wmctrl + gstreamer (needs sudo)…"
  if command -v apt-get >/dev/null; then sudo apt-get install -y xdotool wmctrl gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good || echo "  ! couldn't install Remote Play helpers — app-picker / occlusion-proof capture need them." >&2
  elif command -v pacman  >/dev/null; then sudo pacman -S --noconfirm xdotool wmctrl gst-plugins-base gst-plugins-good || true
  elif command -v dnf     >/dev/null; then sudo dnf install -y xdotool wmctrl gstreamer1-plugins-base gstreamer1-plugins-good || true
  else echo "  ! install xdotool + wmctrl + gstreamer with your package manager for the Remote Play helpers." >&2; fi
fi
# autostart on login (graphical session has DISPLAY), and start it now
AUTOSTART="$HOME/.config/autostart"
mkdir -p "$AUTOSTART"
cat > "$AUTOSTART/discordish-rp-hide.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Discord-ish RP Spacewar hider
Exec=$HERE/rp-hide-spacewar.sh
X-GNOME-Autostart-enabled=true
NoDisplay=true
EOF
if command -v xdotool >/dev/null && ! pgrep -f "rp-hide-spacewar.sh" >/dev/null; then
  nohup "$HERE/rp-hide-spacewar.sh" >/dev/null 2>&1 &
  echo "Started Spacewar-hider watcher (autostarts on login)."
fi

echo "RESTART Steam to load the plugin + clean-boot the CSS."
