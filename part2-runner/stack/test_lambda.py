#!/usr/bin/env python3
"""Offline tests for the build-runner Lambda code (no AWS, no GitHub, no boto3 needed).

    python3 -I part2-runner/stack/test_lambda.py

Needs openssl (to check our RS256 signatures against a real implementation). CI runs it on every PR.
"""
import hashlib
import hmac
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lambda"))  # python3 -I leaves it out
import ghapp    # noqa: E402
import runner   # noqa: E402

failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failed
    print(f"{'ok  ' if ok else 'FAIL'}  {name}{'' if ok else ': ' + detail}")
    failed += 0 if ok else 1


class ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


# --- RS256 against openssl, for both key formats GitHub could hand out
with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    for fmt, flag in (("PKCS#1", ["-traditional"]), ("PKCS#8", [])):
        key = d / "k.pem"
        subprocess.run(["openssl", "genrsa", *flag, "-out", str(key), "2048"], check=True, capture_output=True)
        subprocess.run(["openssl", "rsa", "-in", str(key), "-pubout", "-out", str(d / "p.pem")], check=True,
                       capture_output=True)
        jwt = ghapp.app_jwt("5777641", key.read_text(), now=1_800_000_000)
        header, claims, sig = jwt.split(".")
        (d / "m").write_bytes(f"{header}.{claims}".encode())
        import base64
        (d / "s").write_bytes(base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4)))
        v = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(d / "p.pem"), "-signature", str(d / "s"),
                            str(d / "m")], capture_output=True, text=True)
        check(f"RS256 JWT verifies with openssl ({fmt} key)", v.returncode == 0, v.stdout + v.stderr)
        body = json.loads(base64.urlsafe_b64decode(claims + "=" * (-len(claims) % 4)))
        check(f"JWT claims ({fmt})", body == {"iat": 1_800_000_000 - 60, "exp": 1_800_000_000 + 540,
                                              "iss": "5777641"}, str(body))
        check(f"JWT doesn't contain the key ({fmt})", "PRIVATE" not in jwt)

# --- webhook signature and filtering
secret, body = "s3cret", b'{"action":"queued"}'
good = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
check("signature: correct accepted", runner.signature_ok(secret, body, good))
check("signature: wrong secret rejected", not runner.signature_ok("other", body, good))
check("signature: tampered body rejected", not runner.signature_ok(secret, body + b" ", good))
check("signature: missing header rejected", not runner.signature_ok(secret, body, ""))
check("signature: sha1 header rejected", not runner.signature_ok(secret, body, "sha1=" + good[7:]))
check("signature: empty secret rejects everything", not runner.signature_ok("", body, good))


def event(action="queued", repo="stickleprojects/spellbound3d", labels=("aws-spellbound3d",)):
    return {"action": action, "repository": {"full_name": repo}, "workflow_job": {"id": 7, "labels": list(labels)}}


check("event: our queued job", runner.wanted_event("workflow_job", event()) is None)
check("event: other event kinds ignored", runner.wanted_event("push", event()) is not None)
check("event: completed ignored", runner.wanted_event("workflow_job", event("completed")) is not None)
check("event: other repo ignored", runner.wanted_event("workflow_job", event(repo="x/y")) is not None)
check("event: the local runner's label ignored",
      runner.wanted_event("workflow_job", event(labels=["self-hosted"])) is not None)
check("event: ubuntu-latest ignored", runner.wanted_event("workflow_job", event(labels=["ubuntu-latest"])) is not None)
check("event: self-hosted plus ours ignored (a JIT runner only has our label)",
      runner.wanted_event("workflow_job", event(labels=["self-hosted", "aws-spellbound3d"])) is not None)
check("event: the check label is ours too", runner.wanted_event("workflow_job", event(labels=["aws-spellbound3d-check"])) is None)
check("event: both of our labels at once ignored (no single runner has both)",
      runner.wanted_event("workflow_job", event(labels=["aws-spellbound3d", "aws-spellbound3d-check"])) is not None)
check("event: label case doesn't matter", runner.wanted_event("workflow_job", event(labels=["AWS-Spellbound3d"])) is None)
check("event: our label plus one we don't have ignored",
      runner.wanted_event("workflow_job", event(labels=["aws-spellbound3d", "gpu"])) is not None)
