#!/usr/bin/env python3
"""Daily AWS cost and guardrail check, run on Kieron's laptop by a systemd user timer (install.sh).

    python3 -I part2-runner/laptop/daily_check.py            check, log, and notify the desktop
    python3 -I part2-runner/laptop/daily_check.py --print    check and print the report only

A second pair of eyes on the alerts, which haven't been tested end to end. Each day it reports the
account's spend this month (all services, what the bill will say: AWS Budgets) and checks that the
guardrails look alive:
  - spend: the month so far, the forecast, against the US$29 stop and the busiest expected month
  - the budget is still updating (Budgets data older than 2 days means its alerts can't fire)
  - the budget stop hasn't fired (its action is STANDBY), and the fast-stop alarms aren't in ALARM
  - our Lambdas aren't throttled to 0 (what the kill switch does)
  - no runner instance older than the 90-minute limit, none running during the curfew
  - nothing left behind that bills monthly: unattached disks, unattached Elastic IPs, more images than kept
  - the alert emails are confirmed (SNS subscriptions not "PendingConfirmation")
  - spellbound3d's pools.json wasn't refused by the launcher (/build/runner/pools-status, #54)
  - our Lambdas are each capped and the concurrency alarm exists (#30 fix 6; AWS's minimum limit is 1,000)

Free calls only: Budgets, CloudWatch, EC2, Lambda and SNS reads, with Claude's read-only CLI profile.
NOT Cost Explorer: its API costs US$0.01 a call, and nothing scheduled may call it (cost_boundary).
The result goes to Telegram (Kieron's own "AWS daily check" bot; token and chat in
~/.config/aws-daily-check/notify.env, chmod 600, written by install.sh), to the log
~/.local/state/aws-daily-check/log, and to a desktop notification.

    python3 -I part2-runner/laptop/daily_check.py --telegram-chats   list chats that messaged the bot (setup)
"""
import datetime as dt
import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
REGION = "ap-southeast-2"
SYDNEY = ZoneInfo("Australia/Sydney")
BUDGET = "guardrail-monthly"
USAGE_BUDGET = "guardrail-monthly-usage"  # usage before credits: sees spending while credits cover the bill
OUR_LAMBDAS = ["build-runner-launcher", "build-runner-webhook"]
LOG = Path.home() / ".local" / "state" / "aws-daily-check" / "log"
CONFIG = Path.home() / ".config" / "aws-daily-check" / "notify.env"


def config() -> dict:
    """KEY=value lines; the file must be private, as it holds the bot token."""
    if not CONFIG.is_file():
        return {}
    if CONFIG.stat().st_mode & 0o077:
        raise RuntimeError(f"{CONFIG} is readable by others; chmod 600 it")
    out = {}
    for line in CONFIG.read_text().splitlines():
        k, sep, v = line.partition("=")
        if sep and not k.strip().startswith("#"):
            out[k.strip()] = v.strip()
    return out


def telegram(method: str, token: str, **fields) -> dict:
    data = urllib.parse.urlencode(fields).encode() if fields else None
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}", data=data, timeout=20) as r:
        return json.loads(r.read())


def send_telegram(text: str) -> str | None:
    """Sends the report; returns why not, or None when sent (or when Telegram isn't set up)."""
    try:
        c = config()
        if not c.get("AWS_CHECK_TELEGRAM_BOT_TOKEN"):
            return "Telegram isn't set up (part2-runner/laptop/install.sh)"
        telegram("sendMessage", c["AWS_CHECK_TELEGRAM_BOT_TOKEN"], chat_id=c["AWS_CHECK_TELEGRAM_CHAT_ID"],
                 text=text[:4000], disable_web_page_preview="true")
        return None
    except Exception as e:  # noqa: BLE001 - never print the token: only the error type and Telegram's reason
        return f"Telegram send failed: {type(e).__name__}: {getattr(e, 'reason', '') or str(e).split('bot')[0]}"


