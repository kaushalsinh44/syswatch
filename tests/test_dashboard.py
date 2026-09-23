"""Tests for dashboard/app.py -- Flask routes via the test client.

Covers two things found the hard way earlier this project: the fresh/empty
database crash (every route must degrade gracefully, not 500), and the
localhost-CSRF gap on /api/kill-process (must reject cross-origin requests).
"""

import os
import sqlite3
from types import SimpleNamespace

import pytest

import app as dashboard_app


@pytest.fixture
def client(temp_db_path, monkeypatch):
    monkeypatch.setattr(dashboard_app, "DB_PATH", temp_db_path)
    dashboard_app.app.config["TESTING"] = True
    with dashboard_app.app.test_client() as c:
        yield c


def seed_sample(db_path, timestamp, cpu=50.0, mem=60.0, battery=80.0, charging=1):
    conn = sqlite3.connect(db_path)
    conn.executescript(dashboard_app.ENSURE_SCHEMA)
    conn.execute(
        """
        INSERT INTO samples (timestamp, interval_sec, cpu_pct, mem_pct, disk_read_bytes,
                              disk_write_bytes, net_sent_bytes, net_recv_bytes, battery_pct, charging)
        VALUES (?, 45, ?, ?, 0, 0, 0, 0, ?, ?)
        """,
        (timestamp, cpu, mem, battery, charging),
    )
    conn.commit()
    conn.close()


class TestFreshEmptyDatabase:
    """Regression tests for the crash-on-fresh-clone bug."""

    @pytest.mark.parametrize(
        "path",
        ["/", "/api/stats", "/api/top-processes", "/api/timeline", "/trends",
         "/api/trends", "/battery", "/api/battery-health", "/api/protected-processes"],
    )
    def test_route_does_not_crash_on_empty_db(self, client, path):
        response = client.get(path)
        assert response.status_code == 200

    def test_case_page_404s_cleanly_not_500(self, client):
        response = client.get("/case/999")
        assert response.status_code == 404


class TestPickAnomalyDate:
    def test_prefers_latest_date_when_it_has_anomalies(self, temp_db_path):
        conn = sqlite3.connect(temp_db_path)
        conn.executescript(dashboard_app.ENSURE_SCHEMA)
        conn.execute(
            "INSERT INTO anomalies (run_at, target_date, timestamp, category, value, "
            "baseline_mean, baseline_std, z_score, description) "
            "VALUES ('x', '2026-09-23', '2026-09-23T10:00:00', 'process_cpu', 1, 1, 1, 5, 'x')"
        )
        conn.commit()

        assert dashboard_app.pick_anomaly_date(conn, "2026-09-23") == "2026-09-23"

    def test_falls_back_to_most_recent_date_with_anomalies(self, temp_db_path):
        conn = sqlite3.connect(temp_db_path)
        conn.executescript(dashboard_app.ENSURE_SCHEMA)
        conn.execute(
            "INSERT INTO anomalies (run_at, target_date, timestamp, category, value, "
            "baseline_mean, baseline_std, z_score, description) "
            "VALUES ('x', '2026-09-20', '2026-09-20T10:00:00', 'process_cpu', 1, 1, 1, 5, 'x')"
        )
        conn.commit()

        # "today" (2026-09-23) has no anomalies -- should fall back to the 20th
        assert dashboard_app.pick_anomaly_date(conn, "2026-09-23") == "2026-09-20"

    def test_no_anomalies_anywhere_returns_latest_date(self, temp_db_path):
        conn = sqlite3.connect(temp_db_path)
        conn.executescript(dashboard_app.ENSURE_SCHEMA)
        assert dashboard_app.pick_anomaly_date(conn, "2026-09-23") == "2026-09-23"


