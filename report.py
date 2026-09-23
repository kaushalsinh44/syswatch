"""Weekly case report for SysWatch.

Summarizes a week's worth of flagged anomalies (from the `anomalies` table,
populated by anomaly.py) into a readable Markdown report: top mysteries,
breakdown by category, most-flagged processes, and battery drain sessions.

Doesn't do any analysis of its own -- it's purely a summary of what
anomaly.py already found, so run `anomaly.py --all` (or at least for the
target week) first.
"""

import argparse
import sqlite3
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "telemetry.db"
REPORTS_DIR = Path(__file__).parent / "reports"
TOP_MYSTERIES_SHOWN = 10
DASHBOARD_BASE_URL = "http://localhost:5050"

# report.py only reads the anomalies table -- it's anomaly.py's job to create it. But if
# anomaly.py has never been run yet (e.g. right after cloning the repo), querying it would
# otherwise crash instead of just reporting "no anomalies this week", which is what an empty
# table should mean anyway.
ENSURE_ANOMALIES_TABLE = """
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
"""


def most_recent_monday(today: date) -> date:
    return today - timedelta(days=today.weekday())


def get_week_anomalies(conn: sqlite3.Connection, week_start: str, week_end: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM anomalies WHERE target_date >= ? AND target_date <= ?
        ORDER BY z_score DESC
        """,
        (week_start, week_end),
    ).fetchall()


def render_report(week_start: date, week_end: date, anomalies: list[sqlite3.Row]) -> str:
    lines = [
        f"# Case Report: {week_start.isoformat()} to {week_end.isoformat()}",
        "",
    ]

    if not anomalies:
        lines.append(
            "No anomalies were flagged this week. Either it was a quiet week, or "
            "`anomaly.py` hasn't been run for these dates yet (`python anomaly.py --all`)."
        )
        return "\n".join(lines)

    by_category = Counter(a["category"] for a in anomalies)
    by_day = Counter(a["target_date"] for a in anomalies)
    subject_counter = Counter(a["subject"] for a in anomalies if a["subject"])

    lines.append(
        f"**{len(anomalies)} anomalies** flagged across "
        f"{len(by_day)} day{'s' if len(by_day) != 1 else ''} this week."
    )
    lines.append("")

    lines.append("## Top mysteries this week")
    lines.append("")
    for a in anomalies[:TOP_MYSTERIES_SHOWN]:
        link = f"{DASHBOARD_BASE_URL}/case/{a['id']}"
        lines.append(
            f"- **[{a['category']}]** {a['description']} "
            f"(z={a['z_score']:.1f}, {a['timestamp']}) -- [investigate]({link})"
        )
    lines.append("")

    lines.append("## By category")
    lines.append("")
    for category, count in by_category.most_common():
        lines.append(f"- `{category}`: {count}")
    lines.append("")

    if subject_counter:
        lines.append("## Most-flagged processes / metrics")
        lines.append("")
        for subject, count in subject_counter.most_common(10):
            lines.append(f"- {subject}: flagged {count} time{'s' if count != 1 else ''}")
        lines.append("")

    battery_events = [a for a in anomalies if a["category"] == "battery_drain"]
    if battery_events:
        lines.append("## Battery drain sessions")
        lines.append("")
        for a in sorted(battery_events, key=lambda x: x["timestamp"]):
            lines.append(f"- {a['timestamp']}: {a['description']} (z={a['z_score']:.1f})")
        lines.append("")

    return "\n".join(lines)


def generate_report(conn: sqlite3.Connection, week_start: date) -> tuple[str, Path]:
    week_end = week_start + timedelta(days=6)
    anomalies = get_week_anomalies(conn, week_start.isoformat(), week_end.isoformat())
    report_text = render_report(week_start, week_end, anomalies)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORTS_DIR / f"case-report-{week_start.isoformat()}-to-{week_end.isoformat()}.md"
    out_path.write_text(report_text, encoding="utf-8")
    return report_text, out_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a weekly case report from flagged anomalies")
    parser.add_argument(
        "--week-start",
        help="Monday of the target week, YYYY-MM-DD (default: most recent completed Monday-Sunday week)",
    )
    args = parser.parse_args()

    if args.week_start:
        week_start = date.fromisoformat(args.week_start)
    else:
        this_monday = most_recent_monday(date.today())
        week_start = this_monday - timedelta(days=7)  # most recently *completed* week

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(ENSURE_ANOMALIES_TABLE)
    _, out_path = generate_report(conn, week_start)
    conn.close()

    print(f"Report written to {out_path}")


if __name__ == "__main__":
    main()
