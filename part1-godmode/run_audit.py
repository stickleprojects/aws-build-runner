#!/usr/bin/env python3
"""What a run actually did (#42): every change its AWS session made, from CloudTrail. Read-only.

    run_audit.py --session github-guardrails-RUN_ID --ended ISO8601 [--wait-minutes 12]

Waits until CloudTrail has caught up (events can take ~5-15 minutes), then looks up every event made by that
role session in Sydney and us-east-1 (IAM, STS, Budgets and Cost Explorer record theirs there), and prints
Markdown: each kind of change, with counts and the resources it touched, and every call AWS **refused**.
A refused call means the code tried something outside its role: exit 1, so the run fails and is seen.
Exit 2 if it couldn't run. The account id is replaced with <account> (the summary is public).
"""
import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import time
from collections import defaultdict

REGIONS = ("ap-southeast-2", "us-east-1")
REFUSED = re.compile(r"AccessDenied|Unauthorized|NotAuthorized|Forbidden", re.I)


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


def report(session: str, changes: list[str], refused: list[str], account: str) -> str:
    out = [f"### What the run actually did in AWS (CloudTrail, session `{session}`)", ""]
    if refused:
        out += [f"**{len(refused)} call(s) AWS refused.** The code tried something outside its role:", ""] + refused + [""]
    out += (["Changes it made:", ""] + changes) if changes else ["No changes recorded."]
    text = "\n".join(out)
    return text.replace(account, "<account>") if account else text


def main(argv) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--session", required=True)
    p.add_argument("--ended", required=True, help="when the run ended (ISO 8601)")
    p.add_argument("--wait-minutes", type=float, default=12)
    a = p.parse_args(argv[1:])
    account = ""
    try:
        if not re.fullmatch(r"github-(apply|guardrails)-\d+", a.session):
            raise ValueError(f"unexpected session name {a.session!r}")
        ended = dt.datetime.fromisoformat(a.ended.replace("Z", "+00:00"))
        wait = (ended + dt.timedelta(minutes=a.wait_minutes) - dt.datetime.now(dt.timezone.utc)).total_seconds()
        if wait > 0:
            time.sleep(wait)
        account = aws("sts", "get-caller-identity", region=REGIONS[0])["Account"]
        start = ended - dt.timedelta(hours=2)   # runs are limited to well under 2 hours
        events = [e for r in REGIONS for e in lookup(a.session, start, r)]
        changes, refused = summarise(events)
    except Exception as e:  # noqa: BLE001 - an audit that can't run must say so, not pass
        msg = f"The run audit couldn't run: {type(e).__name__}: {e}"
        print(msg.replace(account, "<account>") if account else msg)
        return 2
    print(report(a.session, changes, refused, account))
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
