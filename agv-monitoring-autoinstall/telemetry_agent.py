#!/usr/bin/env python3
"""Collect Linux hardware telemetry and periodically deliver it to the monitor."""

# This file deliberately uses only the Python standard library so it can run on
# a Raspberry Pi without installing packages.

import hashlib
import json
import logging
import os
import pwd
import re
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from heapq import heappush, heapreplace
import uuid
from datetime import datetime, timedelta, timezone
from logging.handlers import SysLogHandler
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


# All device-specific settings belong in the EnvironmentFile specified here.
CONFIG_PATH = Path(os.environ.get("AGV_MONITOR_CONFIG", "/etc/agv-monitor/telemetry.conf"))
AGENT_VERSION = "1.3.0"
DEFAULTS = {
    "SERVER_URL": "https://10.54.168.27:8085/api/v1/telemetry",
    "DEVICE_TOKEN": "",
    "SAMPLE_INTERVAL_SECONDS": "5",
    "INTERNAL_SAMPLE_INTERVAL_SECONDS": "0.5",
    "UPLOAD_INTERVAL_SECONDS": "300",
    "DISK_PATH": "/",
    "SPOOL_PATH": "/var/lib/agv-monitor/telemetry-spool.jsonl",
    "MAX_SPOOL_SAMPLES": "120960",  # seven days at the five-second default
    "HTTP_TIMEOUT_SECONDS": "20",
    "SNAPSHOT_CPU_THRESHOLD_PERCENT": "95",
    "SNAPSHOT_MEMORY_THRESHOLD_PERCENT": "95",
    "SNAPSHOT_STORAGE_THRESHOLD_PERCENT": "90",
    "SNAPSHOT_DISK_IO_THRESHOLD_PERCENT": "95",
    "SNAPSHOT_SWAP_THRESHOLD_PERCENT": "95",
    "SNAPSHOT_ETH0_THRESHOLD_PERCENT": "95",
    "SNAPSHOT_TEMPERATURE_THRESHOLD_C": "80",
    "SNAPSHOT_LOG_RETENTION_DAYS": "30",
    "SYSTEM_HEALTH_INTERVAL_SECONDS": "30",
    "STORAGE_PROBE_DEVICE": "/dev/mmcblk0",
    "AGENT_UPDATE_STATE_PATH": "/var/lib/agv-monitor/agent-update-state.json",
    # This only stages boot-time crash capture.  It never reboots the vehicle.
    "CRASH_CAPTURE_AUTO_ENABLE": "true",
}
MAX_BATCH_SIZE = 90  # The server API's explicit maximum.
NETWORK_INTERFACES = ("eth0",)
STOP_REQUESTED = False
SNAPSHOT_LOG_DIRECTORY = Path("/var/log")
SNAPSHOT_LOG_PREFIX = "AGV-Monitor-"
HEALTH_CACHE: dict[str, object] = {"updated_at": 0.0, "slow": {}}
SNAPSHOT_METRICS = {
    "CPU": ("cpu_percent", "SNAPSHOT_CPU_THRESHOLD_PERCENT"),
    "RAM": ("memory_percent", "SNAPSHOT_MEMORY_THRESHOLD_PERCENT"),
    "Storage": ("disk_percent", "SNAPSHOT_STORAGE_THRESHOLD_PERCENT"),
    "Disk I/O": ("disk_io_percent", "SNAPSHOT_DISK_IO_THRESHOLD_PERCENT"),
    "Swap": ("swap_percent", "SNAPSHOT_SWAP_THRESHOLD_PERCENT"),
    "eth0": ("eth0_percent", "SNAPSHOT_ETH0_THRESHOLD_PERCENT"),
    "Temperature": ("cpu_temp_c", "SNAPSHOT_TEMPERATURE_THRESHOLD_C"),
}
MAX_SNAPSHOT_TRANSPORT_BYTES = 64_000
SNAPSHOT_TRUNCATION_NOTICE = (
    "\n… snapshot upload truncated; see the local log for the complete content.\n"
)
BEARER_CREDENTIAL_PATTERN = re.compile(
    r"(?i)(\bauthorization\s*:\s*bearer\s+)[^\s,;]+"
)
LABELED_SECRET_PATTERN = re.compile(
    r"""(?ix)
    (?P<label>
        \b(?:api[_-]?key|access[_-]?token|authorization|password|passwd|secret|token)\b
        [\"']?\s*[:=]\s*(?:bearer\s+)?[\"']?
    )
    (?P<secret>[^\s,;'\"}\]]+)
    """
)
URI_PASSWORD_PATTERN = re.compile(r"(?i)(://[^/\s:@]+:)[^@/\s]+(@)")


def configure_logging() -> logging.Logger:
    logger = logging.getLogger("agv-monitor")
    logger.setLevel(logging.INFO)
    handler = SysLogHandler(address="/dev/log") if Path("/dev/log").exists() else logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(name)s[%(process)d]: %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


LOG = configure_logging()


def redact_sensitive_values(content: str) -> str:
    """Remove common secret values before a diagnostic log is written or uploaded."""
    content = LABELED_SECRET_PATTERN.sub(r"\g<label>[REDACTED]", content)
    content = BEARER_CREDENTIAL_PATTERN.sub(r"\1[REDACTED]", content)
    return URI_PASSWORD_PATTERN.sub(r"\1[REDACTED]\2", content)


def bounded_snapshot_content(content: str) -> str:
    """Return a UTF-8-safe diagnostic attachment within the server's byte limit."""
    encoded_content = content.encode("utf-8")
    if len(encoded_content) <= MAX_SNAPSHOT_TRANSPORT_BYTES:
        return content
    content_limit = MAX_SNAPSHOT_TRANSPORT_BYTES - len(
        SNAPSHOT_TRUNCATION_NOTICE.encode("utf-8")
    )
    return (
        encoded_content[:content_limit].decode("utf-8", errors="ignore")
        + SNAPSHOT_TRUNCATION_NOTICE
    )


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
    for key in (
        "SAMPLE_INTERVAL_SECONDS", "INTERNAL_SAMPLE_INTERVAL_SECONDS",
        "UPLOAD_INTERVAL_SECONDS", "HTTP_TIMEOUT_SECONDS",
        "SYSTEM_HEALTH_INTERVAL_SECONDS",
    ):
        if float(config[key]) <= 0:
            raise ValueError(f"{key} must be greater than zero")
    if int(config["MAX_SPOOL_SAMPLES"]) <= 0:
        raise ValueError("MAX_SPOOL_SAMPLES must be greater than zero")
    if int(config["SNAPSHOT_LOG_RETENTION_DAYS"]) <= 0:
        raise ValueError("SNAPSHOT_LOG_RETENTION_DAYS must be greater than zero")
    for _label, (_metric, threshold_key) in SNAPSHOT_METRICS.items():
        if float(config[threshold_key]) < 0:
            raise ValueError(f"{threshold_key} must be zero or greater")
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


