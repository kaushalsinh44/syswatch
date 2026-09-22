"""Background system telemetry logger.

Samples CPU/memory/disk/network/battery and top processes on an interval and
writes them to a SQLite database (data/telemetry.db). Runs in the foreground
until Ctrl+C, or is registered as a background service (see service/).
"""

import argparse
import platform
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil

DB_PATH = Path(__file__).parent / "data" / "telemetry.db"
DEFAULT_INTERVAL_SEC = 45
TOP_N_PROCESSES = 5
BATTERY_HEALTH_INTERVAL_SEC = 24 * 60 * 60  # capacity drifts slowly -- once/day is plenty

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp        TEXT NOT NULL,
    interval_sec     REAL NOT NULL,
    cpu_pct          REAL NOT NULL,
    mem_pct          REAL NOT NULL,
    disk_read_bytes  INTEGER NOT NULL,
    disk_write_bytes INTEGER NOT NULL,
    net_sent_bytes   INTEGER NOT NULL,
    net_recv_bytes   INTEGER NOT NULL,
    battery_pct      REAL,
    charging         INTEGER
);
CREATE INDEX IF NOT EXISTS idx_samples_timestamp ON samples(timestamp);

CREATE TABLE IF NOT EXISTS process_samples (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id    INTEGER NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
    timestamp    TEXT NOT NULL,
    process_name TEXT NOT NULL,
    pid          INTEGER,
    cpu_pct      REAL NOT NULL,
    mem_pct      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_process_samples_timestamp ON process_samples(timestamp);
CREATE INDEX IF NOT EXISTS idx_process_samples_sample_id ON process_samples(sample_id);
CREATE INDEX IF NOT EXISTS idx_process_samples_name ON process_samples(process_name);

CREATE TABLE IF NOT EXISTS battery_health (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp                TEXT NOT NULL,
    full_charge_capacity_mwh INTEGER,
    design_capacity_mwh      INTEGER,
    cycle_count              INTEGER,
    health_pct               REAL
);
CREATE INDEX IF NOT EXISTS idx_battery_health_timestamp ON battery_health(timestamp);

CREATE TABLE IF NOT EXISTS process_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp    TEXT NOT NULL,
    event        TEXT NOT NULL,
    process_name TEXT NOT NULL,
    pid          INTEGER
);
CREATE INDEX IF NOT EXISTS idx_process_events_timestamp ON process_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_process_events_name ON process_events(process_name);
"""


def configure_connection(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA foreign_keys = ON;")


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        configure_connection(conn)
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def get_battery_status() -> tuple[float | None, int | None]:
    battery = psutil.sensors_battery()
    if battery is None:
        return None, None
    charging = None if battery.power_plugged is None else int(battery.power_plugged)
    return battery.percent, charging


def snapshot_io() -> tuple[object, object]:
    return psutil.disk_io_counters(), psutil.net_io_counters()


def compute_deltas(prev_disk, cur_disk, prev_net, cur_net) -> dict:
    def clamp(cur_val, prev_val):
        delta = cur_val - prev_val
        return delta if delta > 0 else 0

    return {
        "disk_read_bytes": clamp(cur_disk.read_bytes, prev_disk.read_bytes),
        "disk_write_bytes": clamp(cur_disk.write_bytes, prev_disk.write_bytes),
        "net_sent_bytes": clamp(cur_net.bytes_sent, prev_net.bytes_sent),
        "net_recv_bytes": clamp(cur_net.bytes_recv, prev_net.bytes_recv),
    }


def sample_processes() -> list[dict]:
    procs = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            cpu_pct = p.cpu_percent(interval=None)
            mem_pct = p.memory_percent()
            name = p.info["name"] or "?"
            procs.append(
                {"pid": p.info["pid"], "name": name, "cpu_pct": cpu_pct, "mem_pct": mem_pct}
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    top_cpu = sorted(procs, key=lambda x: x["cpu_pct"], reverse=True)[:TOP_N_PROCESSES]
    top_mem = sorted(procs, key=lambda x: x["mem_pct"], reverse=True)[:TOP_N_PROCESSES]
    return top_cpu + top_mem


def write_sample(conn: sqlite3.Connection, sample_row: dict, process_rows: list[dict]) -> None:
    cur = conn.execute(
        """
        INSERT INTO samples (
            timestamp, interval_sec, cpu_pct, mem_pct,
            disk_read_bytes, disk_write_bytes, net_sent_bytes, net_recv_bytes,
            battery_pct, charging
        ) VALUES (:timestamp, :interval_sec, :cpu_pct, :mem_pct,
                   :disk_read_bytes, :disk_write_bytes, :net_sent_bytes, :net_recv_bytes,
                   :battery_pct, :charging)
        """,
        sample_row,
    )
    sample_id = cur.lastrowid
    conn.executemany(
        """
        INSERT INTO process_samples (sample_id, timestamp, process_name, pid, cpu_pct, mem_pct)
        VALUES (:sample_id, :timestamp, :process_name, :pid, :cpu_pct, :mem_pct)
        """,
        [
            {
                "sample_id": sample_id,
                "timestamp": sample_row["timestamp"],
                "process_name": p["name"],
                "pid": p["pid"],
                "cpu_pct": p["cpu_pct"],
                "mem_pct": p["mem_pct"],
            }
            for p in process_rows
        ],
    )
    conn.commit()


def get_battery_health() -> dict | None:
    """Windows-only: full-charge capacity, cycle count, and (if available) design
    capacity via WMI's root\\wmi battery classes. Sampled far less often than the
    main loop since capacity drifts slowly -- see BATTERY_HEALTH_INTERVAL_SEC.

    `BatteryStaticData` (design capacity) is missing on some laptops/drivers --
    e.g. it fails with a generic WMI error on the machine this was developed on,
    while FullChargedCapacity and CycleCount both work fine. So design capacity
    (and therefore health_pct) is stored as NULL when unavailable rather than
    skipping the whole sample -- a declining full-charge-capacity trend is
    itself a meaningful degradation signal even without a fixed denominator.
    """
    if platform.system() != "Windows":
        return None
    try:
        ps_script = (
            "$ErrorActionPreference = 'SilentlyContinue'; "
            "$fc = (Get-CimInstance -Namespace root\\wmi -ClassName BatteryFullChargedCapacity).FullChargedCapacity; "
            "$cc = (Get-CimInstance -Namespace root\\wmi -ClassName BatteryCycleCount).CycleCount; "
            "$dc = (Get-CimInstance -Namespace root\\wmi -ClassName BatteryStaticData).DesignedCapacity; "
            "if (-not $fc) { $fc = 'NA' }; if (-not $cc) { $cc = 'NA' }; if (-not $dc) { $dc = 'NA' }; "
            "Write-Output \"$fc,$cc,$dc\""
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return None
        parts = result.stdout.strip().split(",")
        if len(parts) != 3:
            return None

        def parse(v):
            return None if v == "NA" else int(v)

        full_charge, cycle_count, design = (parse(p) for p in parts)
        if full_charge is None:
            return None
        health_pct = (full_charge / design * 100) if design else None
        return {
            "full_charge_capacity_mwh": full_charge,
            "design_capacity_mwh": design,
            "cycle_count": cycle_count,
            "health_pct": health_pct,
        }
    except Exception:
        return None


def diff_process_events(
    prev_pids: set[int], known_names: dict[int, str]
) -> tuple[list[dict], set[int], dict[int, str]]:
    """Compare the currently-running PIDs against the previous snapshot and
    report what started/stopped since then. `psutil.pids()` is a cheap
    syscall-only listing (unlike sample_processes(), which builds full
    Process objects), so diffing it every loop iteration is negligible
    overhead -- names for genuinely new PIDs are looked up individually.
    """
    cur_pids = set(psutil.pids())
    new_pids = cur_pids - prev_pids
    gone_pids = prev_pids - cur_pids

    events = []
    updated_names = dict(known_names)
    for pid in new_pids:
        try:
            name = psutil.Process(pid).name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            name = "?"
        updated_names[pid] = name
        events.append({"event": "started", "process_name": name, "pid": pid})
    for pid in gone_pids:
        name = updated_names.pop(pid, "?")
        events.append({"event": "stopped", "process_name": name, "pid": pid})

    return events, cur_pids, updated_names


def write_process_events(conn: sqlite3.Connection, timestamp: str, events: list[dict]) -> None:
    if not events:
        return
    conn.executemany(
        "INSERT INTO process_events (timestamp, event, process_name, pid) "
        "VALUES (:timestamp, :event, :process_name, :pid)",
        [{**e, "timestamp": timestamp} for e in events],
    )
    conn.commit()


def write_battery_health(conn: sqlite3.Connection, timestamp: str, health: dict) -> None:
    conn.execute(
        """
        INSERT INTO battery_health (
            timestamp, full_charge_capacity_mwh, design_capacity_mwh, cycle_count, health_pct
        ) VALUES (:timestamp, :full_charge_capacity_mwh, :design_capacity_mwh, :cycle_count, :health_pct)
        """,
        {**health, "timestamp": timestamp},
    )
    conn.commit()


def macos_powermetrics_supplement() -> dict | None:
    """OPTIONAL, macOS-ONLY, UNTESTED. Not wired into the schema yet -- see README."""
    if platform.system() != "Darwin":
        return None
    try:
        result = subprocess.run(
            ["sudo", "-n", "powermetrics", "--samplers", "cpu_power", "-i", "1000", "-n", "1"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        # Not parsed/persisted in Phase 1 -- reserved extension point.
        return None
    except Exception:
        return None


def warm_up() -> None:
    psutil.cpu_percent(interval=None)
    for p in psutil.process_iter():
        try:
            p.cpu_percent(interval=None)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    time.sleep(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Laptop Mystery Detective telemetry logger")
    parser.add_argument(
        "--interval", type=float, default=DEFAULT_INTERVAL_SEC, help="Sampling interval in seconds"
    )
    args = parser.parse_args()
    interval = args.interval

    init_db(DB_PATH)
    warm_up()

    prev_disk, prev_net = snapshot_io()
    prev_time = time.monotonic()
    last_battery_health_sample = 0.0  # 0 forces a sample on the first loop iteration
    prev_pids = set(psutil.pids())  # baseline so the first loop doesn't flag every running process as "started"
    known_process_names: dict[int, str] = {}

    print(f"[{datetime.now(timezone.utc).isoformat()}] Logger started. interval={interval}s db={DB_PATH}")

    try:
        while True:
            loop_start = time.monotonic()
            now = datetime.now(timezone.utc)

            cpu_pct = psutil.cpu_percent(interval=None)
            mem_pct = psutil.virtual_memory().percent
            battery_pct, charging = get_battery_status()

            cur_disk, cur_net = snapshot_io()
            elapsed = time.monotonic() - prev_time
            deltas = compute_deltas(prev_disk, cur_disk, prev_net, cur_net)
            prev_disk, prev_net = cur_disk, cur_net
            prev_time = time.monotonic()

            process_rows = sample_processes()
            process_events, prev_pids, known_process_names = diff_process_events(prev_pids, known_process_names)

            if platform.system() == "Darwin":
                macos_powermetrics_supplement()

            sample_row = {
                "timestamp": now.isoformat(),
                "interval_sec": elapsed,
                "cpu_pct": cpu_pct,
                "mem_pct": mem_pct,
                "battery_pct": battery_pct,
                "charging": charging,
                **deltas,
            }

            conn = sqlite3.connect(DB_PATH)
            try:
                configure_connection(conn)
                write_sample(conn, sample_row, process_rows)
                write_process_events(conn, sample_row["timestamp"], process_events)

                if time.monotonic() - last_battery_health_sample >= BATTERY_HEALTH_INTERVAL_SEC:
                    health = get_battery_health()
                    last_battery_health_sample = time.monotonic()
                    if health:
                        write_battery_health(conn, sample_row["timestamp"], health)
                        print(f"  battery health: {health}")
            finally:
                conn.close()

            print(
                f"[{now.isoformat()}] cpu={cpu_pct:.1f}% mem={mem_pct:.1f}% "
                f"disk_r={deltas['disk_read_bytes']}B disk_w={deltas['disk_write_bytes']}B "
                f"net_s={deltas['net_sent_bytes']}B net_r={deltas['net_recv_bytes']}B "
                f"battery={battery_pct} charging={charging} "
                f"({len(process_rows)} process rows, {len(process_events)} launch/exit events)"
            )

            sleep_for = max(0.0, interval - (time.monotonic() - loop_start))
            time.sleep(sleep_for)
    except KeyboardInterrupt:
        print(f"\n[{datetime.now(timezone.utc).isoformat()}] Ctrl+C received -- shutting down cleanly.")
        return


if __name__ == "__main__":
    main()
