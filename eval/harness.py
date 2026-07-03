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
from pathlib import Path

from src.sources import (
    load_fixture, logs_from_fixture, deploys_from_fixture, times_from_fixture
)
from src.collector import build_context

FIXTURES_DIR = Path("fixtures")


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


def score_cause_hit(draft: str, seeded_cause: str, threshold: float = 0.35) -> tuple[bool, float]:
    """
    Fraction of the seeded-cause's meaningful tokens that appear in the draft.
    Above threshold = the draft named the real cause. Crude but honest, and
    tunable — the point is a consistent yardstick, not NLP perfection.
    """
    seed = _tokens(seeded_cause)
    if not seed:
        return False, 0.0
    hit = seed & _tokens(draft)
    ratio = len(hit) / len(seed)
    return ratio >= threshold, round(ratio, 3)


def score_false_blame(draft: str, innocent_commits: list[str], deploy_caused: bool) -> tuple[bool, list[str]]:
    """
    Returns (false_blame_occurred, offending_refs).
    - Any innocent commit SHA quoted in the draft's root-cause framing = bad.
    - On a no-deploy incident (deploy_caused=False), mentioning ANY deploy SHA
      as causal is the trap we care about most.
    """
    low = draft.lower()
    offenders = [c for c in innocent_commits if c.lower() in low]
    return (len(offenders) > 0), offenders


def verify_citations(draft: str, context: dict) -> tuple[bool, list[str]]:
    """
    Grounding check. Pulls quoted spans out of the draft and confirms each
    traces to something actually in the collected input — a log message or a
    commit SHA. Unverifiable quotes are hallucination candidates.

    This is verify-story-ids.mjs's philosophy applied to RCA: a claim that
    cites a source that doesn't exist is worse than no citation. We only judge
    QUOTED material (backticks or double quotes) — prose paraphrase is fair
    game, fabricated evidence is not.

    Returns (all_verified, unverifiable_quotes).
    """
    log_corpus = " ".join(e["message"].lower() for e in context.get("all_logs", []))
    shas = {d.get("commit", "").lower() for d in context.get("deploys", [])}
    shas.discard("")

    # quoted spans: `backtick` or "double-quoted", length-filtered to skip
    # trivial one-word quotes that aren't really evidence claims
    quotes = re.findall(r"`([^`]{6,})`|\"([^\"]{6,})\"", draft)
    flat = [q[0] or q[1] for q in quotes]

    unverifiable = []
    for q in flat:
        ql = q.lower().strip()
        # a commit sha reference verifies against the deploy set
        if re.fullmatch(r"[0-9a-f]{6,40}", ql):
            if ql[:8] not in {s[:8] for s in shas}:
                unverifiable.append(q)
            continue
        # otherwise it should be a substring of some real log line. Compare on
        # a token-overlap basis so minor reformatting (timestamps stripped,
        # whitespace) doesn't cause false alarms, but invented lines do.
        qt = _tokens(ql)
        if not qt:
            continue
        overlap = len(qt & set(re.findall(r"[a-z0-9_]+", log_corpus))) / len(qt)
        if overlap < 0.7:
            unverifiable.append(q)

    return (len(unverifiable) == 0), unverifiable


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
    deploy_caused = "not a code change" not in seeded.lower() and "no deploy" not in seeded.lower()

    if use_llm:
        from src.generator import generate_rca
        draft = generate_rca(context)
        if isinstance(draft, dict):
            draft = json.dumps(draft)
    else:
        # plumbing check: feed the seeded cause back as a fake "perfect" draft
        # so scoring/citation logic can be exercised without spending tokens
        sample_log = context["all_logs"][1]["message"] if len(context["all_logs"]) > 1 else ""
        draft = f"Root cause: {seeded}\nEvidence: `{sample_log}`"

    cause_hit, cause_ratio = score_cause_hit(draft, seeded)
    false_blame, offenders = score_false_blame(draft, innocent, deploy_caused)
    cite_ok, bad_cites = verify_citations(draft, context)

    return {
        "fixture": fixture_path.stem,
        "cause_hit": cause_hit, "cause_ratio": cause_ratio,
        "false_blame": false_blame, "offenders": offenders,
        "citations_ok": cite_ok, "bad_citations": bad_cites,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="logmortem RCA eval harness")
    ap.add_argument("--fixture", help="run a single fixture by filename")
    ap.add_argument("--no-llm", action="store_true", help="skip Claude, exercise scoring plumbing only")
    args = ap.parse_args()

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
            print(f"  ⚠ {r['fixture']}: unverifiable quotes {r['bad_citations'][:3]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