def process_cpu_times() -> dict[tuple[int, int], dict[str, int | str]]:
    """Read per-process CPU counters without running a diagnostic command.

    The process start time is part of the key so a reused PID cannot be mistaken
    for the process present at the beginning of the sampling interval.
    """
    processes: dict[tuple[int, int], dict[str, int | str]] = {}
    for process_path in Path("/proc").iterdir():
        if not process_path.name.isdigit():
            continue
        try:
            stat_line = (process_path / "stat").read_text(encoding="utf-8")
            command_end = stat_line.rfind(")")
            if command_end < 0:
                continue
            pid = int(process_path.name)
            command = stat_line[stat_line.find("(") + 1:command_end]
            fields = stat_line[command_end + 2:].split()
            # These indexes are relative to Linux proc(5) field 3 (state).
            start_time = int(fields[19])
            user_ticks = int(fields[11])
            system_ticks = int(fields[12])
            user = pwd.getpwuid(process_path.stat().st_uid).pw_name
            processes[(pid, start_time)] = {
                "user": user,
                "pid": pid,
                "ppid": int(fields[1]),
                "nice": int(fields[16]),
                "state": fields[0],
                "command": command,
                "start_time": start_time,
                "ticks": user_ticks + system_ticks,
            }
        except (IndexError, KeyError, OSError, ValueError):
            continue
    return processes


def process_cpu_attribution(
    previous: dict[tuple[int, int], dict[str, int | str]] | None,
    current: dict[tuple[int, int], dict[str, int | str]],
    total_cpu_delta: int,
) -> tuple[list[dict[str, int | str | float]], float]:
    """Attribute a system CPU interval to processes present at both endpoints."""
    if previous is None or total_cpu_delta <= 0:
        return [], 0.0
    rows: list[dict[str, int | str | float]] = []
    accounted_ticks = 0
    for identity, process in current.items():
        old_process = previous.get(identity)
        if old_process is None:
            continue
        tick_delta = int(process["ticks"]) - int(old_process["ticks"])
        if tick_delta <= 0:
            continue
        accounted_ticks += tick_delta
        rows.append({
            **process,
            "tick_delta": tick_delta,
            "system_percent": tick_delta * 100 / total_cpu_delta,
        })
    rows.sort(key=lambda row: float(row["system_percent"]), reverse=True)
    return rows, accounted_ticks * 100 / total_cpu_delta


def process_cpu_output(
    persistent_rows: list[dict[str, int | str | float]],
    persistent_percent: float,
    transient_rows: list[dict[str, int | str | float]],
    transient_percent: float,
    system_cpu_percent: float,
    cpu_count: int,
    limit: int = 40,
) -> str:
    """Format persistent and observed transient process CPU attribution."""
    lines = [
        "Persistent rows span the full alert interval; transient rows were observed in one-second samples.",
        "USER                 PID    PPID  NI STAT SYSTEM% CORE%  TICKS COMMAND",
    ]
    for row in persistent_rows[:limit]:
        system_percent = float(row["system_percent"])
        lines.append(
            f"{str(row['user'])[:16]:<16} {int(row['pid']):>7} {int(row['ppid']):>7} "
            f"{int(row['nice']):>3} {str(row['state']):<4} {system_percent:>6.1f} "
            f"{system_percent * cpu_count:>5.1f} {int(row['tick_delta']):>6} {row['command']}"
        )
    if len(persistent_rows) > limit:
        lines.append(f"… {len(persistent_rows) - limit} lower-CPU processes omitted")
    if transient_rows:
        lines.extend(("", "OBSERVED NEW/SHORT-LIVED PROCESSES (PARTIAL INTERVAL CPU)"))
        for row in transient_rows[:limit]:
            system_percent = float(row["system_percent"])
            lines.append(
                f"{str(row['user'])[:16]:<16} {int(row['pid']):>7} {int(row['ppid']):>7} "
                f"{int(row['nice']):>3} {str(row['state']):<4} {system_percent:>6.1f} "
                f"{system_percent * cpu_count:>5.1f} {int(row['tick_delta']):>6} {row['command']}"
            )
        if len(transient_rows) > limit:
            lines.append(f"… {len(transient_rows) - limit} lower-CPU processes omitted")
    unattributed = max(0.0, system_cpu_percent - persistent_percent - transient_percent)
    lines.extend((
        "",
        f"System CPU: {system_cpu_percent:.1f}%",
        f"Persistent processes accounted for: {persistent_percent:.1f}%",
        f"Observed new/short-lived process CPU: {transient_percent:.1f}%",
        f"Kernel/interrupts or still-unobserved CPU: {unattributed:.1f}%",
    ))
    return "\n".join(lines) + "\n"


WINDOW_METRICS = (
    "cpu_percent", "memory_percent", "swap_percent", "disk_percent",
    "disk_io_percent", "cpu_temp_c", "load_1m", "eth0_percent",
)


def aggregate_window(
    samples: list[dict],
    cpu_start: tuple[int, int] | None = None,
    cpu_end: tuple[int, int] | None = None,
) -> tuple[dict, str]:
    """Return one five-second telemetry record plus its average/min/max evidence."""
    if not samples:
        raise ValueError("Cannot aggregate an empty telemetry window")
    aggregate = dict(samples[-1])
    aggregate["extra"] = dict(samples[-1]["extra"])
    summary = ["METRIC                    AVERAGE     MINIMUM     MAXIMUM"]
    for metric in WINDOW_METRICS:
        values = [float(sample[metric]) for sample in samples if sample.get(metric) is not None]
        if not values:
            aggregate[metric] = None
            continue
        aggregate[metric] = round(sum(values) / len(values), 2)
        summary.append(
            f"{metric:<25} {aggregate[metric]:>7.2f} {min(values):>11.2f} {max(values):>11.2f}"
        )
    if cpu_start is not None and cpu_end is not None:
        total_delta = cpu_end[0] - cpu_start[0]
        idle_delta = cpu_end[1] - cpu_start[1]
        if total_delta > 0:
            aggregate["cpu_percent"] = round(
                max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100)), 1
            )
            summary[1] = (
                f"{'cpu_percent':<25} {aggregate['cpu_percent']:>7.2f} "
                f"{min(float(item['cpu_percent']) for item in samples):>11.2f} "
                f"{max(float(item['cpu_percent']) for item in samples):>11.2f}"
            )
    for metric in ("eth0_link_up",):
        aggregate[metric] = all(bool(sample.get(metric)) for sample in samples)
    return aggregate, "\n".join(summary) + "\n"


def accumulate_process_rows(
    totals: dict[tuple[int, int], dict[str, int | str | float]],
    rows: list[dict[str, int | str | float]],
) -> None:
    """Accumulate one-second process deltas for later transient-process evidence."""
    for row in rows:
        identity = (int(row["pid"]), int(row["start_time"]))
        existing = totals.get(identity)
        if existing is None:
            totals[identity] = dict(row)
        else:
            existing["tick_delta"] = int(existing["tick_delta"]) + int(row["tick_delta"])


def memory_percent() -> float:
    values: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        key, value = line.split(":", 1)
        values[key] = int(value.split()[0])
    total = values["MemTotal"]
    available = values.get("MemAvailable", values.get("MemFree", 0))
    return round((total - available) * 100 / total, 1)


