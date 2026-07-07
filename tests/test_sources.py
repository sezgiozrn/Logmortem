"""
tests/test_sources.py — unit tests for the offline fixture-source layer.

Covers: fixture loading + validation, noise filtering parity with
collector.py's live path, sort order, and malformed-input error paths.
"""

import json
import pytest
from datetime import datetime

from src.sources import (
    load_fixture, logs_from_fixture, deploys_from_fixture, times_from_fixture
)


def write_fixture(tmp_path, data):
    p = tmp_path / "fixture.json"
    p.write_text(json.dumps(data))
    return str(p)


def base_fixture(**overrides):
    d = {
        "log_group": "/aws/ecs/test-service",
        "start_time": "2026-04-08T02:00:00",
        "end_time": "2026-04-08T03:00:00",
        "alert": "ECS TaskCount dropped below threshold",
        "logs": [],
        "deploys": [],
    }
    d.update(overrides)
    return d


# ── load_fixture ──────────────────────────────────────────────

def test_load_fixture_missing_file():
    with pytest.raises(FileNotFoundError):
        load_fixture("/nonexistent/path/fixture.json")


def test_load_fixture_missing_required_key(tmp_path):
    bad = base_fixture()
    del bad["alert"]
    path = write_fixture(tmp_path, bad)
    with pytest.raises(ValueError, match="alert"):
        load_fixture(path)


def test_load_fixture_defaults_deploys_to_empty(tmp_path):
    d = base_fixture()
    del d["deploys"]
    path = write_fixture(tmp_path, d)
    loaded = load_fixture(path)
    assert loaded["deploys"] == []


def test_load_fixture_valid_roundtrip(tmp_path):
    d = base_fixture(log_group="/aws/ecs/payment-service")
    path = write_fixture(tmp_path, d)
    loaded = load_fixture(path)
    assert loaded["log_group"] == "/aws/ecs/payment-service"


# ── logs_from_fixture: noise filtering + sort order ──────────

def test_logs_from_fixture_strips_healthcheck_noise():
    data = base_fixture(logs=[
        {"timestamp": "2026-04-08T02:01:00", "message": "ERROR real failure"},
        {"timestamp": "2026-04-08T02:00:00", "message": "GET /health 200 OK"},
        {"timestamp": "2026-04-08T02:00:30", "message": "ELB-HealthChecker/2.0 ping"},
    ])
    events = logs_from_fixture(data)
    assert len(events) == 1
    assert events[0]["message"] == "ERROR real failure"


def test_logs_from_fixture_sorts_by_timestamp():
    data = base_fixture(logs=[
        {"timestamp": "2026-04-08T02:05:00", "message": "second"},
        {"timestamp": "2026-04-08T02:01:00", "message": "first"},
    ])
    events = logs_from_fixture(data)
    assert [e["message"] for e in events] == ["first", "second"]


def test_logs_from_fixture_strips_whitespace():
    data = base_fixture(logs=[
        {"timestamp": "2026-04-08T02:00:00", "message": "  padded msg  "},
    ])
    events = logs_from_fixture(data)
    assert events[0]["message"] == "padded msg"


def test_logs_from_fixture_handles_missing_message_key():
    # a malformed log entry shouldn't crash the reader — empty string, not KeyError
    data = base_fixture(logs=[{"timestamp": "2026-04-08T02:00:00"}])
    events = logs_from_fixture(data)
    assert events[0]["message"] == ""


# ── deploys_from_fixture ──────────────────────────────────────

def test_deploys_from_fixture_sorts_by_time():
    data = base_fixture(deploys=[
        {"time": "2026-04-08T01:00:00Z", "commit": "second"},
        {"time": "2026-04-07T20:00:00Z", "commit": "first"},
    ])
    deploys = deploys_from_fixture(data)
    assert [d["commit"] for d in deploys] == ["first", "second"]


def test_deploys_from_fixture_empty_ok():
    assert deploys_from_fixture(base_fixture()) == []


# ── times_from_fixture ─────────────────────────────────────────

def test_times_from_fixture_parses_iso():
    data = base_fixture(start_time="2026-04-08T02:00:00", end_time="2026-04-08T03:00:00")
    start, end = times_from_fixture(data)
    assert start == datetime(2026, 4, 8, 2, 0, 0)
    assert end == datetime(2026, 4, 8, 3, 0, 0)
    assert end > start
