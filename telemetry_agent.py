#!/usr/bin/env python3
"""Collect Linux hardware telemetry and periodically deliver it to the monitor."""

# This file deliberately uses only the Python standard library so it can run on
# a Raspberry Pi without installing packages.

import json
import logging
import os
import signal
import socket
import sys
import time
import uuid
from datetime import datetime, timezone
from logging.handlers import SysLogHandler
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


# All device-specific settings belong in the EnvironmentFile specified here.
CONFIG_PATH = Path(os.environ.get("AGV_MONITOR_CONFIG", "/etc/agv-monitor/telemetry.conf"))
DEFAULTS = {
    "SERVER_URL": "https://monitor.example.com/api/v1/telemetry",
    "DEVICE_TOKEN": "",
    "SAMPLE_INTERVAL_SECONDS": "5",
    "UPLOAD_INTERVAL_SECONDS": "300",
    "DISK_PATH": "/",
    "SPOOL_PATH": "/var/lib/agv-monitor/telemetry-spool.jsonl",
    "MAX_SPOOL_SAMPLES": "120960",  # seven days at the five-second default
    "HTTP_TIMEOUT_SECONDS": "20",
}
MAX_BATCH_SIZE = 90  # The server API's explicit maximum.
NETWORK_INTERFACES = ("eth0", "eth1")
STOP_REQUESTED = False


def configure_logging() -> logging.Logger:
    logger = logging.getLogger("agv-monitor")
    logger.setLevel(logging.INFO)
    handler = SysLogHandler(address="/dev/log") if Path("/dev/log").exists() else logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(name)s[%(process)d]: %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


LOG = configure_logging()


def load_config() -> dict[str, str]:
    config = DEFAULTS.copy()
    if CONFIG_PATH.exists():
        for raw_line in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            config[key.strip()] = value.strip().strip('"').strip("'")
    # systemd EnvironmentFile values override the file, useful for secret stores.
    config.update({key: os.environ[key] for key in DEFAULTS if key in os.environ})
    if not config["DEVICE_TOKEN"] or config["DEVICE_TOKEN"] == "replace-with-device-token":
        raise ValueError("DEVICE_TOKEN must be set in " + str(CONFIG_PATH))
    for key in ("SAMPLE_INTERVAL_SECONDS", "UPLOAD_INTERVAL_SECONDS", "HTTP_TIMEOUT_SECONDS"):
        if float(config[key]) <= 0:
            raise ValueError(f"{key} must be greater than zero")
    if int(config["MAX_SPOOL_SAMPLES"]) <= 0:
        raise ValueError("MAX_SPOOL_SAMPLES must be greater than zero")
    return config


def read_cpu_times() -> tuple[int, int]:
    fields = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:]
    values = [int(value) for value in fields]
    total = sum(values)
    idle = values[3] + (values[4] if len(values) > 4 else 0)  # idle + iowait
    return total, idle


def cpu_percent(previous: tuple[int, int] | None) -> tuple[float, tuple[int, int]]:
    current = read_cpu_times()
    if previous is None:
        return 0.0, current
    total_delta, idle_delta = current[0] - previous[0], current[1] - previous[1]
    usage = 0.0 if total_delta <= 0 else (1 - idle_delta / total_delta) * 100
    return round(max(0.0, min(100.0, usage)), 1), current


def memory_percent() -> float:
    values: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        key, value = line.split(":", 1)
        values[key] = int(value.split()[0])
    total = values["MemTotal"]
    available = values.get("MemAvailable", values.get("MemFree", 0))
    return round((total - available) * 100 / total, 1)


def disk_percent(path: str) -> float:
    stats = os.statvfs(path)
    total = stats.f_blocks * stats.f_frsize
    available = stats.f_bavail * stats.f_frsize
    return round((total - available) * 100 / total, 1) if total else 0.0


def cpu_temperature() -> float | None:
    candidates = sorted(Path("/sys/class/thermal").glob("thermal_zone*/temp"))
    for path in candidates:
        try:
            value = float(path.read_text(encoding="utf-8").strip())
            return round(value / 1000 if value > 1000 else value, 1)
        except (OSError, ValueError):
            continue
    return None