def swap_percent() -> float:
    """Return used swap as a percentage, or zero when swap is unavailable."""
    values: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        key, value = line.split(":", 1)
        values[key] = int(value.split()[0])
    total = values.get("SwapTotal", 0)
    free = values.get("SwapFree", 0)
    return round((total - free) * 100 / total, 1) if total else 0.0


def uptime_seconds() -> int:
    """Return elapsed seconds since the Linux kernel last booted."""
    return int(float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0]))


def disk_percent(path: str) -> float:
    stats = os.statvfs(path)
    total = stats.f_blocks * stats.f_frsize
    available = stats.f_bavail * stats.f_frsize
    return round((total - available) * 100 / total, 1) if total else 0.0


def disk_io_percent(
    previous: tuple[int, float] | None,
    path: str,
) -> tuple[float | None, tuple[int, float] | None]:
    """Return disk active-time percentage using the kernel's I/O busy counter."""
    device = os.stat(path).st_dev
    stat_path = Path("/sys/dev/block") / f"{os.major(device)}:{os.minor(device)}" / "stat"
    try:
        fields = stat_path.read_text(encoding="utf-8").split()
        busy_ms = int(fields[9])  # Linux diskstats field 10: time doing I/O.
    except (OSError, ValueError, IndexError):
        return None, None

    current = (busy_ms, time.monotonic())
    if previous is None:
        return None, current
    busy_delta = busy_ms - previous[0]
    elapsed = current[1] - previous[1]
    if busy_delta < 0 or elapsed <= 0:
        return None, current
    return round(max(0.0, min(100.0, busy_delta / (elapsed * 10))), 2), current


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
) -> tuple[
    dict[str, float | None], dict[str, bool], dict[str, tuple[int, int, float]]
]:
    """Return combined RX/TX use as a percentage of each interface link speed."""
    now = time.monotonic()
    current: dict[str, tuple[int, int, float]] = {}
    utilisation: dict[str, float | None] = {}
    link_up: dict[str, bool] = {}
    for interface in NETWORK_INTERFACES:
        base = Path("/sys/class/net") / interface
        try:
            rx_bytes = int((base / "statistics/rx_bytes").read_text(encoding="utf-8").strip())
            tx_bytes = int((base / "statistics/tx_bytes").read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            utilisation[interface] = None
            link_up[interface] = False
            continue
        try:
            link_up[interface] = (base / "carrier").read_text(encoding="utf-8").strip() == "1"
            speed_mbps = int((base / "speed").read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            link_up[interface] = False
            speed_mbps = 0
        current[interface] = (rx_bytes, tx_bytes, now)
        old = previous.get(interface) if previous else None
        if old is None or not link_up[interface] or speed_mbps <= 0:
            utilisation[interface] = None
            continue
        transferred_bytes = (rx_bytes - old[0]) + (tx_bytes - old[1])
        elapsed = now - old[2]
        if transferred_bytes < 0 or elapsed <= 0:
            utilisation[interface] = None
            continue
        percent = transferred_bytes * 8 * 100 / (elapsed * speed_mbps * 1_000_000)
        utilisation[interface] = round(max(0.0, min(100.0, percent)), 2)
    return utilisation, link_up, current


def collect(
    previous_cpu: tuple[int, int] | None,
    previous_processes: dict[tuple[int, int], dict[str, int | str]] | None,
    previous_network: dict[str, tuple[int, int, float]] | None,
    previous_disk_io: tuple[int, float] | None,
    disk_path: str,
    config: dict[str, str],
) -> tuple[
    dict,
    tuple[int, int],
    dict[tuple[int, int], dict[str, int | str]],
    dict[str, tuple[int, int, float]],
    tuple[int, float] | None,
    list[dict[str, int | str | float]],
    float,
]:
    cpu, current_cpu = cpu_percent(previous_cpu)
    current_processes = process_cpu_times()
    total_cpu_delta = 0 if previous_cpu is None else current_cpu[0] - previous_cpu[0]
    process_cpu, accounted_cpu = process_cpu_attribution(
        previous_processes, current_processes, total_cpu_delta
    )
    network, link_up, current_network = network_utilisation(previous_network)
    disk_io, current_disk_io = disk_io_percent(previous_disk_io, disk_path)
    try:
        load = round(os.getloadavg()[0], 2)
    except OSError:
        load = None
    return {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "cpu_percent": cpu,
        "memory_percent": memory_percent(),
        "swap_percent": swap_percent(),
        "disk_percent": disk_percent(disk_path),
        "disk_io_percent": disk_io,
        "uptime_seconds": uptime_seconds(),
        "cpu_temp_c": cpu_temperature(),
        "load_1m": load,
        "eth0_percent": network["eth0"],
        "eth0_link_up": link_up["eth0"],
        "extra": {"agent_version": AGENT_VERSION, "system_health": system_health(config)},
    }, current_cpu, current_processes, current_network, current_disk_io, process_cpu, accounted_cpu


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


def snapshot_thresholds(config: dict[str, str]) -> dict[str, float]:
    """Read per-device diagnostic thresholds from the root-only configuration."""
    return {
        label: float(config[threshold_key])
        for label, (_metric, threshold_key) in SNAPSHOT_METRICS.items()
    }


def settings_url(server_url: str) -> str:
    """Build the authenticated client-settings endpoint from the telemetry URL."""
    parsed = urlsplit(server_url)
    return urlunsplit((parsed.scheme, parsed.netloc, "/api/v1/device-settings", "", ""))


def agent_update_url(server_url: str) -> str:
    """Build the authenticated agent-update endpoint from the telemetry URL."""
    parsed = urlsplit(server_url)
    return urlunsplit((parsed.scheme, parsed.netloc, "/api/v1/agent-update", "", ""))


def agent_update_status_url(server_url: str) -> str:
    """Build the endpoint used to report installation outcomes."""
    parsed = urlsplit(server_url)
    return urlunsplit((parsed.scheme, parsed.netloc, "/api/v1/agent-update/status", "", ""))


def agent_update_state_path(config: dict[str, str]) -> Path:
    return Path(config["AGENT_UPDATE_STATE_PATH"])


def update_status(config: dict[str, str], status: str, from_version: str,
                  to_version: str, message: str, log: str = "") -> None:
    """Report a bounded, redacted update result without interrupting telemetry."""
    payload = {
        "status": status,
        "from_version": from_version,
        "to_version": to_version,
        "message": message[:500],
        "log": redact_sensitive_values(log)[-64_000:] or None,
    }
    request = Request(
        agent_update_status_url(config["SERVER_URL"]),
        json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + config["DEVICE_TOKEN"], "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=float(config["HTTP_TIMEOUT_SECONDS"])) as response:
            if response.status != 202:
                raise OSError(f"unexpected HTTP status {response.status}")
    except (HTTPError, URLError, OSError) as error:
        LOG.warning("Unable to report agent update outcome: %s", error)


def write_update_state(path: Path, state: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=path.name + ".", delete=False) as temporary:
        json.dump(state, temporary, sort_keys=True)
        temporary.flush()
        os.fsync(temporary.fileno())
    Path(temporary.name).replace(path)


def complete_pending_update(config: dict[str, str]) -> None:
    """Confirm a replacement only after the new code loaded its real config."""
    state_path = agent_update_state_path(config)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if state.get("to_version") != AGENT_VERSION:
        return
    update_status(
        config, "installed", state.get("from_version", "unknown"), AGENT_VERSION,
        "Downloaded agent passed validation and started successfully.",
    )
    try:
        state_path.unlink()
    except OSError as error:
        LOG.warning("Unable to clear completed agent-update state: %s", error)


def same_server_origin(server_url: str, candidate_url: str) -> bool:
    expected, candidate = urlsplit(server_url), urlsplit(candidate_url)
    return expected.scheme == candidate.scheme and expected.netloc == candidate.netloc


def check_for_agent_update(config: dict[str, str]) -> bool:
    """Fetch and atomically start a newer server-published agent, if available."""
    check_url = agent_update_url(config["SERVER_URL"])
    check_url = check_url + "?" + urlencode({"current_version": AGENT_VERSION})
    request = Request(check_url, headers={"Authorization": "Bearer " + config["DEVICE_TOKEN"]})
    try:
        with urlopen(request, timeout=float(config["HTTP_TIMEOUT_SECONDS"])) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, OSError, json.JSONDecodeError) as error:
        LOG.warning("Unable to check for telemetry-agent update: %s", error)
        return False
    if not isinstance(payload, dict) or not payload.get("update_available"):
        return False
    payload["available"] = True
    return install_agent_update(config, payload)


def install_agent_update(config: dict[str, str], payload: dict[str, object]) -> bool:
    """Atomically start the agent advertised in a telemetry acknowledgement."""
    if not payload.get("available"):
        return False

    target_version = payload.get("latest_version")
    expected_sha256 = payload.get("sha256")
    download_url = payload.get("download_url")
    if not all(isinstance(value, str) and value for value in (target_version, expected_sha256, download_url)):
        error = "Server returned incomplete agent-update metadata"
        LOG.error(error)
        update_status(config, "failed", AGENT_VERSION, str(target_version or "unknown"), error)
        return False
    if not same_server_origin(config["SERVER_URL"], download_url):
        error = "Server returned an agent download URL outside the configured server origin"
        LOG.error(error)
        update_status(config, "failed", AGENT_VERSION, target_version, error)
        return False

    script_path: Path | None = None
    backup_path: Path | None = None
    replacement_installed = False
    try:
        download_request = Request(download_url, headers={"Authorization": "Bearer " + config["DEVICE_TOKEN"]})
        with urlopen(download_request, timeout=float(config["HTTP_TIMEOUT_SECONDS"])) as response:
            content = response.read()
        if hashlib.sha256(content).hexdigest() != expected_sha256.lower():
            raise ValueError("download SHA-256 does not match the server response")
        text = content.decode("utf-8")
        version_match = re.search(r'^AGENT_VERSION\s*=\s*["\']([^"\']+)["\']\s*$', text, re.MULTILINE)
        if version_match is None or version_match.group(1) != target_version:
            raise ValueError("downloaded agent version does not match the server response")
        script_path = Path(__file__).resolve()
        with tempfile.NamedTemporaryFile("wb", dir=script_path.parent, prefix=script_path.name + ".",
                                         suffix=".new", delete=False) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        candidate_path = Path(temporary.name)
        try:
            result = subprocess.run(
                [sys.executable, str(candidate_path), "--self-test"],
                capture_output=True, text=True, timeout=30, check=False,
            )
            if result.returncode != 0:
                raise ValueError("agent self-test failed: " + (result.stderr or result.stdout)[-4_000:])
            backup_path = script_path.with_name(script_path.name + ".previous")
            backup_path.write_bytes(script_path.read_bytes())
            os.chmod(backup_path, stat.S_IMODE(script_path.stat().st_mode))
            write_update_state(agent_update_state_path(config), {
                "from_version": AGENT_VERSION,
                "to_version": target_version,
                "backup_path": str(backup_path),
            })
            os.chmod(candidate_path, stat.S_IMODE(script_path.stat().st_mode))
            candidate_path.replace(script_path)
            replacement_installed = True
        finally:
            if "candidate_path" in locals() and candidate_path.exists():
                candidate_path.unlink()
        LOG.info("Installed telemetry agent %s; restarting the agent process", target_version)
        os.execv(sys.executable, [sys.executable, str(script_path)])
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        LOG.exception("Unable to install telemetry-agent %s: %s", target_version, error)
        if replacement_installed and script_path and backup_path and backup_path.exists():
            try:
                backup_path.replace(script_path)
                agent_update_state_path(config).unlink(missing_ok=True)
                update_status(config, "rolled_back", AGENT_VERSION, target_version,
                              "Agent restart failed; restored the previous agent.", repr(error))
                return False
            except OSError as rollback_error:
                error = OSError(f"{error}; rollback also failed: {rollback_error}")
        update_status(config, "failed", AGENT_VERSION, target_version, str(error), repr(error))
    return False


def synchronise_snapshot_settings(config: dict[str, str]) -> None:
    """Fetch the globally managed snapshot settings without overwriting local config files."""
    request = Request(
        settings_url(config["SERVER_URL"]),
        headers={"Authorization": "Bearer " + config["DEVICE_TOKEN"]},
        method="GET",
    )
    try:
        with urlopen(request, timeout=float(config["HTTP_TIMEOUT_SECONDS"])) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, OSError, json.JSONDecodeError) as error:
        LOG.warning("Unable to synchronise global snapshot settings: %s", error)
        return

    settings = payload.get("snapshot_settings")
    if not isinstance(settings, dict):
        LOG.warning("Server returned invalid global snapshot settings")
        return
    for key in DEFAULTS:
        if key.startswith("SNAPSHOT_") and key in settings:
            config[key] = str(settings[key])
    LOG.info("Synchronised global snapshot settings from the server")


def apply_server_directives(config: dict[str, str], payload: dict[str, object]) -> None:
    """Apply every safe directive in one acknowledgement before handling an update.

    A successful upload is the single configuration channel.  Settings are kept
    in memory for the running agent, while boot-time crash capture is staged on
    disk and deliberately waits for an operator-planned reboot.
    """
    directives = payload.get("directives")
    if not isinstance(directives, dict):
        return
    settings = directives.get("snapshot_settings")
    if isinstance(settings, dict):
        for key in DEFAULTS:
            if key.startswith("SNAPSHOT_") and key in settings:
                config[key] = str(settings[key])
        LOG.info("Applied global snapshot settings revision %s", directives.get("revision", "unknown"))
    crash_capture = directives.get("crash_capture")
    if isinstance(crash_capture, dict) and enabled_setting(crash_capture.get("enabled")):
        enable_crash_capture()


def seconds_until_next_midday() -> float:
    """Return the interval until the next local 12:00 midday maintenance run."""
    now = datetime.now().astimezone()
    midday = now.replace(hour=12, minute=0, second=0, microsecond=0)
    if midday <= now:
        midday += timedelta(days=1)
    return (midday - now).total_seconds()


def high_utilisation_metrics(sample: dict, thresholds: dict[str, float]) -> set[str]:
    """Return monitored metrics at or above their diagnostic snapshot threshold."""
    return {
        label
        for label, (key, _threshold_key) in SNAPSHOT_METRICS.items()
        if sample.get(key) is not None and float(sample[key]) >= thresholds[label]
    }


def command_output(command: list[str], line_limit: int = 80) -> str:
    """Capture a bounded command output for a diagnostic snapshot."""
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"Unable to run {' '.join(command)}: {error}\n"

    output = completed.stdout
    if completed.stderr:
        output += f"\n[stderr]\n{completed.stderr}"
    lines = output.splitlines()
    if len(lines) > line_limit:
        lines = lines[:line_limit] + [f"… output limited to {line_limit} lines"]
    return "\n".join(lines) + "\n"


def file_contents(path: Path, line_limit: int = 120) -> str:
    """Read a kernel status file without allowing one large file to dominate a log."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as error:
        return f"Unable to read {path}: {error}\n"
    if len(lines) > line_limit:
        lines = lines[:line_limit] + [f"… output limited to {line_limit} lines"]
    return "\n".join(lines) + "\n"


def storage_read_latency_ms(device_path: str) -> float | None:
    """Read 4 KiB from the eMMC device without modifying it, measuring latency."""
    try:
        started = time.monotonic()
        descriptor = os.open(device_path, os.O_RDONLY | os.O_CLOEXEC)
        try:
            os.pread(descriptor, 4096, 0)
        finally:
            os.close(descriptor)
        return round((time.monotonic() - started) * 1000, 2)
    except OSError:
        return None


def pressure_status(resource: str) -> dict[str, object]:
    """Return parsed kernel PSI, or a clear unsupported status on older kernels."""
    path = Path(f"/proc/pressure/{resource}")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {"available": False, "reason": "kernel PSI is unavailable"}
    result: dict[str, object] = {"available": True}
    for line in lines:
        parts = line.split()
        if not parts:
            continue
        metrics: dict[str, float | int] = {}
        for item in parts[1:]:
            key, separator, value = item.partition("=")
            if not separator:
                continue
            try:
                metrics[key] = int(value) if key == "total" else float(value)
            except ValueError:
                continue
        result[parts[0]] = metrics
    return result


def emmc_lifetime_range(value: int | None) -> str | None:
    """Translate JEDEC EXT_CSD lifetime buckets into an operator-friendly range."""
    if value is None or not 1 <= value <= 10:
        return None
    return f"{(value - 1) * 10}-{value * 10}%"


def emmc_health() -> dict[str, object]:
    """Read eMMC wear data using mmc-utils, with a kernel EXT_CSD fallback."""
    keys = ("PRE_EOL_INFO", "DEVICE_LIFE_TIME_EST_TYP_A", "DEVICE_LIFE_TIME_EST_TYP_B")
    extcsd = command_output(["mmc", "extcsd", "read", "/dev/mmcblk0"], line_limit=160)
    values: dict[str, int | None] = {}
    for key in keys:
        match = re.search(rf"{key}[^0-9A-Fa-f]*(0x[0-9A-Fa-f]+|[0-9]+)", extcsd)
        values[key] = int(match.group(1), 0) if match else None
    source = "mmc-utils"
    if not any(value is not None for value in values.values()):
        source = "unavailable"
        for path in sorted(Path("/sys/kernel/debug").glob("mmc*/mmc*:*/ext_csd")):
            try:
                data = bytes.fromhex(path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                continue
            if len(data) < 270:
                continue
            values = {
                "PRE_EOL_INFO": data[267],
                "DEVICE_LIFE_TIME_EST_TYP_A": data[268],
                "DEVICE_LIFE_TIME_EST_TYP_B": data[269],
            }
            source = "kernel_debug_ext_csd"
            break
    pre_eol = values["PRE_EOL_INFO"]
    return {
        **{key: (f"0x{value:02x}" if value is not None else None) for key, value in values.items()},
        "source": source,
        "pre_eol_status": {1: "normal", 2: "warning", 3: "urgent"}.get(pre_eol, "unknown"),
        "life_time_a_percent_range": emmc_lifetime_range(values["DEVICE_LIFE_TIME_EST_TYP_A"]),
        "life_time_b_percent_range": emmc_lifetime_range(values["DEVICE_LIFE_TIME_EST_TYP_B"]),
    }


CRASH_CAPTURE_FILES = {
    Path("/etc/systemd/journald.conf.d/95-agv-crash-capture.conf"): "[Journal]\nStorage=persistent\n",
    Path("/etc/sysctl.d/95-agv-crash-capture.conf"): (
        "kernel.hung_task_panic=1\n"
        "kernel.hung_task_timeout_secs=180\n"
        "kernel.panic_on_oops=1\n"
        "kernel.panic=30\n"
    ),
    Path("/etc/systemd/system.conf.d/95-agv-watchdog.conf"): (
        "[Manager]\nRuntimeWatchdogSec=30s\nRebootWatchdogSec=10min\n"
    ),
}
RAMOOPS_OVERLAY = "dtoverlay=ramoops,total-size=1048576,record-size=262144,console-size=262144"


def enabled_setting(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def write_root_configuration(path: Path, content: str) -> None:
    """Write a small root-owned configuration file atomically; never restart a service."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".agv-monitor-new")
    temporary.write_text(content, encoding="utf-8")
    os.chmod(temporary, 0o644)
    temporary.replace(path)


def stage_crash_capture_via_systemd() -> bool:
    """Bypass this service's read-only mount namespace without restarting anything.

    Older agent units protect /etc and /boot.  systemd starts this fixed,
    local one-shot command in the manager namespace, so existing installations
    can still stage the same audited files.  No server supplied shell content
    is executed here.
    """
    script = Path("/run/agv-monitor-stage-crash-capture.sh")
    content = f"""#!/bin/sh
set -eu
install -d -m 0755 /etc/systemd/journald.conf.d /etc/systemd/system.conf.d /etc/sysctl.d
printf '%s\\n' '[Journal]' 'Storage=persistent' > /etc/systemd/journald.conf.d/95-agv-crash-capture.conf
printf '%s\\n' 'kernel.hung_task_panic=1' 'kernel.hung_task_timeout_secs=180' 'kernel.panic_on_oops=1' 'kernel.panic=30' > /etc/sysctl.d/95-agv-crash-capture.conf
printf '%s\\n' '[Manager]' 'RuntimeWatchdogSec=30s' 'RebootWatchdogSec=10min' > /etc/systemd/system.conf.d/95-agv-watchdog.conf
test ! -e /etc/systemd/journald.conf.d/70-storage-volatile.conf || mv /etc/systemd/journald.conf.d/70-storage-volatile.conf /etc/systemd/journald.conf.d/70-storage-volatile.conf.disabled
grep -qxF '{RAMOOPS_OVERLAY}' /boot/firmware/config.txt || printf '%s\\n' '{RAMOOPS_OVERLAY}' >> /boot/firmware/config.txt
grep -qw 'psi=1' /boot/firmware/cmdline.txt || printf ' psi=1\\n' >> /boot/firmware/cmdline.txt
"""
    try:
        script.write_text(content, encoding="utf-8")
        os.chmod(script, 0o700)
        completed = subprocess.run(
            ["systemd-run", "--quiet", "--wait", "--collect", "--service-type=oneshot", "/bin/sh", str(script)],
            capture_output=True, text=True, timeout=45, check=False,
        )
        if completed.returncode:
            LOG.warning("systemd crash-capture staging failed: %s", (completed.stderr or completed.stdout).strip())
            return False
        LOG.warning("Staged crash capture through systemd for the next reboot")
        return True
    except (OSError, subprocess.SubprocessError) as error:
        LOG.warning("Unable to use systemd crash-capture staging fallback: %s", error)
        return False
    finally:
        try:
            script.unlink()
        except OSError:
            pass


def enable_crash_capture() -> list[str]:
    """Stage persistent crash capture for the next boot without rebooting the AGV."""
    changed: list[str] = []
    for path, content in CRASH_CAPTURE_FILES.items():
        try:
            if path.read_text(encoding="utf-8") != content:
                write_root_configuration(path, content)
                changed.append(str(path))
        except OSError as error:
            LOG.warning("Unable to stage crash-capture setting %s: %s", path, error)
    volatile_override = Path("/etc/systemd/journald.conf.d/70-storage-volatile.conf")
    if volatile_override.exists():
        try:
            volatile_override.rename(volatile_override.with_name(volatile_override.name + ".disabled"))
            changed.append(str(volatile_override))
        except OSError as error:
            LOG.warning("Unable to disable volatile journal override: %s", error)
    boot_config = Path("/boot/firmware/config.txt")
    try:
        content = boot_config.read_text(encoding="utf-8")
        # Keep exactly one ramoops overlay.  Older agent releases used a
        # smaller reservation; leaving both lines makes the final (old) line
        # override the intended capture size at boot.
        retained_lines = [line for line in content.splitlines() if not line.startswith("dtoverlay=ramoops,")]
        desired_content = "\n".join(retained_lines).rstrip() + "\n" + RAMOOPS_OVERLAY + "\n"
        if content != desired_content:
            boot_config.write_text(desired_content, encoding="utf-8")
            changed.append(str(boot_config))
    except OSError as error:
        LOG.warning("Unable to stage ramoops overlay: %s", error)
    command_line = Path("/boot/firmware/cmdline.txt")
    try:
        content = command_line.read_text(encoding="utf-8").strip()
        if "psi=1" not in content.split():
            command_line.write_text(content + " psi=1\n", encoding="utf-8")
            changed.append(str(command_line))
    except OSError as error:
        LOG.warning("Unable to stage PSI boot option: %s", error)
    required = tuple(CRASH_CAPTURE_FILES) + (Path("/boot/firmware/config.txt"), Path("/boot/firmware/cmdline.txt"))
    if not all(path.exists() and os.access(path, os.W_OK) for path in required):
        stage_crash_capture_via_systemd()
    if changed:
        LOG.warning("Staged crash capture for the next reboot: %s", ", ".join(changed))
    return changed


def crash_capture_status() -> dict[str, object]:
    """Report configured and live crash-capture state plus bounded pstore evidence."""
    expected_files = {str(path): path.exists() and path.read_text(encoding="utf-8", errors="replace") == content
                      for path, content in CRASH_CAPTURE_FILES.items()}
    boot_config = file_contents(Path("/boot/firmware/config.txt"), line_limit=400)
    cmdline = file_contents(Path("/proc/cmdline"), line_limit=1)
    pstore_records: list[dict[str, str]] = []
    for record in sorted(Path("/sys/fs/pstore").glob("*"))[:20]:
        if not record.is_file():
            continue
        try:
            pstore_records.append({"name": record.name, "content": redact_sensitive_values(record.read_text(encoding="utf-8", errors="replace")[:8_000])})
        except OSError:
            continue
    ramoops_active = any("ramoops" in line.lower() for line in command_output(["dmesg"], line_limit=400).splitlines())
    configured = all(expected_files.values()) and RAMOOPS_OVERLAY in boot_config and "psi=1" in cmdline.split()
    return {
        "configured": configured,
        "reboot_required": configured and (not Path("/proc/pressure/cpu").exists() or not ramoops_active),
        "persistent_journal_configured": expected_files[str(Path("/etc/systemd/journald.conf.d/95-agv-crash-capture.conf"))],
        "ramoops_configured": RAMOOPS_OVERLAY in boot_config,
        "ramoops_active": ramoops_active,
        "psi_configured": "psi=1" in cmdline.split(),
        "psi_active": Path("/proc/pressure/cpu").exists(),
        "panic_policy_configured": expected_files[str(Path("/etc/sysctl.d/95-agv-crash-capture.conf"))],
        "watchdog_configured": expected_files[str(Path("/etc/systemd/system.conf.d/95-agv-watchdog.conf"))],
        "pstore_records": pstore_records,
        "kernel_panic_detected": bool(pstore_records),
    }


def system_health(config: dict[str, str]) -> dict[str, object]:
    """Collect cheap kernel/storage/I/O indicators for remote diagnosis."""
    blocked = []
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            fields = (process / "stat").read_text().rsplit(") ", 1)[1].split()
            if fields[0] == "D":
                blocked.append((process / "comm").read_text().strip())
        except (IndexError, OSError):
            continue
    stat_fields = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:]
    total_ticks = sum(int(value) for value in stat_fields)
    iowait_ticks = int(stat_fields[4]) if len(stat_fields) > 4 else 0
    pressure = {resource: pressure_status(resource) for resource in ("cpu", "io", "memory")}
    if time.monotonic() - float(HEALTH_CACHE["updated_at"]) >= float(config["SYSTEM_HEALTH_INTERVAL_SECONDS"]):
        HEALTH_CACHE["slow"] = {
            "emmc_health": emmc_health(),
            "kernel_faults": command_output(
                ["dmesg", "--level=err,warn,crit,alert,emerg"], line_limit=30
            ).splitlines()[-30:],
            "throttled": command_output(["vcgencmd", "get_throttled"], line_limit=2).strip(),
            "storage_read_latency_ms": storage_read_latency_ms(config["STORAGE_PROBE_DEVICE"]),
            "boot_id": file_contents(Path("/proc/sys/kernel/random/boot_id"), line_limit=1).strip(),
            "previous_boot_kernel": command_output(["journalctl", "-b", "-1", "-k", "-n", "30"], line_limit=30).splitlines(),
            "crash_capture": crash_capture_status(),
        }
        HEALTH_CACHE["updated_at"] = time.monotonic()
    return {
        "blocked_tasks": sorted(blocked)[:20],
        "blocked_task_count": len(blocked),
        "pi_control_blocked": "piControl I/O" in blocked,
        "filesystem_read_only": not os.access("/", os.W_OK),
        "cpu_iowait_ticks": iowait_ticks,
        "cpu_total_ticks": total_ticks,
        "pressure": pressure,
        **dict(HEALTH_CACHE["slow"]),
    }


