"""
sources.py — pluggable incident-data sources.

The live path (CloudWatch + GitHub Actions) and the offline path (JSON
fixtures) both return the SAME shape, so everything downstream — context
assembly, generation, formatting — neither knows nor cares where the data
came from. This is what unblocks the eval harness (replay seeded incidents
with a known root cause) and the vhs proof-of-run recording (no live AWS).

Fixture format (JSON):
{
  "log_group": "/aws/ecs/payment-service",
  "start_time": "2026-04-08T02:00:00",
  "end_time":   "2026-04-08T03:00:00",
  "alert": "ECS TaskCount dropped below threshold",
  "logs":    [{"timestamp": "...ISO...", "message": "..."}],
  "deploys": [{"time": "...", "workflow": "...", "status": "...",
               "commit": "...", "commit_message": "...", "author": "...",
               "url": "..."}],
  "seeded_cause": "optional — used by the eval harness as ground truth"
}
"""

import json
from datetime import datetime
from pathlib import Path


# Health-check noise filter, kept identical to collector.py so offline and
# live paths produce byte-for-byte comparable log streams.
_NOISE = ("ELB-HealthChecker", "health check", "GET /health", "GET /ping")


def load_fixture(path: str) -> dict:
    """Read and validate an incident fixture JSON. Returns the raw dict."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Fixture not found: {path}")
    data = json.loads(p.read_text())

    required = ("log_group", "start_time", "end_time", "alert", "logs")
    missing = [k for k in required if k not in data]
    if missing:
        raise ValueError(f"Fixture {path} missing keys: {', '.join(missing)}")
    data.setdefault("deploys", [])
    return data


def logs_from_fixture(data: dict) -> list[dict]:
    """
    Mirror of collect_logs() output: [{timestamp, message}, ...] sorted by
    time, health-check noise stripped. Deterministic — no network.
    """
    events = []
    for e in data.get("logs", []):
        msg = (e.get("message") or "").strip()
        if any(n in msg for n in _NOISE):
            continue
        events.append({"timestamp": e["timestamp"], "message": msg})
    return sorted(events, key=lambda x: x["timestamp"])


def deploys_from_fixture(data: dict) -> list[dict]:
    """Mirror of collect_deploys() output, sorted by time."""
    return sorted(data.get("deploys", []), key=lambda x: x.get("time", ""))


def times_from_fixture(data: dict) -> tuple[datetime, datetime]:
    """Parse the fixture's incident window into datetime objects."""
    return (
        datetime.fromisoformat(data["start_time"]),
        datetime.fromisoformat(data["end_time"]),
    )
