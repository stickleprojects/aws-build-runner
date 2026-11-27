#!/usr/bin/env python3
"""Offline tests for the usage report's sums, projection and warnings (no AWS).

    python3 -I part2-runner/usage/test_report.py
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # python3 -I leaves it out
import report  # noqa: E402

failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failed
    print(f"{'ok  ' if ok else 'FAIL'}  {name}{'' if ok else ': ' + detail}")
    failed += 0 if ok else 1


SPOT = {"c7i.large": 0.05, "c7i.xlarge": 0.10}
O = report.OVERHEAD


def t(rows):
    return report.totals(report.cost(rows, SPOT))


r = report.cost([{"class": "light", "type": "c7i.large", "spot": True, "minutes": 60}], SPOT)[0]
check("cost: spot hour = spot price + IPv4 and disk", abs(r["usd"] - (0.05 + O)) < 1e-9, str(r))
r = report.cost([{"class": "heavy", "type": "c7i.xlarge", "spot": False, "minutes": 30}], SPOT)[0]
check("cost: on-demand when spot was short", abs(r["usd"] - 0.5 * (0.2331 + O)) < 1e-9, str(r))
r = report.cost([{"class": "heavy", "type": "x9.huge", "spot": True, "minutes": 60}], SPOT)[0]
check("cost: an unknown type is never free", r["usd"] >= max(report.ON_DEMAND.values()), str(r))

week = t([{"class": "light", "type": "c7i.large", "spot": True, "minutes": 60 * 10},
          {"class": "heavy", "type": "c7i.xlarge", "spot": True, "minutes": 60 * 30}])
check("totals: hours per class", week["by_class"]["light"]["hours"] == 10 and week["by_class"]["heavy"]["hours"] == 30)
check("share: heavy 75% of hours", abs(report.heavy_share(week) - 0.75) < 1e-9)
check("share: nothing run is 0%, not an error", report.heavy_share(t([])) == 0)

now = dt.datetime(2026, 11, 16, 0, 0, tzinfo=dt.timezone.utc)   # 15 days into a 30-day month
flat = {"usd": 7.0, "hours": 0, "by_class": {}}                    # US$1/day for the last 7 days
p = report.projection({"usd": 15.0, "hours": 0, "by_class": {}}, flat, now)
check("projection: month so far + 7-day rate for the rest", abs(p - (15 + 15)) < 1e-6, f"{p}")
early = dt.datetime(2026, 11, 2, 0, 0, tzinfo=dt.timezone.utc)    # 1 day in: rate over 1 day, not 7
p = report.projection({"usd": 2.0, "hours": 0, "by_class": {}}, {"usd": 2.0, "hours": 0, "by_class": {}}, early)
check("projection: early in the month, the rate is over the days so far", abs(p - (2 + 2 * 29)) < 1e-6, f"{p}")

heavy_week = {"hours": 40, "usd": 5, "by_class": {"heavy": {"hours": 30, "usd": 4}, "light": {"hours": 10, "usd": 1}}}
light_week = {"hours": 40, "usd": 5, "by_class": {"heavy": {"hours": 10, "usd": 2}, "light": {"hours": 30, "usd": 3}}}
a = report.alerts(heavy_week, heavy_week, 20)
check("warn: heavy over half and projected over US$15", [x["key"] for x in a] == ["heavy"], str(a))
check("warn: the title names it, for Kieron's phone", a and a[0]["title"].startswith("Kieron: spellbound3d is shredding"))
check("no warning: heavy over half but projected under US$15", report.alerts(heavy_week, heavy_week, 12) == [])
check("no warning: projected over US$15 but mostly light", report.alerts(light_week, light_week, 20) == [])
check("warn: on track for the US$25 stop, whatever the mix",
      [x["key"] for x in report.alerts(light_week, light_week, 27)] == ["budget"])
check("warn: both when both", [x["key"] for x in report.alerts(heavy_week, heavy_week, 27)] == ["heavy", "budget"])

s = report.summary(week, week, 12.34, now)
check("summary: a table with both classes and the projection", "| light | heavy |" in s
      and "4 vCPU" not in s
      and "US$12.34" in s, s)

# --- job fit: our suggestions about sizes
def job(cls="light", **kw):
    return {"class": cls, "workflow": "CI", "job": "check", "n": 10, "wall_s": 600, "wall_max_s": 900,
            "avg_vcpu": 1.0, "mem_peak_mb": 1000, "memory_mb": 3800, "cpu_stall_pct": 2, **kw}


check("fit: a job comfortably inside its machine gets no note", report.fit_notes([job()], []) == [])
n = report.fit_notes([job(mem_peak_mb=3500)], [])
check("fit: memory over 80% of the machine → too small?", len(n) == 1 and "Too small?" in n[0] and "3500 of 3800" in n[0], str(n))
n = report.fit_notes([job(mem_peak_mb=3500, anon_peak_mb=900)], [])
check("fit: a high peak that's mostly file cache isn't 'too small' (2026-11-27)", n == [], str(n))
n = report.fit_notes([job(mem_peak_mb=3700, anon_peak_mb=3500)], [])
check("fit: real memory over 80% → too small?, saying it's real use", len(n) == 1 and "real memory use" in n[0], str(n))
n = report.fit_notes([job(cpu_stall_pct=35)], [])
check("fit: CPU wait over 20% → CPU-starved?", len(n) == 1 and "CPU-starved?" in n[0], str(n))
n = report.fit_notes([job("heavy", avg_vcpu=0.6, memory_mb=7800)], [])
check("fit: heavy job barely using its CPUs → could be light?", len(n) == 1 and "Could be light?" in n[0], str(n))
check("fit: a light job using little CPU is fine", report.fit_notes([job(avg_vcpu=0.2)], []) == [])
n = report.fit_notes([job("heavy", avg_vcpu=0.95, memory_mb=7776, nproc=2)], [])
check("fit: a heavy job on a 2-vCPU machine isn't told it 'could be light' (2026-11-28)", n == [], str(n))
n = report.fit_notes([job("heavy", avg_vcpu=0.5, memory_mb=15701, nproc=4)], [])
check("fit: ...on a 4-vCPU machine it is, with the real count", len(n) == 1 and "of 4 " in n[0], str(n))
rows = report.fit_table([job("heavy", mem_peak_mb=4795, anon_peak_mb=1000, memory_mb=7776, nproc=2)], [])
check("fit table: shows real memory beside the cache-inclusive peak, and the vCPUs",
      any("1000 / 4795 of 7776 MB" in r and "of 2" in r for r in rows), "\n".join(rows))
check("fit: a heavy job using its CPUs is fine", report.fit_notes([job("heavy", avg_vcpu=3.2, memory_mb=7800)], []) == [])
n = report.fit_notes([job(wall_max_s=80 * 60)], [])
check("fit: near the 90-minute limit", len(n) == 1 and "90-minute" in n[0], str(n))
q = [{"label": "aws-light", "n": 20, "wait_avg": 60, "wait_p95": 240, "wait_max": 300}]
n = report.fit_notes([], q)
check("fit: jobs waiting over 2 minutes for a runner", len(n) == 1 and "Waiting for runners" in n[0], str(n))
check("fit: quick starts are fine", report.fit_notes([], [{**q[0], "wait_p95": 45}]) == [])
check("fit: unknown numbers (None) never crash or warn",
      report.fit_notes([job(avg_vcpu=None, mem_peak_mb=None, memory_mb=None, cpu_stall_pct=None, wall_max_s=None)],
                       [{"label": "x", "n": 1, "wait_avg": None, "wait_p95": None, "wait_max": None}]) == [])
s = report.summary(week, week, 1.0, now, [job("heavy", avg_vcpu=0.6, memory_mb=7800)], q)
check("summary: a job fit table with the notes", "Job fit, last 7 days" in s and "CI / check" in s
      and "Could be light?" in s and "Waiting for runners" in s, s)
check("summary: before any per-job records, says so", "No per-job records yet" in report.summary(week, week, 1.0, now))
for qtext in (report.JOBS_QUERY, report.QUEUE_QUERY):
    import re
    shadow = [m for m in re.finditer(r"\b\w+\((\w+)(?:,[^)]*)?\) as (\w+)", qtext) if m.group(1) == m.group(2)]
    check("queries: no alias reuses its field's name (it'd drop the field)", not shadow, qtext)

check("queries: runners that never got a job are left out of job fit", "ispresent(job)" in report.JOBS_QUERY)

print(f"\n{failed} failed")
sys.exit(1 if failed else 0)