def network_utilisation(
    previous: dict[str, tuple[int, int, float]] | None,
) -> tuple[dict[str, float | None], dict[str, tuple[int, int, float]]]:
    """Return combined RX/TX use as a percentage of each interface link speed."""
    now = time.monotonic()
    current: dict[str, tuple[int, int, float]] = {}
    utilisation: dict[str, float | None] = {}
    for interface in NETWORK_INTERFACES:
        base = Path("/sys/class/net") / interface
        try:
            rx_bytes = int((base / "statistics/rx_bytes").read_text(encoding="utf-8").strip())
            tx_bytes = int((base / "statistics/tx_bytes").read_text(encoding="utf-8").strip())
            speed_mbps = int((base / "speed").read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            utilisation[interface] = None
            continue
        current[interface] = (rx_bytes, tx_bytes, now)
        old = previous.get(interface) if previous else None
        if old is None or speed_mbps <= 0:
            utilisation[interface] = None
            continue
        transferred_bytes = (rx_bytes - old[0]) + (tx_bytes - old[1])
        elapsed = now - old[2]
        if transferred_bytes < 0 or elapsed <= 0:
            utilisation[interface] = None
            continue
        percent = transferred_bytes * 8 * 100 / (elapsed * speed_mbps * 1_000_000)
        utilisation[interface] = round(max(0.0, min(100.0, percent)), 2)
    return utilisation, current


def collect(
    previous_cpu: tuple[int, int] | None,
    previous_network: dict[str, tuple[int, int, float]] | None,
    disk_path: str,
) -> tuple[dict, tuple[int, int], dict[str, tuple[int, int, float]]]:
    cpu, current_cpu = cpu_percent(previous_cpu)
    network, current_network = network_utilisation(previous_network)
    try:
        load = round(os.getloadavg()[0], 2)
    except OSError:
        load = None
    return {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "cpu_percent": cpu,
        "memory_percent": memory_percent(),
        "disk_percent": disk_percent(disk_path),
        "cpu_temp_c": cpu_temperature(),
        "load_1m": load,
        "eth0_percent": network["eth0"],
        "eth1_percent": network["eth1"],
        "extra": {"hostname": socket.gethostname()},
    }, current_cpu, current_network


def append_sample(spool_path: Path, sample: dict, maximum_samples: int) -> None:
    spool_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    with spool_path.open("a", encoding="utf-8") as spool:
        spool.write(json.dumps(sample, separators=(",", ":")) + "\n")
        spool.flush()
        os.fsync(spool.fileno())
    samples = pending_samples(spool_path)
    if len(samples) > maximum_samples:
        LOG.warning("Telemetry spool limit reached; discarding %s oldest samples", len(samples) - maximum_samples)
        rewrite_spool(spool_path, samples[-maximum_samples:])


def pending_samples(spool_path: Path) -> list[dict]:
    if not spool_path.exists():
        return []
    samples = []
    for line in spool_path.read_text(encoding="utf-8").splitlines():
        try:
            samples.append(json.loads(line))
        except json.JSONDecodeError:
            LOG.warning("Ignoring corrupt telemetry spool line")
    return samples


def rewrite_spool(spool_path: Path, samples: list[dict]) -> None:
    temporary = spool_path.with_suffix(spool_path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as spool:
        for sample in samples:
            spool.write(json.dumps(sample, separators=(",", ":")) + "\n")
        spool.flush()
        os.fsync(spool.fileno())
    temporary.replace(spool_path)


def remove_sent_samples(spool_path: Path, count: int) -> None:
    rewrite_spool(spool_path, pending_samples(spool_path)[count:])


def upload(config: dict[str, str]) -> bool:
    spool_path = Path(config["SPOOL_PATH"])
    while samples := pending_samples(spool_path)[:MAX_BATCH_SIZE]:
        payload = json.dumps({"batch_id": str(uuid.uuid4()), "samples": samples}).encode()
        request = Request(
            config["SERVER_URL"], payload,
            headers={"Authorization": "Bearer " + config["DEVICE_TOKEN"], "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=float(config["HTTP_TIMEOUT_SECONDS"])) as response:
                if response.status != 202:
                    raise OSError(f"unexpected HTTP status {response.status}")
            remove_sent_samples(spool_path, len(samples))
            LOG.info("Uploaded %s telemetry samples", len(samples))
        except HTTPError as error:
            LOG.error("Telemetry upload rejected: HTTP %s", error.code)
            return False
        except (URLError, OSError) as error:
            LOG.warning("Telemetry upload deferred: %s", error)
            return False
    return True


def stop_handler(_signum: int, _frame: object) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True


def main() -> int:
    config = load_config()
    sample_every = float(config["SAMPLE_INTERVAL_SECONDS"])
    upload_every = float(config["UPLOAD_INTERVAL_SECONDS"])
    if sample_every * MAX_BATCH_SIZE < upload_every:
        LOG.warning("Upload interval produces more than %s samples; multiple uploads will be required", MAX_BATCH_SIZE)
    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    previous_cpu = None
    previous_network = None
    next_sample = time.monotonic()
    # CPU and network utilisation are rates. Wait for the second collection
    # before the first upload so the dashboard never receives a baseline 0/—.
    next_upload = next_sample + sample_every
    while not STOP_REQUESTED:
        now = time.monotonic()
        if now >= next_sample:
            try:
                sample, previous_cpu, previous_network = collect(
                    previous_cpu, previous_network, config["DISK_PATH"]
                )
                append_sample(Path(config["SPOOL_PATH"]), sample, int(config["MAX_SPOOL_SAMPLES"]))
            except (OSError, ValueError, KeyError) as error:
                LOG.exception("Unable to collect telemetry: %s", error)
            next_sample += sample_every
            if next_sample <= now:
                next_sample = now + sample_every
        if now >= next_upload:
            upload(config)
            next_upload += upload_every
            if next_upload <= now:
                next_upload = now + upload_every
        time.sleep(min(1.0, max(0.05, next_sample - time.monotonic(), next_upload - time.monotonic())))
    upload(config)  # Best effort flush when systemd stops the service.
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as error:
        LOG.error("Configuration error: %s", error)
        raise SystemExit(2)
