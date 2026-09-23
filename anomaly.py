"""Anomaly detection for SysWatch.

Compares a given day's telemetry against a rolling per-hour-of-day baseline
built from prior days, and flags readings more than ~2 standard deviations
above normal. Deliberately simple -- no ML, just z-scores over a lookback
window, using only the stdlib `statistics` module.

Flags three categories:
  (a) process_cpu / process_mem -- a process unusually high for this hour
  (b) battery_drain             -- an on-battery session draining unusually fast
  (c) disk_io / net_io          -- a system-wide I/O spike for this hour
"""

import argparse
import platform
import sqlite3
import statistics
import subprocess
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "telemetry.db"
DEFAULT_LOOKBACK_DAYS = 14
Z_THRESHOLD = 3.0
MIN_BASELINE_SAMPLES = 10  # too little history to be meaningful -> skip
EPISODE_GAP_MINUTES = 5.0  # merge same subject+category flags this close together into one episode
NOTIFY_Z_THRESHOLD = 8.0  # only the genuinely severe end -- matches the dashboard's "z-high" highlight

# A tiny baseline stddev (e.g. a process that's almost always idle) turns any
# nonzero reading into a huge z-score even though the absolute jump is trivial.
# Require the raw difference to also clear a minimum before flagging.
MIN_ABS_DELTA = {
    "process_cpu": 15.0,  # percentage points
    "process_mem": 5.0,  # percentage points
    "battery_drain": 0.1,  # %/min
    "disk_io": 1024 * 1024,  # 1MB
    "net_io": 1024 * 1024,  # 1MB
}

# "System Idle Process" reports the CPU capacity NOT in use -- a "spike" in it
# is the inverse of a load spike, not a signal worth flagging.
EXCLUDED_PROCESSES = {"System Idle Process"}

ANOMALIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS anomalies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at        TEXT NOT NULL,
    target_date   TEXT NOT NULL,
    timestamp     TEXT NOT NULL,
    category      TEXT NOT NULL,
    subject       TEXT,
    value         REAL NOT NULL,
    baseline_mean REAL NOT NULL,
    baseline_std  REAL NOT NULL,
    z_score       REAL NOT NULL,
    description   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_anomalies_target_date ON anomalies(target_date);