check("pools: never self-hosted (the local runner's label, and the rollback)",
      not any(l in runner.POOLS for l in ("self-hosted", "linux", "x64", "spellbound3d")))


# webhook handler end to end, with a fake Lambda client and secret
class FakeLambda:
    def __init__(self):
        self.calls = []

    def invoke(self, **kw):
        self.calls.append(kw)


fake_lambda = FakeLambda()
runner.aws = lambda s: fake_lambda
runner.parameter = lambda n: secret
raw = json.dumps(event()).encode()
sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
r = runner.webhook({"body": raw.decode(), "headers": {"X-GitHub-Event": "workflow_job", "X-Hub-Signature-256": sig}}, None)
check("webhook: queued job → 202 and one async launcher call",
      r["statusCode"] == 202 and len(fake_lambda.calls) == 1 and fake_lambda.calls[0]["InvocationType"] == "Event",
      str(r))
r = runner.webhook({"body": raw.decode(), "headers": {"X-GitHub-Event": "workflow_job", "X-Hub-Signature-256": "sha256=00"}}, None)
check("webhook: bad signature → 401, nothing launched", r["statusCode"] == 401 and len(fake_lambda.calls) == 1)
b64 = __import__("base64").b64encode(raw).decode()
r = runner.webhook({"body": b64, "isBase64Encoded": True,
                    "headers": {"x-github-event": "workflow_job", "x-hub-signature-256": sig}}, None)
check("webhook: base64 body handled", r["statusCode"] == 202 and len(fake_lambda.calls) == 2, str(r))

# completed jobs: recorded for the usage report (queue wait, run time), never launch anything
done_event = {"action": "completed", "repository": {"full_name": "stickleprojects/spellbound3d"},
              "workflow_job": {"id": 8, "labels": ["aws-spellbound3d-check"], "workflow_name": "CI", "name": "check",
                               "run_id": 5, "conclusion": "success", "runner_name": "aws-x",
                               "created_at": "2026-11-16T07:00:00Z", "started_at": "2026-11-16T07:01:30Z",
                               "completed_at": "2026-11-16T07:21:30Z"}}
rec = runner.completed_record("workflow_job", done_event)
check("completed: queue wait and run time in seconds", rec and rec["queue_s"] == 90 and rec["run_s"] == 1200, str(rec))
check("completed: label and class", rec and rec["label"] == "aws-spellbound3d-check" and rec["class"] == "heavy", str(rec))
check("completed: another repo's job isn't ours",
      runner.completed_record("workflow_job", {**done_event, "repository": {"full_name": "x/y"}}) is None)
check("completed: a job on another label isn't ours",
      runner.completed_record("workflow_job", {**done_event, "workflow_job": {**done_event["workflow_job"],
                                                                               "labels": ["self-hosted"]}}) is None)
check("completed: a queued job isn't a completed one", runner.completed_record("workflow_job", event()) is None)
check("completed: missing times are None, not an error",
      runner.completed_record("workflow_job", {**done_event, "workflow_job": {"id": 9, "labels": ["aws-spellbound3d"]}})
      ["queue_s"] is None)
raw_done = json.dumps(done_event).encode()
sig_done = "sha256=" + hmac.new(secret.encode(), raw_done, hashlib.sha256).hexdigest()
before = len(fake_lambda.calls)
r = runner.webhook({"body": raw_done.decode(), "headers": {"X-GitHub-Event": "workflow_job",
                                                           "X-Hub-Signature-256": sig_done}}, None)
check("webhook: completed job → 202 recorded, nothing launched",
      r["statusCode"] == 202 and "recorded" in r["body"] and len(fake_lambda.calls) == before, str(r))
r = runner.webhook({"body": raw_done.decode(), "headers": {"X-GitHub-Event": "workflow_job",
                                                           "X-Hub-Signature-256": "sha256=00"}}, None)
check("webhook: a forged completed event → 401", r["statusCode"] == 401)

# --- planning
now = time.time()
L, C = "aws-spellbound3d", "aws-spellbound3d-check"


