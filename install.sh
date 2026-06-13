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
echo "Reload the Friends window (or restart Steam) to apply."
