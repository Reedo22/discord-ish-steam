#!/usr/bin/env bash
# Fetch the Linux host binaries the screen-share daemon needs into this bin/ folder:
#   mediamtx        - the RTSP->WebRTC server (required for screen share)
#   cloudflared     - optional, only for sharing to friends OFF your LAN (https tunnel)
#   ffmpeg          - only if the system ffmpeg is older than 7.1 (no Opus over RTMP)
# Run:  ./bin/fetch-linux.sh     (install.sh calls this automatically)
set -euo pipefail
BIN="$(cd "$(dirname "$0")" && pwd)"

case "$(uname -m)" in
  x86_64|amd64) ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  *) echo "Unsupported arch $(uname -m)"; exit 1 ;;
esac

# --- MediaMTX (latest linux_<arch>.tar.gz from the GitHub API) ---
if [[ -x "$BIN/mediamtx" ]]; then
  echo "  mediamtx already present — skipping"
else
  echo "  fetching mediamtx ($ARCH)…"
  URL="$(curl -fsSL https://api.github.com/repos/bluenviron/mediamtx/releases/latest \
        | grep -oE "https://[^\"]*linux_${ARCH}\.tar\.gz" | head -1)"
  [[ -n "$URL" ]] || { echo "  ! could not resolve mediamtx download URL" >&2; exit 1; }
  tmp="$(mktemp -d)"; curl -fsSL "$URL" -o "$tmp/m.tar.gz"
  tar -xzf "$tmp/m.tar.gz" -C "$tmp" mediamtx
  install -m0755 "$tmp/mediamtx" "$BIN/mediamtx"; rm -rf "$tmp"
  echo "  installed mediamtx"
fi

# --- cloudflared (stable direct download; optional — off-LAN sharing only) ---
if [[ -x "$BIN/cloudflared" ]]; then
  echo "  cloudflared already present — skipping"
else
  echo "  fetching cloudflared ($ARCH)…"
  if curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH}" -o "$BIN/cloudflared"; then
    chmod +x "$BIN/cloudflared"; echo "  installed cloudflared"
  else
    echo "  ! cloudflared download failed — off-LAN sharing won't work, LAN sharing will." >&2
  fi
fi
# --- ffmpeg (only if the system one is too old) ---
# The daemon needs an ffmpeg that can put Opus audio into RTMP (ffmpeg >= 7.1) — that's what
# carries sound on the WebRTC path. Many distros still ship 6.x (Ubuntu 24.04: 6.1), so
# fetch a current static build (BtbN, GPL, has NVENC/VAAPI) into bin/ when needed. The
# daemon prefers bin/ffmpeg over the system one. ~150 MB download; checksum-verified.
opus_flv_ok() { "$1" -hide_banner -loglevel error -f lavfi -i anullsrc=r=48000:cl=stereo -t 0.1 \
                  -c:a libopus -f flv - >/dev/null 2>&1; }
if [[ -x "$BIN/ffmpeg" ]] && opus_flv_ok "$BIN/ffmpeg"; then
  echo "  bundled ffmpeg already present — skipping"
elif command -v ffmpeg >/dev/null 2>&1 && opus_flv_ok ffmpeg; then
  echo "  system ffmpeg is new enough (Opus over RTMP) — not bundling one"
else
  echo "  system ffmpeg can't carry Opus over RTMP (needs >= 7.1); fetching a static build…"
  [[ $ARCH == amd64 ]] && FA=linux64 || FA=linuxarm64
  api="$(curl -fsSL https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest)"
  base="https://github.com/BtbN/FFmpeg-Builds/releases/latest/download"
  sums="$(curl -fsSL "$base/checksums.sha256")"
  # OLDEST suitable branch first: each FFmpeg release raises the minimum NVIDIA driver its
  # NVENC needs (9.0 wants >= 610), so the newest build can lose GPU encoding on a working
  # driver. Keep the first build that does Opus-over-RTMP AND (on NVIDIA) still encodes.
  installed=0
  for NAME in $(grep -oE "ffmpeg-n[0-9.]+-latest-${FA}-gpl-[0-9.]+\.tar\.xz" <<<"$api" | sort -uV); do
    ver="$(sed -E 's/^ffmpeg-n([0-9.]+)-.*/\1/' <<<"$NAME")"
    [[ "$(printf '%s\n' 7.1 "$ver" | sort -V | head -1)" == 7.1 ]] || continue   # need >= 7.1
    tmp="$(mktemp -d)"
    echo "  trying $NAME…"
    curl -fsSL "$base/$NAME" -o "$tmp/$NAME" || { rm -rf "$tmp"; continue; }
    want="$(awk -v n="$NAME" '$2==n || $2=="*"n {print $1}' <<<"$sums")"
    got="$(sha256sum "$tmp/$NAME" | cut -d' ' -f1)"
    if [[ -z "$want" || "$want" != "$got" ]]; then
      echo "  ! checksum mismatch for $NAME — skipped" >&2; rm -rf "$tmp"; continue
    fi
    tar -xJf "$tmp/$NAME" -C "$tmp" --wildcards '*/bin/ffmpeg'
    cand=$(ls "$tmp"/*/bin/ffmpeg)
    if ! opus_flv_ok "$cand"; then rm -rf "$tmp"; continue; fi
    if command -v nvidia-smi >/dev/null 2>&1 && ! "$cand" -hide_banner -loglevel error -f lavfi \
         -i color=c=black:s=256x144:r=30 -vf format=nv12 -frames:v 1 -c:v h264_nvenc -f null - >/dev/null 2>&1; then
      echo "  $NAME can't use NVENC with this driver — trying an older build"; rm -rf "$tmp"; continue
    fi
    install -m0755 "$cand" "$BIN/ffmpeg"; rm -rf "$tmp"; installed=1
    echo "  installed $("$BIN/ffmpeg" -version | head -1 | cut -d' ' -f1-3)"
    break
  done
  [[ $installed == 1 ]] || echo "  ! no suitable static ffmpeg — WebRTC viewers will get video without sound" >&2
fi
echo "Done."
