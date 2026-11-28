#!/usr/bin/env python3
"""Offline tests for run_audit.py (#42), its wiring in guardrails-check.yml and the check role's CloudTrail access.

    python3 -I part1-godmode/tests/test_run_audit.py
"""
import importlib.util
import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
P1 = HERE.parent
ROOT = P1.parent
spec = importlib.util.spec_from_file_location("run_audit", P1 / "run_audit.py")
ra = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ra)
failed = 0


def check(name, ok, detail=""):
    global failed
    print(f"{'ok  ' if ok else 'FAIL'}  {name}{'' if ok else ': ' + str(detail)[:400]}")
    failed += 0 if ok else 1


ACCT, USER = "123456789012", "claude-test-user"


def event(name, source="iam.amazonaws.com", err=None, msg=None, read_only=False, **params):
    e = {"eventName": name, "eventSource": source, "readOnly": read_only, "requestParameters": params}
    if err:
        e["errorCode"], e["errorMessage"] = err, msg or ""
    return e


# Which sessions a run signed in as (apply.yml: github-${action}-${run_id}; guardrails.yml: github-guardrails-${run_id}).
check("an apply run: its apply and rollback sessions", ra.sessions_for("apply", "123") ==
      ["github-apply-123", "github-rollback-123"], ra.sessions_for("apply", "123"))
check("a guardrails run: its one session", ra.sessions_for("guardrails", "9") == ["github-guardrails-9"])
for wf, rid in (("ami", "1"), ("apply", "12a"), ("apply", ""), ("guardrails", "1 --x")):
    try:
        ra.sessions_for(wf, rid)
        check(f"refuses workflow {wf!r} run {rid!r}", False, "accepted")
    except ValueError:
        check(f"refuses workflow {wf!r} run {rid!r}", True)
for f, pattern in (("apply.yml", "role-session-name: github-${{ inputs.action }}-${{ github.run_id }}"),
                   ("guardrails.yml", "role-session-name: github-guardrails-${{ github.run_id }}")):
    check(f"{f} still signs in with the session name the audit looks up", pattern in
          (ROOT / ".github" / "workflows" / f).read_text())

# Summarising.
events = [
    event("PutRolePolicy", roleName="claude-x"),
    event("PutRolePolicy", roleName="claude-y"),
    event("GetRole", read_only=True, roleName="claude-x"),
    {"eventName": "Something", "eventSource": "health.amazonaws.com", "eventType": "AwsServiceEvent"},
    event("UpdateFunctionCode20150331v2", source="lambda.amazonaws.com", functionName="guardrail-kill-switch"),
    event("CreateBudget", source="budgets.amazonaws.com", err="DuplicateRecordException", budgetName="guardrail-monthly"),
    event("CreateUser", err="AccessDenied", msg=f"User: arn:aws:sts::{ACCT}:assumed-role/x is not authorized",
          userName="intruder"),
]
changes, refused = ra.summarise(events)
check("reads and AWS service events are left out", not any("GetRole" in c or "health" in c for c in changes), changes)
check("repeated changes are counted, with their resources",
      "- `iam:PutRolePolicy` ×2: `claude-x`, `claude-y`" in changes, changes)
check("Lambda's API version is dropped from the name", any(c.startswith("- `lambda:UpdateFunctionCode` ×1") for c in changes),
      changes)
check("a change that was already so is noted, not refused",
      "- `budgets:CreateBudget` ×1, 1 already so: `guardrail-monthly`" in changes and len(refused) == 1, (changes, refused))
check("a refused call is listed with its error", len(refused) == 1 and "`iam:CreateUser`" in refused[0]
      and "**AccessDenied**" in refused[0], refused)
check("no events: says so", ra.report(["s"], [], []).endswith("No changes recorded."))
text = ra.redact(ra.report(["github-apply-1"], changes, refused) + f" {USER}", ACCT, USER)
check("the report counts refused calls", "**1 call(s) AWS refused.**" in text, text)
check("the account id and Claude's user name are redacted", ACCT not in text and USER not in text
      and "<account>" in text and "<claude-user>" in text, text)
check("redact does nothing with nothing to hide", ra.redact("abc") == "abc")

# Exit codes, without AWS: a bad run id can't run (2).
check("can't run: exit 2", ra.main(["run_audit.py", "--workflow", "apply", "--run-id", "x", "--ended",
                                     "2026-11-28T00:00:00Z", "--wait-minutes", "0"]) == 2)

# The workflow runs it after apply and guardrails, and acts on what it finds.
wf = yaml.safe_load((ROOT / ".github" / "workflows" / "guardrails-check.yml").read_text())
on = wf.get(True) or wf.get("on")
check("guardrails-check runs after apply and guardrails", set(on["workflow_run"]["workflows"]) == {"apply", "guardrails"})
job = wf["jobs"]["check"]
steps = {s.get("name"): s for s in job["steps"]}
audit = steps.get("What the run did in AWS (CloudTrail)", {})
check("the audit step runs only after a workflow_run, even if the drift check failed",
      audit.get("if") == "always() && github.event_name == 'workflow_run'", audit.get("if"))
check("it passes the audited run's workflow, id and end", all(
    k in audit.get("env", {}) for k in ("WORKFLOW", "RUN_ID", "ENDED")) and "run_audit.py" in audit.get("run", ""))
check("refused calls open an issue for Kieron", "--assignee stickleprojects" in
      steps.get("Issue for calls AWS refused", {}).get("run", ""))
check("refused or couldn't run fails the run", "exit 1" in
      steps.get("Fail if the audit found refused calls or couldn't run", {}).get("run", ""))
check("the job has time for CloudTrail's ~12 min delay", job["timeout-minutes"] >= 25, job["timeout-minutes"])
check("the job's AWS env includes Claude's user name, so it can be redacted", "AWS_CLAUDE_USER" in job["env"])

# The check role may look up CloudTrail events, read-only, in Sydney and us-east-1 only.
pol = json.loads((P1 / "bootstrap" / "guardrails-check-role-policy.json").read_text())
ct = [st for st in pol["Statement"] if "cloudtrail" in json.dumps(st["Action"])]
check("the check role's CloudTrail access is LookupEvents alone",
      len(ct) == 1 and ct[0]["Action"] == "cloudtrail:LookupEvents" and ct[0]["Effect"] == "Allow", ct)
check("only in Sydney and us-east-1", ct and ct[0].get("Condition") ==
      {"StringEquals": {"aws:RequestedRegion": ["ap-southeast-2", "us-east-1"]}}, ct)

print(f"\n{failed} failed")
sys.exit(1 if failed else 0)
