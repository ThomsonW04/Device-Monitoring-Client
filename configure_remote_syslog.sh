#!/bin/sh
# Run as root on a RevPi after deploying the monitoring agent.
# Usage: ./configure_remote_syslog.sh 10.54.168.27 [5001]
set -eu

collector_host=${1:?usage: configure_remote_syslog.sh COLLECTOR_HOST [PORT]}
collector_port=${2:-5001}

case "$collector_host" in
  *[!0-9A-Fa-f:.*-]*|'') echo 'Collector host must be an IP address or hostname.' >&2; exit 2 ;;
esac
case "$collector_port" in
  *[!0-9]*|'') echo 'Collector port must be numeric.' >&2; exit 2 ;;
esac

if ! command -v rsyslogd >/dev/null 2>&1; then
  echo 'rsyslog is not installed; install it before enabling remote kernel logging.' >&2
  exit 1
fi

install -d -m 0755 /etc/rsyslog.d
printf '%s\n' '# AGV Monitoring: duplicate all local syslog messages to the independent collector.' \
  "*.* @${collector_host}:${collector_port};RSYSLOG_SyslogProtocol23Format" \
  > /etc/rsyslog.d/60-agv-monitor-remote.conf
systemctl restart rsyslog
echo "Remote syslog enabled: UDP ${collector_host}:${collector_port}"
