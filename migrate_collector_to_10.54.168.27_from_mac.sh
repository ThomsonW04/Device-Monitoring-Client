#!/bin/zsh
# Discover reachable AGVs and move their monitoring destinations to .27.
# Run on macOS: ./migrate_collector_to_10.54.168.27_from_mac.sh
# Prerequisite: brew install sshpass

set -u
set -o pipefail

old_collector='10.54.168.13'
new_collector='10.54.168.27'
collector_port='5001'
ssh_options=(
  -o BatchMode=no
  -o ConnectTimeout=10
  -o StrictHostKeyChecking=accept-new
  -o UserKnownHostsFile="$HOME/.ssh/known_hosts"
)

fail() {
  print -u2 -- "Error: $*"
  exit 1
}

command -v sshpass >/dev/null 2>&1 || fail 'sshpass is required. Install it once with: brew install sshpass'

scan_results=$(mktemp "${TMPDIR:-/tmp}/agv-online.XXXXXX")
remote_script=$(mktemp "${TMPDIR:-/tmp}/agv-collector-migration.XXXXXX")
chmod 600 "$scan_results" "$remote_script"
trap 'rm -f "$scan_results" "$remote_script"' EXIT

cat > "$remote_script" <<'REMOTE_SCRIPT'
#!/bin/sh
set -eu
old_collector='10.54.168.13'
new_collector='10.54.168.27'
collector_port='5001'
telemetry_config='/etc/agv-monitor/telemetry.conf'
agent_program='/usr/local/lib/agv-monitor/telemetry_agent.py'
rsyslog_config='/etc/rsyslog.d/60-agv-monitor-remote.conf'
backup_suffix="pre-collector-migration-$(date -u +%Y%m%dT%H%M%SZ)"

backup_file() {
    [ -f "$1" ] && cp -p "$1" "$1.$backup_suffix" || true
}

agent_result='agent config not present'
if [ -f "$telemetry_config" ]; then
    backup_file "$telemetry_config"
    temporary_config=$(mktemp "${telemetry_config}.new.XXXXXX")
    sed "s#\\(https\\?://\\)${old_collector}#\\1${new_collector}#g" "$telemetry_config" > "$temporary_config"
    if cmp -s "$telemetry_config" "$temporary_config"; then
        rm -f "$temporary_config"
        agent_result='agent URL already uses a different collector (unchanged)'
    else
        chmod 600 "$temporary_config"
        chown root:root "$temporary_config"
        mv "$temporary_config" "$telemetry_config"
        systemctl restart agv-monitor.service
        systemctl is-active --quiet agv-monitor.service
        agent_result="agent TCP telemetry moved to ${new_collector}"
    fi
fi

# All released agent versions use telemetry.conf.  This fallback also updates
# the built-in default in the installed program for an incomplete/legacy setup.
if [ -f "$agent_program" ] && grep -Fq "$old_collector" "$agent_program"; then
    backup_file "$agent_program"
    temporary_program=$(mktemp "${agent_program}.new.XXXXXX")
    sed "s#${old_collector}#${new_collector}#g" "$agent_program" > "$temporary_program"
    chmod 755 "$temporary_program"
    chown root:root "$temporary_program"
    mv "$temporary_program" "$agent_program"
    if systemctl list-unit-files agv-monitor.service >/dev/null 2>&1; then
        systemctl restart agv-monitor.service
        systemctl is-active --quiet agv-monitor.service
    fi
    agent_result="${agent_result}; agent fallback URL moved to ${new_collector}"
fi

rsyslog_result='rsyslog is not installed'
if command -v rsyslogd >/dev/null 2>&1; then
    backup_file "$rsyslog_config"
    install -d -m 0755 /etc/rsyslog.d
    temporary_rsyslog=$(mktemp "${rsyslog_config}.new.XXXXXX")
    printf '%s\n' \
        '# AGV Monitoring: duplicate all local syslog messages to the independent collector.' \
        "*.* @${new_collector}:${collector_port};RSYSLOG_SyslogProtocol23Format" \
        > "$temporary_rsyslog"
    chmod 644 "$temporary_rsyslog"
    chown root:root "$temporary_rsyslog"
    mv "$temporary_rsyslog" "$rsyslog_config"
    systemctl restart rsyslog.service
    systemctl is-active --quiet rsyslog.service
    rsyslog_result="rsyslog UDP moved to ${new_collector}:${collector_port}"
fi
printf 'OK: %s; %s\n' "$agent_result" "$rsyslog_result"
REMOTE_SCRIPT
chmod 700 "$remote_script"

print 'Scanning 10.54.192.2–255 and 10.54.193.1–254 with parallel ICMP ping…'
{
  printf '10.54.192.%s\n' {2..255}
  printf '10.54.193.%s\n' {1..254}
} | xargs -P 64 -n 1 sh -c 'ping -c 1 -W 1000 "$1" >/dev/null 2>&1 && printf "%s\n" "$1"' sh > "$scan_results"

typeset -a online_targets
online_targets=("${(@f)$(sort -t. -k1,1n -k2,2n -k3,3n -k4,4n "$scan_results")}")
(( ${#online_targets[@]} > 0 )) || fail 'No hosts replied to ping in either range.'

print
print -- "Online hosts (${#online_targets[@]}):"
for target in "${online_targets[@]}"; do print -- "  - $target"; done
print
read "ssh_username?SSH username for every AGV: "
[[ -n "$ssh_username" ]] || fail 'An SSH username is required.'
read -s "ssh_password?SSH password for every AGV: "
print
[[ -n "$ssh_password" ]] || fail 'An SSH password is required.'

print 'This creates timestamped remote backups, then:'
print -- "  • sets rsyslog UDP forwarding to ${new_collector}:${collector_port}"
print -- "  • replaces only ${old_collector} in the agent SERVER_URL with ${new_collector}"
print '  • restarts rsyslog and agv-monitor only; it never reboots an AGV'
read "confirmation?Apply this to every online host shown above? [y/N]: "
case ${confirmation:-N} in
  Y|y|yes|YES|Yes) ;;
  *) print 'Cancelled.'; exit 0 ;;
esac

typeset -a successful failed
for target in "${online_targets[@]}"; do
  print -- "----- $target -----"
  if result=$(SSHPASS="$ssh_password" sshpass -e ssh "${ssh_options[@]}" \
      "$ssh_username@$target" 'sudo -n /bin/sh -s' < "$remote_script" 2>&1); then
    print -- "$result"
    successful+=("$target")
  else
    print -u2 -- "FAILED: $result"
    failed+=("$target")
  fi
done

print
print '================================================'
print -- "Successful: ${#successful[@]}"
for target in "${successful[@]}"; do print -- "  - $target"; done
print -- "Failed:     ${#failed[@]}"
for target in "${failed[@]}"; do print -- "  - $target"; done
print '================================================'
(( ${#failed[@]} == 0 ))
