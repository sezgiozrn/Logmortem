"""
tests/test_formatter.py — unit tests for markdown formatting
"""

from src.formatter import format_markdown


MOCK_RCA = {
    "title": "ECS Service Crash — Connection Pool Exhaustion",
    "severity": "P1",
    "summary": "The payment service crashed at 02:00 UTC due to connection pool exhaustion. A deploy 30 minutes prior doubled the number of service instances without adjusting the DB connection pool limit.",
    "timeline": [
        {"time": "2026-04-08T01:30:00", "event": "Deploy of commit abc12345 to production"},
        {"time": "2026-04-08T02:00:00", "event": "Alert fired: ECS TaskCount dropped below threshold"},
        {"time": "2026-04-08T02:05:00", "event": "ERROR: too many connections to database"},
        {"time": "2026-04-08T02:45:00", "event": "Incident resolved after connection pool config updated"},
    ],
    "root_cause": "DB connection pool limit (max_connections=20) was not increased when the deploy doubled service instances from 4 to 8, exhausting available connections.",
    "contributing_factors": [
        "No automated check for connection pool capacity before deploy",
        "Staging environment uses a smaller instance with a different pool limit",
    ],
    "impact": "Payment service unavailable for 45 minutes. Estimated 1,200 failed transactions.",
    "trigger": "ECS task count dropped below minimum threshold as tasks crashed and failed to restart",
    "deploy_correlation": "Commit abc12345 (fix: update connection pool size) deployed 30 minutes before incident. Ironically, the deploy attempted to fix pool size but used the wrong config key.",
    "action_items": [
        {"priority": "high", "action": "Add pre-deploy check for DB connection pool headroom", "owner": "platform-team"},
        {"priority": "high", "action": "Align staging DB config with production", "owner": "infra"},
        {"priority": "medium", "action": "Add CloudWatch alarm for connection count approaching limit", "owner": "observability"},
    ],
    "hypotheses": [],
}

MOCK_CONTEXT = {
    "log_group": "/aws/ecs/payment-service",
    "start_time": "2026-04-08T02:00:00",
    "end_time": "2026-04-08T02:45:00",
    "duration_minutes": 45,
    "alert": "ECS TaskCount dropped below threshold",
}


def test_format_markdown_contains_title():
    md = format_markdown(MOCK_RCA, MOCK_CONTEXT)
    assert "ECS Service Crash" in md


def test_format_markdown_contains_severity():
    md = format_markdown(MOCK_RCA, MOCK_CONTEXT)
    assert "P1" in md


def test_format_markdown_contains_root_cause():
    md = format_markdown(MOCK_RCA, MOCK_CONTEXT)
    assert "connection pool" in md.lower()


def test_format_markdown_contains_timeline():
    md = format_markdown(MOCK_RCA, MOCK_CONTEXT)
    assert "Timeline" in md
    assert "01:30:00" in md


def test_format_markdown_contains_action_items():
    md = format_markdown(MOCK_RCA, MOCK_CONTEXT)
    assert "Action Items" in md
    assert "platform-team" in md


def test_format_markdown_contains_deploy_correlation():
    md = format_markdown(MOCK_RCA, MOCK_CONTEXT)
    assert "Deploy Correlation" in md
    assert "abc12345" in md


def test_format_markdown_contains_footer():
    md = format_markdown(MOCK_RCA, MOCK_CONTEXT)
    assert "logmortem" in md


def test_format_markdown_no_hypotheses_section_when_empty():
    md = format_markdown(MOCK_RCA, MOCK_CONTEXT)
    assert "Alternative Hypotheses" not in md


def test_format_markdown_hypotheses_shown_when_present():
    rca_with_hypotheses = {**MOCK_RCA, "hypotheses": ["Memory leak in v2.3.1", "Network partition"]}
    md = format_markdown(rca_with_hypotheses, MOCK_CONTEXT)
    assert "Alternative Hypotheses" in md
    assert "Memory leak" in md
