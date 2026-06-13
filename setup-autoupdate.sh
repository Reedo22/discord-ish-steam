#!/usr/bin/env bash
# Install a systemd user timer that runs update.sh daily (Linux).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
UD="$HOME/.config/systemd/user"
mkdir -p "$UD"

cat > "$UD/discordish-update.service" <<EOF
[Unit]
Description=Update discord-ish-steam theme/plugin from GitHub

[Service]
Type=oneshot
ExecStart=$HERE/update.sh
EOF

cat > "$UD/discordish-update.timer" <<EOF
[Unit]
Description=Daily discord-ish-steam update

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now discordish-update.timer
echo "Daily auto-update timer installed. Check: systemctl --user list-timers discordish-update.timer"