def largest_files(path: str, limit: int = 30) -> list[tuple[int, str]]:
    """Find the largest regular files on the monitored filesystem only."""
    root = Path(path)
    try:
        filesystem_id = root.stat().st_dev
    except OSError as error:
        return [(0, f"Unable to inspect {root}: {error}")]

    files: list[tuple[int, str]] = []
    for directory, _subdirectories, names in os.walk(root, topdown=True, followlinks=False):
        for name in names:
            file_path = Path(directory) / name
            try:
                metadata = file_path.stat(follow_symlinks=False)
            except OSError:
                continue
            if metadata.st_dev != filesystem_id or not stat.S_ISREG(metadata.st_mode):
                continue
            entry = (metadata.st_size, str(file_path))
            if len(files) < limit:
                heappush(files, entry)
            elif entry[0] > files[0][0]:
                heapreplace(files, entry)
    return sorted(files, reverse=True)


def process_io_output(limit: int = 40) -> str:
    """Return processes ranked by cumulative kernel-accounted read/write bytes."""
    processes: list[tuple[int, int, int, str, int, str]] = []
    for process_path in Path("/proc").iterdir():
        if not process_path.name.isdigit():
            continue
        try:
            io_values = {
                key.rstrip(":"): int(value)
                for key, value in (
                    line.split(":", 1) for line in (process_path / "io").read_text().splitlines()
                )
            }
            read_bytes = io_values.get("read_bytes", 0)
            write_bytes = io_values.get("write_bytes", 0)
            total_bytes = read_bytes + write_bytes
            user = pwd.getpwuid(process_path.stat().st_uid).pw_name
            process_name = (process_path / "comm").read_text(encoding="utf-8").strip()
            if not process_name:
                continue
        except (KeyError, OSError, ValueError):
            continue
        processes.append(
            (total_bytes, read_bytes, write_bytes, user, int(process_path.name), process_name)
        )

    lines = ["TOTAL BYTES       READ BYTES        WRITE BYTES       USER             PID  PROCESS"]
    for total_bytes, read_bytes, write_bytes, user, process_id, process_name in sorted(
        processes, reverse=True
    )[:limit]:
        lines.append(
            f"{total_bytes:>15,} {read_bytes:>15,} {write_bytes:>18,} "
            f"{user[:16]:<16} {process_id:>7}  {process_name}"
        )
    lines.append("Values are cumulative process I/O totals at the capture time.")
    return "\n".join(lines) + "\n"


