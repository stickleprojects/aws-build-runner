#!/usr/bin/env python3
"""Offline tests for the nightly runner curfew (lambda/curfew.py) and its CLI's wiring. No AWS.

    python3 -I part2-runner/stack/test_curfew.py
"""
import datetime as dt
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lambda"))  # python3 -I leaves it out
import curfew  # noqa: E402

failed = 0
SYD = ZoneInfo("Australia/Sydney")
H = 3600


def check(name: str, ok: bool, detail: str = "") -> None:
    global failed
    print(f"{'ok  ' if ok else 'FAIL'}  {name}{'' if ok else ': ' + detail}")
    failed += 0 if ok else 1


def at(y, mo, d, h, mi=0) -> float:
    return dt.datetime(y, mo, d, h, mi, tzinfo=SYD).timestamp()


def hhmm(epoch) -> str:
    return dt.datetime.fromtimestamp(epoch, SYD).strftime("%d %H:%M")


# --- the quiet hours
s = curfew.state(at(2026, 11, 17, 15), {})
check("afternoon: on, stops at 22:00 tonight", not s["active"] and hhmm(s["stop_at"]) == "17 22:00", str(s))
s = curfew.state(at(2026, 11, 17, 22, 30), {})
check("22:30: off until 07:00 tomorrow", s["active"] and s["reason"] == "quiet hours" and hhmm(s["until"]) == "18 07:00", str(s))
s = curfew.state(at(2026, 11, 18, 3), {})
check("03:00: still off, until 07:00 today", s["active"] and hhmm(s["until"]) == "18 07:00", str(s))
s = curfew.state(at(2026, 11, 18, 7), {})
check("07:00 exactly: on again", not s["active"] and hhmm(s["stop_at"]) == "18 22:00", str(s))
s = curfew.state(at(2026, 11, 17, 21, 59), {})
check("21:59: still on", not s["active"])
check("22:00 exactly: off", curfew.state(at(2026, 11, 17, 22), {})["active"])

# --- daylight saving (Sydney: AEDT ends 5 Apr 2026 03:00, starts 4 Oct 2026 02:00)
s = curfew.state(at(2026, 4, 4, 23), {})
check("DST ends overnight: off until 07:00 local", s["active"] and hhmm(s["until"]) == "05 07:00", str(s))
check("DST ends: that night is 10 h long", curfew.window(at(2026, 4, 4, 23))[1] - curfew.window(at(2026, 4, 4, 23))[0] == 10 * H)
check("DST starts: that night is 8 h long", curfew.window(at(2026, 10, 3, 23))[1] - curfew.window(at(2026, 10, 3, 23))[0] == 8 * H)

# --- the notice
check("no notice in the afternoon", curfew.notice_due(at(2026, 11, 17, 15), {}) is None)
n = curfew.notice_due(at(2026, 11, 17, 21), {})
check("notice at 21:00 for the 22:00 stop", n is not None and hhmm(n["stop_at"]) == "17 22:00", str(n))
check("notice at 21:30 too (the same stop; the workflow posts it once)",
      curfew.notice_due(at(2026, 11, 17, 21, 30), {}) == n)
check("no notice once stopped", curfew.notice_due(at(2026, 11, 17, 22, 5), {}) is None)

# --- extending
now = at(2026, 11, 17, 21, 10)
o = curfew.extend(now, {}, 1)
check("/extend 1h at 21:10: the stop moves to 23:00", hhmm(o["extend_until"]) == "17 23:00", str(o))
s = curfew.state(at(2026, 11, 17, 22, 30), o)
check("...so at 22:30 runners are on (extended)", not s["active"] and s["reason"] == "extended", str(s))
check("...and the next notice is due at 22:00 for 23:00",
      hhmm(curfew.notice_due(at(2026, 11, 17, 22, 0), o)["stop_at"]) == "17 23:00")
check("...and at 23:00 they stop", curfew.state(at(2026, 11, 17, 23), o)["active"])
o2 = curfew.extend(at(2026, 11, 17, 22, 15), o, 2)
check("/extend 2h on top: 01:00", hhmm(o2["extend_until"]) == "18 01:00", str(o2))
late = curfew.extend(at(2026, 11, 17, 23, 30), o, 1)
check("/extend 1h after the stop: on again until 00:30", hhmm(late["extend_until"]) == "18 00:30"
      and not curfew.state(at(2026, 11, 17, 23, 31), late)["active"], str(late))
