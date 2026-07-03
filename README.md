# Logmortem

Writing RCAs manually after a 2-hour incident — digging through logs, reconstructing timelines, correlating deploys — is slow, tedious, and happens when you're already exhausted. logmortem does the first draft so engineers can focus on validating and improving it instead of building it from scratch at 3am.

Feed it a CloudWatch log group, a time window, and the alert that fired. It pulls the logs, correlates recent GitHub Actions deploys, and outputs a structured postmortem in under a minute.

---

## Example output

```markdown
# ECS Service Crash — Connection Pool Exhaustion

| Field | Value |
|---|---|
| Severity | P1 |
| Start | 2026-04-08T02:00:00 |
| Duration | 45 minutes |
| Alert | ECS TaskCount dropped below threshold |

## Root Cause
DB connection pool limit (max_connections=20) was not increased when the deploy
doubled service instances from 4 to 8, exhausting available connections.

## Deploy Correlation
Commit abc12345 deployed 30 minutes before incident attempted to fix pool size
but used the wrong config key.

## Action Items
| Priority | Action | Owner |
|---|---|---|
| HIGH | Add pre-deploy check for DB connection pool headroom | platform-team |
| HIGH | Align staging DB config with production | infra |
```

---

## How it works

1. Fetches CloudWatch log events for the incident window (plus 10 min pre-window for context)
2. Pulls GitHub Actions workflow runs from the 24h before the incident
3. Filters health check noise automatically
4. Sends everything to Claude with a structured RCA prompt
5. Outputs a markdown postmortem with timeline, root cause, deploy correlation, contributing factors, and action items

---

## Limitations & what I'd do differently

- **Correlation is temporal, not causal.** A deploy 30 minutes before an
  incident gets flagged; Claude decides if it's relevant. It's a draft for
  a human to validate, not a verdict — and it will occasionally be
  confidently wrong.
- **Logs only.** No CloudWatch Metrics or traces. Root causes that live in
  a latency graph rather than a log line get missed.
- **GitHub Actions only** for deploy history. Other CD systems are invisible.
- **Large incident windows can exceed the context budget.** Noisy log groups
  over long windows get truncated, not summarized.
- If I rebuilt it: pluggable log sources, chunked ingestion with
  pre-summarization instead of truncation, and an eval harness that scores
  RCA drafts against known-cause incidents instead of trusting vibes.

---

## Automated trigger

logmortem can run automatically when a deploy fails. Add `.github/workflows/auto-rca.yml`
to your repo and set these secrets:

| Secret | Required | Description |
|--------|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes | Claude API key |
| `AWS_ACCESS_KEY_ID` | Yes | AWS credentials |
| `AWS_SECRET_ACCESS_KEY` | Yes | AWS credentials |
| `AWS_DEFAULT_REGION` | No | Defaults to us-east-1 |
| `LOG_GROUP` | No | CloudWatch log group to query |

When any workflow fails, logmortem automatically generates an RCA and posts it
to the GitHub Actions job summary — visible directly in the failed run.

---

## Usage

```bash
# Basic — logs only
python src/main.py \
  --log-group /aws/ecs/payment-service \
  --start-time 2026-04-08T02:00:00 \
  --end-time 2026-04-08T03:00:00 \
  --alert "ECS TaskCount dropped below threshold"

# With deploy correlation
python src/main.py \
  --log-group /aws/ecs/payment-service \
  --start-time 2026-04-08T02:00:00 \
  --end-time 2026-04-08T03:00:00 \
  --alert "ECS TaskCount dropped below threshold" \
  --repo your-org/your-app

# Dry run — see what data was collected without calling Claude
python src/main.py \
  --log-group /aws/ecs/payment-service \
  --start-time 2026-04-08T02:00:00 \
  --end-time 2026-04-08T03:00:00 \
  --alert "ECS TaskCount dropped below threshold" \
  --repo your-org/your-app \
  --dry-run

# Custom output file
python src/main.py \
  --log-group /aws/ecs/payment-service \
  --start-time 2026-04-08T02:00:00 \
  --end-time 2026-04-08T03:00:00 \
  --alert "ECS TaskCount dropped below threshold" \
  --output incidents/2026-04-08-payment-outage.md
```

---

## Setup

```bash
git clone https://github.com/sezgiozrn/Logmortem.git
cd Logmortem
pip install -r requirements.txt
```

```bash
export ANTHROPIC_API_KEY=your_key_here
export GITHUB_TOKEN=your_github_token      # optional, for deploy correlation
```

AWS credentials via standard boto3 chain (`~/.aws/credentials`, env vars, or instance profile).

---

## Running tests

```bash
pip install pytest pytest-cov
pytest tests/ -v --cov=src --cov-report=term-missing
```

---

## Stack

- **Python** — CLI and data ingestion
- **boto3** — CloudWatch Logs
- **GitHub REST API** — Actions workflow history
- **Claude API** — RCA synthesis

---

## Related

The runbooks and postmortem templates that informed this tool live in [Platform-Runbooks](https://github.com/sezgiozrn/Platform-Runbooks) — severity levels, escalation paths, and triage steps for AWS/ECS incidents.
