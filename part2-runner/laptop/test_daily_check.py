#!/usr/bin/env python3
"""Offline tests for daily_check.py's evaluation (no AWS, no Telegram).

    python3 -I part2-runner/laptop/test_daily_check.py
"""
import copy
import datetime as dt
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("daily_check", HERE / "daily_check.py")
dc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dc)
failed = 0


def check(name, ok, detail=""):
    global failed
    print(f"{'ok  ' if ok else 'FAIL'}  {name}{'' if ok else ': ' + str(detail)}")
    failed += 0 if ok else 1


LIM = dc.limits()
# 15 November, 10:00 Sydney: half the month gone, outside the curfew.
NOW = dt.datetime(2026, 11, 15, 10, 0, tzinfo=dc.SYDNEY).astimezone(dt.timezone.utc)
BASE = {
    "budget": {"BudgetLimit": {"Amount": "29.0"}, "LastUpdatedTime": "2026-11-15T08:00:00+11:00",
               "CalculatedSpend": {"ActualSpend": {"Amount": "4.10"}, "ForecastedSpend": {"Amount": "9.50"}}},
    "actions": [{"ActionType": "APPLY_IAM_POLICY", "Status": "STANDBY"}],
    "alarms": [{"AlarmName": "guardrail-fast-network-out", "StateValue": "OK"}],
    "concurrency": {"build-runner-launcher": None, "build-runner-webhook": None},
    "instances": [], "volumes": [], "addresses": [], "images": [{}, {}, {}],
    "subscriptions": [{"TopicArn": "arn:aws:sns:x:1:guardrail-fast-stop", "Protocol": "email", "SubscriptionArn": "arn:x"}],
}


def run(**changes):
    d = copy.deepcopy(BASE)
    for k, v in changes.items():
        d[k] = v
    return dc.evaluate(d, NOW, LIM)


def spend(actual, forecast="9.50", updated="2026-11-15T08:00:00+11:00", limit="29.0"):
    return {"BudgetLimit": {"Amount": limit}, "LastUpdatedTime": updated,
            "CalculatedSpend": {"ActualSpend": {"Amount": actual}, "ForecastedSpend": {"Amount": forecast}}}


s, a, w = run()
check("all well: OK, with the month's spend and forecast", s.startswith("AWS OK: US$4.10 spent this month, forecast US$9.50")
      and not a and not w, (s, a, w))
s, a, w = run(budget=spend("15.00"))
check("over 50% of the stop: a warning", s.startswith("AWS WARN") and any("50%" in x for x in w), w)
s, a, w = run(budget=spend("24.00"))
check("over 80%: a warning", any("80%" in x for x in w), w)
s, a, w = run(budget=spend("29.00"))
check("at the stop: an alert", s.startswith("AWS ALERT") and any("stop should have fired" in x for x in a), a)
s, a, w = run(budget=spend("4.10", forecast="31.0"))
check("forecast over the stop: a warning", any("forecast" in x for x in w), w)
s, a, w = run(budget=spend("13.00"))
check("ahead of the busiest month's pace (US$25 × 47%)", any("busiest" in x for x in w), w)
s, a, w = run(budget=spend("4.10", forecast="None"))
check("no forecast yet (early in the month) is fine", not a and "forecast" not in s, s)
s, a, w = run(budget=spend("4.10", updated="2026-11-12T08:00:00+11:00"))
check("Budgets not updated for 3 days: an alert", any("hasn't updated" in x for x in a), a)
s, a, w = run(budget=spend("4.10", limit="40.0"))
check("budget limit not the policy's US$29: an alert", any("not the US$29" in x for x in a), a)
s, a, w = run(actions=[{"ActionType": "APPLY_IAM_POLICY", "Status": "EXECUTION_SUCCESS"}])
check("the budget stop fired: an alert", any("EXECUTION_SUCCESS" in x for x in a), a)
s, a, w = run(alarms=[{"AlarmName": "guardrail-fast-lambda-invocations", "StateValue": "ALARM"}])
check("a fast-stop alarm in ALARM: an alert", any("in ALARM" in x for x in a), a)
s, a, w = run(concurrency={"build-runner-launcher": 0, "build-runner-webhook": None})
check("a Lambda throttled to 0: an alert", any("throttled to 0" in x for x in a), a)
old = {"InstanceId": "i-old", "InstanceType": "c7i.xlarge", "LaunchTime": (NOW - dt.timedelta(hours=2)).isoformat(),
       "Tags": [{"Key": "Name", "Value": "build-runner aws-1"}]}
s, a, w = run(instances=[old])
check("an instance past 90 min (+15): an alert", any("past the 90-minute limit" in x for x in a), a)
young = {**old, "LaunchTime": (NOW - dt.timedelta(minutes=30)).isoformat()}
s, a, w = run(instances=[young])
check("a 30-minute runner in the day is fine", not a, a)
night = dt.datetime(2026, 11, 15, 23, 0, tzinfo=dc.SYDNEY).astimezone(dt.timezone.utc)
d = copy.deepcopy(BASE)
d["instances"] = [{**old, "LaunchTime": (night - dt.timedelta(minutes=10)).isoformat()}]
check("an instance during the curfew: an alert", any("curfew" in x for x in dc.evaluate(d, night, LIM)[1]))
s, a, w = run(volumes=[{"VolumeId": "vol-1", "Size": 30}])
check("an unattached disk: a warning", any("unattached disk" in x for x in w), w)
s, a, w = run(addresses=[{"PublicIp": "1.2.3.4"}, {"PublicIp": "5.6.7.8", "AssociationId": "x"}])
check("an unattached Elastic IP: a warning, attached ones ignored", any("1 unattached Elastic IP" in x for x in w), w)
s, a, w = run(images=[{}] * 5)
check("more images than kept (+1 candidate): a warning", any("images kept" in x for x in w), w)
s, a, w = run(images=[{}] * 4)
check("kept images plus one candidate is fine", not w, w)
s, a, w = run(subscriptions=[{"TopicArn": "arn:aws:sns:x:1:guardrail-fast-stop", "Protocol": "email",
                              "SubscriptionArn": "PendingConfirmation"}])
check("an unconfirmed alert email: a warning", any("PendingConfirmation" in x for x in w), w)

# Telegram config: missing is reported (not fatal); a file others can read is refused.
with tempfile.TemporaryDirectory() as d:
    dc.CONFIG = Path(d) / "notify.env"
    check("no config: says Telegram isn't set up", "isn't set up" in (dc.send_telegram("x") or ""))
    dc.CONFIG.write_text("AWS_CHECK_TELEGRAM_BOT_TOKEN=1:abc\nAWS_CHECK_TELEGRAM_CHAT_ID=5\n")
    os.chmod(dc.CONFIG, 0o644)
    check("a config others can read is refused", "chmod 600" in (dc.send_telegram("x") or ""))

src = (HERE / "daily_check.py").read_text()
code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
check("never calls Cost Explorer (US$0.01 a call)", '"ce"' not in code and "'ce'" not in code and " ce " not in code)

print(f"\n{failed} failed")
sys.exit(1 if failed else 0)
