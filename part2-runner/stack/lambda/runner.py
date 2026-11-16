"""The build-runner Lambda functions (stack build-runner, part2-runner/stack/template.yaml).

webhook   behind a public function URL. Checks GitHub's HMAC signature, keeps only spellbound3d
          workflow_job "queued" events for our label, and hands off to the launcher (asynchronously,
          so GitHub gets its answer well inside its 10 s limit).
launcher  reconciles, one run at a time (an SSM-parameter lock: this account's Lambda concurrency is
          the minimum 10, so none can be reserved): counts the queued jobs for our label against the
          runners that haven't taken a job yet, and launches the difference, up to MAX_RUNNERS live
          instances. Each instance gets a just-in-time (single-job) runner registration in its user
          data; the image runs that one job and terminates itself.
          Every 5 minutes (EventBridge Scheduler) it also reconciles (catching missed webhooks) and reaps:
          instances older than MAX_AGE, runners idle for IDLE_MAX with nothing queued, and runner
          registrations whose instance is gone.

GitHub assigns a queued job to any idle runner with matching labels, so a runner can't be reserved for
one job: that is why every runner gets the same instance types (see PLAN.md, "Instance types").
"""
import base64
import contextlib
import functools
import hashlib
import hmac
import json
import os
import random
import secrets
import time

import ghapp

REPO = os.environ.get("REPO", "stickleprojects/spellbound3d")
LABEL = os.environ.get("RUNNER_LABEL", "aws-spellbound3d")
# A JIT runner has exactly the labels it's registered with (seen 2026-11-16: no self-hosted, OS or
# architecture added). Only ours, on purpose: with "self-hosted" it could take jobs meant for the local
# runner (SDLC_RUNNER=self-hosted is the rollback), and the two must never compete.
RUNNER_LABELS = {LABEL.lower()}
NAME_PREFIX = "aws-"
GROUP = os.environ.get("RESOURCE_GROUP", "build-runner")
ROLE_TAG = "build-runner-role"
MAX_RUNNERS = int(os.environ.get("MAX_RUNNERS", "4"))
MAX_AGE = int(os.environ.get("MAX_AGE_MINUTES", "90")) * 60
IDLE_MAX = int(os.environ.get("IDLE_MINUTES", "20")) * 60
BOOT_GRACE = 5 * 60   # an instance whose runner isn't listed yet counts as booting for this long
SPOT_TYPES = [t for t in os.environ.get("SPOT_TYPES", "c7i.xlarge,c6i.xlarge,m7i.xlarge").split(",") if t]
ON_DEMAND_TYPES = [t for t in os.environ.get("ON_DEMAND_TYPES", "c7i.xlarge").split(",") if t]
SUBNETS = [s for s in os.environ.get("SUBNETS", "").split(",") if s]
LAUNCH_TEMPLATE = os.environ.get("LAUNCH_TEMPLATE_ID", "")
LAUNCHER = os.environ.get("LAUNCHER_FUNCTION", "build-runner-launcher")
LOCK = "/build/runner/launcher-lock"
LOCK_STALE = 150      # longer than the launcher's 120 s timeout
TOKEN_PERMISSIONS = {"administration": "write", "actions": "read"}
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


def log(msg: str, **fields) -> None:
    print(json.dumps({"msg": msg, **fields}, default=str))


# ---------------------------------------------------------------------------- webhook

def signature_ok(secret: str, body: bytes, header: str) -> bool:
    if not secret or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):])


def labels_ok(labels) -> bool:
    """Ours, and nothing a runner of ours doesn't have (else it couldn't take the job anyway)."""
    wanted = {str(l).lower() for l in labels or []}
    return LABEL.lower() in wanted and wanted <= RUNNER_LABELS


def wanted_event(kind: str, payload: dict) -> str | None:
    """None if we act on it, else why it's ignored."""
    if kind != "workflow_job":
        return f"event {kind}"
    if payload.get("action") != "queued":
        return f"action {payload.get('action')}"
    if (payload.get("repository") or {}).get("full_name") != REPO:
        return "another repository"
    if not labels_ok((payload.get("workflow_job") or {}).get("labels")):
        return "not our label"
    return None


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
    if kind == "ping":
        return response(200, "pong")
    try:
        payload = json.loads(raw)
    except ValueError:
        return response(400, "not JSON")
    why = wanted_event(kind, payload)
    if why:
        return response(202, f"ignored: {why}")
    job = payload["workflow_job"]
    log("queued", job_id=job.get("id"), workflow=job.get("workflow_name"), job=job.get("name"))
    aws("lambda").invoke(FunctionName=LAUNCHER, InvocationType="Event",
                         Payload=json.dumps({"source": "webhook", "job_id": job.get("id")}).encode())
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


def launches_needed(queued: list[dict], instances: list[dict], runners: list[dict], now: float,
                    cap: int = MAX_RUNNERS) -> int:
    need = len(queued) - len(unclaimed(instances, runners, now))
    room = cap - len(instances)
    return max(0, min(need, room))


