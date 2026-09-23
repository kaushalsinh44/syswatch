"""Tests for trends.py -- the recent-vs-prior-window comparison that's meant
to catch gradual drift a snapshot-based tool can't see."""

import sqlite3
from datetime import date, timedelta

import pytest

import logger as logger_module
import trends


def make_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(logger_module.SCHEMA)
    conn.executescript(trends.TRENDS_SCHEMA)
    conn.commit()
    return conn


AS_OF = date(2026, 9, 23)


def insert_samples(conn, process_name: str, day_offset_range, cpu: float, mem: float = 2.0, per_day: int = 3):
    # MIN_SAMPLES_PER_WINDOW counts individual sample rows, not days -- real
    # data has many rows/day, so tests need multiple rows/day too to realistically
    # clear that floor (e.g. a 7-day recent window needs 7*per_day >= 20).
    for day_offset in day_offset_range:
        d = (AS_OF - timedelta(days=day_offset)).isoformat()
        for hour in range(per_day):
            conn.execute(
                "INSERT INTO process_samples (sample_id, timestamp, process_name, pid, cpu_pct, mem_pct) "
                "VALUES (1, ?, ?, 100, ?, ?)",
                (f"{d}T{10 + hour:02d}:00:00+00:00", process_name, cpu, mem),
            )
    conn.commit()


class TestComputeTrends:
    def test_detects_growth(self, temp_db_path):
        conn = make_db(temp_db_path)
        # Prior 21 days (offsets 8-28): steady 5% CPU
        insert_samples(conn, "chrome.exe", range(8, 29), cpu=5.0)
        # Recent 7 days (offsets 1-7): jumped to 40% CPU
        insert_samples(conn, "chrome.exe", range(1, 8), cpu=40.0)

        result = trends.compute_trends(conn, AS_OF)

        chrome_cpu = [t for t in result if t["subject"] == "chrome.exe" and t["metric"] == "cpu"]
        assert len(chrome_cpu) == 1
        assert chrome_cpu[0]["direction"] == "up"
        assert chrome_cpu[0]["pct_change"] == pytest.approx(7.0, rel=0.01)  # 5 -> 40 is +700%

    def test_detects_decline(self, temp_db_path):
        conn = make_db(temp_db_path)
        insert_samples(conn, "oldapp.exe", range(8, 29), cpu=50.0)
        insert_samples(conn, "oldapp.exe", range(1, 8), cpu=10.0)

        result = trends.compute_trends(conn, AS_OF)

        oldapp_cpu = [t for t in result if t["subject"] == "oldapp.exe" and t["metric"] == "cpu"]
        assert len(oldapp_cpu) == 1
        assert oldapp_cpu[0]["direction"] == "down"

    def test_small_changes_not_reported(self, temp_db_path):
        conn = make_db(temp_db_path)
        # Only a 10% relative change -- below MIN_RELATIVE_CHANGE (25%)
        insert_samples(conn, "steady.exe", range(8, 29), cpu=20.0)
        insert_samples(conn, "steady.exe", range(1, 8), cpu=22.0)

        result = trends.compute_trends(conn, AS_OF)

        assert [t for t in result if t["subject"] == "steady.exe"] == []

    def test_trivially_small_processes_ignored_even_with_big_pct_swing(self, temp_db_path):
        # A process going from 0.1% to 0.3% CPU is a 200% relative change but
        # both values are below MIN_ABS_CPU -- not worth reporting.
        conn = make_db(temp_db_path)
        insert_samples(conn, "tiny.exe", range(8, 29), cpu=0.1)
        insert_samples(conn, "tiny.exe", range(1, 8), cpu=0.3)

        result = trends.compute_trends(conn, AS_OF)

        assert [t for t in result if t["subject"] == "tiny.exe"] == []

    def test_process_missing_from_either_window_skipped(self, temp_db_path):
        # A process that only exists in the recent window (e.g. newly
        # installed) has no prior baseline to compare against.
        conn = make_db(temp_db_path)
        insert_samples(conn, "newapp.exe", range(1, 8), cpu=50.0)

        result = trends.compute_trends(conn, AS_OF)

        assert [t for t in result if t["subject"] == "newapp.exe"] == []

    def test_insufficient_samples_per_window_skipped(self, temp_db_path):
        # Below MIN_SAMPLES_PER_WINDOW (20) in each window.
        conn = make_db(temp_db_path)
        insert_samples(conn, "sparse.exe", [10, 15, 20], cpu=5.0, per_day=1)
        insert_samples(conn, "sparse.exe", [2, 4], cpu=50.0, per_day=1)

        result = trends.compute_trends(conn, AS_OF)

        assert [t for t in result if t["subject"] == "sparse.exe"] == []

    def test_results_sorted_by_magnitude_descending(self, temp_db_path):
        conn = make_db(temp_db_path)
        insert_samples(conn, "small_change.exe", range(8, 29), cpu=20.0)
        insert_samples(conn, "small_change.exe", range(1, 8), cpu=26.0)  # +30%
        insert_samples(conn, "big_change.exe", range(8, 29), cpu=10.0)
        insert_samples(conn, "big_change.exe", range(1, 8), cpu=50.0)  # +400%

        result = trends.compute_trends(conn, AS_OF)

        subjects_in_order = [t["subject"] for t in result]
        assert subjects_in_order.index("big_change.exe") < subjects_in_order.index("small_change.exe")


class TestTableExists:
    def test_missing_table(self, temp_db_path):
        conn = sqlite3.connect(temp_db_path)
        assert trends.table_exists(conn, "process_samples") is False

    def test_existing_table(self, temp_db_path):
        conn = make_db(temp_db_path)
        assert trends.table_exists(conn, "process_samples") is True
