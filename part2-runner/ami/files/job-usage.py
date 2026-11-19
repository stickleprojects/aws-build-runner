#!/usr/bin/env python3
"""One record per job: how hard it worked this runner. Runs as root after the job, before power-off
(github-runner-jit.service, ExecStopPost), and sends one line to CloudWatch Logs /build/runner/jobs.

    job-usage.py             collect and send (failures are logged, never block the power-off)
    job-usage.py --dry-run   print the record, send nothing

The record: which job (from the job-completed hook's /run/build-runner/job-info.json), this instance's
class, type, vCPUs and memory (/etc/build-runner/instance.json), and from cgroup v2:
  - the runner service's memory peak and CPU time (the job's processes)
  - system.slice's memory peak and CPU time (everything on the box, containers included)
  - CPU and memory pressure: the share of the job's wall time that tasks stalled waiting for CPU or memory
The daily usage report (part2-runner/usage/report.py) turns these into "too small / too big" notes.

Standard library only. Credentials from the instance role via IMDSv2 (root only: jobs are blocked from
IMDS), signed by files/report.py's aws_call, installed beside this script.
"""
import json
import re
import sys
import time
import urllib.error
from pathlib import Path

CGROUP = Path("/sys/fs/cgroup")
SERVICE = CGROUP / "system.slice/github-runner-jit.service"
SYSTEM = CGROUP / "system.slice"
JOB_INFO = Path("/run/build-runner/job-info.json")
INSTANCE = Path("/etc/build-runner/instance.json")
PREWARM = Path("/run/build-runner-prewarm.json")  # prewarm.py's result (root's)
LOG_GROUP = "/build/runner/jobs"
# job-info.json is written by the job user, so only these fields, as short strings, are kept.
JOB_FIELDS = ("repository", "workflow", "job", "run_id", "run_attempt", "event", "started", "ended")


# ---------------------------------------------------------------------------- reading (pure-ish)

def read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def number(text: str) -> float | None:
    text = text.strip()
    return float(text) if re.fullmatch(r"\d+(\.\d+)?", text) else None


def cpu_seconds(cpu_stat: str) -> float | None:
    """cpu.stat 'usage_usec N' -> seconds."""
    m = re.search(r"^usage_usec (\d+)$", cpu_stat, re.M)
    return int(m.group(1)) / 1e6 if m else None


def stall_seconds(pressure: str) -> float | None:
    """cpu.pressure / memory.pressure 'some ... total=N' (microseconds) -> seconds."""
    m = re.search(r"^some .*\btotal=(\d+)", pressure, re.M)
    return int(m.group(1)) / 1e6 if m else None


def cgroup_stats(path: Path) -> dict:
    peak = number(read(path / "memory.peak"))
    return {"mem_peak_mb": round(peak / 2**20) if peak is not None else None,
            "cpu_s": cpu_seconds(read(path / "cpu.stat")),
            "cpu_stall_s": stall_seconds(read(path / "cpu.pressure")),
            "mem_stall_s": stall_seconds(read(path / "memory.pressure"))}


def clean_job(raw: str) -> dict:
    try:
        info = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(info, dict):
        return {}
    return {k: str(info[k])[:100] for k in JOB_FIELDS if k in info and info[k] is not None}


def record(service: dict, system: dict, instance: dict, job: dict, wall_s: float | None) -> dict:
    def pct(part, whole):
        return round(100 * part / whole, 1) if part is not None and whole else None
    rec = {"msg": "job", **job,
           "class": instance.get("class", "unknown"), "label": instance.get("label", "unknown"),
           "type": instance.get("instance_type", "unknown"), "instance": instance.get("instance_id", "unknown"),
           "nproc": instance.get("nproc"), "memory_mb": instance.get("memory_mb"),
           "wall_s": round(wall_s, 1) if wall_s is not None else None,
           "mem_peak_mb": service["mem_peak_mb"], "box_mem_peak_mb": system["mem_peak_mb"],
           "cpu_s": service["cpu_s"], "box_cpu_s": system["cpu_s"]}
    if wall_s:
        cpu = system["cpu_s"] if system["cpu_s"] is not None else service["cpu_s"]
        rec["avg_vcpu"] = round(cpu / wall_s, 2) if cpu is not None else None
        # Pressure is whole-box from system.slice when there, else the service: the share of wall time stalled.
        rec["cpu_stall_pct"] = pct(system["cpu_stall_s"] if system["cpu_stall_s"] is not None
                                   else service["cpu_stall_s"], wall_s)
        rec["mem_stall_pct"] = pct(system["mem_stall_s"] if system["mem_stall_s"] is not None
                                   else service["mem_stall_s"], wall_s)
    return rec


def prewarm_fields(raw: str, job: dict) -> dict:
    """The boot prewarm's time and size, and how long before the job started it finished (negative: it
    was still reading when the job began). Empty if it didn't run or hasn't finished."""
    try:
        p = json.loads(raw)
        out = {"prewarm_s": float(p["seconds"]), "prewarm_mb": float(p["mb"])}
    except (ValueError, KeyError, TypeError):
        return {}
    try:
        out["prewarm_lead_s"] = round(float(job["started"]) - float(p["ended"]), 1)
    except (ValueError, KeyError, TypeError):
        pass
    return out


def wall_seconds(job: dict) -> float | None:
    try:
        return max(0.0, float(job["ended"]) - float(job["started"]))
    except (KeyError, ValueError):
        return None


def collect() -> dict:
    try:
        instance = json.loads(read(INSTANCE) or "{}")
    except ValueError:
        instance = {}
    job = clean_job(read(JOB_INFO))
    if not job.get("started"):  # start-jit.sh touches it just before the runner starts
        try:
            job["started"] = str(int(JOB_INFO.with_name("job").stat().st_mtime))
        except OSError:
            pass
    if "ended" not in job:
        job["ended"] = str(int(time.time()))  # the hook didn't run (e.g. the job was cancelled)
    rec = record(cgroup_stats(SERVICE), cgroup_stats(SYSTEM), instance, job, wall_seconds(job))
    return {**rec, **prewarm_fields(read(PREWARM), job)}


# ---------------------------------------------------------------------------- sending

def logs_call(ctx, target, payload):
    from report import aws_call  # files/report.py, installed beside this script: IMDS creds + SigV4
    try:
        return aws_call(ctx["region"], ctx["creds"], "logs", f"Logs_20140328.{target}", payload)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        if "ResourceAlreadyExistsException" in detail:
            return {}
        raise RuntimeError(f"{target}: HTTP {e.code} {detail[:300]}") from None


def send(rec: dict) -> None:
    from report import instance_context
    ctx = instance_context()
    stream = ctx["instance"]
    logs_call(ctx, "CreateLogStream", {"logGroupName": LOG_GROUP, "logStreamName": stream})
    logs_call(ctx, "PutLogEvents", {
        "logGroupName": LOG_GROUP, "logStreamName": stream,
        "logEvents": [{"timestamp": int(time.time() * 1000), "message": json.dumps(rec)}]})


def main() -> int:
    rec = collect()
    print(json.dumps(rec))
    if "--dry-run" in sys.argv[1:]:
        return 0
    try:
        send(rec)
        print(f"job usage sent to {LOG_GROUP}")
    except Exception as e:  # never block the power-off
        print(f"job usage not sent: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