def limits() -> dict:
    """The cost boundary from policy.yaml (plain 'key: number' lines; no YAML library needed)."""
    repo = ROOT / "part1-godmode" / "policy.yaml"
    text = (repo if repo.is_file() else Path(__file__).resolve().parent / "policy.yaml").read_text()  # installed copy
    block = text[text.index("\ncost_boundary:"):]
    block = block[:re.search(r"\n[a-z_]+:\s*\n(?!  )", block[1:]).start() + 1] if re.search(
        r"\n[a-z_]+:\s*\n(?!  )", block[1:]) else block

    def num(key):
        m = re.search(rf"^\s+{key}:\s*([0-9.]+)", block, re.M)
        if not m:
            raise SystemExit(f"daily_check.py: {key} not found in policy.yaml cost_boundary")
        return float(m.group(1))
    return {"stop": num("max_budget_usd"), "ceiling": num("ceiling_usd"), "busiest": num("busiest"),
            "typical": num("typical"), "max_minutes": num("max_instance_minutes"), "kept_amis": num("max_kept_amis")}


def aws(*args) -> dict | list:
    r = subprocess.run(["aws", *args, "--region", REGION, "--output", "json"], capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise RuntimeError(f"aws {' '.join(args[:2])}: {r.stderr.strip()[:300]}")
    return json.loads(r.stdout or "{}")


def pools_status() -> dict:
    """The launcher's verdict on the consumer's pools.json; {} if there's none yet."""
    try:
        return json.loads(aws("ssm", "get-parameter", "--name", "/build/runner/pools-status")["Parameter"]["Value"])
    except (RuntimeError, ValueError, KeyError):
        return {}


def usage_budget(acct: str) -> dict | None:
    """The usage-before-credits budget (alerts only), or None if it isn't there yet."""
    try:
        return aws("budgets", "describe-budget", "--account-id", acct, "--budget-name", USAGE_BUDGET)["Budget"]
    except (RuntimeError, KeyError):
        return None


def gather() -> dict:
    acct = aws("sts", "get-caller-identity")["Account"]
    d = {"budget": aws("budgets", "describe-budget", "--account-id", acct, "--budget-name", BUDGET)["Budget"],
         "actions": aws("budgets", "describe-budget-actions-for-budget", "--account-id", acct,
                        "--budget-name", BUDGET).get("Actions", []),
         "alarms": aws("cloudwatch", "describe-alarms", "--alarm-name-prefix", "guardrail-").get("MetricAlarms", []),
         "concurrency": {f: aws("lambda", "get-function-concurrency", "--function-name", f).get(
             "ReservedConcurrentExecutions") for f in OUR_LAMBDAS},
         "instances": [i for r in aws("ec2", "describe-instances", "--filters",
                                      "Name=instance-state-name,Values=pending,running").get("Reservations", [])
                       for i in r["Instances"]],
         "volumes": aws("ec2", "describe-volumes", "--filters", "Name=status,Values=available").get("Volumes", []),
         "addresses": aws("ec2", "describe-addresses").get("Addresses", []),
         "images": aws("ec2", "describe-images", "--owners", "self").get("Images", []),
         "subscriptions": [],
         "pools_status": pools_status(),
         "lambda_limit": aws("lambda", "get-account-settings").get("AccountLimit", {}).get("ConcurrentExecutions"),
         "usage": usage_budget(acct)}
    for t in aws("sns", "list-topics").get("Topics", []):
        if t["TopicArn"].split(":")[-1].startswith("guardrail-"):
            d["subscriptions"] += aws("sns", "list-subscriptions-by-topic", "--topic-arn", t["TopicArn"]).get(
                "Subscriptions", [])
    return d


def parse_time(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def evaluate(d: dict, now: dt.datetime, lim: dict) -> tuple[str, list[str], list[str]]:
    """(summary line, alerts, warnings). Pure, so it's tested offline."""
    alerts, warns = [], []
    b = d["budget"]
    spend = float(b.get("CalculatedSpend", {}).get("ActualSpend", {}).get("Amount", 0))
    fc = b.get("CalculatedSpend", {}).get("ForecastedSpend", {}).get("Amount")
    forecast = float(fc) if fc not in (None, "", "None") else None
    limit = float(b["BudgetLimit"]["Amount"])
    local = now.astimezone(SYDNEY)
    days = (local.replace(month=local.month % 12 + 1, year=local.year + local.month // 12, day=1) - dt.timedelta(days=1)).day
    elapsed = (local.day - 1 + local.hour / 24) / days

    if limit != lim["stop"]:
        alerts.append(f"the budget's limit is US${limit:g}, not the US${lim['stop']:g} in policy.yaml")
    if spend >= limit:
        alerts.append(f"spend US${spend:.2f} has reached the US${limit:g} stop: the budget stop should have fired")
    elif spend >= 0.8 * limit:
        warns.append(f"spend US${spend:.2f} is over 80% of the US${limit:g} stop (an email should have come)")
    elif spend >= 0.5 * limit:
        warns.append(f"spend US${spend:.2f} is over 50% of the US${limit:g} stop (an email should have come)")
    if forecast is not None and forecast > limit:
        warns.append(f"the month is forecast at US${forecast:.2f}, over the US${limit:g} stop")
    if elapsed > 0.1 and spend > lim["busiest"] * elapsed:
        warns.append(f"spend is running ahead of the busiest expected month (US${lim['busiest']:g}): "
                     f"US${spend:.2f} after {elapsed:.0%} of the month")
    updated = b.get("LastUpdatedTime")
    if updated and now - parse_time(updated) > dt.timedelta(days=2):
        alerts.append(f"Budgets hasn't updated since {updated[:16]}: its alerts and stop can't fire")

    # Usage before credits (2026-11-26): while credits cover the bill, the stop's budget reads US$0, so only this
    # one sees a slow leak. Alerts only; nothing stops on it.
    u = d.get("usage")
    usage = None
    if u is None:
        warns.append(f"budget {USAGE_BUDGET} is missing: nothing sees usage while credits cover the bill (run apply)")
    else:
        usage = float(u.get("CalculatedSpend", {}).get("ActualSpend", {}).get("Amount", 0))
        ulimit = float(u["BudgetLimit"]["Amount"])
        if usage >= ulimit:
            alerts.append(f"usage before credits US${usage:.2f} has reached US${ulimit:g}: credits may be paying, "
                          "but nothing stops it until they run out")
        elif usage >= 0.8 * ulimit:
            warns.append(f"usage before credits US${usage:.2f} is over 80% of US${ulimit:g}")
        elif elapsed > 0.1 and usage > lim["busiest"] * elapsed:
            warns.append(f"usage before credits is running ahead of the busiest expected month (US${lim['busiest']:g}): "
                         f"US${usage:.2f} after {elapsed:.0%} of the month")

    for a in d["actions"]:
        if a.get("Status") != "STANDBY":
            alerts.append(f"the budget stop ({a.get('ActionType')}) is {a.get('Status')}: it has fired or is pending")
    for a in d["alarms"]:
        if a.get("StateValue") == "ALARM":
            alerts.append(f"alarm {a['AlarmName']} is in ALARM: the fast stop has fired")
    for f, c in d["concurrency"].items():
        if c == 0:
            alerts.append(f"Lambda {f} is throttled to 0 (the kill switch fired); turning it back on is Kieron's call")

    curfew = local.hour >= 22 or local.hour < 7
    for i in d["instances"]:
        age = (now - parse_time(i["LaunchTime"])).total_seconds() / 60
        name = next((t["Value"] for t in i.get("Tags", []) if t["Key"] == "Name"), i["InstanceId"])
        if age > lim["max_minutes"] + 15:
            alerts.append(f"instance {i['InstanceId']} ({name}, {i['InstanceType']}) has run {age / 60:.1f} h, "
                          f"past the {lim['max_minutes']:g}-minute limit: the reaper missed it")
        elif curfew:
            alerts.append(f"instance {i['InstanceId']} ({name}) is running during the 22:00-07:00 curfew")
    if d["volumes"]:
        warns.append(f"{len(d['volumes'])} unattached disk(s), billed monthly: "
                     + ", ".join(f"{v['VolumeId']} ({v['Size']} GiB)" for v in d["volumes"][:5]))
    loose = [a for a in d["addresses"] if not a.get("AssociationId")]
    if loose:
        warns.append(f"{len(loose)} unattached Elastic IP(s), ≈US$3.60/month each: "
                     + ", ".join(a.get("PublicIp", "?") for a in loose))
    if len(d["images"]) > lim["kept_amis"] + 1:  # +1: a candidate awaiting its smoke test
        warns.append(f"{len(d['images'])} images kept (limit {lim['kept_amis']:g}); their snapshots bill monthly")
    pending = [s for s in d["subscriptions"] if s.get("SubscriptionArn") == "PendingConfirmation"]
    if pending:
        warns.append(f"{len(pending)} alert subscription(s) still PendingConfirmation: "
                     + ", ".join(f"{s['TopicArn'].split(':')[-1]} ({s['Protocol']})" for s in pending))

    # #30 fix 6: Lambda keeps 100 unreserved, so reserving our caps and the kill switch's 2 needs >= 111. AWS won't
    # go below 1,000 (2026-11-26), so the ceiling comes from each function's cap and the concurrency alarm instead.
    lam = d.get("lambda_limit")
    if isinstance(lam, int) and lam < 111:
        warns.append(f"the account's Lambda limit is {lam}: too low to cap our Lambdas and reserve the kill switch "
                     "(#30 fix 6; AWS's minimum is 1,000)")
    for f, c in d["concurrency"].items():
        if c is None:
            warns.append(f"Lambda {f} has no concurrency cap: a fault could run hundreds at once (deploy the stack)")
    if not any(a.get("AlarmName") == "guardrail-fast-lambda-concurrency" for a in d["alarms"]):
        warns.append("alarm guardrail-fast-lambda-concurrency is missing: rerun bootstrap-budget-stop.sh")
    ps = d.get("pools_status") or {}
    if ps.get("state") == "refused":
        alerts.append(f"the launcher refused spellbound3d's pools.json, so the last good copy is in force: {ps.get('reason', '')[:300]}")
    elif ps.get("state") == "unreachable":
        warns.append(f"the launcher couldn't read spellbound3d's pools.json: {ps.get('reason', '')[:200]}")

    state = "ALERT" if alerts else "WARN" if warns else "OK"
    fc_text = f", forecast US${forecast:.2f}" if forecast is not None else ""
    usage_text = f", usage before credits US${usage:.2f}" if usage is not None else ""
    summary = (f"AWS {state}: US${spend:.2f} spent this month{fc_text} (stop US${limit:g}, ceiling US${lim['ceiling']:g})"
               f"{usage_text}; {len(d['instances'])} instance(s) running")
    return summary, alerts, warns


def notify(state: str, summary: str, details: list[str]) -> None:
    urgency = {"ALERT": "critical", "WARN": "normal"}.get(state, "low")
    try:
        subprocess.run(["notify-send", "-u", urgency, "-a", "AWS daily check", summary, "\n".join(details)[:900]],
                       timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass


def main(argv) -> int:
    if "--telegram-chats" in argv:
        token = config().get("AWS_CHECK_TELEGRAM_BOT_TOKEN", "")
        for u in telegram("getUpdates", token).get("result", []) if token else []:
            chat = (u.get("message") or u.get("channel_post") or {}).get("chat", {})
            if chat:
                print(chat["id"], chat.get("type", ""), chat.get("title") or chat.get("username") or chat.get("first_name", ""))
        return 0
    now = dt.datetime.now(dt.timezone.utc)
    try:
        summary, alerts, warns = evaluate(gather(), now, limits())
    except Exception as e:  # noqa: BLE001 - a check that can't run is itself worth knowing about
        summary, alerts, warns = f"AWS ALERT: the daily check couldn't run: {e}", [], []
    state = summary.split()[1].rstrip(":")
    lines = [summary] + [f"ALERT {a}" for a in alerts] + [f"WARN  {w}" for w in warns]
    stamp = now.astimezone(SYDNEY).strftime("%Y-%m-%d %H:%M")
    if "--print" in argv:
        print("\n".join(lines))
    else:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write("".join(f"{stamp} {line}\n" for line in lines))
        icon = {"OK": "✅", "WARN": "⚠️", "ALERT": "🚨"}.get(state, "")
        why = send_telegram(f"{icon} " + "\n".join(lines) + f"\n\n{stamp} Sydney · part2-runner/laptop/daily_check.py")
        if why:
            lines.append(f"WARN  {why}")
            with LOG.open("a") as f:
                f.write(f"{stamp} WARN  {why}\n")
        notify(state, summary, lines[1:])
    return 1 if state == "ALERT" else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
