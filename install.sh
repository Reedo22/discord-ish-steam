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

echo "RESTART Steam to load the plugin + clean-boot the CSS."
