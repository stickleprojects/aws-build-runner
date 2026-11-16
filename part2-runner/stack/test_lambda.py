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
check("launch: runners registered with our label only (never self-hosted: the local runner's)",
      runner.RUNNER_LABELS == {"aws-spellbound3d"})
check("event: label case doesn't matter", runner.wanted_event("workflow_job", event(labels=["AWS-Spellbound3d"])) is None)
check("event: our label plus one we don't have ignored",
      runner.wanted_event("workflow_job", event(labels=["aws-spellbound3d", "gpu"])) is not None)


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

# --- planning
now = time.time()


def inst(name, age_min, job="1"):
    return {"id": "i-" + name, "name": name, "job_id": job, "launched": now - age_min * 60, "runner_id": "9"}


def run_(name, busy=False, status="online"):
    return {"id": hash(name) % 1000, "name": name, "busy": busy, "status": status}


q = [{"id": n} for n in range(3)]
check("plan: nothing running, 3 queued → 3", runner.launches_needed(q, [], [], now, cap=4) == 3)
check("plan: cap respected", runner.launches_needed([{"id": n} for n in range(9)], [], [], now, cap=4) == 4)
check("plan: busy runners count against the cap, not the queue",
      runner.launches_needed(q, [inst("aws-a", 3)], [run_("aws-a", busy=True)], now, cap=4) == 3)
check("plan: idle runner covers a queued job",
      runner.launches_needed(q, [inst("aws-a", 3)], [run_("aws-a")], now, cap=4) == 2)
check("plan: booting instance (runner not listed yet) covers a queued job",
      runner.launches_needed(q, [inst("aws-a", 1)], [], now, cap=4) == 2)
check("plan: finished instance (runner gone, powering off) doesn't cover one",
      runner.launches_needed(q, [inst("aws-a", 30)], [], now, cap=4) == 3)
check("plan: duplicate webhook doesn't launch twice",
      runner.launches_needed([{"id": 1}], [inst("aws-a", 0)], [run_("aws-a", status="offline")], now) == 0)
check("plan: never negative", runner.launches_needed([], [inst("aws-a", 1)], [], now) == 0)

p = runner.reap_plan([], [inst("aws-old", 95), inst("aws-busy", 30), inst("aws-idle", 25), inst("aws-new", 5)],
                     [run_("aws-old", busy=True), run_("aws-busy", busy=True), run_("aws-idle"), run_("aws-new"),
                      run_("aws-gone", status="offline"), run_("aws-gone-busy", busy=True, status="offline"),
                      run_("local-runner", status="offline")], now)
check("reap: older than 90 min", [i["name"] for i in p["terminate"]] == ["aws-old"], str(p["terminate"]))
check("reap: idle over 20 min with nothing queued", [i["name"] for i in p["idle"]] == ["aws-idle"], str(p["idle"]))
check("reap: stale registrations, ours only, not busy",
      [r["name"] for r in p["stale_runners"]] == ["aws-gone"], str(p["stale_runners"]))
p = runner.reap_plan([{"id": 1}], [inst("aws-idle", 25)], [run_("aws-idle")], now)
check("reap: idle runner kept while a job is queued", p["idle"] == [])

# --- launch fallback: spot through each type and subnet, then on-demand
defaults = runner.SPOT_TYPES + runner.ON_DEMAND_TYPES
check("defaults: never 2 vCPU or t3 (spellbound3d's timing tests fail on 2 vCPU)",
      bool(defaults) and all(t.split(".")[1] == "xlarge" and not t.startswith("t") for t in defaults), str(defaults))
runner.SUBNETS = ["subnet-a", "subnet-b"]
runner.SPOT_TYPES = ["c7i.xlarge", "c6i.xlarge"]
runner.ON_DEMAND_TYPES = ["c7i.xlarge"]
check("attempts: spot types first, then on-demand",
      runner.attempts() == [("c7i.xlarge", True), ("c6i.xlarge", True), ("c7i.xlarge", False)])
tried = []


def ec2_run(fail_until):
    def run(**kw):
        tried.append((kw["InstanceType"], "InstanceMarketOptions" in kw, kw.get("SubnetId")))
        if len(tried) <= fail_until:
            raise ClientError("InsufficientInstanceCapacity")
        return {"Instances": [{"InstanceId": "i-123"}]}
    return run


iid, itype, spot = runner.run_instance("aws-1", 9, 1, "JIT", run=ec2_run(4))
check("fallback: on-demand after every spot type and subnet is short",
      (iid, itype, spot) == ("i-123", "c7i.xlarge", False) and len(tried) == 5, str(tried))
tried.clear()
runner.run_instance("aws-1", 9, 1, "JIT", run=ec2_run(0))
check("fallback: first try is spot", tried[0][1] is True)
tried.clear()


def run_args(**kw):
    tried.append(kw)
    return {"Instances": [{"InstanceId": "i-1"}]}


runner.run_instance("aws-1", 9, 42, "JIT", run=run_args)
kw = tried[0]
check("launch: tagged resource-group on instance and volume",
      {t["ResourceType"] for t in kw["TagSpecifications"]} == {"instance", "volume"}
      and all({"Key": "resource-group", "Value": "build-runner"} in t["Tags"] for t in kw["TagSpecifications"]))
check("launch: user data is the image's {jitconfig} JSON", json.loads(kw["UserData"]) == {"jitconfig": "JIT"})
check("launch: from the launch template", "LaunchTemplate" in kw and "ImageId" not in kw)
try:
    runner.run_instance("aws-1", 9, 1, "JIT", run=lambda **kw: (_ for _ in ()).throw(ClientError("UnauthorizedOperation")))
    check("fallback: real errors aren't swallowed", False, "no exception")
except ClientError:
    check("fallback: real errors aren't swallowed", True)
try:
    runner.run_instance("aws-1", 9, 1, "JIT", run=lambda **kw: (_ for _ in ()).throw(ClientError("InsufficientInstanceCapacity")))
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

print(f"\n{failed} failed")
sys.exit(1 if failed else 0)
