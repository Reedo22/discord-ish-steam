#!/usr/bin/env bash
# Build the one-click Windows installer (discord-ish-steam-setup.exe) on Linux.
# Needs NSIS:  sudo apt-get install -y nsis
# Output:      dist/discord-ish-steam-setup.exe
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(dirname "$here")"

if ! command -v makensis >/dev/null 2>&1; then
  echo "makensis not found. Install it:  sudo apt-get install -y nsis" >&2
  exit 1
fi

# Version: $VERSION, else the latest tag (v0.7.0 -> 0.7.0), else 0.0.0.
ver="${VERSION:-$(git -C "$root" describe --tags --abbrev=0 2>/dev/null || true)}"
ver="${ver#v}"; [[ "$ver" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || ver=0.0.0
mkdir -p "$root/dist"
echo "Building installer $ver with $(makensis -VERSION)..."
(cd "$here" && makensis -V2 -DVERSION="$ver" installer.nsi)

out="$root/dist/discord-ish-steam-setup.exe"
if [[ -f "$out" ]]; then
  echo "OK -> $out  ($(du -h "$out" | cut -f1))"
else
  echo "Build reported success but $out is missing." >&2
  exit 1
fi
