#!/usr/bin/env python3
"""Guardrails drift check (#42 C): compare what's live in this account with the repo. Read-only.

    guardrails_check.py            check; print a Markdown report; exit 1 if anything differs, 2 if it couldn't run
    guardrails_check.py --json     the same findings as JSON

Checks what bootstrap-budget-stop.sh, bootstrap-github-apply.sh, apply.sh and the runner stack set, against
policy.yaml and bootstrap/: both budgets, the budget action, guardrails/budget-stop, the kill switch (its
settings and reserved executions), the alarms, the SNS wiring, Cost Anomaly Detection, the apply and check
roles' policies, and the caps on our Lambdas. Each finding names the command that fixes it.

Runs in the guardrails-check workflow as godmode/aws-build-runner-guardrails-check, or as an admin. The
report goes into a public issue, so the account id is replaced with <account> in everything it prints.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
REGION = "ap-southeast-2"
KILL_SWITCH = "guardrail-kill-switch"
STOP_TOPIC = "guardrail-budget-stop"
FAST_TOPIC = "guardrail-fast-stop"
APPLY_ROLE = "aws-build-runner-apply"
CHECK_ROLE = "aws-build-runner-guardrails-check"
GUARD_ROLE = "aws-build-runner-guardrails"
# What fixes each kind of drift. Each command is complete (see CHECKLIST.md for CloudShell's settings).
FIX_BOOTSTRAP = "run the guardrails workflow (bootstrap-budget-stop.sh)"
FIX_GITHUB = "rerun bootstrap-github-apply.sh in CloudShell"
FIX_APPLY = "run the apply workflow (action apply)"
FIX_STACK = "Claude reruns part2-runner/stack/deploy.sh"


# ---------------------------------------------------------------------------- what the repo says

def expected(policy: dict, template: str, account: str, user: str) -> dict:
    s = policy["account_settings"]
    stop = s["budget_stop"]
    fast = stop["fast_stop"]
    ks = stop["kill_switch"]
    caps = {}
    for name, fn in (("Launcher", "build-runner-launcher"), ("Webhook", "build-runner-webhook")):
        m = re.search(rf"^  {name}:\n(?:(?!^  \S).*\n?)*", template, re.M)
        c = re.search(r"^\s+ReservedConcurrentExecutions:\s*(\d+)", m.group(0), re.M) if m else None
        caps[fn] = int(c.group(1)) if c else None

    def render(name):
        text = (HERE / "bootstrap" / name).read_text()
        return json.loads(text.replace("{{ACCOUNT_ID}}", account).replace("{{CLAUDE_USER}}", user))

    flag = lambda k: json.dumps(bool(ks.get(k, False)))
    return {
        "budget": {"limit": float(s["budget_usd"]), "credits": True, "refunds": False, "tax": True},
        "usage": {"limit": float(s["usage_alert_usd"]), "credits": False} if "usage_alert_usd" in s else None,
        "action": {"at": float(stop["at_percent"]), "user": user, "roles": sorted(stop["roles"]),
                   "policy": f"arn:aws:iam::{account}:policy/guardrails/budget-stop"},
        "stop_policy": render("budget-stop-policy.json"),
        "kill_switch_env": {"REGIONS": "ap-southeast-2,us-east-1", "TERMINATE_INSTANCES": flag("terminate_instances"),
                            "DELETE_RUNNER_AMIS": flag("delete_runner_amis"), "THROTTLE_LAMBDAS": flag("throttle_lambdas"),
                            "DELETE_DETACHED_VOLUMES": flag("delete_detached_volumes"),
                            "RELEASE_UNUSED_ADDRESSES": flag("release_unused_addresses"),
                            "DELETE_INTERFACE_ENDPOINTS": flag("delete_interface_endpoints")},
        "kill_switch_reserved": 2,
        "alarms": {"guardrail-fast-network-out": fast["network_out_gib"] * 1024 ** 3,
                   "guardrail-fast-lambda-invocations": fast["lambda_invocations"],
                   "guardrail-fast-lambda-concurrency": fast["lambda_concurrent"],
                   "guardrail-fast-log-ingest": fast["log_ingest_mib"] * 1024 ** 2},
        "anomaly_usd": float(s["anomaly_alert_usd"]),
        "apply_role": {"trust": render("apply-role-trust.json"), "policy": render("apply-role-policy.json")},
        "check_role": {"trust": render("guardrails-check-role-trust.json"),
                       "policy": render("guardrails-check-role-policy.json")},
        "guard_role": {"trust": render("guardrails-role-trust.json"), "policy": render("guardrails-role-policy.json")},
        "caps": caps,
    }


# ---------------------------------------------------------------------------- comparing (pure, tested offline)

def same(a, b) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def compare(exp: dict, live: dict) -> list[dict]:
    """[{what, live, want, fix}] for everything that differs."""
    out = []

    def differ(what, got, want, fix):
        out.append({"what": what, "live": got, "want": want, "fix": fix})

    b = live.get("budget")
    if not b:
        differ("budget guardrail-monthly", "missing", "present", FIX_APPLY)
    else:
        e = exp["budget"]
        if float(b["BudgetLimit"]["Amount"]) != e["limit"]:
            differ("budget guardrail-monthly limit", b["BudgetLimit"]["Amount"], e["limit"], FIX_APPLY)
        ct = b.get("CostTypes", {})
        for key, field in (("credits", "IncludeCredit"), ("refunds", "IncludeRefund"), ("tax", "IncludeTax")):
            if ct.get(field) is not e[key]:
                differ(f"budget guardrail-monthly {field}", ct.get(field), e[key], FIX_APPLY)

    if exp["usage"]:
        u = live.get("usage")
        if not u:
            differ("budget guardrail-monthly-usage", "missing", "present", FIX_APPLY)
        else:
            if float(u["BudgetLimit"]["Amount"]) != exp["usage"]["limit"]:
                differ("budget guardrail-monthly-usage limit", u["BudgetLimit"]["Amount"], exp["usage"]["limit"], FIX_APPLY)
            if u.get("CostTypes", {}).get("IncludeCredit") is not False:
                differ("budget guardrail-monthly-usage IncludeCredit", u.get("CostTypes", {}).get("IncludeCredit"), False, FIX_APPLY)
        if live.get("usage_action"):
            differ("budget guardrail-monthly-usage", "has a budget action", "alerts only, no stop", FIX_APPLY)

    acts = live.get("actions") or []
    e = exp["action"]
    if len(acts) != 1:
        differ("budget stop actions", len(acts), 1, FIX_BOOTSTRAP)
    for a in acts:
        d = a.get("Definition", {}).get("IamActionDefinition", {})
        got = {"at": float(a.get("ActionThreshold", {}).get("ActionThresholdValue", -1)),
               "approval": a.get("ApprovalModel"), "status": a.get("Status"), "policy": d.get("PolicyArn"),
               "users": d.get("Users", []), "roles": sorted(d.get("Roles", []))}
        want = {"at": e["at"], "approval": "AUTOMATIC", "status": "STANDBY", "policy": e["policy"],
                "users": [e["user"]], "roles": e["roles"]}
        for k in want:
            if got[k] != want[k]:
                fix = "it has fired: lifting it is Kieron's call (Budgets → Actions)" if k == "status" else FIX_BOOTSTRAP
                differ(f"budget stop action {k}", got[k], want[k], fix)

    if not same(live.get("stop_policy"), exp["stop_policy"]):
        differ("policy guardrails/budget-stop", "differs from bootstrap/budget-stop-policy.json", "the same", FIX_BOOTSTRAP)

    env = live.get("kill_switch_env")
    if env is None:
        differ(f"Lambda {KILL_SWITCH}", "missing", "present", FIX_BOOTSTRAP)
    else:
        for k, v in exp["kill_switch_env"].items():
            if env.get(k) != v:
                differ(f"kill switch {k}", env.get(k), v, FIX_BOOTSTRAP)
        if live.get("kill_switch_reserved") != exp["kill_switch_reserved"]:
            differ("kill switch reserved executions", live.get("kill_switch_reserved"), exp["kill_switch_reserved"], FIX_BOOTSTRAP)

    alarms = {a["AlarmName"]: a for a in live.get("alarms", [])}
    for name, threshold in exp["alarms"].items():
        a = alarms.get(name)
        if not a:
            differ(f"alarm {name}", "missing", "present", FIX_BOOTSTRAP)
            continue
        if float(a.get("Threshold", -1)) != float(threshold):
            differ(f"alarm {name} threshold", a.get("Threshold"), threshold, FIX_BOOTSTRAP)
        if not any(x.endswith(f":{FAST_TOPIC}") for x in a.get("AlarmActions", [])):
            differ(f"alarm {name} action", a.get("AlarmActions"), f"SNS {FAST_TOPIC}", FIX_BOOTSTRAP)
        if not a.get("ActionsEnabled", False):
            differ(f"alarm {name}", "actions disabled", "actions enabled", FIX_BOOTSTRAP)

    subs = live.get("subscriptions", {})
    for topic in (STOP_TOPIC, FAST_TOPIC):
        s = subs.get(topic)
        if s is None:
            differ(f"SNS topic {topic}", "missing", "present", FIX_BOOTSTRAP)
            continue
        if not any(x.get("Protocol") == "lambda" and x.get("Endpoint", "").endswith(f":function:{KILL_SWITCH}") for x in s):
            differ(f"SNS topic {topic}", "doesn't reach the kill switch", "subscribed by the kill switch", FIX_BOOTSTRAP)
    if any(x.get("SubscriptionArn") == "PendingConfirmation" for x in subs.get(FAST_TOPIC) or []):
        differ(f"SNS topic {FAST_TOPIC} email", "PendingConfirmation", "confirmed", "Kieron clicks Confirm in AWS's email")
    if not live.get("budget_notifies_kill_switch"):
        differ(f"budget guardrail-monthly {exp['action']['at']:g}% notification", f"doesn't reach SNS {STOP_TOPIC}",
               f"reaches SNS {STOP_TOPIC}", FIX_BOOTSTRAP)

    if live.get("anomaly_usd") is None:
        differ("Cost Anomaly Detection guardrail-daily", "missing", "present", FIX_BOOTSTRAP)
    elif float(live["anomaly_usd"]) != exp["anomaly_usd"]:
        differ("Cost Anomaly Detection threshold", live["anomaly_usd"], exp["anomaly_usd"], FIX_BOOTSTRAP)

    for role, key in ((APPLY_ROLE, "apply_role"), (CHECK_ROLE, "check_role"), (GUARD_ROLE, "guard_role")):
        got = live.get(key)
        if not got:
            differ(f"role godmode/{role}", "missing", "present", FIX_GITHUB)
            continue
        for part in ("trust", "policy"):
            if not same(got.get(part), exp[key][part]):
                differ(f"role godmode/{role} {part}", "differs from bootstrap/", "the same", FIX_GITHUB)

    for fn, cap in exp["caps"].items():
        if live.get("caps", {}).get(fn) != cap:
            differ(f"Lambda {fn} reserved executions", live.get("caps", {}).get(fn), cap, FIX_STACK)
    return out


def redact(text: str, account: str) -> str:
    return text.replace(account, "<account>") if account else text


def report(findings: list[dict], account: str) -> str:
    if not findings:
        return "Everything live matches the repo."
    lines = [f"**{len(findings)} difference(s)** between what's live and the repo (`main`):", "",
             "| What | Live | Repo | Fix |", "|---|---|---|---|"]
    for f in findings:
        cells = [str(f[k]).replace("|", "\\|")[:200] for k in ("what", "live", "want", "fix")]
        lines.append("| " + " | ".join(cells) + " |")
    return redact("\n".join(lines), account)


# ---------------------------------------------------------------------------- reading AWS

def aws(*args, region=REGION, ok_missing=False):
    r = subprocess.run(["aws", *args, "--region", region, "--output", "json"], capture_output=True, text=True)
    if r.returncode:
        if ok_missing and re.search(r"NotFound|NoSuchEntity|does not exist|ResourceNotFound", r.stderr):
            return None
        raise RuntimeError(f"aws {' '.join(args[:2])}: {r.stderr.strip()[:300]}")
    return json.loads(r.stdout or "{}")


def gather(account: str) -> dict:
    live = {}
    b = aws("budgets", "describe-budget", "--account-id", account, "--budget-name", "guardrail-monthly", ok_missing=True)
    live["budget"] = b and b["Budget"]
    u = aws("budgets", "describe-budget", "--account-id", account, "--budget-name", "guardrail-monthly-usage", ok_missing=True)
    live["usage"] = u and u["Budget"]
    if live["usage"]:
        live["usage_action"] = aws("budgets", "describe-budget-actions-for-budget", "--account-id", account,
                                   "--budget-name", "guardrail-monthly-usage").get("Actions")
    if live["budget"]:
        live["actions"] = aws("budgets", "describe-budget-actions-for-budget", "--account-id", account,
                              "--budget-name", "guardrail-monthly").get("Actions", [])
        notes = aws("budgets", "describe-notifications-for-budget", "--account-id", account,
                    "--budget-name", "guardrail-monthly").get("Notifications", [])
        live["budget_notifies_kill_switch"] = False
        for n in notes:
            if n.get("NotificationType") != "ACTUAL":
                continue
            subs = aws("budgets", "describe-subscribers-for-notification", "--account-id", account,
                       "--budget-name", "guardrail-monthly", "--notification", json.dumps(n)).get("Subscribers", [])
            if any(s.get("SubscriptionType") == "SNS" and s.get("Address", "").endswith(f":{STOP_TOPIC}") for s in subs):
                live["budget_notifies_kill_switch"] = True
    pol = aws("iam", "get-policy", "--policy-arn", f"arn:aws:iam::{account}:policy/guardrails/budget-stop", ok_missing=True)
    if pol:
        v = aws("iam", "get-policy-version", "--policy-arn", pol["Policy"]["Arn"], "--version-id", pol["Policy"]["DefaultVersionId"])
        live["stop_policy"] = v["PolicyVersion"]["Document"]
    ks = aws("lambda", "get-function-configuration", "--function-name", KILL_SWITCH, ok_missing=True)
    live["kill_switch_env"] = ks and ks.get("Environment", {}).get("Variables", {})
    if ks:
        live["kill_switch_reserved"] = aws("lambda", "get-function-concurrency", "--function-name", KILL_SWITCH).get(
            "ReservedConcurrentExecutions")
    live["alarms"] = aws("cloudwatch", "describe-alarms", "--alarm-name-prefix", "guardrail-fast-").get("MetricAlarms", [])
    live["subscriptions"] = {}
    for topic in (STOP_TOPIC, FAST_TOPIC):
        arn = f"arn:aws:sns:{REGION}:{account}:{topic}"
        s = aws("sns", "list-subscriptions-by-topic", "--topic-arn", arn, ok_missing=True)
        if s is not None:
            live["subscriptions"][topic] = s.get("Subscriptions", [])
    # Cost Explorer has one endpoint, in us-east-1; nothing runs or is stored there.
    subs = aws("ce", "get-anomaly-subscriptions", region="us-east-1").get("AnomalySubscriptions", [])
    daily = next((s for s in subs if s.get("SubscriptionName") == "guardrail-daily"), None)
    if daily:
        vals = daily.get("ThresholdExpression", {}).get("Dimensions", {}).get("Values") or [None]
        live["anomaly_usd"] = vals[0]
    for role, key, policy_name in ((APPLY_ROLE, "apply_role", "apply-and-rollback"),
                                   (CHECK_ROLE, "check_role", "guardrails-check"),
                                   (GUARD_ROLE, "guard_role", "guardrails")):
        r = aws("iam", "get-role", "--role-name", role, ok_missing=True)
        if r:
            p = aws("iam", "get-role-policy", "--role-name", role, "--policy-name", policy_name, ok_missing=True)
            live[key] = {"trust": r["Role"]["AssumeRolePolicyDocument"], "policy": p and p["PolicyDocument"]}
    live["caps"] = {}
    for fn in ("build-runner-launcher", "build-runner-webhook"):
        c = aws("lambda", "get-function-concurrency", "--function-name", fn, ok_missing=True)
        live["caps"][fn] = (c or {}).get("ReservedConcurrentExecutions")
    return live


def main(argv) -> int:
    account = ""
    try:
        account = aws("sts", "get-caller-identity")["Account"]
        user = os.environ.get("AWS_CLAUDE_USER") or subprocess.run(
            [sys.executable, "-I", str(HERE / "config.py"), "AWS_CLAUDE_USER"], capture_output=True, text=True).stdout.strip()
        if not user:
            raise RuntimeError("AWS_CLAUDE_USER isn't set")
        policy = yaml.safe_load((HERE / "policy.yaml").read_text())
        template = (REPO / "part2-runner" / "stack" / "template.yaml").read_text()
        findings = compare(expected(policy, template, account, user), gather(account))
    except Exception as e:  # noqa: BLE001 - a check that can't run must say so, not pass
        print(redact(f"The guardrails check couldn't run: {type(e).__name__}: {e}", account))
        return 2
    print(json.dumps(json.loads(redact(json.dumps(findings), account)), indent=1) if "--json" in argv
          else report(findings, account))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
