#!/usr/bin/env bash
# Pull the latest theme/plugin from git and re-install. Run this to update;
# changes apply on the next Steam restart.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

echo "Updating discord-ish-steam…"
git pull --ff-only origin "$(git rev-parse --abbrev-ref HEAD)" || {
  echo "git pull failed (local changes? wrong branch?)" >&2; exit 1
}
bash "$HERE/install.sh"
echo "Updated. Restart Steam (steam -shutdown then relaunch) to apply."
