"""Trend insights for SysWatch.

Compares each process's average CPU/memory over a recent window against an
earlier window to surface longer-term drift -- "is this process gradually
getting worse" -- something a snapshot-based optimizer tool has no way to
see, since it never remembers what "normal" looked like a month ago.

Deliberately simple: two averages and a percent change, no ML. Needs at
least RECENT_WINDOW_DAYS + PRIOR_WINDOW_DAYS of history to say anything.
"""

import argparse
import sqlite3
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "telemetry.db"
RECENT_WINDOW_DAYS = 7
PRIOR_WINDOW_DAYS = 21  # the 21 days before the recent window -- trend spans ~4 weeks total
MIN_SAMPLES_PER_WINDOW = 20  # too little data in either window to be meaningful -> skip
MIN_RELATIVE_CHANGE = 0.25  # only report >=25% change -- smaller swings are just noise
MIN_ABS_CPU = 2.0  # ignore processes trivially small in both windows even if % change is big
MIN_ABS_MEM = 1.0

TRENDS_SCHEMA = """
CREATE TABLE IF NOT EXISTS trends (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    computed_at TEXT NOT NULL,
    as_of_date  TEXT NOT NULL,
    subject     TEXT NOT NULL,
    metric      TEXT NOT NULL,
    recent_avg  REAL NOT NULL,
    prior_avg   REAL NOT NULL,
    pct_change  REAL NOT NULL,
    direction   TEXT NOT NULL,
    description TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trends_as_of_date ON trends(as_of_date);
"""


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def init_trends_table(conn: sqlite3.Connection) -> None:
    conn.executescript(TRENDS_SCHEMA)
    conn.commit()


def compute_trends(conn: sqlite3.Connection, as_of: date) -> list[dict]:
    recent_start = as_of - timedelta(days=RECENT_WINDOW_DAYS)
    prior_start = recent_start - timedelta(days=PRIOR_WINDOW_DAYS)

    recent_rows = conn.execute(
        "SELECT process_name, cpu_pct, mem_pct FROM process_samples WHERE timestamp >= ? AND timestamp < ?",
        (recent_start.isoformat(), as_of.isoformat()),
    ).fetchall()
    prior_rows = conn.execute(
        "SELECT process_name, cpu_pct, mem_pct FROM process_samples WHERE timestamp >= ? AND timestamp < ?",
        (prior_start.isoformat(), recent_start.isoformat()),
    ).fetchall()

    def aggregate(rows):
        agg = defaultdict(lambda: {"cpu": [], "mem": []})
        for name, cpu, mem in rows:
            agg[name]["cpu"].append(cpu)
            agg[name]["mem"].append(mem)
        return agg

    recent = aggregate(recent_rows)
    prior = aggregate(prior_rows)

    trends = []
    for name in recent:
        if name not in prior:
            continue
        r, p = recent[name], prior[name]
        for metric, min_abs, label in (("cpu", MIN_ABS_CPU, "CPU"), ("mem", MIN_ABS_MEM, "memory")):
            if len(r[metric]) < MIN_SAMPLES_PER_WINDOW or len(p[metric]) < MIN_SAMPLES_PER_WINDOW:
                continue
            recent_avg = statistics.mean(r[metric])
            prior_avg = statistics.mean(p[metric])
            if prior_avg < min_abs and recent_avg < min_abs:
                continue  # both trivially small -- not interesting even if the % swing looks big
            if prior_avg == 0:
                continue
            pct_change = (recent_avg - prior_avg) / prior_avg
            if abs(pct_change) < MIN_RELATIVE_CHANGE:
                continue
            direction = "up" if pct_change > 0 else "down"
            trends.append(
                {
                    "subject": name,
                    "metric": metric,
                    "recent_avg": recent_avg,
                    "prior_avg": prior_avg,
                    "pct_change": pct_change,
                    "direction": direction,
                    "description": (
                        f"{name} average {label} usage {'grew' if direction == 'up' else 'dropped'} "
                        f"{abs(pct_change) * 100:.0f}% -- {prior_avg:.1f}% -> {recent_avg:.1f}% over the "
                        f"last {RECENT_WINDOW_DAYS} days vs. the {PRIOR_WINDOW_DAYS} days before that"
                    ),
                }
            )
    return sorted(trends, key=lambda t: abs(t["pct_change"]), reverse=True)


def save_trends(conn: sqlite3.Connection, as_of: date, trends: list[dict]) -> None:
    conn.execute("DELETE FROM trends")  # always reflects only the latest computation
    computed_at = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        """
        INSERT INTO trends (
            computed_at, as_of_date, subject, metric, recent_avg, prior_avg, pct_change, direction, description
        ) VALUES (:computed_at, :as_of_date, :subject, :metric, :recent_avg, :prior_avg, :pct_change, :direction, :description)
        """,
        [{**t, "computed_at": computed_at, "as_of_date": as_of.isoformat()} for t in trends],
    )
    conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Compute longer-term per-process usage trends")
    parser.add_argument("--as-of", help="Compute trends as of this date YYYY-MM-DD (default: today, UTC)")
    args = parser.parse_args()

    as_of = date.fromisoformat(args.as_of) if args.as_of else datetime.now(timezone.utc).date()

    conn = sqlite3.connect(DB_PATH)
    init_trends_table(conn)

    if not table_exists(conn, "process_samples"):
        print("No telemetry data found yet -- run logger.py first to start collecting data.")
        conn.close()
        return

    trends = compute_trends(conn, as_of)
    save_trends(conn, as_of, trends)
    conn.close()

    print(f"{len(trends)} trends computed as of {as_of.isoformat()}.")
    for t in trends[:15]:
        arrow = "^" if t["direction"] == "up" else "v"
        print(f"  [{arrow}] {t['description']}")


if __name__ == "__main__":
    main()
