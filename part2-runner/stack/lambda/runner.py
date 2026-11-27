"""The build-runner Lambda functions (stack build-runner, part2-runner/stack/template.yaml).

webhook   behind a public function URL. Checks GitHub's HMAC signature, keeps only spellbound3d
          workflow_job "queued" events for one of our labels, and hands off to the launcher (asynchronously,
          so GitHub gets its answer well inside its 10 s limit).
launcher  reconciles, one run at a time (an SSM-parameter lock: this account's Lambda concurrency is
          the minimum 10, so none can be reserved): per label, counts the queued jobs against the runners
          that haven't taken a job yet, and launches the difference, up to MAX_RUNNERS live instances
          in all. Each instance gets a just-in-time (single-job) runner registration in its user
          data; the image runs that one job and terminates itself.
          Every 5 minutes (EventBridge Scheduler) it also reconciles (catching missed webhooks) and reaps:
          instances older than MAX_AGE, runners idle for IDLE_MAX with nothing queued, and runner
          registrations whose instance is gone.

GitHub assigns a queued job to any idle runner with matching labels, so a runner can't be reserved for
one job. Sizes are therefore per LABEL (a pool), never per job: aws-light (2 vCPU, Claude jobs)
and aws-heavy (4 vCPU, make check). See PLAN.md, "Instance types".
"""
import base64
import contextlib
import datetime as dt
import functools
import hashlib
import hmac
import json
import os
import random
import secrets
import time

import curfew
import ghapp
import pools as pools_mod

REPO = os.environ.get("REPO", "stickleprojects/spellbound3d")
# Label -> instance types, tried in order: every spot type, then on-demand. A JIT runner has exactly the
# one label it's registered with (seen 2026-11-16: no self-hosted, OS or architecture added). Only ours,
# on purpose: with "self-hosted" it could take jobs meant for the local runner (SDLC_RUNNER=self-hosted is
# the rollback), and the two must never compete.
DEFAULT_POOLS = {
    "aws-light": {"class": "light", "spot": ["c7i.large", "t3.large"], "on_demand": ["c7i.large"]},
    "aws-heavy": {"class": "heavy", "spot": ["c7i.xlarge", "c6i.xlarge", "m7i.xlarge"],
                               "on_demand": ["c7i.xlarge"]},
    # Consumer-qualified labels (Kieron, 2026-11-18: chosen before the cut-over, so never a rename). The old
    # aws-light/aws-heavy stay live alongside until spellbound3d's cut-over has settled; then they go.
    "aws-spellbound3d-light": {"class": "light", "spot": ["c7i.large", "t3.large"], "on_demand": ["c7i.large"]},
    "aws-spellbound3d-heavy": {"class": "heavy", "spot": ["c7i.xlarge", "c6i.xlarge", "m7i.xlarge"],
                               "on_demand": ["c7i.xlarge"]},
}
POOLS = {k.lower(): v for k, v in json.loads(os.environ.get("POOLS") or json.dumps(DEFAULT_POOLS)).items()}
NAME_PREFIX = "aws-"
GROUP = os.environ.get("RESOURCE_GROUP", "build-runner")
ROLE_TAG = "build-runner-role"
MAX_RUNNERS = int(os.environ.get("MAX_RUNNERS", "4"))
MAX_AGE = int(os.environ.get("MAX_AGE_MINUTES", "90")) * 60
IDLE_MAX = int(os.environ.get("IDLE_MINUTES", "20")) * 60
BOOT_GRACE = 5 * 60   # an instance whose runner isn't listed yet counts as booting for this long
SUBNETS = [s for s in os.environ.get("SUBNETS", "").split(",") if s]
LAUNCH_TEMPLATE = os.environ.get("LAUNCH_TEMPLATE_ID", "")
LAUNCHER = os.environ.get("LAUNCHER_FUNCTION", "build-runner-launcher")
# Quiet hours (curfew.py): no runners overnight unless Kieron extends. {"start", "end", "tz"}.
CURFEW = {**curfew.DEFAULT, **json.loads(os.environ.get("CURFEW") or "{}")}
LOCK = "/build/runner/launcher-lock"
LOCK_STALE = 150      # longer than the launcher's 120 s timeout
# What recent runs launched, written under the lock. DescribeInstances lags RunInstances, so the next run
# counts these as booting until EC2 lists them (2026-11-18, #47: a second heavy instance for one job).
LAUNCHES = "/build/runner/launches"
# Curfew warnings (Kieron, 2026-11-27): at T-15 and T-5 before a stop, while jobs run or wait, a Telegram message
# to Kieron through the "AWS daily check" bot, so he can extend. The last warning sent, so each goes once.
CURFEW_WARNED = "/build/runner/curfew-warned"
TELEGRAM_TOKEN = "/build/telegram/bot-token"   # SecureString
TELEGRAM_CHAT = "/build/telegram/chat-id"      # SecureString
CURFEW_ISSUE = "https://github.com/stickleprojects/aws-build-runner/issues?q=is%3Aissue+is%3Aopen+label%3Arunner-curfew"
TOKEN_PERMISSIONS = {"administration": "write", "actions": "read", "contents": "read"}  # contents: pools.json
# The consumer's pools (#54, phase 1): spellbound3d's tools/runner-image/pools.json on main, read at most every
# POOLS_TTL, checked against BOUNDARY (the cost boundary from policy.yaml, passed by deploy.sh). A refused or
# unreachable copy keeps the last good one (SSM POOLS_LAST_GOOD); POOLS_STATUS says which, for the daily check.
# Until the consumer's file exists, config.json's Pools (env POOLS) are the starting copy.
CONSUMER_POOLS = os.environ.get("CONSUMER_POOLS", "tools/runner-image/pools.json")
POOLS_LAST_GOOD = "/build/runner/pools"
POOLS_STATUS = "/build/runner/pools-status"
POOLS_TTL = 600
DEFAULT_BOUNDARY = {"instance_types": ["t3.*", "t3a.*", "m*.large", "m*.xlarge", "c*.large", "c*.xlarge"],
                    "instance_sizes": ["micro", "small", "medium", "large", "xlarge"],
                    "max_runners": 4, "max_instance_minutes": 90, "max_idle_minutes": 20}
