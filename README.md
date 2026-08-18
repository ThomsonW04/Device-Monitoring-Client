# AGV device telemetry agent

A dependency-free Python 3 systemd daemon for Linux/Raspberry Pi. It samples CPU, memory, swap use, system uptime, root storage use, disk read/write active time, CPU temperature, 1-minute load, and `eth0`/`eth1` network utilisation every five seconds, then uploads a batch every five minutes. Disk read/write active time is the percentage of the sample interval that the device was busy serving I/O, equivalent to Task Manager's disk active-time view. Network utilisation is combined receive/send traffic as a percentage of the interface's negotiated link speed. It also reports each port's physical carrier state, allowing the server to alert when a cable or interface goes down. Unsent readings are stored in `/var/lib/agv-monitor/` and retry after networking/server failures.

The five-second default is intentional: it produces 60 samples per five-minute upload. The server accepts up to 90 samples, leaving 50% headroom for a delayed upload.

After a daemon start, the first upload occurs after the second sample (five
seconds with the default configuration). This gives CPU and network utilisation
enough counter history to report meaningful percentages immediately.

## High-utilisation diagnostic snapshots

When a metric first crosses its threshold, the agent writes a timestamped
diagnostic file to `/var/log/AGV-Monitor-<UTC timestamp>.log`. It captures the
triggering telemetry, top CPU and memory processes (including user, PID, and
command), memory, disk I/O, network counters, socket summary, and Raspberry Pi
thermal-throttling status when available. Storage events also include the 30
largest regular files on the monitored filesystem.

Thresholds are CPU/RAM/Disk I/O/Swap/eth0/eth1 at 95%, Storage at 90%, and CPU
temperature at 80°C. A snapshot is created every time a metric crosses from
below its threshold to at or above it. It does not create another snapshot at
each sampling interval while that same breach remains active. Snapshot logs
are removed automatically after 30 days.

The settings are global: an authenticated agent retrieves them from the server
at startup and again at 12:00 noon local time each day. Changes made in the
server Settings page therefore reach every device within 24 hours. The local
values in `/etc/agv-monitor/telemetry.conf` are safe fallbacks while the
server is unavailable:

```ini
SNAPSHOT_CPU_THRESHOLD_PERCENT=95
SNAPSHOT_MEMORY_THRESHOLD_PERCENT=95
SNAPSHOT_STORAGE_THRESHOLD_PERCENT=90
SNAPSHOT_DISK_IO_THRESHOLD_PERCENT=95
SNAPSHOT_SWAP_THRESHOLD_PERCENT=95
SNAPSHOT_ETH0_THRESHOLD_PERCENT=95
SNAPSHOT_ETH1_THRESHOLD_PERCENT=95
SNAPSHOT_TEMPERATURE_THRESHOLD_C=80
SNAPSHOT_LOG_RETENTION_DAYS=30
```

When a snapshot is created, the agent attaches a bounded copy of the log to
the same telemetry sample. The server stores it with the matching alert, where
it can be opened from the Alerts page.

## Install on each device

Copy this directory to the device and run:

```sh
sudo ./install.sh
sudo systemctl status agv-monitor
```

On its first run the installer prompts for `SERVER_URL`, the device's unique
`DEVICE_TOKEN`, and collection settings before starting the service. The token
entry is hidden while you type. The resulting root-only configuration remains
at `/etc/agv-monitor/telemetry.conf`; the installer never overwrites an
existing configuration. The token must be provisioned for the Pi's current
source IP address; the server rejects a token used from any other IP.

HTTPS is selected by default. Place the server's public `agv-monitor-ca.crt`
beside `install.sh`; the installer adds it to the Pi's system trust store using
`update-ca-certificates`. Select `n` only when deliberately connecting to a
plain-HTTP server: HTTP exposes telemetry and device tokens to the network.

## Useful checks

```sh
sudo journalctl -u agv-monitor -f
sudo systemctl restart agv-monitor
sudo systemctl status agv-monitor
```

The service has no third-party dependencies and runs as root only because the unit's filesystem hardening requires root-managed configuration, state, and diagnostic log directories. The process reads `/proc` and `/sys`, writes its queue under `/var/lib/agv-monitor`, and writes high-utilisation snapshots under `/var/log`.

If you change the sampling interval to below five seconds, increase the upload cadence too: the server accepts no more than 90 readings per request. After an outage, the agent drains queued readings in consecutive 90-reading requests once the server is available.
