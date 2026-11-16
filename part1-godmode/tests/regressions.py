#!/usr/bin/env python3
"""Regression rules: one per mistake that actually happened, so it can't happen again.

Run in CI on every PR (policy workflow) and locally:  python3 -I part1-godmode/tests/regressions.py
Each rule says what went wrong and when. When a fix is made for a new mistake, add a rule here.
Exit 1 if any rule fails.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
P1 = ROOT / "part1-godmode"
P2 = ROOT / "part2-runner"
SHELL_SCRIPTS = sorted(P1.glob("*.sh")) + sorted(P2.rglob("*.sh"))
PY_SCRIPTS = sorted(P1.glob("*.py")) + sorted((P1 / "tests").glob("*.py"))
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
IAM_TAG_VALUE = re.compile(r"[\w\s_.:/=+\-@]*", re.ASCII)
# Regions the kieron-wray-team organization's SCPs block (seen 2026-11-16).
ORG_BLOCKED_REGIONS = {"eu-west-2", "ap-southeast-4"}

RULES = []


def rule(incident):
    def register(fn):
        RULES.append((fn.__name__, incident, fn))
        return fn
    return register


def lines_matching(paths, pattern, exclude=()):
    hits = []
    for path in paths:
        if path.name in exclude:
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(pattern, line) and not line.lstrip().startswith("#"):
                hits.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()[:100]}")
    return hits


def bash(script):
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, cwd=P1)


@rule("2026-11-16: '(approved in environment aws)' in a tag value made iam tag-policy fail after an apply.")
def iam_tags_only_through_the_cleaning_helper():
    problems = lines_matching(SHELL_SCRIPTS, r"aws iam tag-policy", exclude={"lib.sh"})
    problems = [f"tag directly, not via tag_applied (lib.sh): {p}" for p in problems]
    samples = ["stickleprojects via GitHub Actions run 1, approved in environment aws (x)",
               "a [b] {c} <d> 'e' \"f\" ; g & h | i # j % k ! l ? m * n ~ o",
               "arn:aws:sts::123456789012:assumed-role/Role/session", "x" * 400]
    for s in samples:
        r = bash(f'. ./lib.sh; iam_tag_value {json.dumps(s)}')
        out = r.stdout.rstrip("\n")
        if r.returncode or not IAM_TAG_VALUE.fullmatch(out) or len(out) > 256:
            problems.append(f"iam_tag_value gives an invalid tag value for {s[:40]!r}: {out[:60]!r}")
    return problems


@rule("2026-11-16: a GitHub deployment status description over 140 characters got HTTP 422.")
def deployment_descriptions_only_through_the_truncating_helper():
    problems = [f"description set directly, not via gh_deployment_status (lib.sh): {p}"
                for p in lines_matching(SHELL_SCRIPTS, r"-f description=", exclude={"lib.sh"})]
    # The helper discards gh's stdout, so the stub reports its arguments on stderr.
    r = bash('gh() { printf "%s\\n" "$@" >&2; }; . ./lib.sh; gh_deployment_status o/r 1 success aws "$(printf "x%.0s" {1..300})"')
    desc = [a for a in r.stderr.splitlines() if a.startswith("description=")]
    if not desc or len(desc[0]) - len("description=") > 140:
        problems.append("gh_deployment_status doesn't cap the description at 140 characters")
    return problems


@rule("2026-11-16: JMESPath (aws --query) can't compare text with < or >; rollback's version lookup was always empty.")
def no_text_comparisons_in_aws_query():
    return [f"comparison inside --query (use jq): {p}"
            for p in lines_matching(SHELL_SCRIPTS + PY_SCRIPTS, r"--query[^|]*\[\?[^\]]*[<>]")]


@rule("2026-11-16: python3 -I leaves the script's folder off sys.path, so 'import config' failed.")
def local_imports_work_under_python_isolated_mode():
    problems = []
    for path in PY_SCRIPTS:
        text = path.read_text()
        if re.search(r"^import config|^from config", text, re.M) and "sys.path.insert" not in text:
            problems.append(f"{path.relative_to(ROOT)} imports config without adding its folder to sys.path")
    problems += [f"inline 'import config' under -I (use config.py's CLI): {p}"
                 for p in lines_matching(SHELL_SCRIPTS + WORKFLOWS, r"python3 -I -c .*import config")]
    return problems


@rule("2026-11-16: 'gh variable get' doesn't exist in older gh (2.46); config.py failed silently.")
def no_gh_variable_get():
    return [f"use 'gh api repos/{{owner}}/{{repo}}/actions/variables/NAME' instead: {p}"
            for p in lines_matching(SHELL_SCRIPTS + PY_SCRIPTS + WORKFLOWS, r"gh variable get",
                                    exclude={"regressions.py"})]


@rule("2026-11-16: the apply role's trust used name-based subjects, but this repo sends GitHub's immutable "
      "(ID-based) subject, so the first automated apply couldn't sign in. Access Analyzer also asked for a branch check.")
def apply_role_trust_uses_immutable_subject_and_main_only():
    trust = json.loads((P1 / "bootstrap" / "apply-role-trust.json").read_text())
    problems = []
    for st in trust["Statement"]:
        cond = st.get("Condition", {}).get("StringEquals", {})
        subs = cond.get("token.actions.githubusercontent.com:sub", [])
        subs = subs if isinstance(subs, list) else [subs]
        if not subs:
            problems.append("trust has no token.actions.githubusercontent.com:sub condition")
        for sub in subs:
            if not re.fullmatch(r"repo:[^/@]+@\d+/[^:@]+@\d+:environment:[\w-]+", sub):
                problems.append(f"sub isn't the immutable form repo:OWNER@ID/REPO@ID:environment:ENV: {sub}")
        if cond.get("token.actions.githubusercontent.com:ref") != "refs/heads/main":
            problems.append("trust doesn't require token.actions.githubusercontent.com:ref = refs/heads/main")
        if cond.get("token.actions.githubusercontent.com:aud") != "sts.amazonaws.com":
            problems.append("trust doesn't require aud = sts.amazonaws.com")
    return problems


@rule("2026-11-16: an ERR trap needs set -E to fire inside functions (apply.sh's failure record).")
def err_traps_have_errtrace():
    problems = []
    for path in SHELL_SCRIPTS:
        text = path.read_text()
        if re.search(r"^\s*trap .* ERR\b", text, re.M) and not re.search(r"^set -[a-zA-Z]*E", text, re.M):
            problems.append(f"{path.relative_to(ROOT)} traps ERR without set -E")
    return problems


@rule("2026-11-16: literal tab characters in workflow bash were fragile; replaced with '|'.")
def no_tabs_in_workflows():
    return [f"{p.relative_to(ROOT)} contains a tab character" for p in WORKFLOWS if "\t" in p.read_text()]


@rule("2026-11-16: verify.py's 'other region' was eu-west-2, which the organization's SCPs block, so a live check "
      "failed and region tests passed for the wrong reason.")
def other_region_is_not_blocked_by_the_organization():
    text = (P1 / "verify.py").read_text()
    m = re.search(r'^OTHER_REGION = "([a-z0-9-]+)"', text, re.M)
    if not m:
        return ["verify.py has no OTHER_REGION"]
    return [f"OTHER_REGION {m.group(1)} is blocked by the organization; pick one it allows"] \
        if m.group(1) in ORG_BLOCKED_REGIONS else []


@rule("2026-11-16: exported CloudShell credentials (~15 min) expired mid-apply; apply.sh must refuse when they're about to.")
def apply_keeps_the_credential_expiry_guard():
    return [] if "AWS_CREDENTIAL_EXPIRATION" in (P1 / "apply.sh").read_text() \
        else ["apply.sh lost its AWS_CREDENTIAL_EXPIRATION guard"]


@rule("2026-11-16: verify.py simulated EC2 creates against resource types they don't create (24 false failures).")
def every_enforced_ec2_create_has_its_resource_types():
    import yaml
    sys.path.insert(0, str(P1))
    import importlib.util
    spec = importlib.util.spec_from_file_location("verify", P1 / "verify.py")
    verify = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verify)
    p = yaml.safe_load((P1 / "policy.yaml").read_text())
    problems = []
    for group in (p.get("resource_groups") or {}).get("enforced_on", []):
        for action in group["actions"]:
            if action.startswith("ec2:") and action not in verify.EC2_CREATES:
                problems.append(f"{action} is enforced but missing from verify.EC2_CREATES")
    return problems


@rule("2026-11-16: two real AMI bakes reported nothing: EC2's console capture came back empty, so bake.sh never "
      "saw BAKE-OK and made no AMI, with no clue why. The result now goes through SSM (files/report.py).")
def bake_result_comes_back_through_ssm():
    setup = (P2 / "ami" / "setup.sh").read_text()
    bake = (P2 / "ami" / "bake.sh").read_text()
    problems = []
    if not (P2 / "ami" / "files" / "report.py").is_file():
        problems.append("files/report.py is missing")
    if not re.search(r"^report STARTED", setup, re.M):
        problems.append("setup.sh doesn't report STARTED first (proves the channel before anything else)")
    if "report FAILED" not in setup:
        problems.append("setup.sh doesn't report FAILED with its log")
    if "/build/bake/result/" not in bake or "--iam-instance-profile" not in bake:
        problems.append("bake.sh doesn't launch with the bake role and read /build/bake/result/<instance>")
    if re.search(r"grep .*BAKE-OK.*console|console.*BAKE-OK", bake):
        problems.append("bake.sh relies on the console output again")
    return problems


@rule("2026-11-16: make-user-data.py tried to pack a __pycache__ folder left in files/ by a test import.")
def user_data_packs_only_regular_files():
    text = (P2 / "ami" / "make-user-data.py").read_text()
    return [] if "is_file()" in text else ["make-user-data.py packs everything in files/, not only regular files"]


@rule("2026-11-16: the third bake reported STARTED, then nothing: the clean-up deleted /opt/bake (and report.py "
      "with it) before the final OK report, so a probably good bake made no AMI.")
def bake_reporter_survives_the_cleanup():
    setup = (P2 / "ami" / "setup.sh").read_text()
    m = re.search(r"^REPORTER=(\S+)", setup, re.M)
    if not m:
        return ["setup.sh doesn't copy report.py to a fixed REPORTER path"]
    removed = re.findall(r"rm -rf ([^\n]+)", setup)
    hits = [r for r in removed for path in r.split() if m.group(1).startswith(path.rstrip("/*")) and path.startswith("/")]
    return [f"the clean-up ({hits[0]}) deletes the reporter {m.group(1)}"] if hits else []


@rule("2026-11-16: the first good bake stopped at 'aws ec2 wait image-available', which gives up after 10 min, "
      "while the 30 GB AMI took longer; the AMI id wasn't recorded.")
def no_short_aws_waiters_for_images():
    return [f"use a polling loop with a long deadline, not the 10-minute CLI waiter: {p}"
            for p in lines_matching(SHELL_SCRIPTS, r"aws ec2 wait image-available")]


@rule("2026-11-16: the one-job service had After=cloud-final.service and WantedBy=multi-user.target; on Ubuntu "
      "cloud-final runs after multi-user, so systemd dropped the service to break the cycle and it never started.")
def runner_service_has_no_ordering_cycle():
    unit = (P2 / "ami" / "files" / "github-runner-jit.service").read_text()
    after = " ".join(re.findall(r"^After=(.*)$", unit, re.M))
    if "multi-user.target" in unit and "cloud-final" in after:
        return ["github-runner-jit.service orders After=cloud-final.service but is WantedBy=multi-user.target (cycle)"]
    return []


@rule("2026-11-16 (Kieron): jobs must not run as root; like a Dockerfile USER, the runner service runs as ghrunner "
      "with NoNewPrivileges, and the boot step blocks ghrunner from the instance metadata service.")
def jobs_run_unprivileged_and_without_imds():
    unit = (P2 / "ami" / "files" / "github-runner-jit.service").read_text()
    boot = (P2 / "ami" / "files" / "build-runner-boot.sh").read_text()
    start = (P2 / "ami" / "files" / "start-jit.sh").read_text()
    problems = []
    for line in ("User=ghrunner", "NoNewPrivileges=yes", "ProtectSystem=full", "PrivateTmp=yes"):
        if not re.search(rf"^{re.escape(line)}$", unit, re.M):
            problems.append(f"github-runner-jit.service lost {line}")
    for addr in ("169.254.169.254", "fd00:ec2::254"):
        if not re.search(rf"--uid-owner ghrunner.*{re.escape(addr)}|{re.escape(addr)}.*--uid-owner ghrunner", boot) \
                and addr not in boot:
            problems.append(f"build-runner-boot.sh doesn't block ghrunner from {addr}")
    if "--uid-owner ghrunner" not in boot:
        problems.append("build-runner-boot.sh has no owner-match rule for ghrunner")
    if "169.254.169.254" in start:
        problems.append("start-jit.sh (runs as ghrunner) talks to IMDS; the boot step should hand it the jitconfig")
    return problems


@rule("2026-11-16: the bake gets spellbound3d with a short-lived GitHub token. It must never be in user data "
      "(readable via DescribeInstanceAttribute) and must be deleted from SSM and revoked when the bake ends.")
def bake_token_stays_out_of_user_data_and_is_revoked():
    problems = []
    r = subprocess.run([sys.executable, "-I", str(P2 / "ami" / "make-user-data.py")], capture_output=True, text=True)
    if r.returncode:
        problems.append(f"make-user-data.py failed: {r.stderr.strip()[:200]}")
    elif re.search(r"gh[sp]_[A-Za-z0-9]{20,}|github_pat_", r.stdout):
        problems.append("the user data contains a GitHub token")
    bake = (P2 / "ami" / "bake.sh").read_text()
    cleanup = bake[bake.find("cleanup() {"):bake.find("trap cleanup EXIT")]
    if "github_app_revoke" not in cleanup or "/build/bake/github-token" not in cleanup:
        problems.append("bake.sh's cleanup doesn't both delete /build/bake/github-token and revoke the token")
    setup = (P2 / "ami" / "setup.sh").read_text()
    if "--get /build/bake/github-token" not in setup:
        problems.append("setup.sh doesn't fetch the token from SSM")
    return problems


@rule("2026-11-16: bake.sh made a new AMI current before its smoke test, and that AMI then failed the test "
      "(Claude's npm cache couldn't install offline). Only a smoke-tested image may become current.")
def only_smoke_tested_images_become_current():
    bake = (P2 / "ami" / "bake.sh").read_text()
    smoke = (P2 / "ami" / "smoke-test.sh").read_text()
    problems = []
    if re.search(r"put-parameter --name /build/runner/ami-id\b", bake):
        problems.append("bake.sh sets /build/runner/ami-id directly; it must only set the candidate")
    if not re.search(r"put-parameter --name /build/runner/ami-id\b", smoke):
        problems.append("smoke-test.sh doesn't promote the candidate after passing")
    return problems


@rule("2026-11-16: 'npm cache add' cached Claude Code without its platform-native binary, so the offline "
      "install that the warm cache exists for failed.")
def claude_cache_is_warmed_by_a_real_install():
    return [f"warms Claude Code with 'npm cache add' (misses the native binary): {p}"
            for p in lines_matching([P2 / "ami" / "setup.sh"], r"npm cache add")]


@rule("2026-11-16 (Kieron): no AWS bill over £30, and at the limit everything stops spending: the budget counts "
      "what would be billed across the whole account (credits reduce it, refunds don't), budget-stop blocks anything new, and the kill switch terminates instances "
      "and deletes the runner AMIs. Claude can't touch the kill switch.")
def budget_stop_covers_everything():
    problems = []
    stop = json.loads((P1 / "bootstrap" / "budget-stop-policy.json").read_text())
    denied = {a for st in stop["Statement"] if st["Effect"] == "Deny" for a in st["Action"]}
    for must in ("ec2:RunInstances", "ec2:StartInstances", "ec2:RequestSpotInstances", "ec2:CreateFleet",
                 "ec2:CreateVolume", "lambda:CreateFunction", "cloudformation:CreateStack", "iam:PassRole"):
        if must not in denied:
            problems.append(f"budget-stop no longer denies {must}")
    script = (P1 / "bootstrap-budget-stop.sh").read_text()
    # Credits are applied on the invoice (never billed); refunds come back later, if at all (2026-11-16).
    if "IncludeCredit = true" not in script or "IncludeRefund = false" not in script or "IncludeTax = true" not in script:
        problems.append("bootstrap-budget-stop.sh doesn't make the budget count what would be billed")
    apply = (P1 / "apply.sh").read_text().replace(" ", "")
    if "IncludeCredit:true" not in apply or "IncludeRefund:false" not in apply:
        problems.append("apply.sh creates the budget without credits on and refunds off")
    if "--approval-model AUTOMATIC" not in script:
        problems.append("the budget action isn't automatic")
    # Kieron (2026-11-16): at the limit, everything must stop spending, not just new things.
    import yaml
    p = yaml.safe_load((P1 / "policy.yaml").read_text())
    ks = ((p.get("account_settings") or {}).get("budget_stop") or {}).get("kill_switch") or {}
    if not (ks.get("terminate_instances") and ks.get("delete_runner_amis")):
        problems.append("policy.yaml budget_stop.kill_switch no longer terminates instances and deletes runner AMIs")
    code = (P1 / "bootstrap" / "kill_switch.py").read_text()
    for call in ("terminate_instances", "cancel_spot_instance_requests", "deregister_image", "delete_snapshot"):
        if call not in code:
            problems.append(f"kill_switch.py no longer calls {call}")
    if "create-subscriber" not in script or "SubscriptionType=SNS" not in script:
        problems.append("the budget no longer notifies the kill switch")
    protected = [r["arn"] for r in (p.get("protected") or {}).get("resources", [])]
    if not any("function:guardrail-" in a for a in protected):
        problems.append("the kill switch (guardrail-* Lambda) isn't protected from Claude in policy.yaml")
    if int((p.get("account_settings") or {}).get("budget_usd", 999)) > 30:
        problems.append("budget_usd is above US$30: no headroom under the £30 (≈US$38) bill ceiling for billing lag")
    return problems


def main():
    failed = 0
    for name, incident, fn in RULES:
        problems = fn()
        print(f"{'ok  ' if not problems else 'FAIL'} {name}")
        for p in problems:
            print(f"       {p}")
        if problems:
            print(f"       why: {incident}")
            failed += 1
    print(f"\n{len(RULES)} regression rules, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
