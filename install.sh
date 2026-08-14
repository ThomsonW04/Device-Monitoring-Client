#!/bin/sh
# Run as root on the Raspberry Pi: sudo ./install.sh
set -eu

install -d -m 0750 /etc/agv-monitor /var/lib/agv-monitor /usr/local/lib/agv-monitor
install -m 0755 telemetry_agent.py /usr/local/lib/agv-monitor/telemetry_agent.py
install -m 0644 agv-monitor.service /etc/systemd/system/agv-monitor.service
if [ ! -f /etc/agv-monitor/telemetry.conf ]; then
    install -m 0600 telemetry.conf.example /etc/agv-monitor/telemetry.conf
    systemctl daemon-reload
    echo 'Edit /etc/agv-monitor/telemetry.conf, then rerun: sudo systemctl enable --now agv-monitor'
else
    systemctl daemon-reload
    systemctl enable --now agv-monitor.service
fi
