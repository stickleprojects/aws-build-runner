#!/usr/bin/env python3
"""Offline tests for guardrails_check.py's comparison and report (no AWS).

    python3 -I part1-godmode/tests/test_guardrails_check.py
"""
import copy
import importlib.util
import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
P1 = HERE.parent
spec = importlib.util.spec_from_file_location("guardrails_check", P1 / "guardrails_check.py")
gc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gc)
failed = 0


def check(name, ok, detail=""):
    global failed
    print(f"{'ok  ' if ok else 'FAIL'}  {name}{'' if ok else ': ' + str(detail)[:400]}")
    failed += 0 if ok else 1


ACCT, USER = "123456789012", "claude-test-user"
POLICY = yaml.safe_load((P1 / "policy.yaml").read_text())
TEMPLATE = (P1.parent / "part2-runner" / "stack" / "template.yaml").read_text()
EXP = gc.expected(POLICY, TEMPLATE, ACCT, USER)
S = POLICY["account_settings"]


def live_matching():
    """What a correctly set-up account returns."""
    e = EXP
    action = {"ActionThreshold": {"ActionThresholdValue": e["action"]["at"]}, "ApprovalModel": "AUTOMATIC",
              "Status": "STANDBY",
              "Definition": {"IamActionDefinition": {"PolicyArn": e["action"]["policy"], "Users": [USER],
                                                     "Roles": list(reversed(e["action"]["roles"]))}}}
    fast = f"arn:aws:sns:ap-southeast-2:{ACCT}:guardrail-fast-stop"
    ks = f"arn:aws:lambda:ap-southeast-2:{ACCT}:function:guardrail-kill-switch"
    return {
        "budget": {"BudgetLimit": {"Amount": f"{e['budget']['limit']}"},
                   "CostTypes": {"IncludeCredit": True, "IncludeRefund": False, "IncludeTax": True}},
        "usage": {"BudgetLimit": {"Amount": f"{e['usage']['limit']}"}, "CostTypes": {"IncludeCredit": False}},
        "usage_action": [],
        "actions": [action],
        "budget_notifies_kill_switch": True,
        "stop_policy": copy.deepcopy(e["stop_policy"]),
        "kill_switch_env": dict(e["kill_switch_env"]),
        "kill_switch_reserved": 2,
        "alarms": [{"AlarmName": n, "Threshold": float(t), "AlarmActions": [fast], "ActionsEnabled": True}
                   for n, t in e["alarms"].items()],
        "subscriptions": {"guardrail-budget-stop": [{"Protocol": "lambda", "Endpoint": ks, "SubscriptionArn": "x"}],
                          "guardrail-fast-stop": [{"Protocol": "lambda", "Endpoint": ks, "SubscriptionArn": "x"},
                                                  {"Protocol": "email", "Endpoint": "e", "SubscriptionArn": "y"}]},
        "anomaly_usd": str(e["anomaly_usd"]),
        "apply_role": copy.deepcopy(e["apply_role"]),
        "check_role": copy.deepcopy(e["check_role"]),
        "guard_role": copy.deepcopy(e["guard_role"]),
        "caps": dict(e["caps"]),
    }


def drift(**changes):
    live = live_matching()
    for k, v in changes.items():
        live[k] = v
    return gc.compare(EXP, live)


def names(findings):
    return " / ".join(f["what"] for f in findings)


check("the repo's expectations come from policy.yaml",
      EXP["budget"]["limit"] == float(S["budget_usd"]) and EXP["alarms"]["guardrail-fast-lambda-concurrency"] == 20
      and EXP["caps"] == {"build-runner-launcher": 4, "build-runner-webhook": 5}, EXP["caps"])
check("rendering fills in the account and user", ACCT in json.dumps(EXP["apply_role"]) and "{{" not in json.dumps(EXP), "")
check("an account set up as the repo says: no drift", drift() == [], drift())

# 2026-11-17 (#41): the bootstrap left the stop budget at US$25 when the decision was US$29; only a paste showed it.
b = live_matching()["budget"]; b["BudgetLimit"]["Amount"] = "25.0"
f = drift(budget=b)
check("#41: the stop budget at US$25, not 29, is drift", any("guardrail-monthly limit" in x["what"] for x in f), f)
check("#41: and its fix is the apply", any(x["fix"] == gc.FIX_APPLY for x in f), f)
b = live_matching()["budget"]; b["CostTypes"]["IncludeCredit"] = False
check("the stop budget counting gross: drift", "IncludeCredit" in names(drift(budget=b)), drift(budget=b))
u = live_matching()["usage"]; u["CostTypes"]["IncludeCredit"] = True
check("the usage budget counting after credits: drift", "monthly-usage IncludeCredit" in names(drift(usage=u)))
check("the usage budget missing: drift", "guardrail-monthly-usage" in names(drift(usage=None)))
check("a stop on the usage budget: drift", "alerts only" in json.dumps(drift(usage_action=[{"x": 1}])))

