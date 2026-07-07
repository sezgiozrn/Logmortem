#!/usr/bin/env python3
"""
harness.py — eval harness for logmortem RCA drafts.

Replays each fixture in fixtures/ through the real generation pipeline and
scores the resulting draft against the seeded ground truth. Turns the
README's honest "confidently wrong sometimes" into a measured number.

Three scores per incident:

  1. cause_hit       — did the draft name the seeded root cause?
                       (keyword overlap against seeded_cause, threshold-based)
  2. false_blame     — did it wrongly implicate an innocent deploy, OR blame a
                       deploy at all on a no-deploy incident? (the expensive
                       kind of wrong — an engineer chasing the wrong commit)
  3. citation_ok     — does every log line / commit the draft quotes actually
                       exist in the collected input? (grounding check — the
                       verify-story-ids.mjs idea ported from career-ops:
                       no claim without a traceable source)

Usage:
  .venv/bin/python3 -m eval.harness              # run all fixtures
  .venv/bin/python3 -m eval.harness --fixture pool-exhaustion.json
  .venv/bin/python3 -m eval.harness --no-llm     # citation/scoring plumbing only

Requires ANTHROPIC_API_KEY unless --no-llm. Costs a few cents per run.
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from src.sources import (
    load_fixture, logs_from_fixture, deploys_from_fixture, times_from_fixture
)
from src.collector import build_context

FIXTURES_DIR = Path("fixtures")
RESULTS_DIR = Path("eval/results")


# ── tokenization + scoring helpers ──────────────────────────────

_STOP = {
    "the", "a", "an", "to", "of", "and", "or", "in", "on", "at", "by",
    "was", "were", "is", "are", "not", "when", "this", "that", "with",
    "for", "from", "into", "it", "its", "as", "no",
}


def _tokens(text: str) -> set[str]:
    """Lowercase alphanumeric tokens, stopwords + short noise removed."""
    words = re.findall(r"[a-z0-9_]+", text.lower())
    return {w for w in words if len(w) > 2 and w not in _STOP}


def _draft_text(draft, *fields) -> str:
    """
    Pull specific fields from a structured (dict) draft and join them. If the
    draft is a plain string (e.g. --no-llm control), return it as-is. This is
    what lets us score CAUSAL fields (root_cause) rather than the whole
    serialized blob — the v1 bug was scoring json.dumps(draft), where every
    JSON key ("severity", "summary") looked like a quoted evidence claim and
    every timeline mention of a commit looked like a false blame.
    """
    if not isinstance(draft, dict):
        return str(draft)
    parts = []
    for f in fields:
        v = draft.get(f, "")
        if isinstance(v, list):
            v = " ".join(json.dumps(x) if isinstance(x, (dict, list)) else str(x) for x in v)
        parts.append(str(v))
    return " ".join(parts)


def score_cause_hit(draft, seeded_cause: str, threshold: float = 0.35) -> tuple[bool, float]:
    """
    Fraction of the seeded-cause's meaningful tokens present in the draft's
    causal fields (root_cause / summary / trigger / title) — not the whole
    document, so cause tokens that only show up in an unrelated action item
    don't inflate the score.
    """
    seed = _tokens(seeded_cause)
    if not seed:
        return False, 0.0
    text = _draft_text(draft, "root_cause", "summary", "trigger", "title")
    hit = seed & _tokens(text)
    ratio = len(hit) / len(seed)
    return ratio >= threshold, round(ratio, 3)


def score_false_blame(draft, innocent_commits: list[str]) -> tuple[bool, list[str]]:
    """
    False blame = an innocent commit named in the draft's ROOT_CAUSE — the
    model's committed answer. Mentions elsewhere are legitimate: listing a
    deploy in the timeline, naming the rollback target, or explicitly ruling a
    deploy out in a low-confidence hypothesis are all correct behavior, not
    blame. For a no-deploy incident (cert expiry), innocent_commits contains
    every deploy, so ANY deploy SHA appearing in root_cause trips it — exactly
    the "don't reflexively blame the nearest commit" trap.
    """
    rc = _draft_text(draft, "root_cause").lower()
    offenders = []
    for c in innocent_commits:
        cl = c.lower()
        idx = rc.find(cl)
        while idx != -1:
            # Exoneration guard: "rolling back to <sha> restored..." names the
            # innocent commit as the FIX, not the cause. Only count a mention
            # as blame if the surrounding window lacks exculpatory phrasing.
            # (v1 counted rollback targets as blame — the eval's own audit
            # trail proved the model was exonerating, not accusing.)
            window = rc[max(0, idx - 90):idx + len(cl) + 90]
            exculpatory = ("roll back" in window or "rolled back" in window
                           or "rollback" in window or "revert" in window
                           or "restored" in window or "prior commit" in window
                           or "previous commit" in window or "known-good" in window
                           or "ruled out" in window or "not the cause" in window)
            if not exculpatory:
                offenders.append(c)
                break
            idx = rc.find(cl, idx + 1)
    return (len(offenders) > 0), offenders


def verify_citations(draft, context: dict) -> tuple[bool, list[str]]:
    """
    Grounding check, structured-output edition. Every commit-SHA-shaped token
    the draft references (anywhere) must be a real deploy from the collected
    input — a model that invents commit a1b2dead is hallucinating evidence.
    This is the verify-story-ids.mjs philosophy ported to RCA: no claim citing
    a source that doesn't exist.

    Scoped to SHAs deliberately: it's the clean, false-positive-free grounding
    signal for structured JSON. (Prose log-line verification was the v1
    approach — it drowned in JSON keys. Log-content grounding is future work.)

    Returns (all_grounded, invented_shas).
    """
    known = {d.get("commit", "").lower()[:8] for d in context.get("deploys", [])}
    known.discard("")

    full = _draft_text(
        draft, "title", "summary", "root_cause", "trigger", "deploy_correlation",
        "impact", "timeline", "contributing_factors", "action_items", "hypotheses",
    ).lower()

    # commit-SHA-shaped: 7-40 hex chars with at least one hex letter, so pure
    # digit runs (timestamps, "512", "8000") and years don't match
    candidates = {
        s for s in re.findall(r"\b[0-9a-f]{7,40}\b", full)
        if any(c in "abcdef" for c in s)
    }
    invented = sorted(s for s in candidates if s[:8] not in known)
    return (len(invented) == 0), invented


# ── runner ──────────────────────────────────────────────────────

def run_one(fixture_path: Path, use_llm: bool) -> dict:
    data = load_fixture(str(fixture_path))
    start, end = times_from_fixture(data)
    context = build_context(
        log_group=data["log_group"], start_time=start, end_time=end,
        alert=data["alert"], logs=logs_from_fixture(data),
        deploys=deploys_from_fixture(data),
    )

    seeded = data.get("seeded_cause", "")
    innocent = data.get("innocent_deploys", [])

    if use_llm:
        from src.generator import generate_rca
        draft = generate_rca(context)  # dict — scored field-aware, NOT stringified
    else:
        # plumbing check: a minimal structured draft so scorers run token-free
        sample_log = context["all_logs"][1]["message"] if len(context["all_logs"]) > 1 else ""
        draft = {"root_cause": seeded, "summary": seeded, "trigger": sample_log}

    cause_hit, cause_ratio = score_cause_hit(draft, seeded)
    false_blame, offenders = score_false_blame(draft, innocent)
    cite_ok, bad_cites = verify_citations(draft, context)

    # persist the raw draft alongside its scores — audit trail, and lets you
    # inspect *why* a score landed without burning API credits to re-run.
    # Timestamped filename so repeated runs ACCUMULATE (n grows over multiple
    # days) instead of each run clobbering the last one.
    if use_llm:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        record = {
            "fixture": fixture_path.stem, "run_at": stamp, "seeded_cause": seeded,
            "innocent_deploys": innocent, "draft": draft,
            "scores": {
                "cause_hit": cause_hit, "cause_ratio": cause_ratio,
                "false_blame": false_blame, "offenders": offenders,
                "citations_ok": cite_ok, "bad_citations": bad_cites,
            },
        }
        (RESULTS_DIR / f"{fixture_path.stem}-{stamp}.json").write_text(json.dumps(record, indent=2))

    return {
        "fixture": fixture_path.stem,
        "cause_hit": cause_hit, "cause_ratio": cause_ratio,
        "false_blame": false_blame, "offenders": offenders,
        "citations_ok": cite_ok, "bad_citations": bad_cites,
    }


def print_cumulative_summary() -> None:
    """
    Aggregate EVERY persisted run in eval/results/ — not just this session's —
    so the README number reflects accumulated evidence across however many
    times you've run the harness, not a single lucky (or unlucky) pass.
    """
    records = [json.loads(p.read_text()) for p in sorted(RESULTS_DIR.glob("*.json"))]
    if not records:
        print("\nNo persisted runs yet in eval/results/.")
        return

    # RE-SCORE every persisted draft with the CURRENT scorer + the CURRENT
    # fixture's seeded_cause/innocent list, rather than trusting scores frozen
    # at run time. Rationale: the audit trail exists precisely so grader bugs
    # can be fixed retroactively without re-spending API tokens — v1 scorers
    # penalized rollback mentions and grader-directive tokens; the drafts
    # themselves were fine. Frozen per-run scores remain in the JSON files as
    # historical record of what the grader believed at the time.
    for r in records:
        fx_path = FIXTURES_DIR / f"{r['fixture']}.json"
        if fx_path.exists():
            fx = json.loads(fx_path.read_text())
            seeded = fx.get("seeded_cause", r.get("seeded_cause", ""))
            innocent = fx.get("innocent_deploys", r.get("innocent_deploys", []))
        else:
            seeded = r.get("seeded_cause", "")
            innocent = r.get("innocent_deploys", [])
        ch, cr = score_cause_hit(r["draft"], seeded)
        fb, off = score_false_blame(r["draft"], innocent)
        r["scores"] = {"cause_hit": ch, "cause_ratio": cr,
                       "false_blame": fb, "offenders": off,
                       "citations_ok": r["scores"].get("citations_ok", True),
                       "bad_citations": r["scores"].get("bad_citations", [])}

    n = len(records)
    by_fixture: dict[str, list[dict]] = {}
    for r in records:
        by_fixture.setdefault(r["fixture"], []).append(r)

    hits = sum(r["scores"]["cause_hit"] for r in records)
    clean = sum(not r["scores"]["false_blame"] for r in records)
    grounded = sum(r["scores"]["citations_ok"] for r in records)

    print(f"\n{'='*62}\nCUMULATIVE (all persisted runs, n={n})\n{'='*62}")
    for fx, runs in sorted(by_fixture.items()):
        fh = sum(r["scores"]["cause_hit"] for r in runs)
        fc = sum(not r["scores"]["false_blame"] for r in runs)
        fg = sum(r["scores"]["citations_ok"] for r in runs)
        m = len(runs)
        print(f"  {fx:<22} cause {fh}/{m}   clean {fc}/{m}   grounded {fg}/{m}")
    print("─" * 62)
    print(f"  cause identified:   {hits}/{n}  ({100*hits/n:.0f}%)")
    print(f"  no false blame:     {clean}/{n}  ({100*clean/n:.0f}%)")
    print(f"  fully grounded:     {grounded}/{n}  ({100*grounded/n:.0f}%)")


def main() -> int:
    ap = argparse.ArgumentParser(description="logmortem RCA eval harness")
    ap.add_argument("--fixture", help="run a single fixture by filename")
    ap.add_argument("--no-llm", action="store_true", help="skip Claude, exercise scoring plumbing only")
    ap.add_argument("--summary", action="store_true",
                     help="skip running; just print cumulative stats from eval/results/")
    args = ap.parse_args()

    if args.summary:
        print_cumulative_summary()
        return 0

    if args.fixture:
        paths = [FIXTURES_DIR / args.fixture]
    else:
        paths = sorted(FIXTURES_DIR.glob("*.json"))
    if not paths:
        print("No fixtures found.")
        return 1

    results = [run_one(p, use_llm=not args.no_llm) for p in paths]

    print(f"\n{'fixture':<22} {'cause':<7} {'ratio':<7} {'false-blame':<12} {'citations':<10}")
    print("─" * 62)
    for r in results:
        print(f"{r['fixture']:<22} "
              f"{'✓' if r['cause_hit'] else '✗':<7} "
              f"{r['cause_ratio']:<7} "
              f"{'CLEAN' if not r['false_blame'] else 'BLAMED':<12} "
              f"{'ok' if r['citations_ok'] else 'FLAGGED':<10}")

    n = len(results)
    hits = sum(r["cause_hit"] for r in results)
    clean = sum(not r["false_blame"] for r in results)
    grounded = sum(r["citations_ok"] for r in results)
    print("─" * 62)
    print(f"cause identified:   {hits}/{n}")
    print(f"no false blame:     {clean}/{n}")
    print(f"fully grounded:     {grounded}/{n}")

    for r in results:
        if r["offenders"]:
            print(f"  ⚠ {r['fixture']}: falsely implicated {r['offenders']}")
        if r["bad_citations"]:
            print(f"  ⚠ {r['fixture']}: invented commit refs {r['bad_citations'][:3]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
