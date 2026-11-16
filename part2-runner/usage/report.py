#!/usr/bin/env python3
"""Runner usage: hours and estimated cost per class (light / heavy), this month and the last 7 days,
whether to warn Kieron, and whether each job looks the right size (CPU, memory, waits for a runner). Run daily by .github/workflows/usage.yml (or locally as Claude's user).

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
WEBHOOK_LOG_GROUP = "/aws/lambda/build-runner-webhook"   # job-completed: queue wait per label (14 days kept)
JOBS_LOG_GROUP = "/build/runner/jobs"                    # job: CPU, memory, pressure per job (ami/files/job-usage.py)
REGION = "ap-southeast-2"
BUDGET_USD = float(os.environ.get("BUDGET_USD", "25"))
ALERT_USD = float(os.environ.get("ALERT_USD", "15"))          # projected runner spend this month
ALERT_HEAVY_SHARE = float(os.environ.get("ALERT_HEAVY_SHARE", "0.5"))  # of runner hours, last 7 days
# Sydney on-demand, US$/h (AWS Price List, 2026-11-16); only used when spot was short.
ON_DEMAND = {"c7i.large": 0.11655, "t3.large": 0.1056, "c7i.xlarge": 0.2331, "c6i.xlarge": 0.222,
             "m7i.xlarge": 0.252}
# "Is this the right size" notes, per job over the last 7 days (we own the runners: these are our suggestions).
FIT_MEM_HIGH = 0.8        # memory peak over 80% of the machine: too small, or about to be
FIT_CPU_STALL = 20.0      # tasks waiting for CPU over 20% of the job's time: CPU-starved
FIT_HEAVY_LOW_VCPU = 1.5  # a heavy job averaging under 1.5 busy vCPUs might fit light (check its timing tests first)
FIT_WALL_HIGH = 75 * 60   # close to the 90-minute runner limit
FIT_QUEUE_P95 = 120       # jobs waiting over 2 minutes for a runner: too few runners, or slow starts
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


def fit_notes(jobs: list[dict], queues: list[dict]) -> list[str]:
    """jobs: per (class, workflow, job) over 7 days; queues: per label. -> plain-English notes, worst first."""
    notes = []
    for j in jobs:
        name = f"`{j['workflow']} / {j['job']}` ({j['class']}, {j['n']:.0f} runs)"
        total = j.get("memory_mb") or 0
        if total and (j.get("mem_peak_mb") or 0) > FIT_MEM_HIGH * total:
            notes.append(f"**Too small?** {name}: memory peaked at {j['mem_peak_mb']:.0f} of {total:.0f} MB.")
        if (j.get("cpu_stall_pct") or 0) > FIT_CPU_STALL:
            notes.append(f"**CPU-starved?** {name}: tasks waited for CPU {j['cpu_stall_pct']:.0f}% of the time.")
        if (j.get("wall_max_s") or 0) > FIT_WALL_HIGH:
            notes.append(f"**Near the 90-minute limit:** {name}: longest run {j['wall_max_s'] / 60:.0f} min.")
        if j["class"] == "heavy" and j.get("avg_vcpu") is not None and j["avg_vcpu"] < FIT_HEAVY_LOW_VCPU:
            notes.append(f"**Could be light?** {name}: averaged {j['avg_vcpu']:.1f} busy vCPUs of 4 "
                         "(check its timing tests on 2 vCPU before moving it).")
    for q in queues:
        if (q.get("wait_p95") or 0) > FIT_QUEUE_P95:
            notes.append(f"**Waiting for runners:** `{q['label']}`: 95% of {q['n']:.0f} jobs started within "
                         f"{q['wait_p95']:.0f} s (longest {q['wait_max']:.0f} s).")
    return notes


def fit_table(jobs: list[dict], queues: list[dict]) -> list[str]:
    if not jobs and not queues:
        return ["## Job fit, last 7 days", "", "No per-job records yet."]
    def n(v, fmt):
        return format(v, fmt) if v is not None else "–"
    lines = ["## Job fit, last 7 days", "",
             "| Job | Class | Runs | Avg / max time | Avg busy vCPU | Peak memory | CPU wait |",
             "|---|---|---|---|---|---|---|"]
    for j in jobs:
        mem = f"{n(j.get('mem_peak_mb'), '.0f')} of {n(j.get('memory_mb'), '.0f')} MB"
        lines.append(f"| {j['workflow']} / {j['job']} | {j['class']} | {j['n']:.0f} | "
                     f"{n(j.get('wall_s'), '.0f')} / {n(j.get('wall_max_s'), '.0f')} s | {n(j.get('avg_vcpu'), '.2f')} | "
                     f"{mem} | {n(j.get('cpu_stall_pct'), '.0f')}% |")
    if queues:
        lines += ["", "| Label | Jobs | Wait for a runner: avg / 95% / max |", "|---|---|---|"]
        lines += [f"| {q['label']} | {q['n']:.0f} | {n(q.get('wait_avg'), '.0f')} / {n(q.get('wait_p95'), '.0f')} / "
                  f"{n(q.get('wait_max'), '.0f')} s |" for q in queues]
    notes = fit_notes(jobs, queues)
    lines += ["", *(f"- {x}" for x in notes)] if notes else ["", "Nothing looks over- or under-sized."]
    return lines


def summary(month: dict, week: dict, projected: float, now: dt.datetime,
            jobs: list[dict] | None = None, queues: list[dict] | None = None) -> str:
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
        "",
        *fit_table(jobs or [], queues or []),
    ])


# ---------------------------------------------------------------------------- AWS

def aws(*args: str) -> dict:
    out = subprocess.run(["aws", *args, "--region", REGION, "--output", "json"], check=True,
                         capture_output=True, text=True).stdout
    return json.loads(out) if out.strip() else {}


def insights(group: str, q: str, start: dt.datetime, end: dt.datetime) -> list[dict]:
    """A Logs Insights query -> rows as {field: value} (strings)."""
    qid = aws("logs", "start-query", "--log-group-name", group, "--start-time", str(int(start.timestamp())),
              "--end-time", str(int(end.timestamp())), "--query-string", q)["queryId"]
    for _ in range(60):
        res = aws("logs", "get-query-results", "--query-id", qid)
        if res["status"] in ("Complete", "Failed", "Cancelled", "Timeout"):
            break
        time.sleep(2)
    if res["status"] != "Complete":
        raise SystemExit(f"Logs Insights query {res['status']}")
    return [{x["field"]: x["value"] for x in fields} for fields in res["results"]]


def usage_rows(start: dt.datetime, end: dt.datetime) -> list[dict]:
    # Not "as minutes": an alias with the summed field's own name drops it from the results (2026-11-16).
    q = 'filter msg = "usage" | stats sum(minutes) as total_minutes by class, type, spot'
    return [{"class": f.get("class", "unknown"), "type": f.get("type", "?"),
             "spot": f.get("spot") in ("1", "true", "True"), "minutes": float(f.get("total_minutes", 0))}
            for f in insights(LOG_GROUP, q, start, end)]


def numbers(row: dict, keep: tuple) -> dict:
    out = {k: row.get(k, "?") for k in keep}
    for k, v in row.items():
        if k not in keep:
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                out[k] = None
    return out


# Aliases never reuse a field's own name (see usage_rows).
# ispresent(job): a runner that never got a job (reaped idle, or a test) has nothing to say about job sizes.
JOBS_QUERY = ('filter msg = "job" and ispresent(job) | stats count(*) as n, avg(wall_s) as wall_avg_s, max(wall_s) as wall_max_s, '
              'avg(avg_vcpu) as vcpu_avg, max(box_mem_peak_mb) as mem_max_mb, max(memory_mb) as mem_total_mb, '
              'avg(cpu_stall_pct) as cpu_wait_pct by class, workflow, job | sort n desc')
QUEUE_QUERY = ('filter msg = "job-completed" | stats count(*) as n, avg(queue_s) as wait_avg, '
               'pct(queue_s, 95) as wait_p95, max(queue_s) as wait_max by label')


def job_rows(start: dt.datetime, end: dt.datetime) -> list[dict]:
    rows = []
    for f in insights(JOBS_LOG_GROUP, JOBS_QUERY, start, end):
        r = numbers(f, ("class", "workflow", "job"))
        rows.append({"class": r["class"], "workflow": r["workflow"], "job": r["job"], "n": r.get("n") or 0,
                     "wall_s": r.get("wall_avg_s"), "wall_max_s": r.get("wall_max_s"), "avg_vcpu": r.get("vcpu_avg"),
                     "mem_peak_mb": r.get("mem_max_mb"), "memory_mb": r.get("mem_total_mb"),
                     "cpu_stall_pct": r.get("cpu_wait_pct")})
    return rows


def queue_rows(start: dt.datetime, end: dt.datetime) -> list[dict]:
    return [numbers(f, ("label",)) for f in insights(WEBHOOK_LOG_GROUP, QUEUE_QUERY, start, end)]


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
    jobs, queues = job_rows(now - dt.timedelta(days=7), now), queue_rows(now - dt.timedelta(days=7), now)
    (out / "summary.md").write_text(summary(month, week, projected, now, jobs, queues) + "\n")
    (out / "alerts.json").write_text(json.dumps(alerts(month, week, projected), indent=2) + "\n")
    print((out / "summary.md").read_text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
