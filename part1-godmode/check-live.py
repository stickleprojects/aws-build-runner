#!/usr/bin/env python3
"""Compare what's live in AWS with the policies in this checkout (normally main). Read-only.

For each policy: does the live default version match out/<file>, and which commit was applied,
when, and by whom (the applied-* tags that apply.sh writes)? Also checks that the user has exactly
the policies policy.yaml expects.

  check-live.py                        report; exit 1 on drift or a missing policy
  check-live.py --expect-commit SHA    also exit 1 unless every policy is tagged with SHA

Runs as Claude's IAM user (iam:Get*/List*) or an admin. Account and user come from config.py.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I leaves the script dir off sys.path
import config  # noqa: E402

HERE = Path(__file__).resolve().parent


def aws(*args):
    r = subprocess.run(["aws", *args, "--output", "json"], capture_output=True, text=True)
    if r.returncode:
        return None, r.stderr.strip()
    return json.loads(r.stdout), None


def canonical(doc):
    return json.dumps(doc, sort_keys=True, separators=(",", ":"))


def git(*args):
    r = subprocess.run(["git", "-C", str(HERE), *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--expect-commit")
    args = ap.parse_args()

    p = yaml.safe_load((HERE / "policy.yaml").read_text())
    cfg = config.settings()
    acct, user = cfg["AWS_ACCOUNT_ID"], cfg["AWS_CLAUDE_USER"]
    policies = {
        "claude-assistant-policy.json": f"arn:aws:iam::{acct}:policy/claude-assistant-policy",
        "claude-assistant-iam.json": f"arn:aws:iam::{acct}:policy/claude-assistant-iam",
        "claude-boundary.json": f"arn:aws:iam::{acct}:policy/{p['iam']['boundary']}",
    }
    here = git("rev-parse", "HEAD")
    print(f"Checkout: {git('rev-parse', '--abbrev-ref', 'HEAD') or '?'} @ {here[:12] if here else '?'}")
    print()
    problems = []
    applied = set()
    print(f"{'policy':28} {'live = checkout':15} {'applied commit':14} {'applied at':22} applied by")
    for name, arn in policies.items():
        meta, err = aws("iam", "get-policy", "--policy-arn", arn)
        if err:
            print(f"{name:28} {'MISSING':15}")
            problems.append(f"{name}: not found in AWS ({err.splitlines()[-1]})")
            continue
        version = meta["Policy"]["DefaultVersionId"]
        live, _ = aws("iam", "get-policy-version", "--policy-arn", arn, "--version-id", version)
        tags_resp, _ = aws("iam", "list-policy-tags", "--policy-arn", arn)
        tags = {t["Key"]: t["Value"] for t in (tags_resp or {}).get("Tags", [])}
        local = json.loads(config.render((HERE / "out" / name).read_text(), cfg))
        same = canonical(live["PolicyVersion"]["Document"]) == canonical(local)
        commit = tags.get("applied-commit", "-")
        applied.add(commit)
        print(f"{name:28} {'yes' if same else 'NO (drift)':15} {commit[:12]:14} "
              f"{tags.get('applied-at', '-'):22} {tags.get('applied-by', '-')}")
        if not same:
            problems.append(f"{name}: live version {version} differs from out/{name}")
        if args.expect_commit and commit != args.expect_commit:
            problems.append(f"{name}: tagged with commit {commit}, expected {args.expect_commit}")

    attached, err = aws("iam", "list-attached-user-policies", "--user-name", user)
    if attached is not None:
        have = {a["PolicyArn"] for a in attached["AttachedPolicies"]}
        want = {policies["claude-assistant-policy.json"], policies["claude-assistant-iam.json"]}
        for extra in sorted(have - want):
            problems.append(f"{user} also has {extra} attached (not in policy.yaml)")
        for missing in sorted(want - have):
            problems.append(f"{user} is missing {missing}")
    else:
        problems.append(f"can't list {user}'s policies: {err}")

    print()
    if here and applied == {here}:
        print("AWS has this checkout's commit applied.")
    elif here:
        print(f"NOT YET APPLIED: this checkout is {here[:12]}; AWS has {', '.join(sorted(c[:12] for c in applied))}.")
    for problem in problems:
        print(f"PROBLEM: {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
