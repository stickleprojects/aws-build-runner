#!/usr/bin/env python3
"""Offline tests for check_approvals.py (#23): only Kieron's GitHub review approves a policy change.

    python3 -I part1-godmode/tests/test_check_approvals.py
"""
import copy
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("check_approvals", HERE.parent / "check_approvals.py")
ca = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ca)
failed = 0


def check(name, ok, detail=""):
    global failed
    print(f"{'ok  ' if ok else 'FAIL'}  {name}{'' if ok else ': ' + str(detail)[:300]}")
    failed += 0 if ok else 1


HEAD, OLD = "a" * 40, "b" * 40
BASE = {"services": [{"title": "EC2", "approved": "2026-11-16 Kieron"}], "global": [],
        "changes": [{"date": "2026-11-16", "what": "first", "approved": "2026-11-16 Kieron"}]}
OWNERS = ca.owners("# who reviews\n* @stickleprojects\n")


def with_change(approved):
    d = copy.deepcopy(BASE)
    d["changes"].append({"date": "2026-11-28", "what": "new thing", "approved": approved})
    return d


def review(login, state, sha, when="2026-11-28T01:00:00Z"):
    return {"user": {"login": login}, "state": state, "commit_id": sha, "submitted_at": when}


KIERON_OK = [review("stickleprojects", "APPROVED", HEAD)]

check("CODEOWNERS' owner is read", OWNERS == {"stickleprojects"}, OWNERS)
p, r = ca.decide(BASE, BASE, 84, HEAD, [], OWNERS)
check("no approval field changed: passes without a review", not p, p)
p, r = ca.decide(BASE, with_change("review on PR #84"), 84, HEAD, KIERON_OK, OWNERS)
check("Kieron approved the latest commit: passes, and says who and what", not p and "stickleprojects" in r[0] and len(r) == 2, (p, r))
p, _ = ca.decide(BASE, with_change("pending"), 84, HEAD, KIERON_OK, OWNERS)
check("still pending: fails", any("pending" in x for x in p), p)
p, _ = ca.decide(BASE, with_change("2026-11-28 Kieron"), 84, HEAD, KIERON_OK, OWNERS)
check("a hand-written date and name: fails (only his review approves)", any("only Kieron's GitHub review" in x for x in p), p)
p, _ = ca.decide(BASE, with_change("review on PR #83"), 84, HEAD, KIERON_OK, OWNERS)
check("another PR's review: fails", p != [], p)
p, _ = ca.decide(BASE, with_change("review on PR #84"), 84, HEAD, [], OWNERS)
check("no review: fails", any("no approving review" in x for x in p), p)
p, _ = ca.decide(BASE, with_change("review on PR #84"), 84, HEAD, [review("stickleprojects", "APPROVED", OLD)], OWNERS)
check("approved an older commit, then a new push: fails", any("latest" in x for x in p), p)
p, _ = ca.decide(BASE, with_change("review on PR #84"), 84, HEAD, [review("stickleprojects-claude[bot]", "APPROVED", HEAD)], OWNERS)
check("approved by the App (not a code owner): fails", p != [], p)
p, _ = ca.decide(BASE, with_change("review on PR #84"), 84, HEAD,
                 KIERON_OK + [review("stickleprojects", "CHANGES_REQUESTED", HEAD, "2026-11-28T02:00:00Z")], OWNERS)
check("approved, then changes requested: fails", p != [], p)
p, _ = ca.decide(BASE, with_change("review on PR #84"), 84, HEAD,
                 [review("stickleprojects", "COMMENTED", HEAD)] + KIERON_OK + [review("stickleprojects", "COMMENTED", HEAD)], OWNERS)
check("comments around an approval don't undo it", not p, p)
s = copy.deepcopy(BASE); s["services"][0]["approved"] = "review on PR #84"
p, _ = ca.decide(BASE, s, 84, HEAD, KIERON_OK, OWNERS)
check("a service's approval changed counts too", not p, p)
p, _ = ca.decide(BASE, with_change("review on PR #84"), 84, HEAD, KIERON_OK, set())
check("no CODEOWNERS owner: fails", any("CODEOWNERS" in x for x in p), p)

print(f"\n{failed} failed")
sys.exit(1 if failed else 0)
