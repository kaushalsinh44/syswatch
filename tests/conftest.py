"""Shared pytest fixtures.

Every module under test (logger.py, anomaly.py, trends.py, report.py,
dashboard/app.py) is a standalone script with its own module-level DB_PATH
pointing at the real data/telemetry.db -- tests must never touch that file.
Fixtures here monkeypatch DB_PATH to a throwaway sqlite file per test.
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "dashboard"))


@pytest.fixture
def temp_db_path(tmp_path):
    return tmp_path / "test_telemetry.db"