class TestKillProcessCsrfGuard:
    def test_cross_origin_request_blocked(self, client):
        response = client.post(
            "/api/kill-process",
            json={"pid": 4, "process_name": "notepad.exe"},
            headers={"Origin": "http://evil.com"},
        )
        assert response.status_code == 403
        assert response.get_json()["ok"] is False

    def test_missing_origin_and_referer_blocked(self, client):
        response = client.post("/api/kill-process", json={"pid": 4, "process_name": "notepad.exe"})
        assert response.status_code == 403

    def test_same_origin_request_passes_csrf_check(self, client, monkeypatch):
        # Passing the CSRF guard should reach the real business logic -- prove
        # it by making a request that CSRF-passes but fails later for an
        # unrelated, clearly-distinguishable reason (protected process name).
        response = client.post(
            "/api/kill-process",
            json={"pid": 4, "process_name": "system"},
            headers={"Origin": "http://localhost"},
            base_url="http://localhost",
        )
        assert response.status_code == 403
        assert "protected" in response.get_json()["error"].lower()


class TestKillProcessSafetyChecks:
    def _post(self, client, pid, name):
        return client.post(
            "/api/kill-process",
            json={"pid": pid, "process_name": name},
            headers={"Origin": "http://localhost"},
            base_url="http://localhost",
        )

    def test_protected_process_name_rejected(self, client):
        response = self._post(client, 12345, "explorer.exe")
        assert response.status_code == 403
        assert response.get_json()["ok"] is False

    def test_own_pid_rejected(self, client, monkeypatch):
        monkeypatch.setattr(dashboard_app.os, "getpid", lambda: 42)
        response = self._post(client, 42, "python.exe")
        assert response.status_code == 403
        assert "own process" in response.get_json()["error"]

    def test_missing_pid_rejected(self, client):
        response = client.post(
            "/api/kill-process", json={"process_name": "chrome.exe"},
            headers={"Origin": "http://localhost"}, base_url="http://localhost",
        )
        assert response.status_code == 400

    def test_name_mismatch_rejected_pid_reuse_guard(self, client, monkeypatch):
        fake_proc = SimpleNamespace(name=lambda: "totally_different.exe")
        monkeypatch.setattr(dashboard_app.psutil, "Process", lambda pid: fake_proc)

        response = self._post(client, 9999, "chrome.exe")

        assert response.status_code == 409
        assert "already exited" in response.get_json()["error"]

    def test_successful_kill_calls_terminate(self, client, monkeypatch):
        calls = []
        fake_proc = SimpleNamespace(
            name=lambda: "chrome.exe",
            terminate=lambda: calls.append("terminate"),
            wait=lambda timeout: None,
        )
        monkeypatch.setattr(dashboard_app.psutil, "Process", lambda pid: fake_proc)

        response = self._post(client, 9999, "chrome.exe")

        assert response.status_code == 200
        assert response.get_json()["ok"] is True
        assert calls == ["terminate"]

    def test_already_gone_process_handled_cleanly(self, client, monkeypatch):
        def raise_no_such_process(pid):
            raise dashboard_app.psutil.NoSuchProcess(pid)

        monkeypatch.setattr(dashboard_app.psutil, "Process", raise_no_such_process)

        response = self._post(client, 9999, "chrome.exe")

        assert response.status_code == 404
        assert "already gone" in response.get_json()["error"]


class TestApiTimeline:
    def test_live_mode_uses_last_n_hours_from_latest_sample(self, client, temp_db_path):
        seed_sample(temp_db_path, "2026-09-23T10:00:00+00:00")
        seed_sample(temp_db_path, "2026-09-20T10:00:00+00:00")  # older than 24h window

        response = client.get("/api/timeline?range=24h")
        data = response.get_json()

        assert len(data) == 1
        assert data[0]["timestamp"] == "2026-09-23T10:00:00+00:00"

    def test_historical_date_mode_ignores_live_window(self, client, temp_db_path):
        seed_sample(temp_db_path, "2026-09-23T10:00:00+00:00")
        seed_sample(temp_db_path, "2026-09-07T10:00:00+00:00")

        response = client.get("/api/timeline?range=24h&date=2026-09-07")
        data = response.get_json()

        assert len(data) == 1
        assert data[0]["timestamp"] == "2026-09-07T10:00:00+00:00"
