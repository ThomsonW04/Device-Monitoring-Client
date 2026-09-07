#!/bin/sh
# Run as root on the Raspberry Pi: sudo ./uninstall.sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo 'Run this uninstaller as root: sudo ./uninstall.sh' >&2
    exit 1
fi

service_name=agv-monitor.service
service_path=/etc/systemd/system/agv-monitor.service
ca_certificate_path=/usr/local/share/ca-certificates/agv-monitor-ca.crt

if command -v systemctl >/dev/null 2>&1; then
    systemctl disable --now "$service_name" >/dev/null 2>&1 || true
fi

rm -f -- "$service_path"

if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload || true
    systemctl reset-failed "$service_name" >/dev/null 2>&1 || true
fi

# These directories and logs are created exclusively by install.sh/the agent.
rm -rf -- /etc/agv-monitor /var/lib/agv-monitor /usr/local/lib/agv-monitor
find /var/log -maxdepth 1 -type f -name 'AGV-Monitor-*.log' -delete

rm -f -- "$ca_certificate_path" "${ca_certificate_path}.new"
if command -v update-ca-certificates >/dev/null 2>&1; then
    update-ca-certificates --fresh
else
    echo 'Removed the AGV CA file, but update-ca-certificates is unavailable; rebuild the system trust store manually.' >&2
fi

echo 'AGV Monitoring has been removed: service, configuration, state, CA, and snapshot logs are gone.'
echo 'Shared system-journal entries are retained under the host journal retention policy.'
