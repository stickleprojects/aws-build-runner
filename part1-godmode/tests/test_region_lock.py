#!/usr/bin/env python3
"""Offline tests for the organization's region lock, #30 fix 7: two SCPs, evaluated together.
    bootstrap/org-region-lock-scp.json       in us-east-1 only the named global services; elsewhere nothing
    bootstrap/org-sydney-services-scp.json   in Sydney: only the named services we use

    python3 -I part1-godmode/tests/test_region_lock.py

Evaluates the SCP's denies the way AWS does (an action matching a Deny's Action, or not matching its NotAction,
with its region condition true, is denied), against:
  - every AWS call our scripts, workflows and Python make, in the region each runs in (Sydney, or us-east-1
    for the global services): all must stay allowed;
  - sign-in, console and billing calls in us-east-1, where Identity Center lives (checked 2026-11-27);
  - things that must be refused: regional services outside Sydney, us-west-2, Bedrock anywhere.
"""
import fnmatch
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
P1 = HERE.parent
ROOT = P1.parent
SCPS = [json.loads((P1 / "bootstrap" / f).read_text()) for f in ("org-region-lock-scp.json", "org-sydney-services-scp.json")]
failed = 0

# CLI service names that aren't the IAM prefix.
PREFIX = {"s3api": "s3", "s3control": "s3", "accessanalyzer": "access-analyzer", "sso-admin": "sso",
          "logs": "logs", "events": "events", "scheduler": "scheduler", "resourcegroupstaggingapi": "tag",
          "resource-groups": "resource-groups"}
# Global services: their calls go to us-east-1 whatever region the CLI is set to.
GLOBAL = {"iam", "organizations", "budgets", "ce", "support", "account", "pricing", "health", "cur"}


def check(name, ok, detail=""):
    global failed
    print(f"{'ok  ' if ok else 'FAIL'}  {name}{'' if ok else ': ' + str(detail)[:300]}")
    failed += 0 if ok else 1


def matches(action, patterns):
    return any(fnmatch.fnmatchcase(action.lower(), p.lower()) for p in patterns)


def denied(action, region):
    for st in (st for scp in SCPS for st in scp["Statement"]):
        if st["Effect"] != "Deny":
            continue
        if "Action" in st and not matches(action, st["Action"] if isinstance(st["Action"], list) else [st["Action"]]):
            continue
        if "NotAction" in st and matches(action, st["NotAction"]):
            continue
        cond = st.get("Condition", {})
        ok = True
        for op, kv in cond.items():
            for key, vals in kv.items():
                vals = vals if isinstance(vals, list) else [vals]
                if key != "aws:RequestedRegion":
                    raise ValueError(f"unexpected condition key {key}")
                ok = ok and (region not in vals if op == "StringNotEquals" else region in vals)
        if ok:
            return st["Sid"]
    return None


def our_calls():
    """(prefix:Action, file) for every aws CLI call and aws(...) helper call in our code."""
    calls = set()
    files = [p for p in ROOT.rglob("*") if p.suffix in (".sh", ".py", ".yml", ".yaml")
             and ".git" not in p.parts and "tests" not in p.parts and "out" not in p.parts]
    for f in files:
        text = f.read_text(errors="ignore")
        for svc, cmd in re.findall(r"\baws ([a-z0-9-]+) ([a-z][a-z0-9-]+)", text):
            calls.add((svc, cmd, f.name))
        for svc, cmd in re.findall(r'\baws\("([a-z0-9-]+)", "([a-z][a-z0-9-]+)"', text):
            calls.add((svc, cmd, f.name))
    out = set()
    for svc, cmd, fname in calls:
        if svc in ("configure", "help") or cmd in ("help", "wait") or svc.startswith("-"):
            continue
        prefix = PREFIX.get(svc, svc)
        action = prefix + ":" + "".join(w.capitalize() for w in cmd.split("-"))
        out.add((action, fname))
    return sorted(out)


calls = our_calls()
check("found our AWS calls to test", len(calls) > 60, len(calls))
for action, fname in calls:
    prefix = action.split(":")[0]
    region = "us-east-1" if prefix in GLOBAL else "ap-southeast-2"
    sid = denied(action, region)
    check(f"ours stays allowed: {action} in {region} ({fname})", sid is None, sid)

for action in ("sts:GetCallerIdentity", "sts:AssumeRoleWithWebIdentity"):
    check(f"STS works at Sydney's regional endpoint and the global one: {action}",
          denied(action, "ap-southeast-2") is None and denied(action, "us-east-1") is None)
for action in ("sso:ListInstances", "identitystore:ListUsers", "signin:AuthorizeOAuth2Access", "sso-oauth:CreateToken",
               "billing:GetBillingData", "aws-portal:ViewBilling", "budgets:ViewBudget", "ce:GetCostAndUsage",
               "support:CreateCase", "health:DescribeEvents", "cloudtrail:LookupEvents", "uxc:GetAccountColor"):
    check(f"sign-in, billing and console keep working in us-east-1: {action}", denied(action, "us-east-1") is None)
check("CloudShell works in Sydney", denied("cloudshell:CreateEnvironment", "ap-southeast-2") is None)
# Checked in the region of the call that uses them: RunInstances in Sydney passes the runner role there.
for action in ("iam:PassRole", "kms:CreateGrant", "kms:Decrypt", "iam:CreateServiceLinkedRole", "sts:AssumeRole"):
    check(f"works inside a Sydney call: {action}", denied(action, "ap-southeast-2") is None)
# Every service Claude's own policy allows must still work where it runs (global ones in us-east-1).
claude = set()
for f in (P1 / "out").glob("*.json"):
    for st in json.loads(f.read_text()).get("Statement", []):
        if st["Effect"] == "Allow":
            acts = st.get("Action", [])
            claude |= {a.split(":")[0] for a in ([acts] if isinstance(acts, str) else acts)}
for prefix in sorted(claude):
    region = "us-east-1" if prefix in GLOBAL else "ap-southeast-2"
    check(f"Claude's approved service {prefix} works in {region}", denied(prefix + ":Describe", region) is None)

for action, region in (("ec2:RunInstances", "us-east-1"), ("ec2:RunInstances", "us-west-2"),
                       ("ec2:RunInstances", "eu-west-1"), ("ssm:StartSession", "us-east-1"),
                       ("lambda:CreateFunction", "us-east-1"), ("s3:CreateBucket", "us-west-2"),
                       ("cloudtrail:CreateTrail", "us-east-1"), ("logs:CreateLogGroup", "us-east-1"),
                       ("kms:CreateKey", "us-east-1"), ("cloudshell:CreateEnvironment", "us-east-1"),
                       ("bedrock:InvokeModel", "ap-southeast-2"), ("bedrock:InvokeModel", "us-east-1"),
                       ("bedrock-mantle:CreateInference", "us-west-2"),
                       ("iam:CreateRole", "eu-west-1"), ("sso:ListInstances", "us-west-2"),
                       ("sagemaker:CreateNotebookInstance", "ap-southeast-2"), ("dynamodb:CreateTable", "ap-southeast-2"),
                       ("bedrock:InvokeModel", "ap-southeast-2"), ("rds:CreateDBInstance", "ap-southeast-2"),
                       ("ec2:RunInstances", "unspecified")):
    check(f"refused: {action} in {region}", denied(action, region) is not None)

print(f"\n{failed} failed")
sys.exit(1 if failed else 0)
