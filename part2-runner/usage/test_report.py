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
check("summary: a table with both classes and the projection", "light (2 vCPU)" in s and "heavy (4 vCPU)" in s
      and "US$12.34" in s, s)

print(f"\n{failed} failed")
sys.exit(1 if failed else 0)
