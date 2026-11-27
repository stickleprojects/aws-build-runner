"""The nightly runner curfew: when AWS runners may run, as pure functions (no AWS, no clock of its own).

Every night from START to END (local time in TZ; default 22:00-07:00 Sydney) no runner launches and the launcher
terminates any that are running. Kieron gets a notice an hour before (.github/workflows/curfew.yml) and can push
the stop back by 1 or 2 hours at a time (`/extend 1h`), or stop everything now until the morning (`/stop`).

The overrides are one SSM parameter, /build/runner/curfew: {"extend_until": epoch, "stop_until": epoch}.
  - stop_until in the future: curfew, whatever the hour (a manual stop; cleared by `resume`)
  - extend_until in the future: no curfew until then, even inside the quiet hours
Used by the launcher (runner.py), the curfew workflow and the runner-curfew skill (part2-runner/curfew/curfew.py).
"""
import datetime as dt
import json
from zoneinfo import ZoneInfo

DEFAULT = {"start": "22:00", "end": "07:00", "tz": "Australia/Sydney"}
PARAMETER = "/build/runner/curfew"
EXTENSIONS = (1, 2)       # hours Kieron can extend by, each time
NOTICE_BEFORE = 60 * 60   # the notice goes out this long before the stop
WARN_AT = (15, 5)         # minutes before a stop: the launcher's warnings, only while jobs run or wait (runner.py)


def _at(day: dt.date, hhmm: str, tz: ZoneInfo) -> dt.datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return dt.datetime(day.year, day.month, day.day, h, m, tzinfo=tz)


def window(now: float, cfg: dict = DEFAULT) -> tuple[float, float]:
    """The quiet hours that are on now, or else the next ones: (start, end) as epoch seconds."""
    tz = ZoneInfo(cfg["tz"])
    local = dt.datetime.fromtimestamp(now, tz)
    today = local.date()
    for start_day in (today - dt.timedelta(days=1), today):
        start = _at(start_day, cfg["start"], tz)
        end_day = start_day + dt.timedelta(days=1) if cfg["end"] <= cfg["start"] else start_day
        end = _at(end_day, cfg["end"], tz)
        if start.timestamp() <= now < end.timestamp() or start.timestamp() > now:
            return start.timestamp(), end.timestamp()
    start = _at(today + dt.timedelta(days=1), cfg["start"], tz)
    end_day = start.date() + dt.timedelta(days=1) if cfg["end"] <= cfg["start"] else start.date()
    return start.timestamp(), _at(end_day, cfg["end"], tz).timestamp()


def parse(override: str | None) -> dict:
    try:
        o = json.loads(override or "{}")
    except ValueError:
        return {}
    if not isinstance(o, dict):
        return {}
    return {k: float(o[k]) for k in ("extend_until", "stop_until") if isinstance(o.get(k), (int, float))}


def state(now: float, override: dict, cfg: dict = DEFAULT) -> dict:
    """{active, reason, until (when that changes), stop_at (the next stop, while not active)}."""
    start, end = window(now, cfg)
    stop_until, extend_until = override.get("stop_until", 0), override.get("extend_until", 0)
    if stop_until > now:
        return {"active": True, "reason": "stopped by hand", "until": stop_until}
    in_quiet = start <= now < end
    if in_quiet and extend_until > now:
        # Extended to the morning: no stop tonight (stop_at None), so no notice either.
        return {"active": False, "reason": "extended", "until": extend_until,
                "stop_at": extend_until if extend_until < end else None}
    if in_quiet:
        return {"active": True, "reason": "quiet hours", "until": end}
    # Before tonight's curfew: an extension already given for tonight moves the stop.
    stop_at = extend_until if start < extend_until < end else start
    return {"active": False, "reason": "open", "until": stop_at, "stop_at": stop_at}


def extend(now: float, override: dict, hours: int, cfg: dict = DEFAULT) -> dict:
    """The new override after `/extend Nh`: N hours past tonight's stop (or past now, once stopped)."""
    if hours not in EXTENSIONS:
        raise ValueError(f"extend by {' or '.join(map(str, EXTENSIONS))} hours")
    s = state(now, override, cfg)
    start, end = window(now, cfg)
    base = now if s["active"] else (s["stop_at"] or end)
    until = min(base + hours * 3600, end)  # past the morning there's nothing to extend
    return {"extend_until": until}         # an extension also ends a manual stop


def stop(now: float, cfg: dict = DEFAULT) -> dict:
    """Stop now, until the end of the current or next quiet hours (the morning)."""
    start, end = window(now, cfg)
    return {"stop_until": end}


def notice_due(now: float, override: dict, cfg: dict = DEFAULT) -> dict | None:
    """{stop_at} when a notice should go out now (within NOTICE_BEFORE of a stop), else None."""
    s = state(now, override, cfg)
    if s["active"] or not s["stop_at"] or not (now < s["stop_at"] <= now + NOTICE_BEFORE):
        return None
    return {"stop_at": s["stop_at"]}


def warning_due(now: float, override: dict, cfg: dict = DEFAULT) -> dict | None:
    """{stop_at, stage, key} when a stop is at most 15 min away (stage 15) or at most 5 min (stage 5), else None.
    Each stage covers the whole span up to the next, so a late launcher run still sends it; the caller sends each
    key once. An extension moves stop_at, so it gets its own warnings."""
    s = state(now, override, cfg)
    if s["active"] or not s.get("stop_at"):
        return None
    left = s["stop_at"] - now
    stage = next((m for m in sorted(WARN_AT) if 0 < left <= m * 60), None)
    if stage is None:
        return None
    return {"stop_at": s["stop_at"], "stage": stage, "key": f"{int(s['stop_at'])}:{stage}"}


def local(epoch: float, cfg: dict = DEFAULT) -> str:
    return dt.datetime.fromtimestamp(epoch, ZoneInfo(cfg["tz"])).strftime("%H:%M %a %d %b")
