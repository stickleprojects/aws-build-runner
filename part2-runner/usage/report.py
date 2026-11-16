#!/usr/bin/env python3
"""Runner usage: hours and estimated cost per class (light / heavy), this month and the last 7 days,
and whether to warn Kieron. Run daily by .github/workflows/usage.yml (or locally as Claude's user).

    python3 -I part2-runner/usage/report.py OUT_DIR

Reads the launcher's "usage" log lines (one per live runner per 5-minute run) with CloudWatch Logs
Insights, and today's Sydney spot prices. Writes OUT_DIR/summary.md and OUT_DIR/alerts.json
([{key, title, body}], empty when all is well).

It's an ESTIMATE (instance-minutes x price, sampled every 5 minutes), runners only; the real bill is
AWS Billing. The whole-account US$25 stop applies whatever this says.
"""
import calendar
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

LOG_GROUP = "/aws/lambda/build-runner-launcher"
REGION = "ap-southeast-2"
BUDGET_USD = float(os.environ.get("BUDGET_USD", "25"))
ALERT_USD = float(os.environ.get("ALERT_USD", "15"))          # projected runner spend this month
ALERT_HEAVY_SHARE = float(os.environ.get("ALERT_HEAVY_SHARE", "0.5"))  # of runner hours, last 7 days
# Sydney on-demand, US$/h (AWS Price List, 2026-11-16); only used when spot was short.
ON_DEMAND = {"c7i.large": 0.11655, "t3.large": 0.1056, "c7i.xlarge": 0.2331, "c6i.xlarge": 0.222,
             "m7i.xlarge": 0.252}
# Per instance-hour on top: public IPv4 (US$0.005/h) and the 30 GiB gp3 disk (US$0.096/GB-month).
OVERHEAD = 0.005 + 30 * 0.096 / 730


# ---------------------------------------------------------------------------- pure

def cost(rows: list[dict], spot_prices: dict) -> list[dict]:
    """rows: {class, type, spot, minutes} -> the same plus hours and usd."""
    out = []
    for r in rows:
        hours = float(r["minutes"]) / 60
        price = spot_prices.get(r["type"]) if r["spot"] else ON_DEMAND.get(r["type"])
        if price is None:  # unknown type: price it as the dearest we know, never as free
            price = max(ON_DEMAND.values())
        out.append({**r, "hours": hours, "usd": hours * (price + OVERHEAD)})
    return out


def totals(rows: list[dict]) -> dict:
    t = {"hours": 0.0, "usd": 0.0, "by_class": {}}
    for r in rows:
        c = t["by_class"].setdefault(r["class"], {"hours": 0.0, "usd": 0.0})
        c["hours"] += r["hours"]
        c["usd"] += r["usd"]
        t["hours"] += r["hours"]
        t["usd"] += r["usd"]
    return t


def heavy_share(t: dict) -> float:
    return t["by_class"].get("heavy", {}).get("hours", 0.0) / t["hours"] if t["hours"] else 0.0


def projection(month: dict, week: dict, now: dt.datetime) -> float:
    """Month so far, plus the last 7 days' daily rate for the rest of the month."""
    days = calendar.monthrange(now.year, now.month)[1]
    elapsed = (now - now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)).total_seconds() / 86400
    window = min(7.0, max(elapsed, 1 / 24))
    return month["usd"] + week["usd"] / window * (days - elapsed)


def alerts(month: dict, week: dict, projected: float) -> list[dict]:
    share = heavy_share(week)
    out = []
    if projected > ALERT_USD and share > ALERT_HEAVY_SHARE:
        out.append({"key": "heavy",
                    "title": f"Kieron: spellbound3d is shredding your budget: heavy is {share:.0%} of runner hours, "
                             f"projected US${projected:.0f} this month",
                    "body": f"Over the last 7 days **{share:.0%} of runner hours were on `heavy`** (4 vCPU) runners, and "
                            f"runners are projected at **≈ US${projected:.2f}** this month (warning line US${ALERT_USD:.0f}). "
                            "spellbound3d chooses the class per job (`SDLC_RUNNER_HEAVY`): worth asking it which jobs "
                            "really need heavy."})
    if projected > BUDGET_USD:
        out.append({"key": "budget",
                    "title": f"Runners are on track to hit the US${BUDGET_USD:.0f} stop: projected US${projected:.0f} this month",
                    "body": f"Runners alone are projected at **≈ US${projected:.2f}** this month. At US${BUDGET_USD:.0f} for the "
                            "whole account, the automatic stop terminates every instance and blocks new ones."})
    return out


