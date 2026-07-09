#!/usr/bin/env bash
#
# Install the nightly autodiscovery timer. Idempotent; safe to re-run.
#
#   sudo ./deploy/install.sh
#
set -euo pipefail
cd "$(dirname "$0")/.."

echo "Ensuring logs/ exists..."
mkdir -p logs

echo "Making wrapper executable..."
chmod +x deploy/run-autodiscover.sh

echo "Installing systemd unit files..."
install -m 0644 deploy/strategy-discovery.service /etc/systemd/system/strategy-discovery.service
install -m 0644 deploy/strategy-discovery.timer   /etc/systemd/system/strategy-discovery.timer

echo "Installing logrotate config..."
install -m 0644 deploy/logrotate-strategy-discovery /etc/logrotate.d/strategy-discovery

echo "Reloading systemd and enabling the timer..."
systemctl daemon-reload
systemctl enable --now strategy-discovery.timer

echo
echo "Done. Inspect with:"
echo "  systemctl list-timers strategy-discovery.timer"
echo "  systemctl status strategy-discovery.timer"
echo "Trigger a run by hand (respects the lock, writes logs/autodiscover.log):"
echo "  systemctl start strategy-discovery.service"