BOUNDARY = json.loads(os.environ.get("BOUNDARY") or json.dumps(DEFAULT_BOUNDARY))
_pools_cache = {"at": 0.0, "checked": None, "ssm_at": 0.0}
# Try the next type / subnet on these; anything else is a real error.
CAPACITY_ERRORS = {"InsufficientInstanceCapacity", "InsufficientCapacity", "SpotMaxPriceTooLow",
                   "MaxSpotInstanceCountExceeded", "Unsupported", "InsufficientFreeAddressesInSubnet",
                   "VcpuLimitExceeded"}


@functools.cache
def aws(service: str):
    import boto3  # in the Lambda runtime; imported lazily so the tests run without it
    return boto3.client(service)


def error_code(e: Exception) -> str:
    return (getattr(e, "response", None) or {}).get("Error", {}).get("Code", "")


@functools.cache
def parameter(name: str) -> str:
    return aws("ssm").get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]


def curfew_override() -> dict:
    """/build/runner/curfew, or {} when it doesn't exist (no extension, no manual stop). Read fresh on every run:
    parameter() is cached for the life of a warm Lambda, which kept a resumed curfew "extended" (2026-11-17)."""
    try:
        return curfew.parse(aws("ssm").get_parameter(Name=curfew.PARAMETER)["Parameter"]["Value"])
    except Exception as e:  # ParameterNotFound
        if error_code(e) == "ParameterNotFound":
            return {}
        raise


def log(msg: str, **fields) -> None:
    print(json.dumps({"msg": msg, **fields}, default=str))


# ---------------------------------------------------------------------------- webhook

def signature_ok(secret: str, body: bytes, header: str) -> bool:
    if not secret or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):])


def pool_for(labels) -> str | None:
    """The pool whose runners can take a job with these labels: exactly one of ours, nothing else."""
    wanted = {str(l).lower() for l in labels or []}
    return next(iter(wanted)) if len(wanted) == 1 and wanted <= POOLS.keys() else None


def wanted_event(kind: str, payload: dict) -> str | None:
    """None if we act on it, else why it's ignored."""
    if kind != "workflow_job":
        return f"event {kind}"
    if payload.get("action") != "queued":
        return f"action {payload.get('action')}"
    if (payload.get("repository") or {}).get("full_name") != REPO:
        return "another repository"
    if not pool_for((payload.get("workflow_job") or {}).get("labels")):
        return "not one of our labels"
    return None


