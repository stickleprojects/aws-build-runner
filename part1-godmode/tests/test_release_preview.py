#!/usr/bin/env python3
"""Offline tests for release_preview.py (no AWS; works on a shallow checkout).

    python3 -I part1-godmode/tests/test_release_preview.py
"""
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
P1 = HERE.parent
spec = importlib.util.spec_from_file_location("release_preview", P1 / "release_preview.py")
rp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp)
failed = 0


def check(name, ok, detail=""):
    global failed
    print(f"{'ok  ' if ok else 'FAIL'}  {name}{'' if ok else ': ' + str(detail)[:400]}")
    failed += 0 if ok else 1


with tempfile.TemporaryDirectory() as d:
    role = Path(d) / "role.json"
    role.write_text(json.dumps({"Version": "2012-10-17", "Statement": [
        {"Sid": "A", "Effect": "Allow", "Action": ["iam:PutRolePolicy"], "Resource": "arn:aws:iam::{{ACCOUNT_ID}}:role/x",
         "Condition": {"StringEquals": {"iam:PermissionsBoundary": "arn:aws:iam::{{ACCOUNT_ID}}:policy/b"}}},
        {"Sid": "B", "Effect": "Deny", "Action": "iam:DeleteRolePermissionsBoundary", "Resource": "*"}]}))
    lines = rp.role_limits(role)
    check("an Allow is listed as May, with its condition", lines[0].startswith("- May: `iam:PutRolePolicy`") and "only" in lines[0], lines)
    check("a Deny is listed as Never", lines[1].startswith("- **Never**: `iam:DeleteRolePermissionsBoundary`"), lines)
    check("the account placeholder stays out of the summary", "{{" not in "\n".join(lines) and "<account>" in lines[0], lines)

    head = subprocess.run(["git", "-C", str(P1.parent), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    out = rp.preview("", head, ["part1-godmode/release_preview.py"], role, "r", "https://example")
    check("no earlier approved run: says to review all of it", "review all of it" in out, out)
    out = rp.preview("0" * 40, head, ["part1-godmode/release_preview.py"], role, "r", "https://example")
    check("an unknown earlier commit: says to review all of it", "review all of it" in out, out)
    out = rp.preview(head, head, ["part1-godmode/release_preview.py"], role, "r", "https://example")
    check("nothing changed since the last approved run: says None", "**None.**" in out, out)
    check("the role's limits come first", out.index("The most this run can change") < out.index("Code it executes"), out)

print(f"\n{failed} failed")
sys.exit(1 if failed else 0)
