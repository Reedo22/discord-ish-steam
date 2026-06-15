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

# (RemotePlayWhatever removed — the Remote Play session + invite are now created
# natively by the plugin via the real Spacewar/appid-480 RPT anchor + whole-desktop
# streaming. No external binary, no capture server, no launch hijack.)

# --- REMOVE the old Spacewar-hider (it BREAKS streaming) ---
# The hider minimized the Spacewar window; a minimized window presents no GL frames,
# so Remote Play joins the session but the video never starts. Tear it down if a
# previous install set it up.
rm -f "$HOME/.config/autostart/discordish-rp-hide.desktop"
pkill -x rp-hide-spacewar.sh 2>/dev/null || true

# --- VIEWER-side fix: libvpx.so.6 for Steam's streaming client (appid 202355) ---
# Steam's streaming_client links libvpx.so.6, but Ubuntu/Pop 24.04 ship libvpx9 only,
# so the client crashes instantly on every accept. Steam's own runtime bundles a valid
# libvpx.so.6 — install it system-wide so the client (launched outside the runtime with
# only $ORIGIN RUNPATH, which doesn't cover transitive deps) can load it.
if ! ldconfig -p 2>/dev/null | grep -q "libvpx.so.6"; then
  SC_VPX="$(find "$HOME/.steam" "$HOME/.local/share/Steam" -path '*SteamLinuxRuntime_sniper*/libvpx.so.6.3.0' -type f 2>/dev/null | head -1)"
  if [[ -n "$SC_VPX" ]]; then
    echo "Installing libvpx.so.6 system-wide for Steam Remote Play (needs sudo)…"
    sudo install -m0644 "$SC_VPX" /usr/lib/x86_64-linux-gnu/libvpx.so.6 && sudo ldconfig \
      && echo "  installed libvpx.so.6" \
      || echo "  ! couldn't install libvpx.so.6 — Remote Play streaming will crash on this machine." >&2
  else
    echo "  ! libvpx.so.6 missing and no bundled copy found — Remote Play streaming will crash here." >&2
  fi
fi

# --- VIEWER-side WARNING: Millennium vs the streaming client ---
# Millennium LD_PRELOADs libmillennium_hhx64.so into every Steam-launched process,
# including the Remote Play streaming_client — which then exits in ~1s. If THIS machine
# is used to RECEIVE streams, disable the 64-bit hook:
#   sudo mv /usr/lib/millennium/libmillennium_hhx64.so{,.disabled}
if [[ -f /usr/lib/millennium/libmillennium_hhx64.so ]]; then
  echo "  ! NOTE: Millennium's libmillennium_hhx64.so breaks Steam Remote Play *receiving* on this box." >&2
  echo "    If you watch streams here: sudo mv /usr/lib/millennium/libmillennium_hhx64.so{,.disabled}" >&2
fi

echo "RESTART Steam to load the plugin + clean-boot the CSS."
