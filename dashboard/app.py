"""Local read-only dashboard for Laptop Mystery Detective.

Shows a CPU/memory/battery timeline plus flagged anomalies from anomaly.py,
with a "case file" drill-down into what else was happening around a flagged
spike. Safe to run alongside logger.py -- the database is in WAL mode, so
concurrent reads don't block the writer.
"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, abort, g, jsonify, render_template, request

DB_PATH = Path(__file__).parent.parent / "data" / "telemetry.db"
TOP_ANOMALIES_SHOWN = 20
CASE_WINDOW_MINUTES = 15

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

    return render_template(
        "index.html",
        latest=latest,
        anomaly_date=anomaly_date,
        anomalies=anomalies,
        total_count=total_count,
        shown_count=len(anomalies),
        available_dates=available_dates,
    )


@app.route("/battery")
def battery():
    return render_template("battery.html")


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
    hours = 24 if range_param == "24h" else 24 * 7
    db = get_db()
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
