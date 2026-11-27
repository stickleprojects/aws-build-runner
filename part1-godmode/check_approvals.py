#!/usr/bin/env python3
"""The 'approved' check (#23): a policy change is approved only by Kieron's GitHub review.

    check_approvals.py --base SHA --head SHA --pr N --repo OWNER/REPO     (GITHUB_TOKEN in the environment)

Every approval field this pull request adds or changes in policy.yaml (services, global, the sections, the
change list) must read `review on PR #N`, for this PR. Anything else fails, including a hand-written date and
name: Claude's gh can't approve as Kieron any more, so only his review counts. Then a code owner
(.github/CODEOWNERS) must have approved this PR's latest commit; a newer push needs a new review. A pull
request that changes no approval field passes here (branch protection still requires the review). Exit 1 with
the reasons, 2 if it couldn't run.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
POLICY = "part1-godmode/policy.yaml"
SECTIONS = ("read_only", "iam", "resource_groups", "protected", "cost_boundary")


def approvals(doc: dict) -> dict:
    """{label: approved value} for every approval field in policy.yaml."""
    out = {f"service: {s['title']}": s.get("approved") for s in doc.get("services", [])}
    out |= {f"global: {g['title']}": g.get("approved") for g in doc.get("global", [])}
    out |= {key: doc[key].get("approved") for key in SECTIONS if key in doc}
    out |= {f"change {c['date']}: {str(c['what'])[:80]}": c.get("approved") for c in doc.get("changes", [])}
    return {k: (str(v).strip() if v is not None else None) for k, v in out.items()}


def owners(codeowners: str) -> set:
    for line in codeowners.splitlines():
        f = line.split("#", 1)[0].split()
        if f and f[0] == "*":
            return {o.lstrip("@").lower() for o in f[1:]}
    return set()


def decide(base: dict, head: dict, pr: int, head_sha: str, reviews: list, code_owners: set) -> tuple[list, list]:
    """(problems, record). Pure, so it's tested offline."""
    problems, record = [], []
    changed = {k: v for k, v in approvals(head).items() if approvals(base).get(k) != v}
    if not changed:
        return [], ["No approval field changes in policy.yaml."]
    want = f"review on PR #{pr}"
    for label, value in changed.items():
        if value != want:
            problems.append(f"{label}: approved is {value!r}; it must be {want!r} (only Kieron's GitHub review approves)")
    latest = {}
    for r in reviews:   # in order; a later review by the same person replaces an earlier one
        login = (r.get("user") or {}).get("login", "").lower()
        if r.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[login] = r
    ok = [r for login, r in latest.items() if login in code_owners and r["state"] == "APPROVED"
          and r.get("commit_id") == head_sha]
    if not code_owners:
        problems.append("no code owner in .github/CODEOWNERS ('* @<login>')")
    elif not ok:
        problems.append(f"no approving review by a code owner ({', '.join(sorted(code_owners))}) on the latest "
                        f"commit {head_sha[:12]}; approve the PR in GitHub (a newer push needs a new approval)")
    else:
        r = ok[0]
        record = [f"Approved by {r['user']['login']} in GitHub ({r.get('submitted_at', '')[:16]}) on {head_sha[:12]}:"]
        record += [f"- {label}" for label in changed]
    return problems, record


def show(sha: str, path: str) -> str:
    return subprocess.run(["git", "-C", str(REPO), "show", f"{sha}:{path}"], capture_output=True, text=True,
                          check=True).stdout


def reviews_of(repo: str, pr: int, token: str) -> list:
    out, page = [], 1
    while True:
        req = urllib.request.Request(f"https://api.github.com/repos/{repo}/pulls/{pr}/reviews?per_page=100&page={page}",
                                     headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            batch = json.load(resp)
        out += batch
        if len(batch) < 100:
            return out
        page += 1


def main(argv) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base", required=True)
    p.add_argument("--head", required=True)
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--repo", required=True)
    a = p.parse_args(argv[1:])
    try:
        if not re.fullmatch(r"[0-9a-f]{40}", a.head) or not re.fullmatch(r"[0-9a-f]{40}", a.base):
            raise ValueError("--base and --head must be full commit SHAs")
        base, head = yaml.safe_load(show(a.base, POLICY)), yaml.safe_load(show(a.head, POLICY))
        code_owners = owners(show(a.head, ".github/CODEOWNERS"))
        changed = {k for k, v in approvals(head).items() if approvals(base).get(k) != v}
        reviews = reviews_of(a.repo, a.pr, os.environ["GITHUB_TOKEN"]) if changed else []
        problems, record = decide(base, head, a.pr, a.head, reviews, code_owners)
    except Exception as e:  # noqa: BLE001 - a check that can't run must fail, not pass
        print(f"check_approvals.py couldn't run: {type(e).__name__}: {e}")
        return 2
    for line in problems or record:
        print(line)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