def cleanup_snapshot_logs(retention_days: int) -> None:
    """Remove this agent's diagnostic snapshots once they are older than 30 days."""
    cutoff = time.time() - retention_days * 24 * 60 * 60
    for log_path in SNAPSHOT_LOG_DIRECTORY.glob(f"{SNAPSHOT_LOG_PREFIX}*.log"):
        try:
            if log_path.stat().st_mtime < cutoff:
                log_path.unlink()
        except OSError as error:
            LOG.warning("Unable to remove old diagnostic snapshot %s: %s", log_path, error)


def write_section(log_file, title: str, content: str) -> None:
    log_file.write(f"\n{'=' * 20} {title} {'=' * 20}\n")
    log_file.write(redact_sensitive_values(content).rstrip() + "\n")


def write_snapshot(
    triggered: set[str],
    sample: dict,
    process_cpu_rows: list[dict[str, int | str | float]],
    process_cpu_accounted_percent: float,
    transient_process_rows: list[dict[str, int | str | float]],
    transient_process_percent: float,
    window_summary: str,
    disk_path: str,
    thresholds: dict[str, float],
    retention_days: int,
) -> dict | None:
    """Write a diagnostic snapshot and return the bounded content for telemetry upload."""
    timestamp = datetime.now(timezone.utc)
    log_path = SNAPSHOT_LOG_DIRECTORY / (
        f"{SNAPSHOT_LOG_PREFIX}{timestamp.strftime('%Y%m%dT%H%M%S.%fZ')}.log"
    )
    try:
        cleanup_snapshot_logs(retention_days)
        with log_path.open("x", encoding="utf-8") as log_file:
            os.chmod(log_path, 0o640)
            log_file.write("AGV Monitor high-utilisation diagnostic snapshot\n")
            log_file.write(f"Captured (UTC): {timestamp.isoformat()}\n")
            log_file.write(
                "Thresholds: "
                + ", ".join(
                    f"{label} {threshold:.0f}{'°C' if label == 'Temperature' else '%'}"
                    for label, threshold in thresholds.items()
                )
                + "\n"
            )
            log_file.write(f"Triggered by: {', '.join(sorted(triggered))}\n")
            write_section(log_file, "TRIGGERING TELEMETRY", json.dumps(sample, indent=2, sort_keys=True))
            write_section(log_file, "FIVE-SECOND WINDOW SUMMARY", window_summary)
            write_section(
                log_file,
                "SYSTEM",
                "\n".join(
                    (
                        f"Hostname: {socket.gethostname()}",
                        f"Kernel: {os.uname().sysname} {os.uname().release}",
                        f"CPU cores: {os.cpu_count() or 'unknown'}",
                        f"Load average (trigger sample, 1m): {sample['load_1m']}",
                        f"Uptime seconds (trigger sample): {sample['uptime_seconds']}",
                    )
                ),
            )
            write_section(
                log_file,
                "MEASUREMENT BASIS",
                "\n".join((
                    "CPU and process attribution are deltas from the same sampling interval.",
                    "Disk I/O and Ethernet utilisation in triggering telemetry are also interval deltas.",
                    "RAM, swap, storage, temperature, and load are point-in-time kernel readings.",
                    "The later counter and command sections are current diagnostic context, not trigger values.",
                )),
            )
            write_section(
                log_file,
                "CPU INTERVAL ATTRIBUTION",
                process_cpu_output(
                    process_cpu_rows,
                    process_cpu_accounted_percent,
                    transient_process_rows,
                    transient_process_percent,
                    float(sample["cpu_percent"]),
                    os.cpu_count() or 1,
                ),
            )
            write_section(log_file, "TOP MEMORY PROCESSES", command_output([
                "ps", "-eo", "user:16,pid,ppid,ni,stat,pcpu,pmem,rss,vsz,etime,comm", "--sort=-pmem"
            ], line_limit=41))
            write_section(log_file, "MEMORY AT SNAPSHOT TIME", file_contents(Path("/proc/meminfo")))
            write_section(log_file, "MEMORY SUMMARY AT SNAPSHOT TIME", command_output(["free", "-h"]))
            write_section(log_file, "DISK SPACE AT SNAPSHOT TIME", command_output(["df", "-h", disk_path]))
            write_section(log_file, "DISK I/O COUNTERS (CUMULATIVE)", file_contents(Path("/proc/diskstats")))
            if "Disk I/O" in triggered:
                write_section(log_file, "TOP DISK I/O PROCESSES", process_io_output())
            write_section(log_file, "NETWORK COUNTERS (CUMULATIVE)", file_contents(Path("/proc/net/dev")))
            write_section(log_file, "ETH0 DETAILS", command_output(["ip", "-s", "link", "show", "eth0"]))
            write_section(log_file, "KERNEL TASK STATES", command_output(["ps", "-eo", "pid,stat,wchan:32,comm"]))
            write_section(log_file, "KERNEL/MMC WARNINGS", command_output(["dmesg", "--level=err,warn,crit,alert,emerg"]))
            write_section(log_file, "NETWORK SOCKET SUMMARY", command_output(["ss", "-s"]))
            write_section(log_file, "RASPBERRY PI THERMAL STATUS", command_output([
                "vcgencmd", "get_throttled"
            ]))
            if "Storage" in triggered:
                largest = largest_files(disk_path)
                file_list = "\n".join(
                    f"{size:>14,} bytes  {file_path}" for size, file_path in largest
                ) or "No regular files found."
                write_section(log_file, "LARGEST FILES", file_list)
        content = log_path.read_text(encoding="utf-8", errors="replace")
        content = bounded_snapshot_content(content)
        LOG.warning("Wrote high-utilisation diagnostic snapshot to %s", log_path)
        return {
            "captured_at": timestamp.isoformat(),
            "triggered_metrics": sorted(triggered),
            "content": content,
        }
    except OSError as error:
        LOG.error("Unable to write high-utilisation diagnostic snapshot: %s", error)
    return None


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
                response_payload = json.loads(response.read().decode("utf-8") or "{}")
            remove_sent_samples(spool_path, len(samples))
            LOG.info("Uploaded %s telemetry samples", len(samples))
            update_directive = response_payload.get("agent_update", {})
            apply_server_directives(config, response_payload)
            if isinstance(update_directive, dict) and update_directive.get("available"):
                LOG.info(
                    "Server reports telemetry-agent %s is available; checking update now",
                    update_directive.get("latest_version", "newer"),
                )
                install_agent_update(config, update_directive)
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
    complete_pending_update(config)
    if enabled_setting(config["CRASH_CAPTURE_AUTO_ENABLE"]):
        enable_crash_capture()
    sample_every = float(config["SAMPLE_INTERVAL_SECONDS"])
    internal_sample_every = float(config["INTERNAL_SAMPLE_INTERVAL_SECONDS"])
    if internal_sample_every > sample_every:
        raise ValueError("INTERNAL_SAMPLE_INTERVAL_SECONDS cannot exceed SAMPLE_INTERVAL_SECONDS")
    upload_every = float(config["UPLOAD_INTERVAL_SECONDS"])
    if sample_every * MAX_BATCH_SIZE < upload_every:
        LOG.warning("Upload interval produces more than %s samples; multiple uploads will be required", MAX_BATCH_SIZE)
    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    previous_cpu = None
    previous_processes = None
    previous_network = None
    previous_disk_io = None
    window_start_cpu = None
    window_start_processes = None
    window_process_totals: dict[tuple[int, int], dict[str, int | str | float]] = {}
    window_samples: list[dict] = []
    active_high_metrics: set[str] = set()
    thresholds = snapshot_thresholds(config)
    retention_days = int(config["SNAPSHOT_LOG_RETENTION_DAYS"])
    cleanup_snapshot_logs(retention_days)
    next_internal_sample = time.monotonic()
    next_sample = next_internal_sample + sample_every
    # CPU and network utilisation are rates. Wait for the first full telemetry
    # window before upload so the dashboard never receives a baseline 0/—.
    next_upload = next_sample
    while not STOP_REQUESTED:
        now = time.monotonic()
        if now >= next_internal_sample:
            try:
                (
                    internal_sample,
                    previous_cpu,
                    previous_processes,
                    previous_network,
                    previous_disk_io,
                    process_cpu_rows,
                    process_cpu_accounted_percent,
                ) = collect(
                    previous_cpu,
                    previous_processes,
                    previous_network,
                    previous_disk_io,
                    config["DISK_PATH"],
                    config,
                )
                if window_start_cpu is None:
                    window_start_cpu = previous_cpu
                    window_start_processes = previous_processes
                else:
                    window_samples.append(internal_sample)
                    accumulate_process_rows(window_process_totals, process_cpu_rows)
                if now >= next_sample and window_samples and window_start_cpu and window_start_processes:
                    sample, window_summary = aggregate_window(
                        window_samples, window_start_cpu, previous_cpu
                    )
                    total_cpu_delta = previous_cpu[0] - window_start_cpu[0]
                    process_cpu_rows, process_cpu_accounted_percent = process_cpu_attribution(
                        window_start_processes, previous_processes, total_cpu_delta
                    )
                    persistent_identities = {
                        (int(row["pid"]), int(row["start_time"])) for row in process_cpu_rows
                    }
                    transient_process_rows = []
                    for identity, row in window_process_totals.items():
                        if identity in persistent_identities:
                            continue
                        row = dict(row)
                        row["system_percent"] = int(row["tick_delta"]) * 100 / total_cpu_delta if total_cpu_delta else 0.0
                        transient_process_rows.append(row)
                    transient_process_rows.sort(key=lambda row: float(row["system_percent"]), reverse=True)
                    transient_process_percent = sum(
                        float(row["system_percent"]) for row in transient_process_rows
                    )
                    high_metrics = high_utilisation_metrics(sample, thresholds)
                    health = sample.get("extra", {}).get("system_health", {})
                    if health.get("filesystem_read_only"):
                        high_metrics.add("Filesystem read-only")
                    newly_high_metrics = high_metrics - active_high_metrics
                    if newly_high_metrics:
                        snapshot = write_snapshot(
                            high_metrics,
                            sample,
                            process_cpu_rows,
                            process_cpu_accounted_percent,
                            transient_process_rows,
                            transient_process_percent,
                            window_summary,
                            config["DISK_PATH"],
                            thresholds,
                            retention_days,
                        )
                        if snapshot:
                            sample["extra"]["diagnostic_snapshot"] = snapshot
                    active_high_metrics = high_metrics
                    append_sample(Path(config["SPOOL_PATH"]), sample, int(config["MAX_SPOOL_SAMPLES"]))
                    window_start_cpu = previous_cpu
                    window_start_processes = previous_processes
                    window_process_totals = {}
                    window_samples = []
                    next_sample += sample_every
                    if next_sample <= now:
                        next_sample = now + sample_every
            except (OSError, ValueError, KeyError) as error:
                LOG.exception("Unable to collect telemetry: %s", error)
            next_internal_sample += internal_sample_every
            if next_internal_sample <= now:
                next_internal_sample = now + internal_sample_every
        if now >= next_upload:
            upload(config)
            thresholds = snapshot_thresholds(config)
            retention_days = int(config["SNAPSHOT_LOG_RETENTION_DAYS"])
            next_upload += upload_every
            if next_upload <= now:
                next_upload = now + upload_every
        time.sleep(min(1.0, max(
            0.05,
            next_internal_sample - time.monotonic(),
            next_sample - time.monotonic(),
            next_upload - time.monotonic(),
        )))
    upload(config)  # Best effort flush when systemd stops the service.
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        try:
            load_config()
            print(f"telemetry-agent {AGENT_VERSION} self-test passed")
            raise SystemExit(0)
        except (OSError, ValueError) as error:
            print(f"telemetry-agent self-test failed: {error}", file=sys.stderr)
            raise SystemExit(2)
    try:
        raise SystemExit(main())
    except ValueError as error:
        LOG.error("Configuration error: %s", error)
        raise SystemExit(2)