def completed_record(kind: str, payload: dict) -> dict | None:
    """A finished job on one of our labels -> its queue wait and run time, for the usage report. Else None."""
    job = payload.get("workflow_job") or {}
    if kind != "workflow_job" or payload.get("action") != "completed" \
            or (payload.get("repository") or {}).get("full_name") != REPO or not pool_for(job.get("labels")):
        return None

    def seconds(a, b):
        try:
            return round((dt.datetime.fromisoformat(job[b]) - dt.datetime.fromisoformat(job[a])).total_seconds())
        except (KeyError, TypeError, ValueError):
            return None
    pool = pool_for(job.get("labels"))
    return {"job_id": job.get("id"), "label": pool, "class": POOLS[pool].get("class", "unknown"),
            "workflow": job.get("workflow_name"), "job": job.get("name"), "run_id": job.get("run_id"),
            "conclusion": job.get("conclusion"), "runner": job.get("runner_name"),
            "queue_s": seconds("created_at", "started_at"), "run_s": seconds("started_at", "completed_at")}


def response(status: int, text: str) -> dict:
    return {"statusCode": status, "headers": {"Content-Type": "text/plain"}, "body": text + "\n"}


def webhook(event, context):
    body = event.get("body") or ""
    raw = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if not signature_ok(parameter("/build/github/webhook-secret"), raw, headers.get("x-hub-signature-256", "")):
        log("rejected: bad signature")
        return response(401, "bad signature")
    kind = headers.get("x-github-event", "")
    webhook_pools(time.time())
    if kind == "ping":
        return response(200, "pong")
    try:
        payload = json.loads(raw)
    except ValueError:
        return response(400, "not JSON")
    done = completed_record(kind, payload)
    if done:
        log("job-completed", **done)
        return response(202, "recorded")
    why = wanted_event(kind, payload)
    if why:
        return response(202, f"ignored: {why}")
    job = payload["workflow_job"]
    pool = pool_for(job.get("labels"))
    log("queued", job_id=job.get("id"), pool=pool, workflow=job.get("workflow_name"), job=job.get("name"))
    aws("lambda").invoke(FunctionName=LAUNCHER, InvocationType="Event",
                         Payload=json.dumps({"source": "webhook", "job_id": job.get("id"), "pool": pool}).encode())
    return response(202, "launching")


# ---------------------------------------------------------------------------- planning (pure)

def unclaimed(instances: list[dict], runners: list[dict], now: float) -> list[dict]:
    """Live instances whose runner hasn't taken a job: registered but not busy, or still booting."""
    by_name = {r["name"]: r for r in runners}
    out = []
    for i in instances:
        r = by_name.get(i["name"])
        if r is not None:
            if not r.get("busy"):
                out.append(i)
        elif now - i["launched"] < BOOT_GRACE:
            out.append(i)
        # Not listed and older: its single job is done (JIT runners deregister), it's powering off.
    return out


REGISTRATION_LAG = 60  # GitHub's runner list may not show a just-made registration for this long


def with_recent_launches(instances: list[dict], recent: list[dict], now: float,
                         runner_names: set[str] | None = None) -> list[dict]:
    """Live instances plus recent launches EC2 doesn't list yet (each counts until BOOT_GRACE).

    A recent launch whose runner is no longer registered has finished its one job (JIT runners deregister when
    it ends), so it doesn't count, however recent: a 12-second job ended inside BOOT_GRACE, and the gone instance
    held back the next job's launch for ~2.5 min (2026-11-18). REGISTRATION_LAG covers GitHub's list lagging."""
    seen = {i["id"] for i in instances}
    return instances + [r for r in recent if r["id"] not in seen and now - r["launched"] < BOOT_GRACE
                        and (runner_names is None or r.get("name") in runner_names
                             or now - r["launched"] < REGISTRATION_LAG)]


def recent_launches_to_keep(recent: list[dict], new: list[dict], now: float) -> list[dict]:
    """The record to write back: this run's launches and earlier ones still inside BOOT_GRACE."""
    return [r for r in recent + new if now - r["launched"] < BOOT_GRACE]


