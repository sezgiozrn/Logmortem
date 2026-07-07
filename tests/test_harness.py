"""
tests/test_harness.py — unit tests for the eval harness scorers.

No API calls, no fixtures directory needed — these test the grading logic
in isolation with synthetic drafts. Written specifically to lock in the two
real bugs the eval's own audit trail caught (see README "Eval results"):
  1. rollback-target mentions being counted as false blame
  2. JSON structural keys (title/summary/severity) being scored as if they
     were part of the causal narrative, or as quoted evidence to verify
"""

from eval.harness import score_cause_hit, score_false_blame, verify_citations


# ── score_cause_hit ────────────────────────────────────────────

def test_cause_hit_true_on_strong_overlap():
    draft = {"root_cause": "DB connection pool exhausted after scale-up doubled instances"}
    seeded = "connection pool exhausted, not scaled when deploy doubled instances"
    hit, ratio = score_cause_hit(draft, seeded)
    assert hit is True
    assert ratio > 0.35


def test_cause_hit_false_on_no_overlap():
    draft = {"root_cause": "unrelated DNS misconfiguration on the edge proxy"}
    seeded = "TLS certificate expired, auto-renewal job disabled since january"
    hit, ratio = score_cause_hit(draft, seeded)
    assert hit is False


def test_cause_hit_only_scores_causal_fields():
    # cause tokens buried ONLY in an action item shouldn't inflate the score —
    # this is what "score the whole json.dumps(draft) blob" got wrong in v1
    draft = {
        "root_cause": "completely different thing entirely",
        "action_items": [{"action": "fix the connection pool exhaustion issue"}],
    }
    seeded = "connection pool exhaustion"
    hit, ratio = score_cause_hit(draft, seeded)
    assert hit is False


def test_cause_hit_empty_seed_returns_false():
    hit, ratio = score_cause_hit({"root_cause": "anything"}, "")
    assert hit is False
    assert ratio == 0.0


# ── score_false_blame ──────────────────────────────────────────

def test_false_blame_true_when_innocent_commit_is_the_cause():
    draft = {"root_cause": "commit dd33ee44 introduced the memory regression"}
    blamed, offenders = score_false_blame(draft, ["dd33ee44"])
    assert blamed is True
    assert offenders == ["dd33ee44"]


def test_false_blame_false_when_commit_only_named_as_rollback_target():
    # the exact real-world case that was a v1 false positive: the model
    # correctly names the FIX, not the cause
    draft = {"root_cause": "5xx errors resolved after rolling back to 77cc66dd, "
                          "which restored the correct redis endpoint"}
    blamed, offenders = score_false_blame(draft, ["77cc66dd"])
    assert blamed is False
    assert offenders == []


def test_false_blame_false_when_explicitly_ruled_out():
    draft = {"root_cause": "primary cause was the full-res pipeline change; "
                          "the alpine bump (dd33ee44) was ruled out via testing"}
    blamed, offenders = score_false_blame(draft, ["dd33ee44"])
    assert blamed is False


def test_false_blame_no_innocent_commits_mentioned():
    draft = {"root_cause": "cert expired, auto-renewal disabled since january"}
    blamed, offenders = score_false_blame(draft, ["ab12ab12", "cd34cd34"])
    assert blamed is False
    assert offenders == []


def test_false_blame_only_checks_root_cause_field():
    # naming a deploy in the TIMELINE is not blame — only root_cause counts
    draft = {
        "root_cause": "the actual cause was unrelated",
        "timeline": [{"event": "deploy 77cc66dd shipped a logging change"}],
    }
    blamed, offenders = score_false_blame(draft, ["77cc66dd"])
    assert blamed is False


# ── verify_citations ───────────────────────────────────────────

def make_context(deploys):
    return {"deploys": [{"commit": c} for c in deploys]}


def test_citations_ok_when_all_shas_are_real():
    draft = {"root_cause": "caused by commit a1b2c3d4", "deploy_correlation": "a1b2c3d4 shipped 30min prior"}
    ok, invented = verify_citations(draft, make_context(["a1b2c3d4"]))
    assert ok is True
    assert invented == []


def test_citations_flags_invented_sha():
    draft = {"root_cause": "caused by commit deadbeef00, never actually deployed"}
    ok, invented = verify_citations(draft, make_context(["a1b2c3d4"]))
    assert ok is False
    assert "deadbeef00" in invented


def test_citations_ignore_json_structural_keys():
    # the v1 bug: scoring json.dumps(draft) meant "severity"/"summary"/"title"
    # (dict keys) looked like quoted evidence claims. Field-aware scoring on
    # the actual dict shouldn't see JSON syntax as content at all.
    draft = {"title": "Some Incident Title", "severity": "P1", "summary": "a summary"}
    ok, invented = verify_citations(draft, make_context([]))
    assert ok is True
    assert invented == []


def test_citations_ignore_pure_digit_runs():
    # timestamps, dimensions ("8000x6000"), memory sizes ("512") are hex-shaped
    # but shouldn't be treated as invented commit SHAs — no hex letter present
    draft = {"root_cause": "task exceeded 512 limit during 8000 pixel processing"}
    ok, invented = verify_citations(draft, make_context([]))
    assert ok is True
    assert invented == []


def test_citations_match_on_short_sha_prefix():
    # drafts often cite the short 8-char form; should match against the full sha
    draft = {"root_cause": "commit a1b2c3d4 caused this"}
    ok, invented = verify_citations(draft, make_context(["a1b2c3d4e5f6789012345678901234567890abcd"]))
    assert ok is True
