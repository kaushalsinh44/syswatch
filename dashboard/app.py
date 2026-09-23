"""Local read-only dashboard for SysWatch.

Shows a CPU/memory/battery timeline plus flagged anomalies from anomaly.py,
with a "case file" drill-down into what else was happening around a flagged
spike. Safe to run alongside logger.py -- the database is in WAL mode, so
concurrent reads don't block the writer.
"""

import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import psutil
from flask import Flask, abort, g, jsonify, render_template, request

DB_PATH = Path(__file__).parent.parent / "data" / "telemetry.db"
TOP_ANOMALIES_SHOWN = 20
CASE_WINDOW_MINUTES = 15

# Never kill these regardless of what the client sends -- killing the wrong one of these
# can crash networking, the shell, or (for python.exe/pythonw.exe) this very dashboard or
# the logger itself.
PROTECTED_PROCESS_NAMES = {
    "system", "system idle process", "registry", "smss.exe", "csrss.exe", "wininit.exe",
    "winlogon.exe", "services.exe", "lsass.exe", "svchost.exe", "explorer.exe", "dwm.exe",
    "python.exe", "pythonw.exe",
}

app = Flask(__name__)


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception=None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def latest_sample_time(db: sqlite3.Connection) -> str | None:
    row = db.execute("SELECT MAX(timestamp) FROM samples").fetchone()
    return row[0] if row else None


def pick_anomaly_date(db: sqlite3.Connection, latest_date: str | None) -> str | None:
    """Prefer the latest day's anomalies; fall back to the most recent day that has any."""
    if latest_date:
        count = db.execute(
            "SELECT COUNT(*) FROM anomalies WHERE target_date = ?", (latest_date,)
        ).fetchone()[0]
        if count:
            return latest_date
    row = db.execute("SELECT MAX(target_date) FROM anomalies").fetchone()
    return row[0] if row and row[0] else latest_date


@app.route("/")
def index():
    db = get_db()
    latest = latest_sample_time(db)
    latest_date = latest[:10] if latest else None

    anomaly_date = request.args.get("date") or pick_anomaly_date(db, latest_date)

    total_count = 0
    anomalies = []
    if anomaly_date:
        total_count = db.execute(
            "SELECT COUNT(*) FROM anomalies WHERE target_date = ?", (anomaly_date,)
        ).fetchone()[0]
        anomalies = db.execute(
            "SELECT * FROM anomalies WHERE target_date = ? ORDER BY z_score DESC LIMIT ?",
            (anomaly_date, TOP_ANOMALIES_SHOWN),
        ).fetchall()

    available_dates = [
        r[0] for r in db.execute("SELECT DISTINCT target_date FROM anomalies ORDER BY target_date DESC").fetchall()
    ]
    if latest_date and latest_date not in available_dates:
        # Always offer "today" (the live view) even before any anomalies have been flagged for it.
        available_dates.insert(0, latest_date)

    return render_template(
        "index.html",
        latest=latest,
        latest_date=latest_date,
        anomaly_date=anomaly_date,
        anomalies=anomalies,
        total_count=total_count,
        shown_count=len(anomalies),
        available_dates=available_dates,
    )