def launches_needed(queued: list[dict], instances: list[dict], runners: list[dict], now: float,
                    cap: int = MAX_RUNNERS) -> dict[str, int]:
    """Launches per pool. Each pool covers only its own queue (a 2-vCPU runner can't take a check job);
    the cap is for all pools together, filled oldest queued job first."""
    spare = {}
    for i in unclaimed(instances, runners, now):
        spare[i["pool"]] = spare.get(i["pool"], 0) + 1
    room = cap - len(instances)
    out = {}
    for job in sorted(queued, key=lambda j: str(j.get("created_at", ""))):
        p = job["pool"]
        if spare.get(p, 0) > 0:
            spare[p] -= 1
        elif room > 0:
            out[p] = out.get(p, 0) + 1
            room -= 1
    return out


def reap_plan(queued: list[dict], instances: list[dict], runners: list[dict], now: float) -> dict:
    """What to clean up: {"terminate": [instance], "idle": [instance], "stale_runners": [runner]}."""
    by_name = {r["name"]: r for r in runners}
    old = [i for i in instances if now - i["launched"] > MAX_AGE]
    busy_pools = {j["pool"] for j in queued}
    idle = [i for i in unclaimed(instances, runners, now)
            if i["pool"] not in busy_pools and i not in old and i["name"] in by_name
            and now - i["launched"] > IDLE_MAX]
    live = {i["name"] for i in instances}
    stale = [r for r in runners if r["name"].startswith(NAME_PREFIX) and r["name"] not in live
             and r.get("status") == "offline" and not r.get("busy")]
    return {"terminate": old, "idle": idle, "stale_runners": stale}


def attempts(pool: str) -> list[tuple[str, bool]]:
    """(instance type, spot?) in the order tried: every spot type, then on-demand."""
    p = POOLS[pool]
    return [(t, True) for t in p.get("spot", [])] + [(t, False) for t in p.get("on_demand", [])]


# ---------------------------------------------------------------------------- AWS and GitHub

