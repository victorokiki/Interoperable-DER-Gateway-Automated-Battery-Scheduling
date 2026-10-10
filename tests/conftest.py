"""
tests/conftest.py
Suite-wide isolation: no test may read or write the repository's own
schedule_store/ (the gateway's live schedule database). Every test gets
a throw-away schedule database instead, before any of its setup runs.
"""

import pytest

import capstone.scheduler as sched_mod


@pytest.fixture(autouse=True)
def _isolated_schedule_db(tmp_path, monkeypatch):
    monkeypatch.setattr(sched_mod, "DB_PATH", tmp_path / "schedule.db")
