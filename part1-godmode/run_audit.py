#!/usr/bin/env python3
"""What a run actually did in AWS (#42): every change its session made, from CloudTrail. Read-only.

    run_audit.py --workflow apply|guardrails --run-id N --ended ISO8601 [--wait-minutes 12]

guardrails-check runs it after every apply and guardrails run, as godmode/aws-build-runner-guardrails-check. It
waits until CloudTrail has caught up (events take about 5-15 minutes), then looks up every event made by that run's
role session (github-apply-N or github-rollback-N for apply, github-guardrails-N for guardrails) in Sydney and
us-east-1, where IAM, STS, Budgets and Cost Explorer record theirs. It prints Markdown: each kind of change, with
counts and the resources it touched, and every call AWS **refused**.

Exit 0: nothing refused. Exit 1: AWS refused a call, so the code tried something outside its role; the workflow
opens an issue for Kieron. Exit 2: the audit couldn't run. The account id and Claude's user name are replaced
(<account>, <claude-user>), because the summary is public. LookupEvents is free for management events.
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict

REGIONS = ("ap-southeast-2", "us-east-1")
REFUSED = re.compile(r"AccessDenied|Unauthorized|NotAuthorized|Forbidden", re.I)
# The role-session-name each audited workflow signs in with (apply.yml, guardrails.yml).
SESSIONS = {"apply": ("github-apply-{}", "github-rollback-{}"), "guardrails": ("github-guardrails-{}",)}


def sessions_for(workflow: str, run_id: str) -> list[str]:
    if workflow not in SESSIONS or not re.fullmatch(r"\d+", run_id or ""):
        raise ValueError(f"can't audit workflow {workflow!r} run {run_id!r}")
    return [s.format(run_id) for s in SESSIONS[workflow]]


def aws(*args, region):
    r = subprocess.run(["aws", *args, "--region", region, "--output", "json"], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"aws {' '.join(args[:2])} ({region}): {r.stderr.strip()[:300]}")
    return json.loads(r.stdout or "{}")


def lookup(session: str, start: dt.datetime, region: str) -> list[dict]:
    events, token = [], None
    while True:
        args = ["cloudtrail", "lookup-events", "--lookup-attributes", f"AttributeKey=Username,AttributeValue={session}",
                "--start-time", start.isoformat(), "--max-results", "50"]
        if token:
            args += ["--next-token", token]
        page = aws(*args, region=region)
        events += [json.loads(e["CloudTrailEvent"]) for e in page.get("Events", [])]
        token = page.get("NextToken")
        if not token:
            return events
        time.sleep(0.6)   # LookupEvents allows 2 calls a second


def resources(e: dict) -> list[str]:
    names = [r.get("ARN") or r.get("arn") or "" for r in e.get("resources") or []]
    p = e.get("requestParameters") or {}
    for key in ("functionName", "roleName", "policyArn", "budgetName", "alarmName", "name", "topicArn",
                "logGroupName", "subscriptionArn"):
        if isinstance(p.get(key), str):
            names.append(p[key])
    return sorted({n for n in names if n})


def summarise(events: list[dict]) -> tuple[list[str], list[str]]:
    """(changes, refused): Markdown lines. Pure, so it's tested offline."""
    changes = defaultdict(lambda: {"n": 0, "res": set(), "already": 0})
    refused = []
    for e in events:
        if e.get("readOnly") is True or e.get("eventType") == "AwsServiceEvent":
            continue
        service = e.get("eventSource", "?").split(".")[0]
        name = re.sub(r"\d{8}(v\d+)?$", "", e.get("eventName", "?"))   # lambda versions: UpdateFunctionCode20150331v2
        err = e.get("errorCode") or ""
        if REFUSED.search(err):
            refused.append(f"- `{service}:{name}` on {', '.join(f'`{r}`' for r in resources(e)) or 'unknown'}: "
                           f"**{err}** ({(e.get('errorMessage') or '')[:160]})")
            continue
        c = changes[f"{service}:{name}"]
        c["n"] += 1
        c["res"].update(resources(e))
        if err:
            c["already"] += 1
    lines = []
    for key in sorted(changes):
        c = changes[key]
        note = f", {c['already']} already so" if c["already"] else ""
        res = ", ".join(f"`{r}`" for r in sorted(c["res"])[:6]) + (" …" if len(c["res"]) > 6 else "")
        lines.append(f"- `{key}` ×{c['n']}{note}{': ' + res if res else ''}")
    return lines, refused


def redact(text: str, account: str = "", user: str = "") -> str:
    for secret, label in ((account, "<account>"), (user, "<claude-user>")):
        if secret:
            text = text.replace(secret, label)
    return text


def report(sessions: list[str], changes: list[str], refused: list[str]) -> str:
    out = [f"### What the run actually did in AWS (CloudTrail, {', '.join(f'`{s}`' for s in sessions)})", ""]
    if refused:
        out += [f"**{len(refused)} call(s) AWS refused.** The code tried something outside its role:", ""] + refused + [""]
    out += (["Changes it made:", ""] + changes) if changes else ["No changes recorded."]
    return "\n".join(out)


def main(argv) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--workflow", required=True, choices=sorted(SESSIONS))
    p.add_argument("--run-id", required=True)
    p.add_argument("--ended", required=True, help="when the run ended (ISO 8601)")
    p.add_argument("--wait-minutes", type=float, default=12)
    a = p.parse_args(argv[1:])
    account, user = os.environ.get("AWS_ACCOUNT_ID", ""), os.environ.get("AWS_CLAUDE_USER", "")
    try:
        sessions = sessions_for(a.workflow, a.run_id)
        ended = dt.datetime.fromisoformat(a.ended.replace("Z", "+00:00"))
        wait = (ended + dt.timedelta(minutes=a.wait_minutes) - dt.datetime.now(dt.timezone.utc)).total_seconds()
        if wait > 0:
            time.sleep(wait)
        account = account or aws("sts", "get-caller-identity", region=REGIONS[0])["Account"]
        start = ended - dt.timedelta(hours=2)   # apply and guardrails runs are limited to well under 2 hours
        events = [e for s in sessions for r in REGIONS for e in lookup(s, start, r)]
        changes, refused = summarise(events)
    except Exception as e:  # noqa: BLE001 - an audit that can't run must say so, not pass
        print(redact(f"The run audit couldn't run: {type(e).__name__}: {e}", account, user))
        return 2
    print(redact(report(sessions, changes, refused), account, user))
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