def inst(name, age_min, job="1", pool=L):
    return {"id": "i-" + name, "name": name, "job_id": job, "launched": now - age_min * 60, "runner_id": "9",
            "pool": pool}


def run_(name, busy=False, status="online"):
    return {"id": hash(name) % 1000, "name": name, "busy": busy, "status": status}


def jobs(n, pool=L, start=0):
    return [{"id": start + i, "pool": pool, "created_at": f"2026-11-16T07:{start + i:02d}:00Z"} for i in range(n)]


plan = runner.launches_needed
q = jobs(3)
check("plan: nothing running, 3 queued → 3", plan(q, [], [], now, cap=4) == {L: 3})
check("plan: cap respected", plan(jobs(9), [], [], now, cap=4) == {L: 4})
check("plan: busy runners count against the cap, not the queue",
      plan(q, [inst("aws-a", 3)], [run_("aws-a", busy=True)], now, cap=4) == {L: 3})
check("plan: idle runner covers a queued job", plan(q, [inst("aws-a", 3)], [run_("aws-a")], now, cap=4) == {L: 2})
check("plan: booting instance (runner not listed yet) covers a queued job",
      plan(q, [inst("aws-a", 1)], [], now, cap=4) == {L: 2})
check("plan: finished instance (runner gone, powering off) doesn't cover one",
      plan(q, [inst("aws-a", 30)], [], now, cap=4) == {L: 3})
check("plan: duplicate webhook doesn't launch twice",
      plan(jobs(1), [inst("aws-a", 0)], [run_("aws-a", status="offline")], now) == {})
check("plan: never negative", plan([], [inst("aws-a", 1)], [], now) == {})
check("plan: a check job and a Claude job queued together get one runner each, sized per label",
      plan(jobs(1, C) + jobs(1, L, 1), [], [], now) == {C: 1, L: 1})
check("plan: an idle 2-vCPU runner doesn't cover a check job",
      plan(jobs(1, C), [inst("aws-a", 3, pool=L)], [run_("aws-a")], now) == {C: 1})
check("plan: an idle 4-vCPU runner doesn't cover a Claude job either (it couldn't take it)",
      plan(jobs(1, L), [inst("aws-a", 3, pool=C)], [run_("aws-a")], now) == {L: 1})
check("plan: the cap is shared, oldest queued job first",
      plan(jobs(3, C, 0) + jobs(3, L, 10), [], [], now, cap=4) == {C: 3, L: 1})

p = runner.reap_plan([], [inst("aws-old", 95), inst("aws-busy", 30), inst("aws-idle", 25), inst("aws-new", 5)],
                     [run_("aws-old", busy=True), run_("aws-busy", busy=True), run_("aws-idle"), run_("aws-new"),
                      run_("aws-gone", status="offline"), run_("aws-gone-busy", busy=True, status="offline"),
                      run_("local-runner", status="offline")], now)
check("reap: older than 90 min", [i["name"] for i in p["terminate"]] == ["aws-old"], str(p["terminate"]))
check("reap: idle over 20 min with nothing queued", [i["name"] for i in p["idle"]] == ["aws-idle"], str(p["idle"]))
check("reap: stale registrations, ours only, not busy",
      [r["name"] for r in p["stale_runners"]] == ["aws-gone"], str(p["stale_runners"]))
p = runner.reap_plan(jobs(1), [inst("aws-idle", 25)], [run_("aws-idle")], now)
check("reap: idle runner kept while a job for its label is queued", p["idle"] == [])
p = runner.reap_plan(jobs(1, C), [inst("aws-idle", 25, pool=L)], [run_("aws-idle")], now)
check("reap: idle runner reaped when only the other label has jobs queued", [i["name"] for i in p["idle"]] == ["aws-idle"])

# --- launch fallback: spot through each type and subnet, then on-demand
xl = runner.attempts(C)
check("defaults: check runners never 2 vCPU or t3 (spellbound3d's timing tests fail on 2 vCPU)",
      bool(xl) and all(t.split(".")[1] == "xlarge" and not t.startswith("t") for t, _ in xl), str(xl))
check("defaults: Claude-job runners are the agreed 2-vCPU chain",
      runner.attempts(L) == [("c7i.large", True), ("t3.large", True), ("c7i.large", False)])
