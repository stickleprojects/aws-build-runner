#!/usr/bin/env python3
"""Check the policies with the IAM policy simulator. Read-only; changes nothing.

  verify.py           the live policies: Claude's IAM user as attached, and the live boundary
  verify.py --local   the drafts in out/ (run build-policies.py first): test a change before godmode

Two principals are tested:
  user  Claude's IAM user (AWS_CLAUDE_USER; account AWS_ACCOUNT_ID: see config.py)
  role  a role with AdministratorAccess, capped by the boundary: the worst role Claude could make

Cases come from policy.yaml (every approved service, blocked action and limit) plus a fixed
list of things that must always be denied. Exit 1 on any unexpected decision.
Needs iam:SimulatePrincipalPolicy / iam:SimulateCustomPolicy (the laptop user has both).
"""
import argparse
import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I leaves the script dir off sys.path
import config  # noqa: E402

HERE = Path(__file__).resolve().parent
PARALLEL = 8                # simulator calls at once
OTHER_REGION = "eu-west-2"
GLOBAL_REGION = "us-east-1"  # where IAM, STS and billing requests are evaluated
ADMIN = json.dumps({"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]})

# A concrete action for each wildcard in a blocked list, so every pattern gets tested.
WILDCARD_EXAMPLES = {
    "ec2:CreateVerifiedAccess*": "ec2:CreateVerifiedAccessInstance",
    "ec2:Purchase*": "ec2:PurchaseReservedInstancesOffering",
    "ec2:CreateCapacityReservation*": "ec2:CreateCapacityReservationFleet",
}

# Costly or risky actions inside approved services. Kept here, apart from policy.yaml, so that
# removing a block from policy.yaml fails these tests: unblocking one takes a deliberate edit
# in both files, as part of its approval.
MUST_STAY_DENIED = [
    "ec2:CreateNatGateway", "ec2:CreateTransitGateway", "ec2:CreateVpnConnection",
    "ec2:CreateClientVpnEndpoint", "ec2:CreateVerifiedAccessInstance", "ec2:EnableFastSnapshotRestores",
    "ec2:PurchaseReservedInstancesOffering", "ec2:PurchaseCapacityBlock", "ec2:PurchaseHostReservation",
    "ec2:AllocateHosts", "ec2:CreateCapacityReservation", "ec2:CreateCapacityReservationFleet",
    "ec2:CreateFleet", "ec2:RequestSpotFleet", "ec2:RequestSpotInstances",
    "ec2:DisableImageBlockPublicAccess", "ec2:DisableSnapshotBlockPublicAccess",
    "ec2:DisableEbsEncryptionByDefault", "lambda:PutProvisionedConcurrencyConfig",
    "s3:PutAccountPublicAccessBlock", "s3:PutBucketObjectLockConfiguration",
]

# Creates that must always need an approved resource-group tag. Kept apart from policy.yaml, so
# dropping one from resource_groups.enforced_on fails the tests: a deliberate second edit.
MUST_REQUIRE_GROUP = [
    ("ec2:RunInstances", "instance"), ("ec2:RunInstances", "volume"), ("ec2:CreateVolume", "volume"),
    ("ec2:CreateSnapshot", "snapshot"), ("ec2:CreateImage", "image"),
    ("ec2:CreateLaunchTemplate", "launch-template"), ("ec2:CreateSecurityGroup", "security-group"),
    ("lambda:CreateFunction", None), ("logs:CreateLogGroup", None), ("cloudformation:CreateStack", None),
    ("iam:CreateRole", None), ("iam:CreatePolicy", None), ("iam:CreateInstanceProfile", None),
]

# The resource types each enforced EC2 create actually makes. Tests pair an action only with
# these: the IAM simulator rejects an action on a type it doesn't apply to (CreateSnapshot on a
# launch template), which says nothing about the policy.
EC2_CREATES = {
    "ec2:RunInstances": ["instance", "volume"], "ec2:CreateVolume": ["volume"],
    "ec2:CreateSnapshot": ["snapshot"], "ec2:CreateImage": ["image", "snapshot"],
    "ec2:CreateLaunchTemplate": ["launch-template"], "ec2:CreateSecurityGroup": ["security-group"],
}

# Must be denied unless the service gets approved (then the case is skipped).
UNAPPROVED = [
    "rds:CreateDBInstance", "dynamodb:CreateTable", "ecs:CreateCluster", "eks:CreateCluster",
    "sagemaker:CreateEndpoint", "redshift:CreateCluster", "elasticache:CreateCacheCluster",
    "sqs:CreateQueue", "sns:CreateTopic", "secretsmanager:CreateSecret", "ecr:CreateRepository",
    "apigateway:POST", "bedrock:InvokeModel", "kms:CreateKey", "kms:ScheduleKeyDeletion",
    "cloudtrail:StopLogging", "cloudtrail:DeleteTrail", "config:DeleteConfigurationRecorder",
    "servicequotas:RequestServiceQuotaIncrease", "savingsplans:CreateSavingsPlan",
    "route53domains:RegisterDomain", "cloudfront:CreateDistribution", "route53:CreateHostedZone",
    "aws-marketplace:Subscribe", "organizations:LeaveOrganization", "account:PutAlternateContact",
    "budgets:ModifyBudget", "ce:DeleteAnomalyMonitor", "support:CreateCase",
]


class Case:
    def __init__(self, action, expect, resource="*", region=None, **ctx):
        self.action, self.expect, self.resource, self.region, self.ctx = action, expect, resource, region, ctx

    def expected_for(self, who):
        return self.expect[who] if isinstance(self.expect, dict) else self.expect


def build_cases(p, cfg):
    acct, home = cfg["AWS_ACCOUNT_ID"], p["region"]
    path = p["iam"]["path"]
    boundary = f"arn:aws:iam::{acct}:policy/{p['iam']['boundary']}"
    role = f"arn:aws:iam::{acct}:role{path}verify-example"
    approved_prefixes = {a.split(":")[0] for s in p["services"] for a in s["actions"]}
    inst = f"arn:aws:ec2:{home}:{acct}:instance/*"
    vol = f"arn:aws:ec2:{home}:{acct}:volume/*"
    rg = p.get("resource_groups")
    # Tags on every create that should succeed; {} when there's no resource-group rule.
    tag = ({f"aws:RequestTag/{rg['tag_key']}": sorted(rg["groups"])[0], "aws:TagKeys": [rg["tag_key"]]}
           if rg else {})
    cases = []
    add = cases.append

    for s in p["services"]:
        if s.get("via_services"):
            via = f"{s['via_services'][0]}.{home}.amazonaws.com"
            add(Case(s["probe"], "allowed", region=home, **{"kms:ViaService": via}, **tag))
            add(Case(s["probe"], "denied", region=home))  # not through a service
        else:
            add(Case(s["probe"], "allowed", region=home, **tag))
            add(Case(s["probe"], "denied", region=OTHER_REGION, **tag))
        for pattern in s.get("blocked") or {}:
            if "*" in pattern and pattern not in WILDCARD_EXAMPLES:
                raise SystemExit(f"verify.py: add an example for blocked pattern {pattern} to WILDCARD_EXAMPLES")
            add(Case(WILDCARD_EXAMPLES.get(pattern, pattern), "denied", region=home))

    for action in MUST_STAY_DENIED:
        add(Case(action, "denied", region=home))

    limits = next((s.get("limits") or {} for s in p["services"] if s["id"] == "ec2"), {})
    if limits:
        for t in ("t3.large", "t3a.medium", "c7i.xlarge", "m5.large"):
            add(Case("ec2:RunInstances", "allowed", inst, home, **{"ec2:InstanceType": t, "ec2:Tenancy": "default"}, **tag))
        for t in ("m5.4xlarge", "c5.2xlarge", "g4dn.xlarge", "p5.48xlarge", "trn1.2xlarge", "r5.large", "t2.micro"):
            add(Case("ec2:RunInstances", "denied", inst, home, **{"ec2:InstanceType": t, "ec2:Tenancy": "default"}, **tag))
        add(Case("ec2:RunInstances", "denied", inst, home, **{"ec2:InstanceType": "t3.large", "ec2:Tenancy": "dedicated"}, **tag))
        add(Case("ec2:ModifyInstanceAttribute", "denied", inst, home, **{"ec2:Attribute/InstanceType": "c5.9xlarge"}))
        add(Case("ec2:CreateVolume", "allowed", vol, home, **{"ec2:VolumeSize": 100, "ec2:VolumeType": "gp3"}, **tag))
        add(Case("ec2:CreateVolume", "denied", vol, home, **{"ec2:VolumeSize": 500, "ec2:VolumeType": "gp3"}, **tag))
        add(Case("ec2:CreateVolume", "denied", vol, home, **{"ec2:VolumeSize": 50, "ec2:VolumeType": "io2"}, **tag))

    for action in UNAPPROVED:
        if action.split(":")[0] in approved_prefixes and action.split(":")[0] != "kms":
            continue
        add(Case(action, "denied", region=home))
        add(Case(action, "denied", region=GLOBAL_REGION))

    # Read-only everywhere: the user only (roles get nothing outside the home region).
    add(Case("ec2:DescribeInstances", {"user": "allowed", "role": "denied"}, region=OTHER_REGION))
    add(Case("ec2:DescribeInstances", "allowed", region=home))
    add(Case("ce:GetCostAndUsage", {"user": "allowed", "role": "denied"}, region=GLOBAL_REGION))
    add(Case("sts:GetCallerIdentity", "allowed", region=GLOBAL_REGION))

    # IAM for what Claude builds.
    g = GLOBAL_REGION
    user_only = {"user": "allowed", "role": "denied"}
    add(Case("iam:CreateRole", user_only, role, g, **{"iam:PermissionsBoundary": boundary}, **tag))
    add(Case("iam:CreateRole", "denied", role, g, **tag))
    add(Case("iam:CreateRole", "denied", f"arn:aws:iam::{acct}:role/outside-path", g, **{"iam:PermissionsBoundary": boundary}, **tag))
    add(Case("iam:DeleteRolePermissionsBoundary", "denied", role, g))
    add(Case("iam:PassRole", "allowed", role, g, **{"iam:PassedToService": "ec2.amazonaws.com"}))
    add(Case("iam:PassRole", "denied", role, g, **{"iam:PassedToService": "sagemaker.amazonaws.com"}))
    full = f"arn:aws:iam::{acct}:role/managed/AccountFullAccessRole"
    add(Case("iam:PassRole", "denied", full, g, **{"iam:PassedToService": "ec2.amazonaws.com"}))
    add(Case("iam:UpdateAssumeRolePolicy", "denied", full, g))
    add(Case("sts:AssumeRole", "denied", full, g))
    add(Case("sts:AssumeRole", user_only, role, g))
    add(Case("iam:CreatePolicyVersion", user_only, f"arn:aws:iam::{acct}:policy{path}verify-example", g))
    for guarded in ("policy/claude-assistant-policy", "policy/claude-assistant-iam", f"policy/{p['iam']['boundary']}"):
        add(Case("iam:CreatePolicyVersion", "denied", f"arn:aws:iam::{acct}:{guarded}", g))
    me = f"arn:aws:iam::{acct}:user/{cfg['AWS_CLAUDE_USER']}"
    for action in ("iam:CreateUser", "iam:CreateAccessKey", "iam:AttachUserPolicy", "iam:PutUserPolicy",
                   "iam:DetachUserPolicy", "iam:CreateLoginProfile", "iam:CreateGroup"):
        add(Case(action, "denied", me, g))
    add(Case("iam:CreateServiceLinkedRole", user_only, "*", g, **{"iam:AWSServiceName": "spot.amazonaws.com"}))
    add(Case("iam:CreateServiceLinkedRole", "denied", "*", g, **{"iam:AWSServiceName": "rds.amazonaws.com"}))

    if rg:
        key = rg["tag_key"]
        good = tag
        missing = {}
        wrong = {f"aws:RequestTag/{key}": "not-an-approved-group", "aws:TagKeys": [key]}
        # What each enforced create needs to pass every other rule, so only the tag decides.
        def target(action, rtype):
            if action.startswith("ec2:"):
                extra = {"ec2:RunInstances": {"ec2:InstanceType": "t3.large", "ec2:Tenancy": "default"},
                         "ec2:CreateVolume": {"ec2:VolumeSize": 10, "ec2:VolumeType": "gp3"}}.get(action, {})
                return f"arn:aws:ec2:{home}:{acct}:{rtype}/*", home, extra, "allowed"
            if action == "iam:CreateRole":
                return role, g, {"iam:PermissionsBoundary": boundary}, user_only
            if action == "iam:CreatePolicy":
                return f"arn:aws:iam::{acct}:policy{path}verify-example", g, {}, user_only
            if action == "iam:CreateInstanceProfile":
                return f"arn:aws:iam::{acct}:instance-profile{path}verify-example", g, {}, user_only
            service = action.split(":")[0]
            arn = {"lambda": f"arn:aws:lambda:{home}:{acct}:function:verify-example",
                   "logs": f"arn:aws:logs:{home}:{acct}:log-group:verify-example",
                   "cloudformation": f"arn:aws:cloudformation:{home}:{acct}:stack/build-verify/x"}[service]
            return arn, home, {}, "allowed"
        for action, rtype in MUST_REQUIRE_GROUP:
            resource, region, extra, _ = target(action, rtype)
            add(Case(action, "denied", resource, region, **extra, **missing))
        for group in rg["enforced_on"]:
            for action in group["actions"]:
                if group["resources"] == "*":
                    types = [None]
                else:
                    if action not in EC2_CREATES:
                        raise SystemExit(f"verify.py: add {action} to EC2_CREATES (the resource types it creates)")
                    types = [t for t in EC2_CREATES[action] if t in group["resources"]]
                for rtype in types:
                    resource, region, extra, ok = target(action, rtype)
                    add(Case(action, ok, resource, region, **extra, **good))
                    add(Case(action, "denied", resource, region, **extra, **missing))
                    add(Case(action, "denied", resource, region, **extra, **wrong))
        instance = f"arn:aws:ec2:{home}:{acct}:instance/*"
        add(Case("ec2:DeleteTags", "denied", instance, home, **{"aws:TagKeys": [key]}))
        add(Case("ec2:DeleteTags", "allowed", instance, home, **{"aws:TagKeys": ["some-other-tag"]}))
        add(Case("ec2:CreateTags", "denied", instance, home, **wrong))
        add(Case("ec2:CreateTags", "allowed", instance, home, **good))
        add(Case("iam:UntagRole", "denied", role, g, **{"aws:TagKeys": [key]}))
        add(Case("lambda:UntagResource", "denied", f"arn:aws:lambda:{home}:{acct}:function:verify-example", home,
                 **{"aws:TagKeys": [key]}))
    return cases


def context_entries(region, ctx):
    entries = [f"ContextKeyName=aws:RequestedRegion,ContextKeyValues={region},ContextKeyType=string"]
    for key, value in sorted(ctx.items()):
        if isinstance(value, list):
            kind, value = "stringList", ",".join(value)
        else:
            kind = "numeric" if isinstance(value, int) else "string"
        entries.append(f"ContextKeyName={key},ContextKeyValues={value},ContextKeyType={kind}")
    return entries


def _whoami():
    r = subprocess.run(["aws", "sts", "get-caller-identity", "--query", "Arn", "--output", "text"],
                       capture_output=True, text=True)
    return r.stdout.strip() or "unknown (credentials missing or expired?)"


def aws(*args, retries=6):
    """Run an AWS CLI command; retry with backoff when IAM throttles (calls run in parallel)."""
    for attempt in range(retries):
        r = subprocess.run(["aws", *args, "--output", "json"], capture_output=True, text=True)
        if r.returncode == 0:
            return json.loads(r.stdout)
        if "Throttling" in r.stderr or "Rate exceeded" in r.stderr:
            time.sleep(min(2 ** attempt, 20) + random.random())
            continue
        message = f"aws {' '.join(args[:2])} failed:\n{r.stderr}"
        if "expired" in r.stderr.lower() or "ExpiredToken" in r.stderr:
            message += ("\nThe credentials have expired. Godmode credentials exported from CloudShell last about "
                        "an hour: export fresh ones (aws configure export-credentials --format env) and run again.")
        elif "AccessDenied" in r.stderr and "Simulate" in r.stderr:
            message += (f"\nRunning as: {_whoami()}\n"
                        "Before part 1 is applied only an admin can use the simulator: run this in the "
                        "terminal with the exported godmode credentials. After that, Claude's user can too.")
        raise SystemExit(message)
    raise SystemExit(f"aws {' '.join(args[:2])}: still throttled after {retries} attempts")


def live_policy(arn):
    version = aws("iam", "get-policy", "--policy-arn", arn)["Policy"]["DefaultVersionId"]
    return json.dumps(aws("iam", "get-policy-version", "--policy-arn", arn, "--version-id", version)
                      ["PolicyVersion"]["Document"])


def simulate(who, source, cases):
    """source: ('principal', arn) or ('custom', [docs], boundary_doc or None)"""
    groups = defaultdict(list)
    for c in cases:
        groups[(c.resource, c.region, tuple(sorted((k, tuple(v) if isinstance(v, list) else v)
                                                   for k, v in c.ctx.items())))].append(c)
    results = {}
    done = 0
    lock = threading.Lock()

    def run(item):
        (resource, region, ctx), group = item
        actions = sorted({c.action for c in group})
        common = ["--action-names", *actions, "--resource-arns", resource,
                  "--context-entries", *context_entries(region, {k: list(v) if isinstance(v, tuple) else v
                                                                 for k, v in ctx})]
        if source[0] == "principal":
            out = aws("iam", "simulate-principal-policy", "--policy-source-arn", source[1], *common)
        else:
            extra = ["--permissions-boundary-policy-input-list", source[2]] if source[2] else []
            out = aws("iam", "simulate-custom-policy", "--policy-input-list", *source[1], *extra, *common)
        decisions = {r["EvalActionName"]: r["EvalDecision"] for r in out["EvaluationResults"]}
        nonlocal done
        with lock:
            for c in group:
                results[id(c)] = decisions[c.action]
            done += 1
            _progress(f"simulating {who}: {done}/{len(groups)} calls")

    # Calls run in parallel (one AWS CLI process each); aws() retries if IAM throttles.
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        list(pool.map(run, groups.items()))
    _progress(f"simulating {who}: {len(groups)}/{len(groups)} calls, done", end=True)
    return results


def _progress(text, end=False):
    """Progress on stderr, so stdout (the results) stays the same and can be piped."""
    if sys.stderr.isatty():
        sys.stderr.write("\r" + text + ("\n" if end else ""))
    elif end:
        sys.stderr.write(text + "\n")
    sys.stderr.flush()


# --offline: a small local IAM evaluator, enough for the statements build-policies.py writes
# (Allow/Deny, Action/NotAction, Resource, and the condition operators below). An approximation
# for testing before AWS access exists; the real simulator (default and --local) is the authority.
def _like(pattern, value, ignore_case):
    from fnmatch import fnmatchcase
    return fnmatchcase(value.lower(), pattern.lower()) if ignore_case else fnmatchcase(value, pattern)


def _as_list(x):
    return x if isinstance(x, list) else [x]


def _condition_holds(op, key, wanted, ctx):
    if op.startswith("ForAnyValue:"):
        # True if any value of a multi-valued key meets the condition; false if the key is absent.
        values = _as_list(ctx.get(key, []))
        return any(_condition_holds(op.split(":", 1)[1], key, wanted, {key: v}) for v in values)
    wanted = [str(w) for w in _as_list(wanted)]
    present = key in ctx
    value = str(ctx.get(key, ""))
    if op == "Null":
        return (not present) if wanted[0] == "true" else present
    if op == "StringEquals":
        return present and value in wanted
    if op == "StringNotEquals":
        return (not present) or value not in wanted
    if op == "StringLike":
        return present and any(_like(w, value, False) for w in wanted)
    if op == "StringNotLike":
        return (not present) or not any(_like(w, value, False) for w in wanted)
    if op == "NumericGreaterThan":
        return present and any(float(value) > float(w) for w in wanted)
    raise SystemExit(f"offline evaluator: condition operator {op} not supported")


def _statement_matches(st, action, resource, ctx):
    if "Action" in st and not any(_like(a, action, True) for a in _as_list(st["Action"])):
        return False
    if "NotAction" in st and any(_like(a, action, True) for a in _as_list(st["NotAction"])):
        return False
    if not any(_like(r, resource, False) for r in _as_list(st.get("Resource", "*"))):
        return False
    return all(_condition_holds(op, key, wanted, ctx)
               for op, block in (st.get("Condition") or {}).items() for key, wanted in block.items())


def _evaluate(docs, action, resource, ctx):
    stmts = [st for d in docs for st in d["Statement"]]
    if any(st["Effect"] == "Deny" and _statement_matches(st, action, resource, ctx) for st in stmts):
        return "explicitDeny"
    if any(st["Effect"] == "Allow" and _statement_matches(st, action, resource, ctx) for st in stmts):
        return "allowed"
    return "implicitDeny"


def offline_decision(identity_docs, boundary_doc, case):
    ctx = {"aws:RequestedRegion": case.region, **case.ctx}
    d = _evaluate(identity_docs, case.action, case.resource, ctx)
    if boundary_doc is None or d != "allowed":
        return d
    return _evaluate([boundary_doc], case.action, case.resource, ctx)


def main():
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--local", action="store_true", help="simulate the drafts in out/ instead of the live policies")
    mode.add_argument("--offline", action="store_true", help="evaluate the drafts in out/ locally, without AWS (approximate)")
    args = ap.parse_args()
    p = yaml.safe_load((HERE / "policy.yaml").read_text())
    cfg = config.settings(dummy=args.offline)   # offline needs no real account
    acct = cfg["AWS_ACCOUNT_ID"]
    boundary_arn = f"arn:aws:iam::{acct}:policy/{p['iam']['boundary']}"
    cases = build_cases(p, cfg)
    rendered = lambda n: config.render((HERE / "out" / n).read_text(), cfg)  # noqa: E731

    if args.offline:
        out = HERE / "out"
        load = lambda n: json.loads(rendered(n))  # noqa: E731
        user_docs = [load("claude-assistant-policy.json"), load("claude-assistant-iam.json")]
        offline = {"user": (user_docs, None),
                   "role": ([json.loads(ADMIN)], load("claude-boundary.json"))}
        sources = {who: ("offline",) for who in offline}
    elif args.local:
        out = HERE / "out"
        user_docs = [rendered(n) for n in ("claude-assistant-policy.json", "claude-assistant-iam.json")]
        sources = {"user": ("custom", user_docs, None),
                   "role": ("custom", [ADMIN], rendered("claude-boundary.json"))}
    else:
        sources = {"user": ("principal", f"arn:aws:iam::{acct}:user/{cfg['AWS_CLAUDE_USER']}"),
                   "role": ("custom", [ADMIN], live_policy(boundary_arn))}

    failures = 0
    for who, source in sources.items():
        if source[0] == "offline":
            results = {id(c): offline_decision(*offline[who], c) for c in cases}
        else:
            results = simulate(who, source, cases)
        for c in cases:
            got = results[id(c)]
            want = c.expected_for(who)
            ok = (got == "allowed") == (want == "allowed")
            failures += not ok
            ctx = " ".join(f"{k}={v}" for k, v in sorted(c.ctx.items()))
            print(f"{'ok  ' if ok else 'FAIL'} {who:4} {want:7} {c.action} [{c.region}] {ctx}"
                  + ("" if ok else f"  <- got {got}"))
    where = "offline evaluator, drafts" if args.offline else "simulator, local drafts" if args.local else "simulator, live policies"
    print(f"\n{len(cases) * len(sources)} checks ({where}), {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
