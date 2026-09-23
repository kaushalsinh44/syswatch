"""Tests for report.py -- purely a summary of what anomaly.py already found,
so these mostly check formatting/aggregation logic, not detection math."""

import sqlite3
from datetime import date

import pytest

import report


def make_db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(report.ENSURE_ANOMALIES_TABLE)
    conn.commit()
    return conn


def insert_anomaly(conn, target_date, category="process_cpu", subject="chrome.exe", z=5.0, ts=None):
    conn.execute(
        """
        INSERT INTO anomalies (run_at, target_date, timestamp, category, subject, value,
                                baseline_mean, baseline_std, z_score, description)
        VALUES ('2026-09-23T00:00:00+00:00', ?, ?, ?, ?, 90.0, 5.0, 2.0, ?, ?)
        """,
        (target_date, ts or f"{target_date}T10:00:00+00:00", category, subject, z, f"{subject} spike"),
    )
    conn.commit()


class TestMostRecentMonday:
    def test_wednesday_goes_back_to_this_weeks_monday(self):
        wednesday = date(2026, 9, 23)  # a Wednesday
        assert report.most_recent_monday(wednesday) == date(2026, 9, 21)

    def test_monday_stays_on_itself(self):
        monday = date(2026, 9, 21)
        assert report.most_recent_monday(monday) == date(2026, 9, 21)

    def test_sunday_goes_back_to_that_weeks_monday(self):
        sunday = date(2026, 9, 27)
        assert report.most_recent_monday(sunday) == date(2026, 9, 21)


class TestRenderReport:
    def test_empty_week_produces_friendly_message(self):
        text = report.render_report(date(2026, 9, 14), date(2026, 9, 20), [])
        assert "No anomalies were flagged this week" in text
        assert "Case Report: 2026-09-14 to 2026-09-20" in text

    def test_populated_week_includes_all_sections(self, temp_db_path):
        conn = make_db(temp_db_path)
        insert_anomaly(conn, "2026-09-15", category="process_cpu", subject="chrome.exe", z=10.0)
        insert_anomaly(conn, "2026-09-16", category="battery_drain", subject=None, z=6.0)
        anomalies = report.get_week_anomalies(conn, "2026-09-14", "2026-09-20")

        text = report.render_report(date(2026, 9, 14), date(2026, 9, 20), anomalies)

        assert "## Top mysteries this week" in text
        assert "## By category" in text
        assert "## Battery drain sessions" in text
        assert "chrome.exe" in text

    def test_no_battery_section_when_no_battery_events(self, temp_db_path):
        conn = make_db(temp_db_path)
        insert_anomaly(conn, "2026-09-15", category="process_cpu", subject="chrome.exe", z=10.0)
        anomalies = report.get_week_anomalies(conn, "2026-09-14", "2026-09-20")

        text = report.render_report(date(2026, 9, 14), date(2026, 9, 20), anomalies)

        assert "## Battery drain sessions" not in text

    def test_dashboard_links_use_anomaly_id(self, temp_db_path):
        conn = make_db(temp_db_path)
        insert_anomaly(conn, "2026-09-15", z=10.0)
        anomalies = report.get_week_anomalies(conn, "2026-09-14", "2026-09-20")

        text = report.render_report(date(2026, 9, 14), date(2026, 9, 20), anomalies)

        assert f"{report.DASHBOARD_BASE_URL}/case/{anomalies[0]['id']}" in text


class TestGetWeekAnomalies:
    def test_filters_to_date_range(self, temp_db_path):
        conn = make_db(temp_db_path)
        insert_anomaly(conn, "2026-09-13")  # before the week
        insert_anomaly(conn, "2026-09-15")  # inside
        insert_anomaly(conn, "2026-09-21")  # after the week

        result = report.get_week_anomalies(conn, "2026-09-14", "2026-09-20")

        assert len(result) == 1
        assert result[0]["target_date"] == "2026-09-15"

    def test_sorted_by_zscore_descending(self, temp_db_path):
        conn = make_db(temp_db_path)
        insert_anomaly(conn, "2026-09-15", subject="low.exe", z=3.0)
        insert_anomaly(conn, "2026-09-16", subject="high.exe", z=20.0)

        result = report.get_week_anomalies(conn, "2026-09-14", "2026-09-20")

        assert result[0]["subject"] == "high.exe"


class TestGenerateReport:
    def test_writes_file_to_reports_dir(self, temp_db_path, tmp_path, monkeypatch):
        conn = make_db(temp_db_path)
        insert_anomaly(conn, "2026-09-15", z=10.0)
        monkeypatch.setattr(report, "REPORTS_DIR", tmp_path / "reports")

        text, out_path = report.generate_report(conn, date(2026, 9, 14))

        assert out_path.exists()
        assert out_path.read_text(encoding="utf-8") == text
        assert out_path.name == "case-report-2026-09-14-to-2026-09-20.md"