def reap_plan(queued: list[dict], instances: list[dict], runners: list[dict], now: float) -> dict:
    """What to clean up: {"terminate": [instance], "idle": [instance], "stale_runners": [runner]}."""
    by_name = {r["name"]: r for r in runners}
    old = [i for i in instances if now - i["launched"] > MAX_AGE]
    idle = [] if queued else [i for i in unclaimed(instances, runners, now)
                              if i not in old and i["name"] in by_name and now - i["launched"] > IDLE_MAX]
    live = {i["name"] for i in instances}
    stale = [r for r in runners if r["name"].startswith(NAME_PREFIX) and r["name"] not in live
             and r.get("status") == "offline" and not r.get("busy")]
    return {"terminate": old, "idle": idle, "stale_runners": stale}


def attempts() -> list[tuple[str, bool]]:
    """(instance type, spot?) in the order tried: every spot type, then on-demand."""
    return [(t, True) for t in SPOT_TYPES] + [(t, False) for t in ON_DEMAND_TYPES]


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


def queued_jobs(gh: GitHub) -> list[dict]:
    """Queued jobs for our label, in any run that isn't finished (a run can be in progress with a
    later job still queued)."""
    jobs = {}
    for status in ("queued", "in_progress", "pending", "waiting", "requested"):
        for run in gh.paged(f"/repos/{REPO}/actions/runs?status={status}", "workflow_runs"):
            for job in gh.paged(f"/repos/{REPO}/actions/runs/{run['id']}/jobs?filter=latest", "jobs"):
                if job.get("status") == "queued" and labels_ok(job.get("labels")):
                    jobs[job["id"]] = job
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
                            "type": i["InstanceType"], "launched": i["LaunchTime"].timestamp()})
    return out


def tags(name: str, runner_id, job_id) -> list[dict]:
    t = [{"Key": "resource-group", "Value": GROUP}, {"Key": "Name", "Value": f"build-runner {name}"},
         {"Key": ROLE_TAG, "Value": "runner"}, {"Key": "build-runner-name", "Value": name},
         {"Key": "github-runner-id", "Value": str(runner_id)}, {"Key": "github-job-id", "Value": str(job_id)}]
    return [{"ResourceType": r, "Tags": t} for r in ("instance", "volume")]


def run_instance(name: str, runner_id, job_id, jitconfig: str, run=None) -> tuple[str, str, bool]:
    """Launches one runner instance: spot first, through each type and subnet, then on-demand."""
    run = run or aws("ec2").run_instances
    last = None
    for itype, spot in attempts():
        for subnet in random.sample(SUBNETS, len(SUBNETS)) or [None]:
            args = dict(LaunchTemplate={"LaunchTemplateId": LAUNCH_TEMPLATE, "Version": "$Latest"},
                        InstanceType=itype, MinCount=1, MaxCount=1, TagSpecifications=tags(name, runner_id, job_id),
                        UserData=json.dumps({"jitconfig": jitconfig}))
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


def launch(gh: GitHub, job_id) -> None:
    name = f"{NAME_PREFIX}{job_id}-{secrets.token_hex(3)}"
    _, jit = gh.call("POST", f"/repos/{REPO}/actions/runners/generate-jitconfig",
                     {"name": name, "runner_group_id": 1, "labels": [LABEL], "work_folder": "_work"})
    runner_id = jit["runner"]["id"]
    try:
        iid, itype, spot = run_instance(name, runner_id, job_id, jit["encoded_jit_config"])
    except Exception:
        gh.call("DELETE", f"/repos/{REPO}/actions/runners/{runner_id}", ok=(204, 404))
        raise
    log("launched", instance=iid, type=itype, spot=spot, runner=name, job_id=job_id)


def remove_runner(gh: GitHub, runner_id) -> bool:
    """Deregisters a runner; False if it's busy (GitHub refuses while it runs a job)."""
    status, _ = gh.call("DELETE", f"/repos/{REPO}/actions/runners/{runner_id}", ok=(204, 404, 422))
    return status != 422


def terminate(ids: list[str]) -> None:
    if ids:
        aws("ec2").terminate_instances(InstanceIds=ids)


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
            queued = queued_jobs(gh)
            if source == "webhook" and event.get("job_id") and event["job_id"] not in {j["id"] for j in queued}:
                # The runs API can lag the webhook by a moment; the signed event says it's queued.
                queued.append({"id": event["job_id"]})
            runners = [r for r in gh.paged(f"/repos/{REPO}/actions/runners", "runners")
                       if r["name"].startswith(NAME_PREFIX)]
            instances = live_instances()

            if source == "schedule":
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

            n = launches_needed(queued, instances, runners, now)
            covered = {i["job_id"] for i in instances}
            todo = sorted(queued, key=lambda j: str(j["id"]) in covered)  # jobs without an instance first
            log("reconcile", source=source, queued=len(queued), live=len(instances),
                unclaimed=len(unclaimed(instances, runners, now)), launching=n, cap=MAX_RUNNERS)
            for job in todo[:n]:
                launch(gh, job["id"])
        finally:
            ghapp.revoke(token)
    return {"ok": True}
