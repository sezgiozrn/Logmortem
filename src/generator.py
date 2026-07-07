"""
generator.py — sends incident context to Claude and returns structured RCA
"""

import os
import json
import anthropic


SYSTEM_PROMPT = """You are a senior DevOps/SRE engineer writing a postmortem RCA.
You are given incident data including CloudWatch log events, GitHub Actions deploy history,
and the alert that triggered the incident.

Your job is to produce a structured, honest, blameless RCA in JSON format.
Be specific. Use actual log messages and timestamps as evidence.
If the root cause is unclear, say so and list the top hypotheses with supporting evidence.

Return ONLY valid JSON. No markdown fences. No preamble. No explanation outside the JSON.

JSON schema:
{
  "title": "string — short incident title",
  "severity": "P1 | P2 | P3",
  "summary": "string — 2-3 sentence executive summary",
  "timeline": [
    {"time": "ISO timestamp", "event": "string"}
  ],
  "root_cause": "string — most probable root cause with evidence",
  "contributing_factors": ["string"],
  "impact": "string — what was affected and for how long",
  "trigger": "string — what directly triggered the alert",
  "deploy_correlation": "string | null — if a recent deploy correlates with the incident, describe it",
  "action_items": [
    {"priority": "high | medium | low", "action": "string", "owner": "string"}
  ],
  "hypotheses": ["string"] 
}
"""


def build_prompt(context: dict) -> str:
    """Build the user prompt from collected incident context."""

    log_section = "\n".join([
        f"[{e['timestamp']}] {e['message']}"
        for e in context.get("error_logs", [])
    ]) or "No error-level logs found."

    all_logs_section = "\n".join([
        f"[{e['timestamp']}] {e['message']}"
        for e in context.get("all_logs", [])
    ]) or "No logs collected."

    deploy_section = ""
    if context.get("deploys"):
        deploy_lines = []
        for d in context["deploys"]:
            deploy_lines.append(
                f"[{d['time']}] {d['workflow']} — {d['status']} "
                f"(commit {d['commit']}: {d['commit_message']}) by {d['author']}"
            )
        deploy_section = "\n".join(deploy_lines)
    else:
        deploy_section = "No deploy history available."

    return f"""INCIDENT CONTEXT
================
Log Group: {context['log_group']}
Start Time: {context['start_time']}
End Time: {context['end_time']}
Duration: {context['duration_minutes']} minutes
Alert: {context['alert']}
Total Log Events: {context['total_log_events']}
Error-level Events: {context['error_log_count']}

ERROR LOGS (most relevant)
==========================
{log_section}

ALL LOGS (chronological)
========================
{all_logs_section}

GITHUB ACTIONS DEPLOY HISTORY (24h before incident)
====================================================
{deploy_section}

Generate the RCA JSON now."""


def generate_rca(context: dict) -> dict:
    """
    Call Claude API with incident context and return parsed RCA dict.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "ANTHROPIC_API_KEY environment variable not set.\n"
            "Export it: export ANTHROPIC_API_KEY=your_key_here"
        )

    client = anthropic.Anthropic(api_key=api_key)

    prompt = build_prompt(context)

    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=4000,  # shared by adaptive thinking + output; keep room for full JSON
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    # Sonnet 5+ runs adaptive thinking by default, so content[0] may be a
    # ThinkingBlock — grab the actual text block(s), not blindly the first.
    raw = "".join(
        b.text for b in response.content if getattr(b, "type", None) == "text"
    ).strip()
    if not raw:
        raise RuntimeError("No text block in response (got only: "
                           + ", ".join(getattr(b, "type", "?") for b in response.content) + ")")

    # Strip markdown fences if model adds them despite instructions
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Models occasionally emit near-valid JSON (a stray ';', a trailing
        # comma, an unescaped quote). One bad char shouldn't nuke an otherwise
        # sound RCA, so repair and retry. The airtight fix is the API's
        # structured-outputs mode; this is the pragmatic fallback.
        from json_repair import repair_json
        try:
            return json.loads(repair_json(raw))
        except (json.JSONDecodeError, ValueError) as e:
            print(f"⚠️  Claude returned unrepairable JSON: {e}")
            print("Raw response:")
            print(raw)
            raise