a = copy.deepcopy(live_matching()["actions"]); a[0]["Status"] = "EXECUTION_SUCCESS"
f = drift(actions=a)
check("the budget stop has fired: reported, and lifting it is Kieron's call",
      any("status" in x["what"] and "Kieron's call" in x["fix"] for x in f), f)
a = copy.deepcopy(live_matching()["actions"]); a[0]["Definition"]["IamActionDefinition"]["Roles"] = ["build-runner-bake"]
check("the stop missing a role: drift", "action roles" in names(drift(actions=a)))
check("no budget action: drift", "actions" in names(drift(actions=[])))
check("the budget notification not reaching the kill switch: drift",
      "notification" in names(drift(budget_notifies_kill_switch=False)))

p = live_matching()["stop_policy"]; p["Statement"][0]["Action"] = p["Statement"][0]["Action"][1:]
check("budget-stop policy loosened: drift", "guardrails/budget-stop" in names(drift(stop_policy=p)))
env = live_matching()["kill_switch_env"]; env["TERMINATE_INSTANCES"] = "false"
f = drift(kill_switch_env=env)
check("kill switch no longer terminating: drift, fixed by the bootstrap",
      any("TERMINATE_INSTANCES" in x["what"] and x["fix"] == gc.FIX_BOOTSTRAP for x in f), f)
check("kill switch missing: drift", "guardrail-kill-switch" in names(drift(kill_switch_env=None)))
check("kill switch without its reserved executions: drift", "reserved" in names(drift(kill_switch_reserved=None)))

al = [a for a in live_matching()["alarms"] if a["AlarmName"] != "guardrail-fast-lambda-concurrency"]
check("the Lambda concurrency alarm missing: drift", "lambda-concurrency" in names(drift(alarms=al)))
al = live_matching()["alarms"]; al[0]["Threshold"] = al[0]["Threshold"] * 10
check("an alarm threshold raised: drift", "threshold" in names(drift(alarms=al)))
al = live_matching()["alarms"]; al[1]["ActionsEnabled"] = False
check("an alarm with its actions disabled: drift", "actions disabled" in json.dumps(drift(alarms=al)))
al = live_matching()["alarms"]; al[2]["AlarmActions"] = []
check("an alarm that notifies nothing: drift", "action" in names(drift(alarms=al)))

subs = live_matching()["subscriptions"]; subs["guardrail-budget-stop"] = []
check("the budget topic not reaching the kill switch: drift", "guardrail-budget-stop" in names(drift(subscriptions=subs)))
subs = live_matching()["subscriptions"]; subs["guardrail-fast-stop"][1]["SubscriptionArn"] = "PendingConfirmation"
f = drift(subscriptions=subs)
check("the alert email unconfirmed: reported, and the fix is Kieron's click", any("Confirm" in x["fix"] for x in f), f)
check("anomaly threshold changed: drift", "Anomaly" in names(drift(anomaly_usd="30")))
check("anomaly subscription missing: drift", "Anomaly" in names(drift(anomaly_usd=None)))

r = copy.deepcopy(live_matching()["apply_role"]); r["policy"]["Statement"].append({"Effect": "Allow", "Action": "*", "Resource": "*"})
f = drift(apply_role=r)
check("2026-11-27: the apply role out of step with bootstrap/: drift, fixed by bootstrap-github-apply.sh",
      any("aws-build-runner-apply policy" in x["what"] and x["fix"] == gc.FIX_GITHUB for x in f), f)
f = drift(guard_role=None)
check("the guardrails role missing: drift, fixed by bootstrap-github-apply.sh",
      any(x["what"].endswith("aws-build-runner-guardrails") and x["fix"] == gc.FIX_GITHUB for x in f), f)
f = drift(check_role=None)
check("the check role missing: drift, said as missing", any(x["what"].endswith("guardrails-check") and x["live"] == "missing" for x in f), f)
check("the launcher's cap gone: drift, fixed by the stack deploy",
      any("build-runner-launcher" in x["what"] and x["fix"] == gc.FIX_STACK for x in
          drift(caps={"build-runner-launcher": None, "build-runner-webhook": 5})))

# The report goes into a public issue.
f = [{"what": f"role arn:aws:iam::{ACCT}:role/x", "live": ACCT, "want": "a|b", "fix": "f"}]
rep = gc.report(f, ACCT)
check("the report never shows the account id", ACCT not in rep and "<account>" in rep, rep)
check("the report escapes table pipes", "a\\|b" in rep, rep)
check("no drift: a one-line report", gc.report([], ACCT) == "Everything live matches the repo.")

print(f"\n{failed} failed")
sys.exit(1 if failed else 0)
