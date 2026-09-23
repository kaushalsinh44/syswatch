"""Tests for anomaly.py -- the core differentiator logic (z-score detection,
episode collapsing, the noise-control floors that took several iterations of
real-data tuning to get right -- see the git history for why Z_THRESHOLD is
3.0 and not the spec's original ~2)."""

import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

import anomaly
import logger as logger_module

TARGET_DATE = "2026-08-20"


def baseline_day(days_before: int) -> str:
    """A date guaranteed to fall inside detect_process_anomalies' lookback
    window for TARGET_DATE (i.e. strictly before it, within lookback_days)."""
    return (date.fromisoformat(TARGET_DATE) - timedelta(days=days_before)).isoformat()


def make_db(path):
    """A temp DB with both logger.py's samples/process_samples schema and
    anomaly.py's own anomalies table -- anomaly.py only owns the latter but
    reads the former, exactly like production."""
    conn = sqlite3.connect(path)
    conn.executescript(logger_module.SCHEMA)
    conn.executescript(anomaly.ANOMALIES_SCHEMA)
    conn.commit()
    return conn


def ts(day: str, hour: int, minute: int = 0) -> str:
    return f"{day}T{hour:02d}:{minute:02d}:00+00:00"


class TestZscore:
    def test_normal_case(self):
        assert anomaly.zscore(value=15, mean=10, std=2) == 2.5

    def test_below_mean_is_negative(self):
        assert anomaly.zscore(value=5, mean=10, std=2) == -2.5

    def test_zero_std_with_real_deviation_is_infinitely_anomalous(self):
        # A perfectly constant baseline (std=0) with a value that actually
        # differs is maximally anomalous -- must not be silently treated as
        # "normal" just because the deviation can't be expressed as a ratio.
        assert anomaly.zscore(value=95, mean=5, std=0) == float("inf")

    def test_zero_std_with_no_deviation_is_zero(self):
        assert anomaly.zscore(value=5, mean=5, std=0) == 0.0


class TestCollapseEpisodes:
    def _anomaly(self, ts_str, z, subject="chrome.exe", category="process_cpu"):
        return {
            "timestamp": ts_str, "category": category, "subject": subject,
            "value": 90.0, "baseline_mean": 5.0, "baseline_std": 2.0, "z_score": z,
            "description": f"{subject} spike",
        }

    def test_empty_input(self):
        assert anomaly.collapse_episodes([]) == []

    def test_single_anomaly_passes_through(self):
        a = self._anomaly("2026-09-01T10:00:00+00:00", 5.0)
        result = anomaly.collapse_episodes([a])
        assert len(result) == 1
        assert result[0]["z_score"] == 5.0

    def test_consecutive_samples_merge_into_one_episode(self):
        # Same subject+category, all within EPISODE_GAP_MINUTES of each other --
        # should collapse to ONE row (this is what fixed the 1,468-anomalies-in-
        # one-day noise problem).
        anomalies = [
            self._anomaly("2026-09-01T10:00:00+00:00", 4.0),
            self._anomaly("2026-09-01T10:02:00+00:00", 9.0),  # the peak
            self._anomaly("2026-09-01T10:04:00+00:00", 5.0),
        ]
        result = anomaly.collapse_episodes(anomalies)
        assert len(result) == 1
        assert result[0]["z_score"] == 9.0  # keeps the peak reading
        assert "sustained" in result[0]["description"]

    def test_far_apart_samples_stay_separate_episodes(self):
        anomalies = [
            self._anomaly("2026-09-01T10:00:00+00:00", 4.0),
            self._anomaly("2026-09-01T11:00:00+00:00", 5.0),  # 1hr later, way past the gap
        ]
        result = anomaly.collapse_episodes(anomalies)
        assert len(result) == 2

    def test_different_subjects_never_merge(self):
        anomalies = [
            self._anomaly("2026-09-01T10:00:00+00:00", 4.0, subject="chrome.exe"),
            self._anomaly("2026-09-01T10:01:00+00:00", 5.0, subject="discord.exe"),
        ]
        result = anomaly.collapse_episodes(anomalies)
        assert len(result) == 2

    def test_different_categories_never_merge(self):
        anomalies = [
            self._anomaly("2026-09-01T10:00:00+00:00", 4.0, category="process_cpu"),
            self._anomaly("2026-09-01T10:01:00+00:00", 5.0, category="process_mem"),
        ]
        result = anomaly.collapse_episodes(anomalies)
        assert len(result) == 2


