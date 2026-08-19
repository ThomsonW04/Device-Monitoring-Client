#!/bin/sh
# Run as root on the Raspberry Pi: sudo ./install.sh
set -eu

prompt_value() {
    prompt_text=$1
    default_value=$2
    printf '%s [%s]: ' "$prompt_text" "$default_value" >&2
    read -r entered_value
    if [ -n "$entered_value" ]; then
        printf '%s' "$entered_value"
    else
        printf '%s' "$default_value"
    fi
}

prompt_secret() {
    prompt_text=$1
    printf '%s: ' "$prompt_text" >&2
    stty -echo
    if ! read -r entered_value; then
        stty echo
        printf '\n' >&2
        return 1
    fi
    stty echo
    printf '\n' >&2
    printf '%s' "$entered_value"
}

prompt_yes_no() {
    prompt_text=$1
    default_value=$2
    printf '%s [%s]: ' "$prompt_text" "$default_value" >&2
    read -r entered_value
    case ${entered_value:-$default_value} in
        Y|y|yes|YES|Yes) return 0 ;;
        N|n|no|NO|No) return 1 ;;
        *)
            echo 'Please enter y or n.' >&2
            prompt_yes_no "$prompt_text" "$default_value"
            return $? ;;
    esac
}

install_ca_certificate() {
    ca_destination_path=/usr/local/share/ca-certificates/agv-monitor-ca.crt
    ca_temporary_path=${ca_destination_path}.new
    base64 -d > "$ca_temporary_path" <<'CERTIFICATE'
LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0tCk1JSUZ4ekNDQTYrZ0F3SUJBZ0lVV3NTcE82V0J0UXY1ZXlmMERoSTJ6RWMwci9rd0RRWUpLb1pJaHZjTkFRRUwKQlFBd2N6RWlNQ0FHQTFVRUNnd1pVbTk1WVd3Z1RXRnBiQ0JCUjFZZ1RXOXVhWFJ2Y21sdVp6RW1NQ1FHQTFVRQpDd3dkVUhKcGRtRjBaU0JEWlhKMGFXWnBZMkYwWlNCQmRYUm9iM0pwZEhreEpUQWpCZ05WQkFNTUhGSnZlV0ZzCklFMWhhV3dnUVVkV0lFMXZibWwwYjNKcGJtY2dRMEV3SGhjTk1qWXdPREU0TVRFek9EQXhXaGNOTXpjd09ERTQKTVRFek9EQXhXakJ6TVNJd0lBWURWUVFLREJsU2IzbGhiQ0JOWVdsc0lFRkhWaUJOYjI1cGRHOXlhVzVuTVNZdwpKQVlEVlFRTERCMVFjbWwyWVhSbElFTmxjblJwWm1sallYUmxJRUYxZEdodmNtbDBlVEVsTUNNR0ExVUVBd3djClVtOTVZV3dnVFdGcGJDQkJSMVlnVFc5dWFYUnZjbWx1WnlCRFFUQ0NBaUl3RFFZSktvWklodmNOQVFFQkJRQUQKZ2dJUEFEQ0NBZ29DZ2dJQkFMV3RpTURaTjBySHdDZGUyTExIMzc3QXBIZkU2Z054Y3VwTHlKOEJjRFZteTFlLwpFY3BzL3pjdERuVFdGT3lnNnNUNE9ObHpYc0wwZjVON3Z5Z3lPZ2JtMEhjWEhNeC8wQlQyY3I4Vk1Xdkx1ckljCjA5Q3o2anlHTmh5K1RNK0JMdkZwWFZYb0xHQndRL3N4d2lFM2RCYUE4UFBYdm03TUVac0FuRk9zYmtka2FPWDEKREN5aWMvTEkxakY3QmdFSXhXL2xCeWVCOGtHVGhrWldzVW5lWE5KNnNxeWZuSnJtbU9lVVlNdURKYUxpM01qSwpHWkFPNDhwYVd3aTVuVjFVamEyU3JOQWl6Wkg1L1FlN0Q4Nk5tWE5uNWxQKzFLa3R4MGoxVGQxMDViZS9KL1pBCm4xRlQvMWVxMVI4emtqWVJkMWM1RE81VktySHBvMGFpNVFkc00wYkVIZlBOZi9MaU1KNzM3R0drOUJOekh3ZGMKZmJlZ2NHVzRmNU4xZVBRS012UnoxZ2RPZUhtdmgyR2p3Y0tQS1lPS294U1R5Sy9MZTFEVzIxaUJ5TEU1SFVwego5YklnOXhEanRvcmU0K0xFYysrWWZGMmpTZ2pvM0tFOFdmMFVGRkZsY29ZNWtWMXBzaEJGMlBEV2VMdFBZYWRxCjcyMnJkYStFbWNGY1lhWkluQW8wL0I0NDFLR0hmTWZVQndkN2JuRzdRTG8wV1JBRHVuQ1pWYnRaNTE1NkIyNUEKUmtyTmdJQVB4UTk5YzJRTzlKOXIyd1pGYVdweVhEdjZiZEppSWlqK3BpaDVNak9peWc3SWZ1VnVMWnNuSUo4SgpGaW9pVEl2MVBDbmtqaktWWUZ2RjM0ZWNnT2RsWnRUeDdqK3pJM05zU1o0cDNqUjR2NHh2RllvWHd1a0pBZ01CCkFBR2pVekJSTUIwR0ExVWREZ1FXQkJUSm5tKzdyb1NEd09rYTBlRXdTaFlRTU02Q056QWZCZ05WSFNNRUdEQVcKZ0JUSm5tKzdyb1NEd09rYTBlRXdTaFlRTU02Q056QVBCZ05WSFJNQkFmOEVCVEFEQVFIL01BMEdDU3FHU0liMwpEUUVCQ3dVQUE0SUNBUUF2L0RsckpwRDRHdDFXaXpKaCt4QVgyUThaS21qNGhzRE1WTFJnWTA1YWRnYmk1RFlWCk8zaXhLL2E5L0preVgvUkJiMk1xZ0hhTnY2UElKWVgvajlvWlZDQ1FYTS9BbVdiSmhZWkRudk43cVRSMWpPbjUKdkMzZlpMRWVlaDVMc0xqa25DanNTV3BvaTg4bEJ4VmRNeWs2Z1NkSWN1MVJUTkppc0VyVWdGL09sbkNEWVVrSwpHaXlFVkxDTzhMYkU1b3hURHhPMGMwcU40OGFlQjVwZkpLYjRtcUpTNHBYdlcvTDh1bVpvM2ZZM3B4MHF4WnQwCi9qZlp5Q052dUl4eHBlWmhZR1c0SGtzREQ0VEZJU2xzUXdTWWdMNjdoZFNFYkFNWkxTKzBNV0Z3Y0RSRDJEZXEKS1RXSEhRQkhUR2EvU1JOblM2Y01kLzlpdHV5enhvV2NadGhsd1FUSi9acWxld21CUnFkRTRFYXAxcGxOOUhWUwp0ei9yWHRHOXRoWHh6UkppYkNpU3Fya3c0SVZJcFp2L1RGZ3lVTG9kelAwcmRmSjVBcittM295aEl1U0VZUWtaCnJ3OUI1bi9FMlR5ZWpQNUFtcW5rOWJidy9LemxTU1MxTHd5cWNjNndOZUgxMTQxVUlWaWtnVWtXTlpJQlBYdVIKVjlhdzR5L2FHdCtHcW1odXlOV200ZHZ0dFhZaXBXTEkwblBQNThNWEFXSWx0aFlKTjdaVDluQ0llVGZnVDVxSQpoYkRRSHJRTHVtSWZGSVFiZjYvQXFibFBVMTBQL2RYenpTQWxCVXV4YkJVekJlZEkzZU5DSk90MmV3VXFYN3FZCndPN2U0T2hnbzFyQWk5ZEJ5UjBpQkJwK010Wk9rZG4wdEFTUDFCT0RvN3RzdmM5MlUwdUM4VjB0a3c9PQotLS0tLUVORCBDRVJUSUZJQ0FURS0tLS0tCg==
CERTIFICATE
    chmod 0644 "$ca_temporary_path"
    mv "$ca_temporary_path" "$ca_destination_path"
    update-ca-certificates
}