def summary(month: dict, week: dict, projected: float, now: dt.datetime) -> str:
    def row(name, t):
        cells = [f"{t['by_class'].get(c, {}).get('hours', 0):.1f} h, US${t['by_class'].get(c, {}).get('usd', 0):.2f}"
                 for c in ("light", "heavy")]
        return f"| {name} | {cells[0]} | {cells[1]} | {t['hours']:.1f} h, US${t['usd']:.2f} | {heavy_share(t):.0%} |"
    return "\n".join([
        f"## Runner usage, {now:%Y-%m-%d} (estimate)",
        "",
        "| Period | light (2 vCPU) | heavy (4 vCPU) | All runners | Heavy share of hours |",
        "|---|---|---|---|---|",
        row("Last 7 days", week),
        row(f"{now:%B} so far", month),
        "",
        f"**Projected for {now:%B}: ≈ US${projected:.2f}** for runners (warning when over US${ALERT_USD:.0f} with heavy "
        f"over {ALERT_HEAVY_SHARE:.0%} of hours; the whole-account stop is at US${BUDGET_USD:.0f}).",
        "",
        "Instance-minutes sampled every 5 minutes × today's Sydney spot price (on-demand when spot was short), "
        "plus IPv4 and disk. Runners only: AMIs, logs and the rest are ≈ US$3/month more.",
    ])


# ---------------------------------------------------------------------------- AWS

def aws(*args: str) -> dict:
    out = subprocess.run(["aws", *args, "--region", REGION, "--output", "json"], check=True,
                         capture_output=True, text=True).stdout
    return json.loads(out) if out.strip() else {}


def usage_rows(start: dt.datetime, end: dt.datetime) -> list[dict]:
    # Not "as minutes": an alias with the summed field's own name drops it from the results (2026-11-16).
    q = 'filter msg = "usage" | stats sum(minutes) as total_minutes by class, type, spot'
    qid = aws("logs", "start-query", "--log-group-name", LOG_GROUP, "--start-time", str(int(start.timestamp())),
              "--end-time", str(int(end.timestamp())), "--query-string", q)["queryId"]
    for _ in range(60):
        res = aws("logs", "get-query-results", "--query-id", qid)
        if res["status"] in ("Complete", "Failed", "Cancelled", "Timeout"):
            break
        time.sleep(2)
    if res["status"] != "Complete":
        raise SystemExit(f"Logs Insights query {res['status']}")
    rows = []
    for fields in res["results"]:
        f = {x["field"]: x["value"] for x in fields}
        rows.append({"class": f.get("class", "unknown"), "type": f.get("type", "?"),
                     "spot": f.get("spot") in ("1", "true", "True"), "minutes": float(f.get("total_minutes", 0))})
    return rows


def spot_prices(types) -> dict:
    if not types:
        return {}
    res = aws("ec2", "describe-spot-price-history", "--instance-types", *sorted(types),
              "--product-descriptions", "Linux/UNIX", "--start-time", dt.datetime.now(dt.timezone.utc).isoformat())
    by_type = {}
    for p in res.get("SpotPriceHistory", []):
        by_type.setdefault(p["InstanceType"], []).append(float(p["SpotPrice"]))
    return {t: sum(v) / len(v) for t, v in by_type.items()}


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)
    month_rows = usage_rows(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0), now)
    week_rows = usage_rows(now - dt.timedelta(days=7), now)
    prices = spot_prices({r["type"] for r in month_rows + week_rows if r["spot"]})
    month, week = totals(cost(month_rows, prices)), totals(cost(week_rows, prices))
    projected = projection(month, week, now)
    (out / "summary.md").write_text(summary(month, week, projected, now) + "\n")
    (out / "alerts.json").write_text(json.dumps(alerts(month, week, projected), indent=2) + "\n")
    print((out / "summary.md").read_text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
