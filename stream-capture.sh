#!/usr/bin/env bash
# Linux screen-capture mirror + Remote Play Together invite, launched by the
# reskin as a non-Steam game (so Steam treats the ffplay window as the "game"
# and Remote Play Together streams it to the invited friend — low latency, no
# broadcast re-encode). RemotePlayWhatever hosts the RPT session under Spacewar
# (AppID 480) and sends the invite; same trick as the Windows stream-capture.ps1.
#
# Args (from the plugin's launch options — see streamScreen() in index.js):
#   $1  CAP_GEOM   x11grab geometry "WxH+X+Y" of the monitor/region to capture
#   $2  SCALE      "WxH" to downscale to, or "none" for native
#   $3  VIEW_LEFT  x position to place the ffplay mirror window (other monitor)
#   $4  VIEW_TOP   y position to place the ffplay mirror window
#   $5  INVITE     (optional) friend SteamID64 to send the Remote Play invite to
CAP_GEOM="${1:-3840x2160+0+0}"
SCALE="${2:-1920x1080}"
VIEW_LEFT="${3:-3840}"
VIEW_TOP="${4:-0}"
INVITE="${5:-}"

HERE="$(cd "$(dirname "$0")" && pwd)"
RPW="${RPW_APPIMAGE:-$HERE/bin/remoteplaywhatever-x86_64.AppImage}"

WH="${CAP_GEOM%%+*}"
REST="${CAP_GEOM#*+}"; X="${REST%%+*}"; Y="${REST#*+}"

# 1) Send the Remote Play Together invite via RemotePlayWhatever. It must NOT
#    inherit Steam's game-launch environment: because Steam started this script
#    as a non-Steam game, SteamAppId/SteamGameId/etc. point at that shortcut, and
#    RPW's own Steam-client init then gets "ConnectToGlobalUser: Steam denied
#    appID …". So we strip every Steam*/overlay var (RPW locates Steam via $HOME,
#    not these) and setsid it out of the game's process group.
env | sort > /tmp/cap-env.txt   # diagnostics: the env Steam handed us
if [ -n "$INVITE" ] && [ -x "$RPW" ]; then
  (
    # Strip only the GAME-IDENTITY vars that make Steam deny RPW's client init
    # (they point at our non-Steam capture shortcut). Keep Steam3Master / SteamEnv
    # / SteamUser / SteamAppUser so RPW can still find + identify against Steam.
    unset SteamAppId SteamGameId SteamOverlayGameId SteamClientLaunch LD_PRELOAD
    setsid "$RPW" -v -a 480 -i "$INVITE" \
      || setsid "$RPW" --appimage-extract-and-run -v -a 480 -i "$INVITE"
  ) >/tmp/rpw.log 2>&1 &
elif [ -n "$INVITE" ]; then
  echo "RemotePlayWhatever not found at $RPW (run install.sh to fetch it)" >&2
fi

# 2) Mirror the chosen monitor into a borderless ffplay window that RPT streams.
VF=()
[ "$SCALE" != "none" ] && VF=(-vf "scale=${SCALE/x/:}")

exec ffplay -loglevel error -f x11grab -framerate 30 -video_size "$WH" \
  -i "${DISPLAY}+${X},${Y}" \
  "${VF[@]}" \
  -left "$VIEW_LEFT" -top "$VIEW_TOP" -noborder -an \
  -window_title "Discord-ish Screen Share"