class TestDetectProcessAnomalies:
    def test_flags_a_genuine_spike(self, temp_db_path):
        conn = make_db(temp_db_path)
        # 14 days of quiet baseline at hour 10, varying slightly so std isn't
        # exactly zero (matches real noisy data; the std==0 edge case is
        # covered separately below).
        for i, day_offset in enumerate(range(1, 15)):
            cpu = 5.0 + (i % 3) * 0.5  # 5.0, 5.5, 6.0, 5.0, 5.5, 6.0, ...
            conn.execute(
                "INSERT INTO process_samples (sample_id, timestamp, process_name, pid, cpu_pct, mem_pct) "
                "VALUES (1, ?, 'chrome.exe', 100, ?, 2.0)",
                (ts(baseline_day(day_offset), 10), cpu),
            )
        # Today: a genuine spike far above baseline
        conn.execute(
            "INSERT INTO process_samples (sample_id, timestamp, process_name, pid, cpu_pct, mem_pct) "
            "VALUES (1, ?, 'chrome.exe', 100, 95.0, 2.0)",
            (ts(TARGET_DATE, 10),),
        )
        conn.commit()

        result = anomaly.detect_process_anomalies(conn, TARGET_DATE, lookback_days=14)

        assert any(a["subject"] == "chrome.exe" and a["category"] == "process_cpu" for a in result)

    def test_flags_a_spike_against_a_perfectly_flat_baseline(self, temp_db_path):
        # Regression test for the zscore std==0 edge case: a baseline with
        # ZERO variance (every past sample identical) must still flag a real
        # deviation, not silently pass it through as "normal".
        conn = make_db(temp_db_path)
        for day_offset in range(1, 15):
            conn.execute(
                "INSERT INTO process_samples (sample_id, timestamp, process_name, pid, cpu_pct, mem_pct) "
                "VALUES (1, ?, 'chrome.exe', 100, 5.0, 2.0)",
                (ts(baseline_day(day_offset), 10),),
            )
        conn.execute(
            "INSERT INTO process_samples (sample_id, timestamp, process_name, pid, cpu_pct, mem_pct) "
            "VALUES (1, ?, 'chrome.exe', 100, 95.0, 2.0)",
            (ts(TARGET_DATE, 10),),
        )
        conn.commit()

        result = anomaly.detect_process_anomalies(conn, TARGET_DATE, lookback_days=14)

        assert any(a["subject"] == "chrome.exe" and a["category"] == "process_cpu" for a in result)

    def test_does_not_flag_normal_variation(self, temp_db_path):
        conn = make_db(temp_db_path)
        # Baseline with natural spread (5-15%), today's reading is within that range
        cpu_values = [5, 7, 9, 11, 13, 15, 8, 10, 12, 6, 14, 9, 11, 7]
        for day_offset, cpu in zip(range(1, 15), cpu_values):
            conn.execute(
                "INSERT INTO process_samples (sample_id, timestamp, process_name, pid, cpu_pct, mem_pct) "
                "VALUES (1, ?, 'chrome.exe', 100, ?, 2.0)",
                (ts(baseline_day(day_offset), 10), cpu),
            )
        conn.execute(
            "INSERT INTO process_samples (sample_id, timestamp, process_name, pid, cpu_pct, mem_pct) "
            "VALUES (1, ?, 'chrome.exe', 100, 10.0, 2.0)",
            (ts(TARGET_DATE, 10),),
        )
        conn.commit()

        result = anomaly.detect_process_anomalies(conn, TARGET_DATE, lookback_days=14)

        assert result == []

    def test_insufficient_baseline_samples_not_flagged(self, temp_db_path):
        conn = make_db(temp_db_path)
        # Only 3 historical samples -- below MIN_BASELINE_SAMPLES (10), even
        # though today's reading would otherwise look like a huge spike.
        for day_offset in range(1, 4):
            conn.execute(
                "INSERT INTO process_samples (sample_id, timestamp, process_name, pid, cpu_pct, mem_pct) "
                "VALUES (1, ?, 'rareapp.exe', 100, 1.0, 1.0)",
                (ts(baseline_day(day_offset), 10),),
            )
        conn.execute(
            "INSERT INTO process_samples (sample_id, timestamp, process_name, pid, cpu_pct, mem_pct) "
            "VALUES (1, ?, 'rareapp.exe', 100, 90.0, 1.0)",
            (ts(TARGET_DATE, 10),),
        )
        conn.commit()

        result = anomaly.detect_process_anomalies(conn, TARGET_DATE, lookback_days=14)

        assert result == []

    def test_near_zero_stddev_needs_min_abs_delta_to_flag(self, temp_db_path):
        # A process idling at ~0% CPU has a near-zero stddev, which would give
        # ANY nonzero reading a huge z-score. MIN_ABS_DELTA["process_cpu"] (15
        # points) should suppress a trivial absolute bump even if the z-score
        # alone would clear the threshold.
        conn = make_db(temp_db_path)
        for i, day_offset in enumerate(range(1, 15)):
            cpu = 0.1 + (i % 2) * 0.05
            conn.execute(
                "INSERT INTO process_samples (sample_id, timestamp, process_name, pid, cpu_pct, mem_pct) "
                "VALUES (1, ?, 'idlehelper.exe', 100, ?, 0.5)",
                (ts(baseline_day(day_offset), 10), cpu),
            )
        # Today: technically a huge z-score jump, but only ~2 points absolute
        conn.execute(
            "INSERT INTO process_samples (sample_id, timestamp, process_name, pid, cpu_pct, mem_pct) "
            "VALUES (1, ?, 'idlehelper.exe', 100, 2.0, 0.5)",
            (ts(TARGET_DATE, 10),),
        )
        conn.commit()

        result = anomaly.detect_process_anomalies(conn, TARGET_DATE, lookback_days=14)

        assert result == []

    def test_excluded_process_never_flagged(self, temp_db_path):
        conn = make_db(temp_db_path)
        for day_offset in range(1, 15):
            conn.execute(
                "INSERT INTO process_samples (sample_id, timestamp, process_name, pid, cpu_pct, mem_pct) "
                "VALUES (1, ?, 'System Idle Process', 0, 5.0, 0.0)",
                (ts(baseline_day(day_offset), 10),),
            )
        conn.execute(
            "INSERT INTO process_samples (sample_id, timestamp, process_name, pid, cpu_pct, mem_pct) "
            "VALUES (1, ?, 'System Idle Process', 0, 99.0, 0.0)",
            (ts(TARGET_DATE, 10),),
        )
        conn.commit()

        result = anomaly.detect_process_anomalies(conn, TARGET_DATE, lookback_days=14)

        assert result == []