morning = curfew.extend(at(2026, 11, 18, 6, 30), {}, 2)
check("never past 07:00", hhmm(morning["extend_until"]) == "18 07:00", str(morning))
check("extended to the morning: no notice", curfew.notice_due(at(2026, 11, 18, 6, 31), morning) is None)
check("an old extension doesn't carry over to the next night",
      curfew.state(at(2026, 11, 18, 22, 30), o)["active"])
for bad in (0, 3, 24):
    try:
        curfew.extend(now, {}, bad)
        check(f"/extend {bad}h refused", False, "accepted")
    except ValueError:
        check(f"/extend {bad}h refused (1 or 2 only)", True)

# --- stopping by hand
st = curfew.stop(at(2026, 11, 17, 15))
check("/stop in the afternoon: off now", curfew.state(at(2026, 11, 17, 15, 1), st)["active"])
check("...until 07:00 tomorrow", hhmm(st["stop_until"]) == "18 07:00", str(st))
check("...and on again at 07:00", not curfew.state(at(2026, 11, 18, 7), st)["active"])
check("/extend after /stop turns them back on",
      not curfew.state(at(2026, 11, 17, 15, 2), curfew.extend(at(2026, 11, 17, 15, 1), st, 1))["active"])

# --- the parameter
check("parse: missing or junk is no override", curfew.parse(None) == {} and curfew.parse("x") == {} and curfew.parse("[1]") == {})
check("parse: keeps only the two numbers", curfew.parse('{"extend_until": 5, "stop_until": "x", "other": 1}') == {"extend_until": 5})

# --- the stack's settings
cfg = json.loads((HERE / "config.json").read_text())
check("config: the curfew is 22:00-07:00 Sydney (Kieron, 2026-11-17)",
      cfg.get("Curfew") == {"start": "22:00", "end": "07:00", "tz": "Australia/Sydney"}, str(cfg.get("Curfew")))
template = (HERE / "template.yaml").read_text()
check("template: the launcher gets CURFEW and may read /build/runner/curfew",
      "CURFEW: !Ref Curfew" in template and "parameter/build/runner/curfew\"" in template)
runner = (HERE / "lambda" / "runner.py").read_text()
check("launcher: checks the curfew before launching anything",
      0 < runner.find("curfew.state(now, curfew_override(), CURFEW)") < runner.find("plan = launches_needed("))
ami = (HERE.parent.parent / ".github" / "workflows" / "ami.yml").read_text()
cron = ami.split('cron: "')[1].split('"')[0].split()
utc_hour = int(cron[1])
for name, offset in (("AEDT", 11), ("AEST", 10)):
    local_h = (utc_hour + offset) % 24
    check(f"the weekly image rebuild is outside the curfew ({name}: {local_h:02d}:{cron[0]})", 7 <= local_h < 22)

# --- warnings at T-15 and T-5 (Kieron, 2026-11-27); the launcher sends each key once
W = curfew.warning_due
check("warning: 21:30, none yet", W(at(2026, 11, 27, 21, 30), {}) is None)
check("warning: 21:45 is T-15", (W(at(2026, 11, 27, 21, 45), {}) or {}).get("stage") == 15)
check("warning: 21:52 (a late run) is still T-15", (W(at(2026, 11, 27, 21, 52), {}) or {}).get("stage") == 15)
check("warning: 21:55 is T-5", (W(at(2026, 11, 27, 21, 55), {}) or {}).get("stage") == 5)
check("warning: 22:00, the stop itself: none (curfew on)", W(at(2026, 11, 27, 22, 0), {}) is None)
check("warning: the two stages have different keys",
      W(at(2026, 11, 27, 21, 45), {})["key"] != W(at(2026, 11, 27, 21, 55), {})["key"])
o = curfew.extend(at(2026, 11, 27, 22, 14), {}, 1)
check("warning: extended to 23:14, T-15 at 22:59", hhmm(W(at(2026, 11, 27, 22, 59), o)["stop_at"]) == "27 23:14")
check("warning: ...with a new key, so it's sent again",
      W(at(2026, 11, 27, 22, 59), o)["key"] != W(at(2026, 11, 27, 21, 45), {})["key"])
check("warning: extended to the morning, none", W(at(2026, 11, 28, 6, 50), {"extend_until": at(2026, 11, 28, 7)}) is None)
check("warning: stopped by hand, none", W(at(2026, 11, 27, 15), curfew.stop(at(2026, 11, 27, 14))) is None)

print(f"\n{failed} failed")
sys.exit(1 if failed else 0)