check("defaults: the Lambda's built-in pools are the deployed ones (config.json)",
      runner.DEFAULT_POOLS == json.loads((Path(__file__).resolve().parent / "config.json").read_text())["Pools"])
check("defaults: every label has a class (light or heavy), for the usage report",
      all(p.get("class") in ("light", "heavy") for p in runner.POOLS.values()))
runner.SUBNETS = ["subnet-a", "subnet-b"]
runner.POOLS = {C: {"class": "heavy", "spot": ["c7i.xlarge", "c6i.xlarge"], "on_demand": ["c7i.xlarge"]}}
check("attempts: spot types first, then on-demand",
      runner.attempts(C) == [("c7i.xlarge", True), ("c6i.xlarge", True), ("c7i.xlarge", False)])
tried = []


def ec2_run(fail_until):
    def run(**kw):
        tried.append((kw["InstanceType"], "InstanceMarketOptions" in kw, kw.get("SubnetId")))
        if len(tried) <= fail_until:
            raise ClientError("InsufficientInstanceCapacity")
        return {"Instances": [{"InstanceId": "i-123"}]}
    return run


iid, itype, spot = runner.run_instance("aws-1", 9, 1, C, "JIT", run=ec2_run(4))
check("fallback: on-demand after every spot type and subnet is short",
      (iid, itype, spot) == ("i-123", "c7i.xlarge", False) and len(tried) == 5, str(tried))
tried.clear()
runner.run_instance("aws-1", 9, 1, C, "JIT", run=ec2_run(0))
check("fallback: first try is spot", tried[0][1] is True)
tried.clear()


def run_args(**kw):
    tried.append(kw)
    return {"Instances": [{"InstanceId": "i-1"}]}


runner.run_instance("aws-1", 9, 42, C, "JIT", run=run_args)
kw = tried[0]
check("launch: tagged resource-group on instance and volume",
      {t["ResourceType"] for t in kw["TagSpecifications"]} == {"instance", "volume"}
      and all({"Key": "resource-group", "Value": "build-runner"} in t["Tags"] for t in kw["TagSpecifications"]))
check("launch: tagged with its label, so the planner knows its size",
      all({"Key": "build-runner-label", "Value": C} in t["Tags"] for t in kw["TagSpecifications"]))
check("launch: user data is the job's registration plus its label and class (for the usage record)",
      json.loads(kw["UserData"]) == {"jitconfig": "JIT", "label": C, "class": "heavy"}, kw["UserData"])
check("launch: from the launch template", "LaunchTemplate" in kw and "ImageId" not in kw)
try:
    runner.run_instance("aws-1", 9, 1, C, "JIT", run=lambda **kw: (_ for _ in ()).throw(ClientError("UnauthorizedOperation")))
    check("fallback: real errors aren't swallowed", False, "no exception")
except ClientError:
    check("fallback: real errors aren't swallowed", True)
try:
    runner.run_instance("aws-1", 9, 1, C, "JIT", run=lambda **kw: (_ for _ in ()).throw(ClientError("InsufficientInstanceCapacity")))
    check("fallback: gives up after the last option", False, "no exception")
except RuntimeError:
    check("fallback: gives up after the last option", True)



# --- the lock
class FakeSSM:
    def __init__(self):
        self.params = {}

    def put_parameter(self, Name, Value, Overwrite=False, **kw):
        if Name in self.params and not Overwrite:
            raise ClientError("ParameterAlreadyExists")
        self.params[Name] = Value

    def get_parameter(self, Name, **kw):
        if Name not in self.params:
            raise ClientError("ParameterNotFound")
        return {"Parameter": {"Value": self.params[Name]}}

    def delete_parameter(self, Name):
        self.params.pop(Name, None)


ssm = FakeSSM()
with runner.lock(ssm):
    check("lock: held while running", runner.LOCK in ssm.params)
    try:
        with runner.lock(ssm, wait=0):
            check("lock: a second run can't take it", False)
    except TimeoutError:
        check("lock: a second run can't take it", True)