class TestGetBatterySessions:
    def test_groups_contiguous_discharge_stretch(self, temp_db_path):
        conn = make_db(temp_db_path)
        rows = [
            (ts("2026-08-20", 10, 0), 80.0, 0),
            (ts("2026-08-20", 10, 5), 75.0, 0),
            (ts("2026-08-20", 10, 10), 70.0, 0),
            (ts("2026-08-20", 10, 15), 70.0, 1),  # plugged in -- session ends
        ]
        for t, pct, charging in rows:
            conn.execute(
                "INSERT INTO samples (timestamp, interval_sec, cpu_pct, mem_pct, disk_read_bytes, "
                "disk_write_bytes, net_sent_bytes, net_recv_bytes, battery_pct, charging) "
                "VALUES (?, 300, 10, 50, 0, 0, 0, 0, ?, ?)",
                (t, pct, charging),
            )
        conn.commit()

        sessions = anomaly.get_battery_sessions(conn, "2026-08-20")

        assert len(sessions) == 1
        assert sessions[0]["start_pct"] == 80.0
        assert sessions[0]["end_pct"] == 70.0
        assert sessions[0]["duration_min"] == pytest.approx(10, abs=0.01)

    def test_single_sample_session_ignored(self, temp_db_path):
        # A lone on-battery sample with nothing before/after it isn't a real
        # "session" -- needs at least 2 points to compute a drain rate.
        conn = make_db(temp_db_path)
        conn.execute(
            "INSERT INTO samples (timestamp, interval_sec, cpu_pct, mem_pct, disk_read_bytes, "
            "disk_write_bytes, net_sent_bytes, net_recv_bytes, battery_pct, charging) "
            "VALUES (?, 45, 10, 50, 0, 0, 0, 0, 80.0, 0)",
            (ts("2026-08-20", 10, 0),),
        )
        conn.commit()

        assert anomaly.get_battery_sessions(conn, "2026-08-20") == []


class TestTableExists:
    def test_existing_table(self, temp_db_path):
        conn = make_db(temp_db_path)
        assert anomaly.table_exists(conn, "samples") is True

    def test_missing_table(self, temp_db_path):
        conn = sqlite3.connect(temp_db_path)
        assert anomaly.table_exists(conn, "samples") is False


class TestMaybeNotify:
    def test_notifies_for_today_when_severe(self, monkeypatch):
        calls = []
        monkeypatch.setattr(anomaly, "send_notification", lambda title, msg: calls.append((title, msg)))
        today = datetime.now(timezone.utc).date().isoformat()

        anomaly.maybe_notify(today, [{"z_score": 9.0, "description": "big spike"}])

        assert len(calls) == 1

    def test_no_notification_for_past_date_even_if_severe(self, monkeypatch):
        # Critical: --all backfilling a month of history must never spam
        # notifications for old dates, only for today's slice of the run.
        calls = []
        monkeypatch.setattr(anomaly, "send_notification", lambda title, msg: calls.append((title, msg)))

        anomaly.maybe_notify("2020-01-01", [{"z_score": 50.0, "description": "huge spike"}])

        assert calls == []

    def test_no_notification_below_threshold(self, monkeypatch):
        calls = []
        monkeypatch.setattr(anomaly, "send_notification", lambda title, msg: calls.append((title, msg)))
        today = datetime.now(timezone.utc).date().isoformat()

        anomaly.maybe_notify(today, [{"z_score": 3.5, "description": "minor"}])

        assert calls == []
