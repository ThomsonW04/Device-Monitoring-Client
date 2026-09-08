#!/bin/sh
# Device-side automatic configuration wrapper. Run by bulk_install_from_mac.sh.
set -eu

secrets_file=.autoinstall-secrets

secret_value() {
    sed -n "s/^$1=//p" "$secrets_file" | head -n 1 | base64 -d
}

prompt_value() {
    printf '%s [%s]: ' "$1" "$2" >&2
    read -r value
    printf '%s' "${value:-$2}"
}

derive_device_name() {
    hostname | sed -E 's/^[Rr][Ee][Vv][Pp][Ii]//'
}

derive_device_ip() {
    number=$(printf '%s' "$1" | sed -nE 's/^[Aa][Gg][Vv]0*([0-9]+)$/\1/p')
    [ -n "$number" ] || return 1
    [ "$number" -gt 0 ] || return 1
    third=$((192 + ((number - 1) / 255)))
    fourth=$((1 + ((number - 1) % 255)))
    [ "$third" -le 255 ] || return 1
    printf '10.54.%s.%s' "$third" "$fourth"
}

show_configuration() {
    printf '\nAGV Monitoring automatic configuration\n'
    printf '  1) Device name:     %s\n' "$device_name"
    printf '  2) Device IP:       %s\n' "$device_ip"
    printf '  3) Server URL:      %s\n' "$server_url"
    printf '  4) Sample interval: %s seconds\n' "$sample_interval"
    printf '  5) Upload interval: %s seconds\n' "$upload_interval"
    printf '  6) Disk path:       %s\n' "$disk_path"
    printf '  7) Spool samples:   %s\n' "$max_spool_samples"
    printf '  8) HTTP timeout:    %s seconds\n\n' "$http_timeout"
}

edit_configuration() {
    while :; do
        show_configuration
        printf 'Edit option (1-8), or D when done: ' >&2
        read -r choice
        case "$choice" in
            1) device_name=$(prompt_value 'Device name' "$device_name") ;;
            2) device_ip=$(prompt_value 'Device IP address' "$device_ip") ;;
            3) server_url=$(prompt_value 'Server telemetry URL' "$server_url") ;;
            4) sample_interval=$(prompt_value 'Sample interval in seconds' "$sample_interval") ;;
            5) upload_interval=$(prompt_value 'Upload interval in seconds' "$upload_interval") ;;
            6) disk_path=$(prompt_value 'Filesystem path to monitor' "$disk_path") ;;
            7) max_spool_samples=$(prompt_value 'Maximum locally queued samples' "$max_spool_samples") ;;
            8) http_timeout=$(prompt_value 'HTTP timeout in seconds' "$http_timeout") ;;
            D|d) return ;;
            *) echo 'Please choose 1-8 or D.' >&2 ;;
        esac
    done
}

[ "$(id -u)" -eq 0 ] || { echo 'Must run as root.' >&2; exit 1; }
[ -f "$secrets_file" ] || { echo 'Missing deployment secrets.' >&2; exit 1; }

admin_username=$(secret_value ADMIN_USERNAME_B64)
admin_password=$(secret_value ADMIN_PASSWORD_B64)
server_url='https://10.54.168.13:5001/api/v1/telemetry'
device_name=$(derive_device_name)
device_ip=$(derive_device_ip "$device_name" || true)
sample_interval=5
upload_interval=300
disk_path=/
max_spool_samples=120960
http_timeout=90

while :; do
    show_configuration
    printf 'Install this configuration? [Y/n]: ' >&2
    read -r confirmation
    case "${confirmation:-Y}" in
        Y|y|yes|YES|Yes) break ;;
        N|n|no|NO|No) edit_configuration ;;
        *) echo 'Please enter Y or N.' >&2 ;;
    esac
done

# install.sh remains the single implementation of CA installation, registration,
# service setup, and updates. It receives only values that were just confirmed.
if [ -f /etc/agv-monitor/telemetry.conf ]; then
    echo 'Existing local configuration detected; it will be re-registered and replaced after server approval.'
fi
echo 'Registering this AGV with the monitoring server and applying the confirmed configuration...'
if ! AGV_MONITOR_NONINTERACTIVE=1 \
    AGV_MONITOR_SERVER_URL="$server_url" \
    AGV_MONITOR_DEVICE_NAME="$device_name" \
    AGV_MONITOR_DEVICE_IP="$device_ip" \
    AGV_MONITOR_ADMIN_USERNAME="$admin_username" \
    AGV_MONITOR_ADMIN_PASSWORD="$admin_password" \
    AGV_MONITOR_SAMPLE_INTERVAL="$sample_interval" \
    AGV_MONITOR_UPLOAD_INTERVAL="$upload_interval" \
    AGV_MONITOR_DISK_PATH="$disk_path" \
    AGV_MONITOR_MAX_SPOOL_SAMPLES="$max_spool_samples" \
    AGV_MONITOR_HTTP_TIMEOUT="$http_timeout" \
    ./install.sh; then
    echo 'Installation failed while applying the confirmed configuration.' >&2
    exit 1
fi

echo 'AGV Monitor installation completed successfully.'