CREATE INDEX IF NOT EXISTS idx_anomalies_timestamp ON anomalies(timestamp);
"""


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def init_anomalies_table(conn: sqlite3.Connection) -> None:
    conn.executescript(ANOMALIES_SCHEMA)
    conn.commit()


def hour_of(ts: str) -> int:
    return datetime.fromisoformat(ts).hour


def zscore(value: float, mean: float, std: float) -> float:
    if std == 0:
        # A perfectly constant historical baseline (every past sample identical)
        # means ANY deviation is maximally anomalous, not "normal" -- returning 0
        # here would let even a massive spike slip through undetected. Only true
        # if value == mean too (no deviation at all) is 0 actually correct.
        return 0.0 if value == mean else float("inf")
    return (value - mean) / std


def detect_process_anomalies(conn: sqlite3.Connection, target_date: str, lookback_days: int) -> list[dict]:
    lookback_start = (date.fromisoformat(target_date) - timedelta(days=lookback_days)).isoformat()

    hist_rows = conn.execute(
        """
        SELECT DISTINCT process_name, timestamp, cpu_pct, mem_pct FROM process_samples
        WHERE substr(timestamp,1,10) >= ? AND substr(timestamp,1,10) < ?
        """,
        (lookback_start, target_date),
    ).fetchall()

    baseline = defaultdict(lambda: {"cpu": [], "mem": []})
    for name, ts, cpu, mem in hist_rows:
        key = (name, hour_of(ts))
        baseline[key]["cpu"].append(cpu)
        baseline[key]["mem"].append(mem)

    today_rows = conn.execute(
        """
        SELECT DISTINCT process_name, timestamp, cpu_pct, mem_pct FROM process_samples
        WHERE substr(timestamp,1,10) = ?
        """,
        (target_date,),
    ).fetchall()

    anomalies = []
    for name, ts, cpu, mem in today_rows:
        if name in EXCLUDED_PROCESSES:
            continue
        b = baseline.get((name, hour_of(ts)))
        if not b:
            continue
        for metric, val, label, category in (
            ("cpu", cpu, "CPU", "process_cpu"),
            ("mem", mem, "memory", "process_mem"),
        ):
            samples = b[metric]
            if len(samples) < MIN_BASELINE_SAMPLES:
                continue
            mean = statistics.mean(samples)
            std = statistics.pstdev(samples)
            z = zscore(val, mean, std)
            if z > Z_THRESHOLD and (val - mean) >= MIN_ABS_DELTA[category]:
                anomalies.append(
                    {
                        "timestamp": ts,
                        "category": f"process_{metric}",
                        "subject": name,
                        "value": val,
                        "baseline_mean": mean,
                        "baseline_std": std,
                        "z_score": z,
                        "description": (
                            f"{name} {label} at {val:.1f}% vs usual "
                            f"{mean:.1f}% (+/-{std:.1f}) at this hour"
                        ),
                    }
                )
    return anomalies


def get_battery_sessions(conn: sqlite3.Connection, day: str) -> list[dict]:
    """Contiguous on-battery (charging=0) stretches for a given date."""
    rows = conn.execute(
        """
        SELECT timestamp, battery_pct, charging FROM samples
        WHERE substr(timestamp,1,10) = ? AND battery_pct IS NOT NULL AND charging IS NOT NULL
        ORDER BY timestamp
        """,
        (day,),
    ).fetchall()

    sessions = []
    current: list[tuple[str, float]] = []
    for ts, pct, charging in rows:
        if charging == 0:
            current.append((ts, pct))
        else:
            if len(current) >= 2:
                sessions.append(current)
            current = []
    if len(current) >= 2:
        sessions.append(current)

    result = []
    for session in sessions:
        start_ts, start_pct = session[0]
        end_ts, end_pct = session[-1]
        duration_min = (datetime.fromisoformat(end_ts) - datetime.fromisoformat(start_ts)).total_seconds() / 60
        if duration_min <= 0:
            continue
        result.append(
            {
                "start": start_ts,
                "end": end_ts,
                "duration_min": duration_min,
                "drain_rate": (start_pct - end_pct) / duration_min,
                "start_pct": start_pct,
                "end_pct": end_pct,
            }
        )
    return result


def detect_battery_anomalies(conn: sqlite3.Connection, target_date: str, lookback_days: int) -> list[dict]:
    lookback_start = date.fromisoformat(target_date) - timedelta(days=lookback_days)
    end_d = date.fromisoformat(target_date)

    hist_rates = []
    d = lookback_start
    while d < end_d:
        for s in get_battery_sessions(conn, d.isoformat()):
            if s["duration_min"] >= 5:
                hist_rates.append(s["drain_rate"])
        d += timedelta(days=1)

    if len(hist_rates) < MIN_BASELINE_SAMPLES:
        return []

    mean = statistics.mean(hist_rates)
    std = statistics.pstdev(hist_rates)

    anomalies = []
    for s in get_battery_sessions(conn, target_date):
        if s["duration_min"] < 5:
            continue
        z = zscore(s["drain_rate"], mean, std)
        if z > Z_THRESHOLD and (s["drain_rate"] - mean) >= MIN_ABS_DELTA["battery_drain"]:
            anomalies.append(
                {
                    "timestamp": s["start"],
                    "category": "battery_drain",
                    "subject": None,
                    "value": s["drain_rate"],
                    "baseline_mean": mean,
                    "baseline_std": std,
                    "z_score": z,
                    "description": (
                        f"Battery drained {s['drain_rate']:.2f}%/min "
                        f"({s['start_pct']:.0f}%->{s['end_pct']:.0f}% over {s['duration_min']:.0f}min) "
                        f"vs usual {mean:.2f}%/min"
                    ),
                }
            )
    return anomalies


IO_METRIC_LABELS = {
    "disk_read_bytes": ("disk_io", "Disk read"),
    "disk_write_bytes": ("disk_io", "Disk write"),
    "net_sent_bytes": ("net_io", "Network upload"),
    "net_recv_bytes": ("net_io", "Network download"),
}


def detect_io_anomalies(conn: sqlite3.Connection, target_date: str, lookback_days: int) -> list[dict]:
    lookback_start = (date.fromisoformat(target_date) - timedelta(days=lookback_days)).isoformat()

    hist_rows = conn.execute(
        """
        SELECT timestamp, disk_read_bytes, disk_write_bytes, net_sent_bytes, net_recv_bytes
        FROM samples WHERE substr(timestamp,1,10) >= ? AND substr(timestamp,1,10) < ?
        """,
        (lookback_start, target_date),
    ).fetchall()

    baseline = defaultdict(lambda: defaultdict(list))
    for ts, dr, dw, ns, nr in hist_rows:
        h = hour_of(ts)
        baseline[h]["disk_read_bytes"].append(dr)
        baseline[h]["disk_write_bytes"].append(dw)
        baseline[h]["net_sent_bytes"].append(ns)
        baseline[h]["net_recv_bytes"].append(nr)

    today_rows = conn.execute(
        """
        SELECT timestamp, disk_read_bytes, disk_write_bytes, net_sent_bytes, net_recv_bytes
        FROM samples WHERE substr(timestamp,1,10) = ?
        """,
        (target_date,),
    ).fetchall()

    anomalies = []
    for ts, dr, dw, ns, nr in today_rows:
        b = baseline.get(hour_of(ts))
        if not b:
            continue
        for key, val in (
            ("disk_read_bytes", dr),
            ("disk_write_bytes", dw),
            ("net_sent_bytes", ns),
            ("net_recv_bytes", nr),
        ):
            samples = b[key]
            if len(samples) < MIN_BASELINE_SAMPLES:
                continue
            mean = statistics.mean(samples)
            std = statistics.pstdev(samples)
            z = zscore(val, mean, std)
            category, label = IO_METRIC_LABELS[key]
            if z > Z_THRESHOLD and (val - mean) >= MIN_ABS_DELTA[category]:
                anomalies.append(
                    {
                        "timestamp": ts,
                        "category": category,
                        "subject": key,
                        "value": val,
                        "baseline_mean": mean,
                        "baseline_std": std,
                        "z_score": z,
                        "description": (
                            f"{label} spike: {val / 1024:.0f}KB vs usual "
                            f"{mean / 1024:.0f}KB (+/-{std / 1024:.0f}KB) at this hour"
                        ),
                    }
                )
    return anomalies


def collapse_episodes(anomalies: list[dict], gap_minutes: float = EPISODE_GAP_MINUTES) -> list[dict]:
    """Merge consecutive flags for the same subject+category into one episode,
    keeping the peak (highest z-score) reading as the representative row.

    Without this, a single sustained spike (e.g. a process pegged at 100% CPU
    for ten minutes) would otherwise show up as a dozen near-identical rows,
    one per sampling interval -- noise that drowns out genuinely distinct
    incidents.
    """
    if not anomalies:
        return []

    grouped = defaultdict(list)
    for a in anomalies:
        grouped[(a["category"], a.get("subject") or "")].append(a)

    episodes = []
    for items in grouped.values():
        items.sort(key=lambda a: a["timestamp"])
        run = [items[0]]
        for a in items[1:]:
            prev_ts = datetime.fromisoformat(run[-1]["timestamp"])
            cur_ts = datetime.fromisoformat(a["timestamp"])
            if (cur_ts - prev_ts).total_seconds() <= gap_minutes * 60:
                run.append(a)
            else:
                episodes.append(_summarize_episode(run))
                run = [a]
        episodes.append(_summarize_episode(run))
    return episodes


def _summarize_episode(items: list[dict]) -> dict:
    peak = dict(max(items, key=lambda a: a["z_score"]))
    start_ts, end_ts = items[0]["timestamp"], items[-1]["timestamp"]
    if start_ts != end_ts:
        peak["description"] += f" (sustained {start_ts[11:19]}-{end_ts[11:19]}, {len(items)} samples)"
    return peak


def run_detection(conn: sqlite3.Connection, target_date: str, lookback_days: int) -> list[dict]:
    anomalies = []
    anomalies += collapse_episodes(detect_process_anomalies(conn, target_date, lookback_days))
    anomalies += detect_battery_anomalies(conn, target_date, lookback_days)  # already one row per session
    anomalies += collapse_episodes(detect_io_anomalies(conn, target_date, lookback_days))
    return anomalies


def save_anomalies(conn: sqlite3.Connection, target_date: str, anomalies: list[dict]) -> None:
    conn.execute("DELETE FROM anomalies WHERE target_date = ?", (target_date,))
    run_at = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        """
        INSERT INTO anomalies (
            run_at, target_date, timestamp, category, subject,
            value, baseline_mean, baseline_std, z_score, description
        ) VALUES (:run_at, :target_date, :timestamp, :category, :subject,
                   :value, :baseline_mean, :baseline_std, :z_score, :description)
        """,
        [{**a, "run_at": run_at, "target_date": target_date} for a in anomalies],
    )
    conn.commit()


def send_notification(title: str, message: str) -> None:
    """Best-effort Windows toast notification via PowerShell's WinRT toast API --
    no extra pip dependency needed. Uses PowerShell's own registered AUMID
    ("Windows PowerShell") as the notifier, which works for ad-hoc scripts
    without needing to register a dedicated app id. Silently does nothing on
    failure or on non-Windows platforms -- a missed notification should never
    break anomaly detection itself.
    """
    if platform.system() != "Windows":
        return
    try:
        ps_script = f"""
        [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
        [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
        $template = @"
<toast><visual><binding template="ToastGeneric"><text>{title}</text><text>{message}</text></binding></visual></toast>
"@
        $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
        $xml.LoadXml($template)
        $toast = New-Object Windows.UI.Notifications.ToastNotification $xml
        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Windows PowerShell").Show($toast)
        """
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        pass


def maybe_notify(target_date: str, anomalies: list[dict]) -> None:
    """Only notify for the live/current day -- never during a --all backfill of
    old history, which would otherwise fire a notification storm."""
    today = datetime.now(timezone.utc).date().isoformat()
    if target_date != today:
        return
    severe = [a for a in anomalies if a["z_score"] >= NOTIFY_Z_THRESHOLD]
    if not severe:
        return
    top = max(severe, key=lambda x: x["z_score"])
    title = f"{len(severe)} myster{'y' if len(severe) == 1 else 'ies'} flagged today"
    send_notification(title, top["description"])


def main() -> None:
    parser = argparse.ArgumentParser(description="Detect telemetry anomalies vs rolling baseline")
    parser.add_argument("--date", help="Target date YYYY-MM-DD (default: today, UTC)")
    parser.add_argument(
        "--all", action="store_true", help="Recompute for every day present in the database"
    )
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    init_anomalies_table(conn)

    if not table_exists(conn, "samples"):
        print("No telemetry data found yet -- run logger.py first to start collecting data.")
        conn.close()
        return

    if args.all:
        row = conn.execute("SELECT MIN(substr(timestamp,1,10)), MAX(substr(timestamp,1,10)) FROM samples").fetchone()
        min_date, max_date = row
        if not min_date:
            print("No samples in the database yet.")
            return
        d = date.fromisoformat(min_date)
        end_d = date.fromisoformat(max_date)
        total = 0
        while d <= end_d:
            date_str = d.isoformat()
            anomalies = run_detection(conn, date_str, args.lookback_days)
            save_anomalies(conn, date_str, anomalies)
            maybe_notify(date_str, anomalies)
            if anomalies:
                print(f"{date_str}: {len(anomalies)} anomalies")
            total += len(anomalies)
            d += timedelta(days=1)
        print(f"Done. {total} anomalies total across {(end_d - date.fromisoformat(min_date)).days + 1} days.")
    else:
        target_date = args.date or datetime.now(timezone.utc).date().isoformat()
        anomalies = run_detection(conn, target_date, args.lookback_days)
        save_anomalies(conn, target_date, anomalies)
        print(f"{target_date}: {len(anomalies)} anomalies flagged.")
        for a in sorted(anomalies, key=lambda x: x["z_score"], reverse=True):
            print(f"  [{a['category']}] {a['description']} (z={a['z_score']:.1f})")
        maybe_notify(target_date, anomalies)

    conn.close()


if __name__ == "__main__":
    main()