class GitHub:
    def __init__(self, token: str):
        self.auth = f"token {token}"

    def call(self, method: str, path: str, body: dict | None = None, ok=(200, 201, 204)):
        status, data = ghapp.request(method, path, self.auth, body)
        if status not in ok:
            raise RuntimeError(f"GitHub {method} {path.split('?')[0]}: HTTP {status}")
        return status, data

    def paged(self, path: str, key: str, pages: int = 5) -> list[dict]:
        out = []
        for page in range(1, pages + 1):
            _, data = self.call("GET", f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
            items = (data or {}).get(key, [])
            out += items
            if len(items) < 100:
                break
        return out


def bootstrap_pools() -> dict:
    """config.json's pools and limits (deploy-time), until the consumer's file has been read once."""
    return {"pools": {k: {"class": v.get("class", "unknown"), "spot": v.get("spot", []), "on_demand": v.get("on_demand", [])}
                      for k, v in POOLS.items()},
            "limits": {"max_runners": MAX_RUNNERS, "max_instance_minutes": MAX_AGE // 60, "idle_minutes": IDLE_MAX // 60}}


def use_pools(checked: dict) -> None:
    """Puts a checked copy in force for this run: the pools and the limits."""
    global POOLS, MAX_RUNNERS, MAX_AGE, IDLE_MAX
    lim = pools_mod.effective(checked, BOUNDARY)
    POOLS = checked["pools"]
    MAX_RUNNERS, MAX_AGE, IDLE_MAX = lim["max_runners"], lim["max_instance_minutes"] * 60, lim["idle_minutes"] * 60


def last_good_pools(ssm) -> dict | None:
    try:
        return json.loads(ssm.get_parameter(Name=POOLS_LAST_GOOD)["Parameter"]["Value"]) or None
    except Exception as e:
        if error_code(e) != "ParameterNotFound":
            log("couldn't read the last good pools", error=error_code(e) or type(e).__name__)
        return None


def consumer_pools(gh: GitHub) -> dict | None:
    """The consumer's pools.json on main, parsed; None if it doesn't exist (yet)."""
    status, data = gh.call("GET", f"/repos/{REPO}/contents/{CONSUMER_POOLS}?ref=main", ok=(200, 404))
    if status == 404:
        return None
    return json.loads(base64.b64decode(data["content"]))


def load_pools(gh: GitHub, now: float, ssm=None) -> dict:
    """The pools in force: the consumer's, if they pass the boundary; else the last good copy; else config.json's."""
    if _pools_cache["checked"] and now - _pools_cache["at"] < POOLS_TTL:
        return _pools_cache["checked"]
    ssm = ssm or aws("ssm")
    try:
        doc = consumer_pools(gh)
        if doc is None:
            raise FileNotFoundError(CONSUMER_POOLS)
        checked = pools_mod.validate(doc, BOUNDARY)
        status = {"state": "ok", "reason": ""}
        good = last_good_pools(ssm)
        if checked != good:
            ssm.put_parameter(Name=POOLS_LAST_GOOD, Value=json.dumps(checked), Type="String", Overwrite=True)
            log("pools updated from the consumer", repo=REPO, file=CONSUMER_POOLS, pools=sorted(checked["pools"]))
    except pools_mod.Invalid as e:
        status = {"state": "refused", "reason": str(e)[:900]}
    except FileNotFoundError:
        status = {"state": "missing", "reason": f"{REPO} has no {CONSUMER_POOLS} on main"}
    except Exception as e:  # GitHub down, bad JSON...: keep what we had
        status = {"state": "unreachable", "reason": f"{type(e).__name__}: {str(e)[:300]}"}
    if status["state"] != "ok":
        checked = last_good_pools(ssm) or bootstrap_pools()
        log("pools: keeping the last good copy", state=status["state"], reason=status["reason"])
    try:
        old = json.loads(ssm.get_parameter(Name=POOLS_STATUS)["Parameter"]["Value"])
    except Exception:
        old = {}
    if (old.get("state"), old.get("reason")) != (status["state"], status["reason"]):
        ssm.put_parameter(Name=POOLS_STATUS, Value=json.dumps({**status, "since": int(now)}), Type="String",
                          Overwrite=True)
    _pools_cache.update(at=now, checked=checked)
    return checked


def webhook_pools(now: float, ssm=None) -> None:
    """The webhook's view of the labels: the launcher's last good copy (re-read each minute), else config.json's."""
    if now - _pools_cache["ssm_at"] < 60:
        return
    _pools_cache["ssm_at"] = now
    good = last_good_pools(ssm or aws("ssm"))
    if good:
        use_pools(good)


def queued_jobs(gh: GitHub) -> list[dict]:
    """Queued jobs for our label, in any run that isn't finished (a run can be in progress with a
    later job still queued)."""
    jobs = {}
    for status in ("queued", "in_progress", "pending", "waiting", "requested"):
        for run in gh.paged(f"/repos/{REPO}/actions/runs?status={status}", "workflow_runs"):
            for job in gh.paged(f"/repos/{REPO}/actions/runs/{run['id']}/jobs?filter=latest", "jobs"):
                pool = pool_for(job.get("labels"))
                if job.get("status") == "queued" and pool:
                    jobs[job["id"]] = {**job, "pool": pool}
    return list(jobs.values())


def live_instances() -> list[dict]:
    pages = aws("ec2").get_paginator("describe_instances").paginate(Filters=[
        {"Name": "tag:resource-group", "Values": [GROUP]}, {"Name": f"tag:{ROLE_TAG}", "Values": ["runner"]},
        {"Name": "instance-state-name", "Values": ["pending", "running"]}])
    out = []
    for page in pages:
        for res in page["Reservations"]:
            for i in res["Instances"]:
                tags = {t["Key"]: t["Value"] for t in i.get("Tags", [])}
                out.append({"id": i["InstanceId"], "name": tags.get("build-runner-name", ""),
                            "runner_id": tags.get("github-runner-id", ""), "job_id": tags.get("github-job-id", ""),
                            "pool": tags.get("build-runner-label", ""),
                            "spot": i.get("InstanceLifecycle") == "spot",
                            "type": i["InstanceType"], "launched": i["LaunchTime"].timestamp()})
    return out


def tags(name: str, runner_id, job_id, pool: str) -> list[dict]:
    t = [{"Key": "resource-group", "Value": GROUP}, {"Key": "Name", "Value": f"build-runner {name}"},
         {"Key": ROLE_TAG, "Value": "runner"}, {"Key": "build-runner-name", "Value": name},
         {"Key": "github-runner-id", "Value": str(runner_id)}, {"Key": "github-job-id", "Value": str(job_id)},
         {"Key": "build-runner-label", "Value": pool}]
    return [{"ResourceType": r, "Tags": t} for r in ("instance", "volume")]


def user_data(jitconfig: str, pool: str) -> str:
    """The job's registration, plus the label and class, which the runner puts in its per-job usage record."""
    return json.dumps({"jitconfig": jitconfig, "label": pool, "class": POOLS[pool].get("class", "unknown")})


def run_instance(name: str, runner_id, job_id, pool: str, jitconfig: str, run=None) -> tuple[str, str, bool]:
    """Launches one runner instance: spot first, through each type and subnet, then on-demand."""
    run = run or aws("ec2").run_instances
    last = None
    for itype, spot in attempts(pool):
        for subnet in random.sample(SUBNETS, len(SUBNETS)) or [None]:
            args = dict(LaunchTemplate={"LaunchTemplateId": LAUNCH_TEMPLATE, "Version": "$Latest"},
                        InstanceType=itype, MinCount=1, MaxCount=1, TagSpecifications=tags(name, runner_id, job_id, pool),
                        UserData=user_data(jitconfig, pool))
            if subnet:
                args["SubnetId"] = subnet
            if spot:
                args["InstanceMarketOptions"] = {"MarketType": "spot", "SpotOptions": {
                    "SpotInstanceType": "one-time", "InstanceInterruptionBehavior": "terminate"}}
            try:
                return run(**args)["Instances"][0]["InstanceId"], itype, spot
            except Exception as e:  # botocore ClientError
                if error_code(e) not in CAPACITY_ERRORS:
                    raise
                last = error_code(e)
                log("no capacity, trying the next", type=itype, spot=spot, subnet=subnet, error=last)
    raise RuntimeError(f"no capacity for any instance type ({last})")


def launch(gh: GitHub, job_id, pool: str) -> dict:
    name = f"{NAME_PREFIX}{job_id}-{secrets.token_hex(3)}"
    _, jit = gh.call("POST", f"/repos/{REPO}/actions/runners/generate-jitconfig",
                     {"name": name, "runner_group_id": 1, "labels": [pool], "work_folder": "_work"})
    runner_id = jit["runner"]["id"]
    try:
        iid, itype, spot = run_instance(name, runner_id, job_id, pool, jit["encoded_jit_config"])
    except Exception:
        gh.call("DELETE", f"/repos/{REPO}/actions/runners/{runner_id}", ok=(204, 404))
        raise
    log("launched", instance=iid, pool=pool, type=itype, spot=spot, runner=name, job_id=job_id)
    return {"id": iid, "name": name, "runner_id": str(runner_id), "job_id": str(job_id), "pool": pool,
            "spot": spot, "type": itype, "launched": time.time()}


def remove_runner(gh: GitHub, runner_id) -> bool:
    """Deregisters a runner; False if it's busy (GitHub refuses while it runs a job)."""
    status, _ = gh.call("DELETE", f"/repos/{REPO}/actions/runners/{runner_id}", ok=(204, 404, 422))
    return status != 422


def terminate(ids: list[str]) -> None:
    if ids:
        aws("ec2").terminate_instances(InstanceIds=ids)


def read_launches(ssm=None) -> list[dict]:
    ssm = ssm or aws("ssm")
    try:
        value = ssm.get_parameter(Name=LAUNCHES)["Parameter"]["Value"]
    except Exception as e:
        if error_code(e) == "ParameterNotFound":
            return []
        raise
    try:
        recent = json.loads(value)
        return [r for r in recent if isinstance(r, dict) and {"id", "launched", "pool"} <= r.keys()]
    except (ValueError, TypeError):
        log("ignoring an unreadable launch record", value=value[:200])
        return []


def write_launches(recent: list[dict], ssm=None) -> None:
    (ssm or aws("ssm")).put_parameter(Name=LAUNCHES, Value=json.dumps(recent or []), Type="String", Overwrite=True)


# ---------------------------------------------------------------------------- curfew warnings

def job_line(job: dict) -> dict:
    """What a warning says about one job (a GitHub Actions job object)."""
    return {"job_id": job.get("id"), "run_id": job.get("run_id"), "workflow": job.get("workflow_name"),
            "job": job.get("name"), "branch": job.get("head_branch"), "url": job.get("html_url")}


def warning_text(w: dict, running: list[dict], queued: list[dict], cfg: dict = CURFEW) -> str:
    def line(j):
        return f"- {j.get('workflow') or '?'} / {j.get('job') or '?'} on {j.get('branch') or '?'}: {j.get('url') or ''}"
    out = [f"AWS runners stop in {w['stage']} min, at {curfew.local(w['stop_at'], cfg)} Sydney. "
           "Running jobs are killed then; queued ones wait until 07:00."]
    if running:
        out += [f"Running ({len(running)}):"] + [line(j) for j in running]
    if queued:
        out += [f"Queued ({len(queued)}):"] + [line(j) for j in queued]
    out += ["To keep them on, reply /extend 1h or /extend 2h on the curfew issue, or tell a Claude session:",
            CURFEW_ISSUE, "No reply: they stop."]
    return "\n".join(out)


def send_telegram(text: str) -> bool:
    import urllib.parse
    import urllib.request
    data = urllib.parse.urlencode({"chat_id": parameter(TELEGRAM_CHAT), "text": text,
                                   "disable_web_page_preview": "true"}).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{parameter(TELEGRAM_TOKEN)}/sendMessage",
                                data=data, timeout=10) as r:
        return json.load(r).get("ok") is True


def curfew_warning(gh, now: float, override: dict, instances: list[dict], queued: list[dict],
                   ssm=None, send=None) -> dict | None:
    """At T-15 and T-5 (curfew.warning_due), while a runner is up or a job waits: once per stop and stage, log
    "curfew warning" (the aws-build-machine session relays it to the Claude sessions that own the jobs) and
    send it to Kieron on Telegram. Never stops the launcher: a failed send is logged."""
    w = curfew.warning_due(now, override, CURFEW)
    if not w or not (instances or queued):
        return None
    ssm = ssm or aws("ssm")
    try:
        if ssm.get_parameter(Name=CURFEW_WARNED)["Parameter"]["Value"] == w["key"]:
            return None
    except Exception as e:
        if error_code(e) != "ParameterNotFound":
            raise
    ssm.put_parameter(Name=CURFEW_WARNED, Value=w["key"], Type="String", Overwrite=True)  # first: never twice
    running = []
    for i in instances[:8]:
        if not i.get("job_id"):
            continue
        try:
            _, job = gh.call("GET", f"/repos/{REPO}/actions/jobs/{i['job_id']}")
            running.append(job_line(job))
        except Exception as e:
            running.append({"job_id": i["job_id"], "error": type(e).__name__})
    waiting = [job_line(j) for j in queued[:8]]
    log("curfew warning", stage=w["stage"], stop_at=int(w["stop_at"]), stop_local=curfew.local(w["stop_at"], CURFEW),
        running=running, queued=waiting)
    try:
        sent = (send or send_telegram)(warning_text(w, running, waiting))
        log("curfew warning sent" if sent else "curfew warning: Telegram refused it", stage=w["stage"])
    except Exception as e:
        log("curfew warning: couldn't send", stage=w["stage"], error=type(e).__name__)
    return w


@contextlib.contextmanager
def lock(ssm=None, wait: int = 60):
    """One launcher run at a time: creating an SSM parameter fails if it already exists."""
    ssm = ssm or aws("ssm")
    me = f"{time.time():.0f} {secrets.token_hex(4)}"
    deadline = time.time() + wait
    while True:
        try:
            ssm.put_parameter(Name=LOCK, Value=me, Type="String", Overwrite=False,
                              Description="build-runner launcher lock (held while it runs)")
            break
        except Exception as e:
            if error_code(e) != "ParameterAlreadyExists":
                raise
        try:
            held = ssm.get_parameter(Name=LOCK)["Parameter"]["Value"]
        except Exception as e:
            if error_code(e) == "ParameterNotFound":
                continue  # released in between
            raise
        if time.time() - float(held.split()[0]) > LOCK_STALE:
            log("removing a stale lock", held=held)
            ssm.delete_parameter(Name=LOCK)
            continue
        if time.time() > deadline:
            raise TimeoutError("launcher lock still held")
        time.sleep(1)
    try:
        yield
    finally:
        try:
            if ssm.get_parameter(Name=LOCK)["Parameter"]["Value"] == me:
                ssm.delete_parameter(Name=LOCK)
        except Exception:
            pass


def launcher(event, context):
    source = (event or {}).get("source", "manual")
    with lock():
        token = ghapp.installation_token(parameter("/build/github/app-id"),
                                         parameter("/build/github/app-private-key"), REPO, TOKEN_PERMISSIONS)
        try:
            gh = GitHub(token)
            now = time.time()
            use_pools(load_pools(gh, now))
            queued = queued_jobs(gh)
            if (source == "webhook" and event.get("job_id") and event.get("pool") in POOLS
                    and event["job_id"] not in {j["id"] for j in queued}):
                # The runs API can lag the webhook by a moment; the signed event says it's queued.
                queued.append({"id": event["job_id"], "pool": event["pool"]})
            runners = [r for r in gh.paged(f"/repos/{REPO}/actions/runners", "runners")
                       if r["name"].startswith(NAME_PREFIX)]
            recent = read_launches()
            instances = with_recent_launches(live_instances(), recent, now, {r["name"] for r in runners})

            if source == "schedule":
                # Usage, for the daily report (part2-runner/usage/): one sample per live runner per 5-minute run.
                for i in instances:
                    log("usage", label=i["pool"], **{"class": POOLS.get(i["pool"], {}).get("class", "unknown")},
                        type=i["type"], spot=i["spot"], minutes=5)
                plan = reap_plan(queued, instances, runners, now)
                by_name = {r["name"]: r for r in runners}
                for i in plan["terminate"]:
                    log("reaping: too old", instance=i["id"], runner=i["name"], age_min=int((now - i["launched"]) / 60))
                    if i["name"] in by_name:
                        remove_runner(gh, by_name[i["name"]]["id"])
                idle = [i for i in plan["idle"] if remove_runner(gh, by_name[i["name"]]["id"])]
                for i in idle:
                    log("reaping: idle, nothing queued", instance=i["id"], runner=i["name"])
                terminate([i["id"] for i in plan["terminate"] + idle])
                for r in plan["stale_runners"]:
                    log("removing a runner registration with no instance", runner=r["name"])
                    remove_runner(gh, r["id"])
                gone = {i["id"] for i in plan["terminate"] + idle}
                instances = [i for i in instances if i["id"] not in gone]

            if source == "curfew-status":
                return {"curfew": curfew.state(now, curfew_override(), CURFEW), "live": len(instances)}
            override = curfew_override()
            night = curfew.state(now, override, CURFEW)
            if night["active"]:
                # Curfew: nothing runs. Running jobs are killed (Kieron's choice, 2026-11-17); queued ones wait
                # (GitHub keeps them up to 24 h) and launch on the first run after it ends.
                by_name = {r["name"]: r for r in runners}
                for i in instances:
                    log("curfew: terminating", instance=i["id"], runner=i["name"], reason=night["reason"])
                    if i["name"] in by_name:
                        remove_runner(gh, by_name[i["name"]]["id"])  # a busy one goes offline, then stale
                terminate([i["id"] for i in instances])
                log("curfew: not launching", reason=night["reason"], queued=len(queued),
                    until=curfew.local(night["until"], CURFEW))
                return {"ok": True, "curfew": night}

            try:
                curfew_warning(gh, now, override, instances, queued)
            except Exception as e:  # a warning must never stop runners launching
                log("curfew warning failed", error=type(e).__name__)
            plan = launches_needed(queued, instances, runners, now, cap=MAX_RUNNERS)
            covered = {i["job_id"] for i in instances}
            log("reconcile", source=source, queued=len(queued), live=len(instances),
                unclaimed=len(unclaimed(instances, runners, now)), launching=plan, cap=MAX_RUNNERS)
            new = []
            try:
                for pool, n in plan.items():
                    # Name each runner after a job it's likely to take: ones without an instance first.
                    todo = sorted((j for j in queued if j["pool"] == pool), key=lambda j: str(j["id"]) in covered)
                    for job in todo[:n]:
                        new.append(launch(gh, job["id"], pool))
            finally:
                keep = recent_launches_to_keep(recent, new, time.time())
                if keep != recent:
                    write_launches(keep)  # still under the lock, so the next run sees these
        finally:
            ghapp.revoke(token)
    return {"ok": True}
