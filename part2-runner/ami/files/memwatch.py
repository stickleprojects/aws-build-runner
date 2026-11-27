#!/usr/bin/env python3
"""build-runner memwatch: the job's real memory peak, for its usage record (job-usage.py).

cgroup v2's memory.peak counts file cache too (the boot prewarm alone reads ~2 GB into it), so a job could look
like it needed 8.9 GB when it needed under 2 (2026-11-27, spellbound3d 0079 and 0083). Anonymous memory is what
the job's processes actually hold. The kernel keeps no peak of it, so this samples system.slice's memory.stat
`anon` every INTERVAL seconds and writes the highest since the job started (the job-started hook's note) to OUT.

Runs as root from boot (build-runner-memwatch.service). Standard library only. Reads two files; writes one.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

STAT = Path("/sys/fs/cgroup/system.slice/memory.stat")
JOB_STARTED = Path("/run/build-runner/job-started")  # written by the job-started hook (the job's user)
OUT = Path("/run/build-runner-memwatch.json")  # root's; job-usage.py reads it
INTERVAL = 2.0


def anon_bytes(stat: str) -> int | None:
    m = re.search(r"^anon (\d+)$", stat, re.M)
    return int(m.group(1)) if m else None


def job_start(note: str) -> float | None:
    note = note.strip()
    return float(note) if note.isdigit() else None


def step(state: dict, anon: int | None, started: float | None, now: float) -> dict:
    """The next state: the highest anon since boot, and since the job started. Pure, so it's tested offline."""
    if anon is None:
        return state
    state = {**state, "boot_peak_mb": max(state.get("boot_peak_mb", 0), round(anon / 2**20)), "updated": int(now)}
    if started is not None and now >= started:
        if state.get("job_started") != int(started):  # a new job: start its peak afresh
            state["job_started"], state["job_peak_mb"] = int(started), 0
        state["job_peak_mb"] = max(state.get("job_peak_mb", 0), round(anon / 2**20))
    return state


def read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def main() -> int:
    state: dict = {}
    while True:
        state = step(state, anon_bytes(read(STAT)), job_start(read(JOB_STARTED)), time.time())
        tmp = OUT.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(state))
            os.replace(tmp, OUT)
        except OSError as e:
            print(f"memwatch: can't write {OUT}: {e}", file=sys.stderr)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
