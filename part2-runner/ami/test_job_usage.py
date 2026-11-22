#!/usr/bin/env python3
"""Offline tests for the per-job usage record (ami/files/job-usage.py): cgroup parsing, the record, and the
job-completed hook's file. No AWS, no root; a fake cgroup tree in a temp dir.

    python3 -I part2-runner/ami/test_job_usage.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("job_usage", HERE / "files" / "job-usage.py")
ju = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ju)

failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failed
    print(f"{'ok  ' if ok else 'FAIL'}  {name}{'' if ok else ': ' + detail}")
    failed += 0 if ok else 1


CPU_STAT = "usage_usec 120000000\nuser_usec 100000000\nsystem_usec 20000000\n"
PRESSURE = "some avg10=0.00 avg60=0.00 avg300=0.00 total=30000000\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=1000\n"

check("cpu.stat: usage in seconds", ju.cpu_seconds(CPU_STAT) == 120.0)
check("cpu.stat: missing is None", ju.cpu_seconds("") is None)
check("pressure: the 'some' total, in seconds (not 'full')", ju.stall_seconds(PRESSURE) == 30.0)
check("pressure: missing is None", ju.stall_seconds("") is None)
check("number: 'max' or junk is None", ju.number("max\n") is None and ju.number("123\n") == 123)

with tempfile.TemporaryDirectory() as d:
    cg = Path(d)
    (cg / "memory.peak").write_text(str(3 * 2**30) + "\n")
    (cg / "cpu.stat").write_text(CPU_STAT)
    (cg / "cpu.pressure").write_text(PRESSURE)
    (cg / "memory.pressure").write_text(PRESSURE.replace("30000000", "6000000"))
    stats = ju.cgroup_stats(cg)
    check("cgroup: peak memory in MB", stats["mem_peak_mb"] == 3072, str(stats))
    check("cgroup: a missing cgroup gives Nones, not an error",
          ju.cgroup_stats(cg / "nope") == {"mem_peak_mb": None, "cpu_s": None, "cpu_stall_s": None, "mem_stall_s": None})

    instance = {"instance_type": "c7i.large", "instance_id": "i-1", "nproc": 2, "memory_mb": 3800,
                "label": "aws-light", "class": "light"}
    job = ju.clean_job(json.dumps({"workflow": "CI", "job": "check", "run_id": "5", "started": "1000", "ended": "1300",
                                   "secret": "x", "repository": "o/r" * 100}))
    check("job info: only the known fields", "secret" not in job and job["job"] == "check", str(job))
    check("job info: values capped at 100 characters (the job user writes it)", len(job["repository"]) == 100)
    check("job info: junk is an empty record, not an error", ju.clean_job("not json") == {} and ju.clean_job("[1]") == {})
    check("wall time from the hook's start and end", ju.wall_seconds(job) == 300)
    check("wall time unknown without a start", ju.wall_seconds({"ended": "5"}) is None)

    rec = ju.record(stats, stats, instance, job, 300.0)
    check("record: says which job, class and machine",
          rec["msg"] == "job" and rec["job"] == "check" and rec["class"] == "light" and rec["type"] == "c7i.large"
          and rec["nproc"] == 2 and rec["memory_mb"] == 3800, str(rec))
    check("record: average busy vCPUs = CPU seconds / wall", rec["avg_vcpu"] == 0.4, str(rec))
    check("record: CPU wait as a share of wall time", rec["cpu_stall_pct"] == 10.0 and rec["mem_stall_pct"] == 2.0, str(rec))
    check("record: the whole box's memory peak too (containers aren't in the service)", rec["box_mem_peak_mb"] == 3072)
    empty = {"mem_peak_mb": None, "cpu_s": None, "cpu_stall_s": None, "mem_stall_s": None}
    rec = ju.record(empty, empty, {}, {}, None)
    check("record: nothing known still makes a record", rec["msg"] == "job" and rec["class"] == "unknown"
          and rec["wall_s"] is None, str(rec))
    check("record: is one log line of JSON", "\n" not in json.dumps(rec))

# The job-completed hook writes the job-info file that the record reads.
with tempfile.TemporaryDirectory() as d:
    hook = (HERE / "files" / "job-completed.sh").read_text().replace("/run/build-runner", d)
    Path(d, "job").touch()
    env = {**os.environ, "GITHUB_WORKFLOW": "CI", "GITHUB_JOB": "check", "GITHUB_RUN_ID": "5",
           "GITHUB_REPOSITORY": "stickleprojects/spellbound3d"}
    r = subprocess.run(["bash", "-c", hook], env=env, capture_output=True, text=True)
    info = ju.clean_job(Path(d, "job-info.json").read_text()) if Path(d, "job-info.json").exists() else {}
    check("hook: exits 0", r.returncode == 0, r.stderr)
    check("hook: notes the job", info.get("workflow") == "CI" and info.get("job") == "check", str(info))
    check("hook: start and end times, so the record has a wall time", ju.wall_seconds(info) is not None, str(info))
    r = subprocess.run(["bash", "-c", hook.replace(d, "/nonexistent/forbidden")], env=env, capture_output=True, text=True)
    check("hook: never fails the job, even if it can't write", r.returncode == 0, r.stderr)

# A runner that sat ready before its job came (2026-11-22: another job took the runner launched for it).
with tempfile.TemporaryDirectory() as d:
    hook = (HERE / "files" / "job-completed.sh").read_text().replace("/run/build-runner", d)
    Path(d, "job").touch()
    ready = int(Path(d, "job").stat().st_mtime) - 240
    os.utime(Path(d, "job"), (ready, ready))
    Path(d, "job-started").write_text(f"{ready + 240}\n")
    r = subprocess.run(["bash", "-c", hook], env=env, capture_output=True, text=True)
    info = ju.clean_job(Path(d, "job-info.json").read_text())
    check("hook: the wall time starts at the job's real start, not the runner's",
          ju.wall_seconds(info) is not None and ju.wall_seconds(info) < 60, str(info))
    check("hook: the idle wait before the job is kept apart", 230 <= (ju.idle_seconds(info) or 0) <= 250, str(info))
    Path(d, "job-started").write_text("junk")
    subprocess.run(["bash", "-c", hook], env=env, capture_output=True, text=True)
    info = ju.clean_job(Path(d, "job-info.json").read_text())
    check("hook: a junk start note falls back to the runner's start", info.get("started") == str(ready), str(info))

# The job-started hook writes that note only for a job it allows.
with tempfile.TemporaryDirectory() as d:
    t = Path(d)
    (t / "actors.txt").write_text("stickleprojects\n")
    (t / "repos.txt").write_text("stickleprojects/spellbound3d\n")
    (t / "event.json").write_text("{}")
    base = {"PATH": "/usr/bin:/bin", "RUNNER_HOOK_ACTORS_FILE": str(t / "actors.txt"),
            "RUNNER_HOOK_REPOS_FILE": str(t / "repos.txt"), "RUNNER_HOOK_STATE_DIR": d,
            "GITHUB_REPOSITORY": "stickleprojects/spellbound3d", "GITHUB_EVENT_NAME": "push",
            "GITHUB_EVENT_PATH": str(t / "event.json")}
    started_hook = str(HERE / "files" / "job-started.sh")
    r = subprocess.run(["bash", started_hook], env={**base, "GITHUB_ACTOR": "someone-else"}, capture_output=True, text=True)
    check("started hook: a refused job leaves no start note", r.returncode != 0 and not (t / "job-started").exists())
    r = subprocess.run(["bash", started_hook], env={**base, "GITHUB_ACTOR": "stickleprojects"}, capture_output=True, text=True)
    note = (t / "job-started").read_text().strip() if (t / "job-started").exists() else ""
    check("started hook: an allowed job notes its start time", r.returncode == 0 and note.isdigit(), r.stderr + note)
    r = subprocess.run(["bash", started_hook], env={**base, "GITHUB_ACTOR": "stickleprojects",
                                                    "RUNNER_HOOK_STATE_DIR": "/nonexistent/forbidden"},
                       capture_output=True, text=True)
    check("started hook: can't write the note, still allows the job", r.returncode == 0, r.stderr)

check("idle time unknown without the runner's start", ju.idle_seconds({"started": "5"}) is None)

service = (HERE / "files" / "github-runner-jit.service").read_text()
usage_at = service.find("job-usage.py")
check("service: the record is sent before the power-off",
      0 < usage_at < service.find('shutdown -h now "build-runner: job done"'))
check("service: a failing record never blocks the power-off (ExecStopPost=-, with a timeout)",
      "ExecStopPost=-+" in service and "timeout 20 /opt/github-runners/job-usage.py" in service)

print(f"\n{failed} failed")
sys.exit(1 if failed else 0)
