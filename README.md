# AGV device telemetry agent

A dependency-free Python 3 systemd daemon for Linux/Raspberry Pi. It samples CPU, memory, root-disk usage, CPU temperature, 1-minute load, and `eth0`/`eth1` network utilisation every five seconds, then uploads a batch every five minutes. Network utilisation is combined receive/send traffic as a percentage of the interface's negotiated link speed. Unsent readings are stored in `/var/lib/agv-monitor/` and retry after networking/server failures.

The five-second default is intentional: it produces 60 samples per five-minute upload. The server accepts up to 90 samples, leaving 50% headroom for a delayed upload.

After a daemon start, the first upload occurs after the second sample (five
seconds with the default configuration). This gives CPU and network utilisation
enough counter history to report meaningful percentages immediately.

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

## Useful checks

```sh
sudo journalctl -u agv-monitor -f
sudo systemctl restart agv-monitor
sudo systemctl status agv-monitor
```

The service has no third-party dependencies and runs as root only because the unit's filesystem hardening requires a root-managed configuration and state directory. The process itself only reads `/proc` and `/sys`, and writes its queue under `/var/lib/agv-monitor`.

If you change the sampling interval to below five seconds, increase the upload cadence too: the server accepts no more than 90 readings per request. After an outage, the agent drains queued readings in consecutive 90-reading requests once the server is available.