@app.route("/api/stats")
def api_stats():
    db = get_db()
    latest = db.execute(
        "SELECT timestamp, cpu_pct, mem_pct, battery_pct, charging FROM samples ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    days_monitored = db.execute("SELECT COUNT(DISTINCT substr(timestamp,1,10)) FROM samples").fetchone()[0]
    total_samples = db.execute("SELECT COUNT(*) FROM samples").fetchone()[0]

    today = (datetime.fromisoformat(latest["timestamp"]).date().isoformat()) if latest else None
    anomalies_today = 0
    if today:
        anomalies_today = db.execute(
            "SELECT COUNT(*) FROM anomalies WHERE target_date = ?", (today,)
        ).fetchone()[0]

    since_24h = (datetime.fromisoformat(latest["timestamp"]) - timedelta(hours=24)).isoformat() if latest else None
    launches_24h = 0
    if since_24h:
        launches_24h = db.execute(
            "SELECT COUNT(*) FROM process_events WHERE event = 'started' AND timestamp >= ?", (since_24h,)
        ).fetchone()[0]

    battery_row = db.execute(
        "SELECT cycle_count, full_charge_capacity_mwh FROM battery_health ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()

    return jsonify(
        {
            "latest_timestamp": latest["timestamp"] if latest else None,
            "cpu_pct": latest["cpu_pct"] if latest else None,
            "mem_pct": latest["mem_pct"] if latest else None,
            "battery_pct": latest["battery_pct"] if latest else None,
            "charging": latest["charging"] if latest else None,
            "days_monitored": days_monitored,
            "total_samples": total_samples,
            "anomalies_today": anomalies_today,
            "launches_24h": launches_24h,
            "cycle_count": battery_row["cycle_count"] if battery_row else None,
        }
    )


@app.route("/api/top-processes")
def api_top_processes():
    """Top processes as of the most recent sample (up to ~45s old -- the logger's own
    interval -- reusing its already warm-up-primed cpu% readings rather than
    re-implementing psutil's warm-up dance for a one-off request)."""
    db = get_db()
    latest_sample = db.execute("SELECT id, timestamp FROM samples ORDER BY timestamp DESC LIMIT 1").fetchone()
    if not latest_sample:
        return jsonify({"as_of": None, "processes": []})
    rows = db.execute(
        """
        SELECT DISTINCT process_name, pid, cpu_pct, mem_pct FROM process_samples
        WHERE sample_id = ? ORDER BY cpu_pct DESC
        """,
        (latest_sample["id"],),
    ).fetchall()
    return jsonify({"as_of": latest_sample["timestamp"], "processes": [dict(r) for r in rows]})


@app.route("/api/kill-process", methods=["POST"])
def api_kill_process():
    data = request.get_json(silent=True) or {}
    pid = data.get("pid")
    expected_name = str(data.get("process_name") or "").strip().lower()

    if not isinstance(pid, int):
        return jsonify({"ok": False, "error": "Missing or invalid pid."}), 400
    if pid == os.getpid():
        return jsonify({"ok": False, "error": "Refusing to kill the dashboard's own process."}), 403
    if expected_name in PROTECTED_PROCESS_NAMES:
        return jsonify({"ok": False, "error": f"Refusing to kill a protected system process ({expected_name})."}), 403

    try:
        proc = psutil.Process(pid)
        actual_name = proc.name()
        if actual_name.lower() != expected_name:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": (
                            f"PID {pid} is now '{actual_name}', not '{expected_name}' -- "
                            "it likely already exited. Refusing to kill an unrelated process."
                        ),
                    }
                ),
                409,
            )
        if actual_name.lower() in PROTECTED_PROCESS_NAMES:
            return jsonify({"ok": False, "error": f"Refusing to kill a protected system process ({actual_name})."}), 403

        proc.terminate()
        try:
            proc.wait(timeout=3)
        except psutil.TimeoutExpired:
            proc.kill()  # didn't respond to terminate() -- escalate to a hard kill

        return jsonify({"ok": True, "message": f"Ended {actual_name} (PID {pid})."})
    except psutil.NoSuchProcess:
        return jsonify({"ok": False, "error": "That process is already gone."}), 404
    except psutil.AccessDenied:
        return jsonify({"ok": False, "error": "Access denied -- this process needs elevated rights to end."}), 403
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/battery")
def battery():
    return render_template("battery.html")


@app.route("/trends")
def trends():
    return render_template("trends.html")


@app.route("/api/trends")
def api_trends():
    db = get_db()
    row = db.execute("SELECT MAX(as_of_date), MAX(computed_at) FROM trends").fetchone()
    as_of_date, computed_at = (row[0], row[1]) if row else (None, None)
    rows = db.execute(
        """
        SELECT subject, metric, recent_avg, prior_avg, pct_change, direction, description
        FROM trends ORDER BY ABS(pct_change) DESC
        """
    ).fetchall()
    return jsonify({"as_of_date": as_of_date, "computed_at": computed_at, "trends": [dict(r) for r in rows]})


