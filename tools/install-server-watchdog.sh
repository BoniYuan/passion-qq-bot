#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="${1:-/opt/passion-bot}"
UNIT_PATH=/etc/systemd/system/passion-bot-watchdog.service
TIMER_PATH=/etc/systemd/system/passion-bot-watchdog.timer
CHECK_PATH=/usr/local/sbin/passion-bot-watchdog

cat > "$CHECK_PATH" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/passion-bot}"
cd "$PROJECT_DIR"

# Recreate containers that are actually stopped. NapCat uses ACCOUNT for quick login.
/usr/bin/docker compose --profile qq up -d --remove-orphans

# AstrBot's HTTP health check can stay green after its NapCat WebSocket dies.
# A burst of socket send failures with no later event-bus traffic means the link is stale.
recent_logs="$(/usr/bin/docker logs --since 3m sub2-astrbot 2>&1 || true)"
last_error="$(printf '%s\n' "$recent_logs" | grep -n 'socket.send() raised exception' | tail -1 | cut -d: -f1 || true)"
last_event="$(printf '%s\n' "$recent_logs" | grep -n 'core.event_bus' | tail -1 | cut -d: -f1 || true)"

if [[ -n "$last_error" ]] && [[ -z "$last_event" || "$last_error" -gt "$last_event" ]]; then
    logger -t passion-bot-watchdog 'stale NapCat WebSocket detected; restarting AstrBot'
    /usr/bin/docker compose restart astrbot
fi
EOF
chmod 0755 "$CHECK_PATH"

cat > "$UNIT_PATH" <<EOF
[Unit]
Description=Passion bot Docker self-healing check
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
WorkingDirectory=$PROJECT_DIR
Environment=PROJECT_DIR=$PROJECT_DIR
ExecStart=$CHECK_PATH
EOF

cat > "$TIMER_PATH" <<'EOF'
[Unit]
Description=Run Passion bot self-healing check every minute

[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now passion-bot-watchdog.timer
systemctl enable docker
echo "Watchdog installed: $(systemctl is-active passion-bot-watchdog.timer)"
