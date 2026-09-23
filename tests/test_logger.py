"""Tests for logger.py's pure-ish helper functions (not the sampling loop itself,
which needs a real running process to be meaningful -- see the README for the
manual foreground-run verification instead)."""

from types import SimpleNamespace

import pytest

import logger


class TestComputeDeltas:
    def test_normal_increase(self):
        prev_disk = SimpleNamespace(read_bytes=1000, write_bytes=500)
        cur_disk = SimpleNamespace(read_bytes=1500, write_bytes=800)
        prev_net = SimpleNamespace(bytes_sent=200, bytes_recv=300)
        cur_net = SimpleNamespace(bytes_sent=250, bytes_recv=900)

        deltas = logger.compute_deltas(prev_disk, cur_disk, prev_net, cur_net)

        assert deltas == {
            "disk_read_bytes": 500,
            "disk_write_bytes": 300,
            "net_sent_bytes": 50,
            "net_recv_bytes": 600,
        }

    def test_counter_reset_clamps_to_zero(self):
        # e.g. after a reboot mid-run, the cumulative counters restart from a
        # small value -- a naive subtraction would go negative, which is nonsense
        # for a "bytes since last sample" column.
        prev_disk = SimpleNamespace(read_bytes=10_000, write_bytes=5_000)
        cur_disk = SimpleNamespace(read_bytes=100, write_bytes=50)
        prev_net = SimpleNamespace(bytes_sent=9_000, bytes_recv=1_000)
        cur_net = SimpleNamespace(bytes_sent=10, bytes_recv=5)

        deltas = logger.compute_deltas(prev_disk, cur_disk, prev_net, cur_net)

        assert deltas == {
            "disk_read_bytes": 0,
            "disk_write_bytes": 0,
            "net_sent_bytes": 0,
            "net_recv_bytes": 0,
        }

    def test_zero_delta(self):
        prev_disk = SimpleNamespace(read_bytes=100, write_bytes=100)
        cur_disk = SimpleNamespace(read_bytes=100, write_bytes=100)
        prev_net = SimpleNamespace(bytes_sent=100, bytes_recv=100)
        cur_net = SimpleNamespace(bytes_sent=100, bytes_recv=100)

        deltas = logger.compute_deltas(prev_disk, cur_disk, prev_net, cur_net)

        assert all(v == 0 for v in deltas.values())


class TestGetBatteryStatus:
    def test_no_battery_sensor(self, monkeypatch):
        monkeypatch.setattr(logger.psutil, "sensors_battery", lambda: None)
        assert logger.get_battery_status() == (None, None)

    def test_charging(self, monkeypatch):
        battery = SimpleNamespace(percent=73.0, power_plugged=True)
        monkeypatch.setattr(logger.psutil, "sensors_battery", lambda: battery)
        assert logger.get_battery_status() == (73.0, 1)

    def test_not_charging(self, monkeypatch):
        battery = SimpleNamespace(percent=42.5, power_plugged=False)
        monkeypatch.setattr(logger.psutil, "sensors_battery", lambda: battery)
        assert logger.get_battery_status() == (42.5, 0)

    def test_power_plugged_unknown(self, monkeypatch):
        # psutil docs: power_plugged can itself be None on some platforms --
        # must stay None (unknown), not get coerced to 0 (false/not-charging).
        battery = SimpleNamespace(percent=50.0, power_plugged=None)
        monkeypatch.setattr(logger.psutil, "sensors_battery", lambda: battery)
        percent, charging = logger.get_battery_status()
        assert percent == 50.0
        assert charging is None


class TestDiffProcessEvents:
    def test_new_process_detected(self, monkeypatch):
        monkeypatch.setattr(logger.psutil, "pids", lambda: [1, 2, 3])
        monkeypatch.setattr(
            logger.psutil, "Process", lambda pid: SimpleNamespace(name=lambda: f"proc{pid}.exe")
        )

        events, cur_pids, names = logger.diff_process_events(prev_pids={1, 2}, known_names={1: "a.exe", 2: "b.exe"})

        assert events == [{"event": "started", "process_name": "proc3.exe", "pid": 3}]
        assert cur_pids == {1, 2, 3}
        assert names == {1: "a.exe", 2: "b.exe", 3: "proc3.exe"}

    def test_process_exit_detected(self, monkeypatch):
        monkeypatch.setattr(logger.psutil, "pids", lambda: [1])
        monkeypatch.setattr(logger.psutil, "Process", lambda pid: SimpleNamespace(name=lambda: "unused"))

        events, cur_pids, names = logger.diff_process_events(prev_pids={1, 2}, known_names={1: "a.exe", 2: "b.exe"})

        assert events == [{"event": "stopped", "process_name": "b.exe", "pid": 2}]
        assert cur_pids == {1}
        assert names == {1: "a.exe"}

    def test_no_change_produces_no_events(self, monkeypatch):
        monkeypatch.setattr(logger.psutil, "pids", lambda: [1, 2])
        monkeypatch.setattr(logger.psutil, "Process", lambda pid: SimpleNamespace(name=lambda: "unused"))

        events, cur_pids, names = logger.diff_process_events(prev_pids={1, 2}, known_names={1: "a.exe", 2: "b.exe"})

        assert events == []
        assert cur_pids == {1, 2}

    def test_pid_reused_by_gone_process_handled_gracefully(self, monkeypatch):
        # A PID that exits and a new unrelated process reusing it in the SAME
        # loop iteration should still produce a clean stopped+started pair,
        # not crash or silently merge the two into one.
        monkeypatch.setattr(logger.psutil, "pids", lambda: [1, 3])
        monkeypatch.setattr(
            logger.psutil, "Process", lambda pid: SimpleNamespace(name=lambda: f"new{pid}.exe")
        )

        events, cur_pids, names = logger.diff_process_events(prev_pids={1, 2}, known_names={1: "a.exe", 2: "b.exe"})

        event_types = {(e["event"], e["pid"]) for e in events}
        assert ("started", 3) in event_types
        assert ("stopped", 2) in event_types


class TestInitDb:
    def test_creates_expected_tables(self, temp_db_path):
        logger.init_db(temp_db_path)

        import sqlite3

        conn = sqlite3.connect(temp_db_path)
        tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        conn.close()

        assert {"samples", "process_samples", "battery_health", "process_events"} <= tables

    def test_idempotent(self, temp_db_path):
        logger.init_db(temp_db_path)
        logger.init_db(temp_db_path)  # must not raise on re-run