@app.route("/api/battery-health")
def api_battery_health():
    db = get_db()
    rows = db.execute(
        """
        SELECT timestamp, full_charge_capacity_mwh, design_capacity_mwh, cycle_count, health_pct
        FROM battery_health ORDER BY timestamp
        """
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/timeline")
def api_timeline():
    range_param = request.args.get("range", "24h")
    date_param = request.args.get("date")
    db = get_db()

    if date_param:
        # Historical view: show the selected calendar day (or the 7 days ending on it),
        # not "last N hours from now" -- otherwise picking a past date from the anomaly
        # date selector wouldn't actually change what the chart shows.
        end_date = date.fromisoformat(date_param)
        start_date = end_date if range_param == "24h" else end_date - timedelta(days=6)
        since = f"{start_date.isoformat()}T00:00:00"
        until = f"{end_date.isoformat()}T23:59:59.999999"
        rows = db.execute(
            """
            SELECT timestamp, cpu_pct, mem_pct, battery_pct, charging
            FROM samples WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp
            """,
            (since, until),
        ).fetchall()
        return jsonify([dict(r) for r in rows])

    # Live view: last N hours from the most recent sample.
    hours = 24 if range_param == "24h" else 24 * 7
    latest = latest_sample_time(db)
    if not latest:
        return jsonify([])
    since = (datetime.fromisoformat(latest) - timedelta(hours=hours)).isoformat()
    rows = db.execute(
        """
        SELECT timestamp, cpu_pct, mem_pct, battery_pct, charging
        FROM samples WHERE timestamp >= ? ORDER BY timestamp
        """,
        (since,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/case/<int:anomaly_id>")
def case(anomaly_id: int):
    db = get_db()
    anomaly = db.execute("SELECT * FROM anomalies WHERE id = ?", (anomaly_id,)).fetchone()
    if anomaly is None:
        abort(404)

    center = datetime.fromisoformat(anomaly["timestamp"])
    window_start = (center - timedelta(minutes=CASE_WINDOW_MINUTES)).isoformat()
    window_end = (center + timedelta(minutes=CASE_WINDOW_MINUTES)).isoformat()

    samples = db.execute(
        "SELECT * FROM samples WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp",
        (window_start, window_end),
    ).fetchall()

    process_rows = db.execute(
        """
        SELECT DISTINCT process_name, timestamp, cpu_pct, mem_pct FROM process_samples
        WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp
        """,
        (window_start, window_end),
    ).fetchall()

    proc_agg: dict[str, dict] = {}
    for p in process_rows:
        name = p["process_name"]
        entry = proc_agg.setdefault(name, {"name": name, "peak_cpu": 0.0, "peak_mem": 0.0, "count": 0})
        entry["peak_cpu"] = max(entry["peak_cpu"], p["cpu_pct"])
        entry["peak_mem"] = max(entry["peak_mem"], p["mem_pct"])
        entry["count"] += 1
    processes = sorted(proc_agg.values(), key=lambda x: x["peak_cpu"], reverse=True)

    process_events = db.execute(
        """
        SELECT timestamp, event, process_name, pid FROM process_events
        WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp
        """,
        (window_start, window_end),
    ).fetchall()

    other_anomalies = db.execute(
        """
        SELECT * FROM anomalies WHERE timestamp BETWEEN ? AND ? AND id != ?
        ORDER BY z_score DESC
        """,
        (window_start, window_end, anomaly_id),
    ).fetchall()

    return render_template(
        "case.html",
        anomaly=anomaly,
        samples=samples,
        processes=processes,
        process_events=process_events,
        other_anomalies=other_anomalies,
        window_start=window_start,
        window_end=window_end,
    )


if __name__ == "__main__":
    import os

    app.run(debug=True, port=int(os.environ.get("PORT", 5050)))
