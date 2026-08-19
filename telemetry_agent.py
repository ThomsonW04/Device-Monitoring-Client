#!/usr/bin/env python3
"""Collect Linux hardware telemetry and periodically deliver it to the monitor."""

# This file deliberately uses only the Python standard library so it can run on
# a Raspberry Pi without installing packages.

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
import time
from heapq import heappush, heapreplace
import uuid
from datetime import datetime, timedelta, timezone
from logging.handlers import SysLogHandler
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen


# All device-specific settings belong in the EnvironmentFile specified here.
CONFIG_PATH = Path(os.environ.get("AGV_MONITOR_CONFIG", "/etc/agv-monitor/telemetry.conf"))
DEFAULTS = {
    "SERVER_URL": "https://10.54.168.13:8085/api/v1/telemetry",
    "DEVICE_TOKEN": "",
    "SAMPLE_INTERVAL_SECONDS": "5",
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
    "SNAPSHOT_ETH1_THRESHOLD_PERCENT": "95",
    "SNAPSHOT_TEMPERATURE_THRESHOLD_C": "80",
    "SNAPSHOT_LOG_RETENTION_DAYS": "30",
}
MAX_BATCH_SIZE = 90  # The server API's explicit maximum.
NETWORK_INTERFACES = ("eth0", "eth1")
STOP_REQUESTED = False
SNAPSHOT_LOG_DIRECTORY = Path("/var/log")
SNAPSHOT_LOG_PREFIX = "AGV-Monitor-"
SNAPSHOT_METRICS = {
    "CPU": ("cpu_percent", "SNAPSHOT_CPU_THRESHOLD_PERCENT"),
    "RAM": ("memory_percent", "SNAPSHOT_MEMORY_THRESHOLD_PERCENT"),
    "Storage": ("disk_percent", "SNAPSHOT_STORAGE_THRESHOLD_PERCENT"),
    "Disk I/O": ("disk_io_percent", "SNAPSHOT_DISK_IO_THRESHOLD_PERCENT"),
    "Swap": ("swap_percent", "SNAPSHOT_SWAP_THRESHOLD_PERCENT"),
    "eth0": ("eth0_percent", "SNAPSHOT_ETH0_THRESHOLD_PERCENT"),
    "eth1": ("eth1_percent", "SNAPSHOT_ETH1_THRESHOLD_PERCENT"),
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
    for key in ("SAMPLE_INTERVAL_SECONDS", "UPLOAD_INTERVAL_SECONDS", "HTTP_TIMEOUT_SECONDS"):
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
    rows: list[dict[str, int | str | float]],
    accounted_percent: float,
    system_cpu_percent: float,
    cpu_count: int,
    limit: int = 40,
) -> str:
    """Format process CPU deltas measured during the triggering interval."""
    lines = [
        "Each value is measured over the same interval as the CPU alert.",
        "USER                 PID    PPID  NI STAT SYSTEM% CORE%  TICKS COMMAND",
    ]
    for row in rows[:limit]:
        system_percent = float(row["system_percent"])
        lines.append(
            f"{str(row['user'])[:16]:<16} {int(row['pid']):>7} {int(row['ppid']):>7} "
            f"{int(row['nice']):>3} {str(row['state']):<4} {system_percent:>6.1f} "
            f"{system_percent * cpu_count:>5.1f} {int(row['tick_delta']):>6} {row['command']}"
        )
    if len(rows) > limit:
        lines.append(f"… {len(rows) - limit} lower-CPU processes omitted")
    unattributed = max(0.0, system_cpu_percent - accounted_percent)
    lines.extend((
        "",
        f"System CPU: {system_cpu_percent:.1f}%",
        f"Processes accounted for: {accounted_percent:.1f}%",
        f"Kernel/interrupts or processes outside interval: {unattributed:.1f}%",
    ))
    return "\n".join(lines) + "\n"


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
        "eth1_percent": network["eth1"],
        "eth0_link_up": link_up["eth0"],
        "eth1_link_up": link_up["eth1"],
        "extra": {"hostname": socket.gethostname()},
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
            write_section(
                log_file,
                "SYSTEM",
                "\n".join(
                    (
                        f"Hostname: {sample['extra']['hostname']}",
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
            write_section(log_file, "ETH1 DETAILS", command_output(["ip", "-s", "link", "show", "eth1"]))
            write_section(log_file, "NETWORK SOCKET SUMMARY", command_output(["ss", "-s"]))
            write_section(log_file, "RASPBERRY PI THERMAL STATUS", command_output([
                "vcgencmd", "get_throttled"
            ]))
            if "Storage" in triggered:
                largest = largest_files(disk_path)
                file_list = "\n".join(
                    f"{size:>14,} bytes" for size, _file_path in largest
                ) or "No regular files found."
                file_list = (
                    "Individual file paths are omitted to avoid recording potentially "
                    "sensitive names.\n" + file_list
                )
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
    synchronise_snapshot_settings(config)
    sample_every = float(config["SAMPLE_INTERVAL_SECONDS"])
    upload_every = float(config["UPLOAD_INTERVAL_SECONDS"])
    if sample_every * MAX_BATCH_SIZE < upload_every:
        LOG.warning("Upload interval produces more than %s samples; multiple uploads will be required", MAX_BATCH_SIZE)
    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    previous_cpu = None
    previous_processes = None
    previous_network = None
    previous_disk_io = None
    active_high_metrics: set[str] = set()
    thresholds = snapshot_thresholds(config)
    retention_days = int(config["SNAPSHOT_LOG_RETENTION_DAYS"])
    cleanup_snapshot_logs(retention_days)
    next_midday_maintenance = time.monotonic() + seconds_until_next_midday()
    next_sample = time.monotonic()
    # CPU and network utilisation are rates. Wait for the second collection
    # before the first upload so the dashboard never receives a baseline 0/—.
    next_upload = next_sample + sample_every
    while not STOP_REQUESTED:
        now = time.monotonic()
        if now >= next_midday_maintenance:
            synchronise_snapshot_settings(config)
            thresholds = snapshot_thresholds(config)
            retention_days = int(config["SNAPSHOT_LOG_RETENTION_DAYS"])
            cleanup_snapshot_logs(retention_days)
            active_high_metrics = set()
            next_midday_maintenance = time.monotonic() + seconds_until_next_midday()
        if now >= next_sample:
            try:
                (
                    sample,
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
                )
                high_metrics = high_utilisation_metrics(sample, thresholds)
                newly_high_metrics = high_metrics - active_high_metrics
                if newly_high_metrics:
                    snapshot = write_snapshot(
                        high_metrics,
                        sample,
                        process_cpu_rows,
                        process_cpu_accounted_percent,
                        config["DISK_PATH"],
                        thresholds,
                        retention_days,
                    )
                    if snapshot:
                        sample["extra"]["diagnostic_snapshot"] = snapshot
                active_high_metrics = high_metrics
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