registration_url() {
    python3 -c '
import sys
from urllib.parse import urlsplit, urlunsplit

telemetry_url = urlsplit(sys.argv[1])
if not telemetry_url.scheme or not telemetry_url.netloc:
    raise SystemExit("Server telemetry URL must be a full http:// or https:// URL")
print(urlunsplit((telemetry_url.scheme, telemetry_url.netloc, "/api/v1/devices/register", "", "")))
' "$1"
}

register_device() {
    telemetry_url=$1
    admin_username=$2
    admin_password=$3
    device_name=$4
    device_ip=$5
    endpoint=$(registration_url "$telemetry_url")
    payload=$(AGV_REGISTER_USERNAME="$admin_username" \
        AGV_REGISTER_PASSWORD="$admin_password" \
        AGV_REGISTER_NAME="$device_name" \
        AGV_REGISTER_IP="$device_ip" \
        python3 -c '
import json
import os

print(json.dumps({
    "username": os.environ["AGV_REGISTER_USERNAME"],
    "password": os.environ["AGV_REGISTER_PASSWORD"],
    "name": os.environ["AGV_REGISTER_NAME"],
    "ip_address": os.environ["AGV_REGISTER_IP"],
}))
')
    response=$(printf '%s' "$payload" | curl --fail --silent --show-error \
        -H 'Content-Type: application/json' \
        --data-binary @- \
        "$endpoint")
    printf '%s' "$response" | python3 -c '
import json
import sys

response = json.load(sys.stdin)
token = response.get("device_token")
if not isinstance(token, str) or not token:
    raise SystemExit("Registration response did not contain a device token")
print(token)
'
}

install -d -m 0750 /etc/agv-monitor /var/lib/agv-monitor /usr/local/lib/agv-monitor
install -m 0755 telemetry_agent.py /usr/local/lib/agv-monitor/telemetry_agent.py
install -m 0644 agv-monitor.service /etc/systemd/system/agv-monitor.service
if [ ! -f /etc/agv-monitor/telemetry.conf ]; then
    echo 'Configure this device (press Enter to accept a displayed default).'
    if ! command -v curl >/dev/null 2>&1; then
        echo 'curl is required to register this device.' >&2
        exit 1
    fi
    if prompt_yes_no 'Use HTTPS with the AGV Monitoring CA (recommended)' 'Y'; then
        install_ca_certificate
        server_url=$(prompt_value 'Server telemetry URL' 'https://10.54.168.13:5001/api/v1/telemetry')
    else
        echo 'Warning: HTTP leaves device tokens and telemetry visible to the network.' >&2
        server_url=$(prompt_value 'Server telemetry URL' 'http://10.54.168.13:8085/api/v1/telemetry')
    fi
    device_name=$(prompt_value 'Device name' "$(hostname)")
    device_ip=$(prompt_value 'Device IP address' '')
    if [ -z "$device_name" ] || [ -z "$device_ip" ]; then
        echo 'A device name and IP address are required; configuration was not created.' >&2
        exit 1
    fi
    admin_username=$(prompt_value 'Monitoring administrator username' 'admin')
    admin_password=$(prompt_secret 'Monitoring administrator password')
    if [ -z "$admin_username" ] || [ -z "$admin_password" ]; then
        echo 'Administrator credentials are required; configuration was not created.' >&2
        exit 1
    fi
    echo 'Registering device with the monitoring server...'
    device_token=$(register_device "$server_url" "$admin_username" "$admin_password" "$device_name" "$device_ip")
    sample_interval=$(prompt_value 'Sample interval in seconds' '5')
    upload_interval=$(prompt_value 'Upload interval in seconds' '300')
    disk_path=$(prompt_value 'Filesystem path to monitor' '/')
    max_spool_samples=$(prompt_value 'Maximum locally queued samples' '120960')
    http_timeout=$(prompt_value 'HTTP timeout in seconds' '20')
    umask 077
    {
        printf '%s\n' '# Generated by install.sh. Device-specific settings are kept together here.'
        printf 'SERVER_URL=%s\n' "$server_url"
        printf 'DEVICE_TOKEN=%s\n' "$device_token"
        printf 'SAMPLE_INTERVAL_SECONDS=%s\n' "$sample_interval"
        printf '%s\n' 'INTERNAL_SAMPLE_INTERVAL_SECONDS=0.5'
        printf 'UPLOAD_INTERVAL_SECONDS=%s\n' "$upload_interval"
        printf 'DISK_PATH=%s\n' "$disk_path"
        printf '%s\n' 'SPOOL_PATH=/var/lib/agv-monitor/telemetry-spool.jsonl'
        printf 'MAX_SPOOL_SAMPLES=%s\n' "$max_spool_samples"
        printf 'HTTP_TIMEOUT_SECONDS=%s\n' "$http_timeout"
        printf '%s\n' 'SNAPSHOT_CPU_THRESHOLD_PERCENT=95'
        printf '%s\n' 'SNAPSHOT_MEMORY_THRESHOLD_PERCENT=95'
        printf '%s\n' 'SNAPSHOT_STORAGE_THRESHOLD_PERCENT=90'
        printf '%s\n' 'SNAPSHOT_DISK_IO_THRESHOLD_PERCENT=95'
        printf '%s\n' 'SNAPSHOT_SWAP_THRESHOLD_PERCENT=95'
        printf '%s\n' 'SNAPSHOT_ETH0_THRESHOLD_PERCENT=95'
        printf '%s\n' 'SNAPSHOT_ETH1_THRESHOLD_PERCENT=95'
        printf '%s\n' 'SNAPSHOT_TEMPERATURE_THRESHOLD_C=80'
        printf '%s\n' 'SNAPSHOT_LOG_RETENTION_DAYS=30'
    } > /etc/agv-monitor/telemetry.conf
    chmod 0600 /etc/agv-monitor/telemetry.conf
    systemctl daemon-reload
    systemctl enable --now agv-monitor.service
else
    server_url=$(sed -n '/^SERVER_URL=/{s/^SERVER_URL=//;p;q;}' /etc/agv-monitor/telemetry.conf)
    case "$server_url" in
        https://*)
            echo 'Refreshing the AGV Monitoring CA for the configured HTTPS server.'
            install_ca_certificate
            ;;
    esac
    systemctl daemon-reload
    systemctl enable agv-monitor.service
    systemctl restart agv-monitor.service
fi
