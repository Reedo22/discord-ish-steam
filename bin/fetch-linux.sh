#!/usr/bin/env bash
# Fetch the Linux host binaries the screen-share daemon needs into this bin/ folder:
#   mediamtx        - the RTSP->WebRTC server (required for screen share)
#   cloudflared     - optional, only for sharing to friends OFF your LAN (https tunnel)
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
echo "Done."