check("lock: released afterwards", runner.LOCK not in ssm.params)
ssm.params[runner.LOCK] = f"{time.time() - 600:.0f} crashed"
with runner.lock(ssm, wait=0):
    check("lock: a stale lock (crashed run) is taken over", not ssm.params[runner.LOCK].endswith("crashed"))
try:
    with runner.lock(ssm):
        raise ValueError("boom")
except ValueError:
    pass
check("lock: released on failure too", runner.LOCK not in ssm.params)

# --- the curfew, through launcher() end to end (fakes for GitHub, EC2, the lock and the clock)
import contextlib  # noqa: E402
import curfew as curfew_mod  # noqa: E402

calls = {"terminated": [], "launched": [], "removed": []}
runner.lock = lambda *a, **k: contextlib.nullcontext()
runner.ghapp.installation_token = lambda *a, **k: "tok"
runner.ghapp.revoke = lambda *a, **k: None
runner.GitHub = lambda token: type("G", (), {"paged": lambda self, *a: [{"name": "aws-1-x", "id": 3, "busy": True,
                                                                         "status": "online"}]})()
runner.queued_jobs = lambda gh: [{"id": 11, "pool": L, "created_at": 0}]
runner.live_instances = lambda: [{"id": "i-1", "name": "aws-1-x", "runner_id": 3, "job_id": "1", "pool": L,
                                  "spot": True, "type": "c7i.large", "launched": time.time() - 60}]
runner.terminate = lambda ids: calls["terminated"].extend(ids)
runner.remove_runner = lambda gh, rid: calls["removed"].append(rid) or False
runner.launch = lambda gh, job_id, pool: calls["launched"].append(job_id)
params = {"/build/github/app-id": "1", "/build/github/app-private-key": "k"}
def fresh(n):
    return params[n] if n in params else (_ for _ in ()).throw(ClientError("ParameterNotFound"))


runner.parameter = lambda n: fresh(n) if n != curfew_mod.PARAMETER else (_ for _ in ()).throw(
    AssertionError("the curfew must be read fresh, not through the cached parameter()"))
runner.aws = lambda service: type("FreshSSM", (), {
    "get_parameter": lambda self, Name, **k: {"Parameter": {"Value": fresh(Name)}}})()
real_time = runner.time.time


def at_sydney(h, m=0):
    return __import__("datetime").datetime(2026, 11, 17, h, m, tzinfo=__import__("zoneinfo").ZoneInfo("Australia/Sydney")).timestamp()


runner.time.time = lambda: at_sydney(23)
r = runner.launcher({"source": "webhook", "job_id": 12, "pool": L}, None)
check("curfew: at 23:00 the launcher terminates the running runner", calls["terminated"] == ["i-1"], str(calls))
check("curfew: ...deregisters it", calls["removed"] == [3], str(calls))
check("curfew: ...and launches nothing for the queued jobs", calls["launched"] == [] and r.get("curfew", {}).get("active"), str(r))
for k in calls:
    calls[k].clear()
params[curfew_mod.PARAMETER] = json.dumps(curfew_mod.extend(at_sydney(21, 30), {}, 2))
runner.launcher({"source": "webhook", "job_id": 12, "pool": L}, None)
check("curfew: extended to 00:00, at 23:00 nothing is terminated", calls["terminated"] == [], str(calls))
runner.time.time = lambda: at_sydney(15)
del params[curfew_mod.PARAMETER]
runner.launcher({"source": "schedule"}, None)
check("curfew: in the afternoon, queued jobs launch as normal", 11 in calls["launched"], str(calls))
r = runner.launcher({"source": "curfew-status"}, None)
check("curfew: a status call reports the state and launches nothing extra",
      r["curfew"]["active"] is False and r["curfew"]["reason"] == "open", str(r))
params[curfew_mod.PARAMETER] = json.dumps(curfew_mod.stop(at_sydney(15)))
for k in calls:
    calls[k].clear()
runner.launcher({"source": "schedule"}, None)
check("curfew: /stop in the afternoon terminates and launches nothing",
      calls["terminated"] == ["i-1"] and calls["launched"] == [], str(calls))
runner.time.time = real_time

print(f"\n{failed} failed")
sys.exit(1 if failed else 0)
