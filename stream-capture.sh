#!/usr/bin/env bash
# Mirror a screen region into an ffplay window. Steam broadcasts THIS window,
# so whatever's in the captured region gets streamed — any app, already running,
# no relaunch. Launch this as a non-Steam game; broadcast it.
#
# Config: ~/.config/discordish-capture.conf  (written by the reskin's picker)
#   CAP_GEOM=WxH+X+Y   region to capture (default: left monitor)
#   VIEW_LEFT, VIEW_TOP  where to place the ffplay window (put it on the OTHER
#                        monitor so it never captures itself)
CONF="$HOME/.config/discordish-capture.conf"
[ -f "$CONF" ] && . "$CONF"

CAP_GEOM="${CAP_GEOM:-3840x2160+0+0}"   # default: left monitor (DP-4)
VIEW_LEFT="${VIEW_LEFT:-3840}"          # default: ffplay on right monitor (DP-0)
VIEW_TOP="${VIEW_TOP:-0}"
SCALE="${SCALE:-1920x1080}"             # downscale for perf / broadcast size

WH="${CAP_GEOM%%+*}"
REST="${CAP_GEOM#*+}"; X="${REST%%+*}"; Y="${REST#*+}"

exec ffplay -loglevel error -f x11grab -framerate 30 -video_size "$WH" \
  -i "${DISPLAY}+${X},${Y}" \
  -vf "scale=${SCALE/x/:}" \
  -left "$VIEW_LEFT" -top "$VIEW_TOP" -noborder -an \
  -window_title "Steam Stream Capture"
