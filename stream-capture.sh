#!/usr/bin/env bash
# Mirror a screen region into an ffplay window that Steam broadcasts.
# Args (passed via the non-Steam shortcut's launch options):
#   $1 CAP_GEOM   WxH+X+Y region to capture            (default: left monitor)
#   $2 SCALE      output WxH, or "none" for native     (default: 1920x1080)
#   $3 VIEW_LEFT  ffplay window X (put on OTHER monitor, or off-screen to hide)
#   $4 VIEW_TOP   ffplay window Y
CAP_GEOM="${1:-3840x2160+0+0}"
SCALE="${2:-1920x1080}"
VIEW_LEFT="${3:-3840}"
VIEW_TOP="${4:-0}"

WH="${CAP_GEOM%%+*}"
REST="${CAP_GEOM#*+}"; X="${REST%%+*}"; Y="${REST#*+}"

VF=()
[ "$SCALE" != "none" ] && VF=(-vf "scale=${SCALE/x/:}")

exec ffplay -loglevel error -f x11grab -framerate 30 -video_size "$WH" \
  -i "${DISPLAY}+${X},${Y}" \
  "${VF[@]}" \
  -left "$VIEW_LEFT" -top "$VIEW_TOP" -noborder -an \
  -window_title "Steam Stream Capture"
