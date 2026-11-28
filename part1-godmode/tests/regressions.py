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


@rule("2026-11-17: the launcher read Kieron's curfew override through parameter(), which is cached for the life of a "
      "warm Lambda: after /resume it still said 'extended'. An extend or stop could be ignored for hours.")
def curfew_override_is_read_fresh():
    src = (P2 / "stack" / "lambda" / "runner.py").read_text()
    m = re.search(r"def curfew_override\(\).*?(?=\ndef )", src, re.S)
    if not m:
        return ["runner.py has no curfew_override()"]
    body = re.sub(r'"""(.|\n)*?"""|#.*', "", m.group(0))  # the code, not its docstring or comments
    problems = []
    if re.search(r"(?<![\w.])parameter\(", body):
        problems.append("curfew_override() reads through the cached parameter()")
    if "get_parameter(" not in body:
        problems.append("curfew_override() doesn't read the parameter itself")
    return problems


@rule("2026-11-16: the bake gets spellbound3d with a short-lived GitHub token. It must never be in user data "
      "(readable via DescribeInstanceAttribute) and must be deleted from SSM and revoked when the bake ends.")
def bake_token_stays_out_of_user_data_and_is_revoked():
    problems = []
    # Since #48 the bundle goes through S3 and the user data carries only fetch.py: check both, inside out.
    import base64, io, lzma, tarfile, tempfile
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "bundle.txz"
        b = subprocess.run([sys.executable, "-I", str(P2 / "ami" / "make-user-data.py"), "--bundle", str(out)],
                           capture_output=True, text=True)
        sha = b.stdout.strip()
        r = subprocess.run([sys.executable, "-I", str(P2 / "ami" / "make-user-data.py"), "example-bucket",
                            f"bake/{sha}.txz", sha], capture_output=True, text=True)
        if b.returncode or r.returncode or not out.is_file() or not re.fullmatch(r"[0-9a-f]{64}", sha):
            problems.append(f"make-user-data.py --bundle / BUCKET KEY SHA256 failed: {(b.stderr or r.stderr).strip()[:200]}")
        else:
            texts = [r.stdout]
            with tarfile.open(fileobj=io.BytesIO(lzma.decompress(out.read_bytes()))) as tar:
                texts += [tar.extractfile(x).read().decode(errors="replace") for x in tar.getmembers() if x.isfile()]
            for m in re.finditer(r"^    content: (\S+)$", r.stdout, re.M):
                texts.append(lzma.decompress(base64.b64decode(m.group(1))).decode(errors="replace"))
            if any(re.search(r"gh[sp]_[A-Za-z0-9]{20,}|github_pat_", x) for x in texts):
                problems.append("the user data or the bake bundle contains a GitHub token")
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
        problems.append("budget_usd is above US$30: no headroom under the US$35 bill ceiling for billing lag")
    return problems


@rule("2026-11-16: the weekly AMI pipeline added a new role that can launch instances (build-runner-ami-ci); "
      "unless it's in budget_stop.roles, the budget stop wouldn't block it.")
def every_launching_role_is_covered_by_the_budget_stop():
    import yaml
    p = yaml.safe_load((P1 / "policy.yaml").read_text())
    covered = set(((p.get("account_settings") or {}).get("budget_stop") or {}).get("roles", []))
    problems = []
    for script in SHELL_SCRIPTS:
        text = script.read_text()
        m = re.search(r"^NAME=([\w-]+)$", text, re.M)
        if m and "--role-name" in text and "ec2:RunInstances" in text and m.group(1) not in covered:
            problems.append(f"{script.relative_to(ROOT)} creates role {m.group(1)}, which can launch instances, "
                            "but it isn't in policy.yaml budget_stop.roles")
    return problems


@rule("2026-11-16: only a smoke-tested image may be published or cause old images to be pruned.")
def ami_pipeline_tests_before_pruning_or_publishing():
    wf = (ROOT / ".github" / "workflows" / "ami.yml").read_text()
    order = [wf.find(s) for s in ("ami/bake.sh", "ami/smoke-test.sh", "ami/prune-amis.sh", "ci/publish-release.sh")]
    if -1 in order:
        return ["ami.yml is missing one of: bake, smoke test, prune, publish"]
    return [] if order == sorted(order) else ["ami.yml doesn't run bake → smoke test → prune → publish in that order"]


@rule("2026-11-16: the org budget action was set up by hand with AWSManagedBudgetsSpendLimitManagementRole (AWS "
      "controls its permissions, which are conditioned and include CloseAccount) and without the forecast alert.")
def org_budget_stop_uses_its_own_role_and_all_alerts():
    script = (P1 / "bootstrap-org-budget-stop.sh").read_text()
    problems = []
    if "AWSManagedBudgetsSpendLimitManagementRole" in script.replace("not AWS's managed spend-limit role", ""):
        problems.append("bootstrap-org-budget-stop.sh uses AWS's managed spend-limit role")
    if not re.search(r'ROLE="?org-budget-action-runner', script) or "--execution-role-arn \"$role_arn\"" not in script:
        problems.append("the org budget action doesn't use the dedicated org-budget-action-runner role")
    for spec in ("ACTUAL:50", "ACTUAL:80", "ACTUAL:100", "FORECASTED:100"):
        if spec not in script:
            problems.append(f"bootstrap-org-budget-stop.sh doesn't ensure the {spec}% alert")
    if '"Refund"' not in script:
        problems.append("the org budget doesn't exclude refunds")
    if "MasterAccountId" not in script:
        problems.append("bootstrap-org-budget-stop.sh doesn't check it runs in the management account")
    return problems


@rule("2026-11-16: bootstrap-org-budget-stop.sh read only the first line ('{') of the CLI's multi-line JSON "
      "into a variable, passed it as --notification, and failed before doing anything.")
def no_reading_multiline_cli_json_line_by_line():
    problems = []
    for path in SHELL_SCRIPTS:
        # Across line continuations: the 2026-11-16 case had the pipe on the next line.
        text = path.read_text().replace("\\\n", " ")
        for m in re.finditer(r"--output json[^\n]*\|\s*\{?\s*read -r", text):
            problems.append(f"{path.relative_to(ROOT)}: pipes AWS CLI JSON into 'read' (gets only its first line; "
                            f"compact it with jq -c): {m.group(0)[:80]}")
    return problems


@rule("2026-11-16: bootstrap-org-budget-stop.sh tested OU membership with spaces against the CLI's tab-separated "
      "--output text, so every account was falsely reported as outside spend-capped.")
def text_lists_are_space_separated_before_membership_tests():
    problems = []
    for path in SHELL_SCRIPTS:
        text = path.read_text().replace("\\\n", " ")
        for var in re.findall(r'^\s*(\w+)="\$\(aws [^\n]*--output text\)"', text, re.M):
            if re.search(rf'\*" \$\{{?{var}\}}? "\*|" \$\{{{var}\}} "', text):
                problems.append(f"{path.relative_to(ROOT)}: ${var} holds tab-separated CLI text but is "
                                "membership-tested with spaces (pipe it through tr '\\t' ' ')")
    return problems


@rule("2026-11-16: the runner launch template couldn't use /build/runner/ami-id (resolve:ssm): it was saved as plain "
      "text, and launch templates only resolve parameters of data type aws:ec2:image.")
def current_ami_parameter_is_an_image_type():
    problems = []
    for path in SHELL_SCRIPTS:
        text = path.read_text().replace("\\\n", " ")
        for line in text.splitlines():
            if re.search(r"put-parameter --name /build/runner/ami-id\b", line) and "--data-type aws:ec2:image" not in line:
                problems.append(f"{path.relative_to(ROOT)}: sets /build/runner/ami-id without --data-type aws:ec2:image")
    return problems


@rule("2026-11-16: 'aws cloudformation deploy' keeps a parameter's previous value unless it's passed, so changing "
      "the Pools default in template.yaml never reached the stack (c7i-flex stayed, runner classes were missing).")
def stack_parameters_are_always_passed():
    stack = P2 / "stack"
    template, deploy = (stack / "template.yaml").read_text(), (stack / "deploy.sh").read_text()
    settings = json.loads((stack / "config.json").read_text())
    block = template.split("\nParameters:\n", 1)[1].split("\nResources:\n", 1)[0]
    params = re.findall(r"^  (\w+):", block, re.M)
    problems = [f"part2-runner/stack/template.yaml: parameter {p} has a Default (pass it from config.json instead)"
                for p in params if re.search(rf"^  {p}:.*Default:|^  {p}:\n(    .*\n)*?    Default:", block, re.M)]
    if "config.json" not in deploy or '"${settings[@]}"' not in deploy:
        problems.append("part2-runner/stack/deploy.sh doesn't pass the settings from config.json")
    for p in params:
        if p not in settings and not re.search(rf'"{p}=', deploy):
            problems.append(f"template parameter {p} is in neither config.json nor deploy.sh's --parameter-overrides")
    return problems


@rule("2026-11-16: the usage report's Logs Insights query summed 'minutes' AS 'minutes'; an alias with the summed "
      "field's own name comes back missing, so every report said 0 hours.")
def logs_insights_aliases_dont_shadow_their_field():
    problems = []
    for path in sorted(P2.rglob("*.py")) + SHELL_SCRIPTS + WORKFLOWS:
        for m in re.finditer(r"\b(?:sum|avg|count|min|max)\((\w+)\)\s+as\s+(\w+)", path.read_text()):
            if m.group(1) == m.group(2):
                problems.append(f"{path.relative_to(ROOT)}: '{m.group(0)}' (alias it to another name)")
    return problems


@rule("2026-11-16 cost review (#30): a log group without retention keeps (and bills) everything forever, and a Lambda "
      "without its own log group gets one AWS creates on first run, with no retention.")
def every_log_group_has_retention():
    problems = []
    for path in sorted(P2.rglob("*.yaml")):
        text = path.read_text()
        if "AWSTemplateFormatVersion" not in text and "Resources:" not in text:
            continue
        blocks = re.split(r"^  (?=\w+:\n    Type: AWS::)", text, flags=re.M)
        groups = set()
        functions = []
        for block in blocks:
            m = re.match(r"(\w+):\n    Type: (AWS::\S+)", block)
            if not m:
                continue
            name, kind = m.groups()
            if kind == "AWS::Logs::LogGroup":
                if not re.search(r"^      RetentionInDays: \d+", block, re.M):
                    problems.append(f"{path.relative_to(ROOT)}: log group {name} has no RetentionInDays")
                g = re.search(r"^      LogGroupName: (\S+)", block, re.M)
                if g:
                    groups.add(g.group(1))
            elif kind in ("AWS::Lambda::Function", "AWS::Serverless::Function"):
                f = re.search(r"^      FunctionName: (\S+)", block, re.M)
                functions.append((name, f.group(1) if f else None))
        for name, fn in functions:
            if fn is None or fn.startswith("!"):
                problems.append(f"{path.relative_to(ROOT)}: Lambda {name} needs a fixed FunctionName, so its log "
                                "group can be declared (with retention) before AWS makes one without")
            elif f"/aws/lambda/{fn}" not in groups:
                problems.append(f"{path.relative_to(ROOT)}: Lambda {fn} has no /aws/lambda/{fn} log group with retention")
    return problems


@rule("2026-11-16 cost review (#30): the stop at the budget only stopped instances, and only after billing data "
      "(8-36 hours late). Fast leaks (data out, Lambda or log loops) and leftovers (disks, IPs, endpoints) were missed.")
def stops_cover_more_than_instances():
    import yaml
    p = yaml.safe_load((P1 / "policy.yaml").read_text())
    settings = p["account_settings"]
    stop = settings["budget_stop"]
    ks = stop.get("kill_switch") or {}
    problems = []
    for key in ("terminate_instances", "throttle_lambdas", "delete_detached_volumes",
                "release_unused_addresses", "delete_interface_endpoints"):
        if ks.get(key) is not True:
            problems.append(f"policy.yaml kill_switch.{key} isn't true")
    fast = stop.get("fast_stop") or {}
    for key in ("network_out_gib", "lambda_invocations", "log_ingest_mib"):
        if not isinstance(fast.get(key), int) or fast[key] <= 0:
            problems.append(f"policy.yaml fast_stop.{key} is missing")
    if float(settings.get("budget_usd", 99)) > 29:
        problems.append(f"policy.yaml budget_usd is {settings.get('budget_usd')}: over US$29 (Kieron 2026-11-17) "
                        "leaves too little room for the billing lag under the US$35 ceiling")
    if "anomaly_alert_usd" not in settings:
        problems.append("policy.yaml has no anomaly_alert_usd (Cost Anomaly Detection)")
    boot = (P1 / "bootstrap-budget-stop.sh").read_text()
    for alarm in ("guardrail-fast-network-out", "guardrail-fast-lambda-invocations", "guardrail-fast-log-ingest"):
        if alarm not in boot:
            problems.append(f"bootstrap-budget-stop.sh doesn't create alarm {alarm}")
    for env in ("THROTTLE_LAMBDAS", "DELETE_DETACHED_VOLUMES", "RELEASE_UNUSED_ADDRESSES", "DELETE_INTERFACE_ENDPOINTS"):
        if f"{env}: $" not in boot:
            problems.append(f"bootstrap-budget-stop.sh doesn't pass {env} to the kill switch")
    denied = set(json.loads((P1 / "bootstrap" / "budget-stop-policy.json").read_text())["Statement"][0]["Action"])
    # At the limit nothing may undo the kill switch's work or start new spending by another route.
    for action in ("ec2:ModifyVolume", "ec2:CopySnapshot", "lambda:UpdateFunctionCode",
                   "lambda:UpdateFunctionConfiguration", "lambda:PutFunctionConcurrency",
                   "lambda:DeleteFunctionConcurrency", "scheduler:CreateSchedule", "scheduler:UpdateSchedule",
                   "events:PutRule", "events:EnableRule", "events:PutTargets", "logs:CreateLogGroup"):
        if action not in denied:
            problems.append(f"bootstrap/budget-stop-policy.json doesn't deny {action}")
    return problems


@rule("2026-11-16 cost review (#30): nothing limited what a runner sends out. Data out of AWS is US$0.114/GB, so one "
      "job (a bad dependency, a misled Claude job) could run up ≈US$250/h per runner before billing even shows it.")
def runner_egress_is_capped():
    boot = (P2 / "ami" / "files" / "build-runner-boot.sh").read_text()
    smoke = (P2 / "ami" / "smoke-test.sh").read_text()
    problems = []
    m = re.search(r"^EGRESS_KBPS=(\d+)", boot, re.M)
    if not m:
        problems.append("build-runner-boot.sh sets no EGRESS_KBPS")
    elif int(m.group(1)) > 6250:  # ≈ 50 Mbps, the top of what Kieron agreed (#30)
        problems.append(f"build-runner-boot.sh: EGRESS_KBPS={m.group(1)} is over 6250 KiB/s (≈50 Mbps)")
    for needed in ("for cmd in iptables ip6tables", "--uid-owner ghrunner", "--hashlimit-above", "-j DROP"):
        if needed not in boot:
            problems.append(f"build-runner-boot.sh: the egress cap lost '{needed}'")
    if "egress capped for jobs" not in smoke:
        problems.append("smoke-test.sh no longer checks the egress cap")
    return problems



@rule("2026-11-17: smoke-test.sh's remote checks run under Run Command's /bin/sh (dash). The Godot smoke check "
      "used bash's [[ ]], which dash doesn't have, so it skipped silently even after spellbound3d #203 shipped it.")
def smoke_test_remote_checks_are_posix_and_fail_closed():
    smoke = (P2 / "ami" / "smoke-test.sh").read_text()
    m = re.search(r"^read -r -d '' checks <<'EOF'.*?\n(.*?)^EOF$", smoke, re.S | re.M)
    if not m:
        return ["smoke-test.sh: can't find the remote checks block (read -r -d '' checks <<'EOF' ... EOF)"]
    problems = []
    bashisms = {r"\[\[": "[[ ]]", r"\[ [^]]*==": "== inside [ ]", r"<<<": "<<<", r"\$'": "$'...'",
                r"^\s*\w+=\(": "an array", r"^\s*function\s": "'function'", r"\bsource\s": "'source'"}
    for n, line in enumerate(m.group(1).splitlines(), 1):
        code = line.split(" #", 1)[0] if not line.lstrip().startswith("#") else ""
        for pattern, what in bashisms.items():
            if re.search(pattern, code):
                problems.append(f"smoke-test.sh remote checks line {n}: {what} isn't POSIX sh (dash runs this): {line.strip()[:80]}")
    if "spellbound3d godot-smoke" not in m.group(1):
        problems.append("smoke-test.sh no longer runs spellbound3d's godot-smoke")
    if re.search(r"skip\s+spellbound3d godot-smoke", m.group(1)):
        problems.append("smoke-test.sh skips spellbound3d's godot-smoke: it's on their main, so missing must fail")
    return problems


@rule("2026-11-17: bootstrap-budget-stop.sh re-read the live budget and kept its limit, so after budget_usd went "
      "from 25 to 29 (about £23, Kieron) the account budget, and its stop, stayed at US$25.")
def budget_limit_comes_from_policy():
    problems = []
    for script in ("bootstrap-budget-stop.sh", "bootstrap-org-budget-stop.sh"):
        text = (P1 / script).read_text()
        if "['account_settings']['budget_usd']" not in text:
            problems.append(f"{script} doesn't read budget_usd from policy.yaml")
        if not re.search(r"BudgetLimit\.Amount = \$amt|BudgetLimit: \{Amount: \$amt", text):
            problems.append(f"{script} doesn't set the budget's BudgetLimit from budget_usd")
    return problems


@rule("2026-11-17 cost boundary (Kieron): each cost setting had its own rule or none, and a change agreed between "
      "Claude sessions could raise one (more runners, bigger types, no curfew) without Kieron seeing a price.")
def cost_boundary_holds():
    import yaml
    p = yaml.safe_load((P1 / "policy.yaml").read_text())
    cb = p.get("cost_boundary")
    if not cb:
        return ["policy.yaml has no cost_boundary"]
    problems = []
    s = p["account_settings"]
    if float(s["budget_usd"]) > cb["max_budget_usd"]:
        problems.append(f"budget_usd {s['budget_usd']} is over the boundary's US${cb['max_budget_usd']}")
    # At least US$5 between the stop and the ceiling, for the billing lag and anything still billing.
    if float(cb["max_budget_usd"]) > cb["ceiling_usd"] - 5:
        problems.append(f"max_budget_usd US${cb['max_budget_usd']} leaves under US$5 below the US${cb['ceiling_usd']} ceiling")
    if float(s.get("anomaly_alert_usd", 999)) > cb["max_anomaly_alert_usd"]:
        problems.append(f"anomaly_alert_usd {s.get('anomaly_alert_usd')} is over US${cb['max_anomaly_alert_usd']}")
    org = (P1 / "bootstrap-org-budget-stop.sh").read_text()
    if "['account_settings']['budget_usd']" not in org:
        problems.append("the org budget isn't set from budget_usd")

    cfg = json.loads((P2 / "stack" / "config.json").read_text())
    if cfg["MaxRunners"] > cb["max_runners"]:
        problems.append(f"config.json MaxRunners {cfg['MaxRunners']} is over {cb['max_runners']}")
    if cfg["MaxAgeMinutes"] > cb["max_instance_minutes"]:
        problems.append(f"config.json MaxAgeMinutes {cfg['MaxAgeMinutes']} is over {cb['max_instance_minutes']}")
    if cfg["IdleMinutes"] > cb["max_idle_minutes"]:
        problems.append(f"config.json IdleMinutes {cfg['IdleMinutes']} is over {cb['max_idle_minutes']}")
    # The consumer's pools (#54) are checked by the same code the launcher runs: config.json's starting copy here,
    # spellbound3d's pools.json at every read. The boundary comes from policy.yaml, as deploy.sh builds it.
    import importlib.util
    spec = importlib.util.spec_from_file_location("pools_cb", P2 / "stack" / "lambda" / "pools.py")
    pools_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pools_mod)
    ec2 = next(x for x in p["services"] if x["id"] == "ec2")
    boundary = {"instance_types": ec2["limits"]["instance_types"], "instance_sizes": cb["instance_sizes"],
                "max_runners": cb["max_runners"], "max_instance_minutes": cb["max_instance_minutes"],
                "max_idle_minutes": cb["max_idle_minutes"]}
    try:
        pools_mod.validate({"version": 1, "pools": cfg["Pools"]}, boundary)
    except pools_mod.Invalid as e:
        problems += [f"config.json Pools: {r}" for r in e.reasons]
    for probe, why in (("c7i.2xlarge", "over 4 vCPU"), ("t4g.large", "Graviton"), ("r5.large", "unapproved")):
        try:
            pools_mod.validate({"version": 1, "pools": {"aws-x": {"class": "light", "spot": [probe], "on_demand": []}}},
                               boundary)
            problems.append(f"pools.py accepts {probe} ({why}): a consumer could ask for it")
        except pools_mod.Invalid:
            pass
    for name, pool in cfg["Pools"].items():
        types = pool.get("spot", []) + pool.get("on_demand", [])
        for itype in types:
            if itype.split(".", 1)[-1] not in cb["instance_sizes"]:
                problems.append(f"pool {name}: {itype} is bigger than the boundary allows ({cb['instance_sizes'][-1]})")
        if cb["spot_first"] and not pool.get("spot"):
            problems.append(f"pool {name} has no spot types: spot comes first")
    curfew = cfg.get("Curfew")
    if not curfew:
        problems.append("config.json has no Curfew")
    else:
        h = lambda v: int(v.split(":")[0]) + int(v.split(":")[1]) / 60
        hours = (h(curfew["end"]) - h(curfew["start"])) % 24
        if hours < cb["min_curfew_hours"]:
            problems.append(f"the curfew is {hours:g} hours, under {cb['min_curfew_hours']}")

    prune = (P2 / "ami" / "prune-amis.sh").read_text()
    ami_wf = (ROOT / ".github" / "workflows" / "ami.yml").read_text()
    m = re.search(r'KEEP="\$\{1:-(\d+)\}"', prune)
    if not m or int(m.group(1)) > cb["max_kept_amis"]:
        problems.append(f"prune-amis.sh keeps more than {cb['max_kept_amis']} images by default")
    for n in re.findall(r"prune-amis\.sh\s+(\d+)", ami_wf):
        if int(n) > cb["max_kept_amis"]:
            problems.append(f"ami.yml keeps {n} images, over {cb['max_kept_amis']}")
    for path in (P2 / "ami" / "bake.sh", P2 / "stack" / "template.yaml"):
        for size in re.findall(r"VolumeSize[=:]\s*(\d+)", path.read_text()):
            if int(size) > cb["max_root_volume_gib"]:
                problems.append(f"{path.relative_to(ROOT)}: a {size} GiB disk, over {cb['max_root_volume_gib']}")

    boot = (P2 / "ami" / "files" / "build-runner-boot.sh").read_text()
    m = re.search(r"^EGRESS_KBPS=(\d+)", boot, re.M)
    if not m or int(m.group(1)) > cb["max_egress_kbps"]:
        problems.append(f"build-runner-boot.sh EGRESS_KBPS is missing or over {cb['max_egress_kbps']}")

    allowed = set(cb["cost_explorer_only_in"])
    for path in sorted(list(ROOT.glob(".github/workflows/*.yml")) + list(P1.rglob("*.sh")) + list(P1.rglob("*.py"))
                       + list(P2.rglob("*.sh")) + list(P2.rglob("*.py"))):
        rel = str(path.relative_to(ROOT))
        if rel in allowed or rel.startswith("part1-godmode/tests/"):  # the tests plant these on purpose
            continue
        text = path.read_text()
        if re.search(r"\baws ce \b|client\(\s*[\"']ce[\"']", text):
            problems.append(f"{rel} calls Cost Explorer (US$0.01 a call); only {', '.join(sorted(allowed))} may")
    return problems


@rule("2026-11-18 (#45): the image took Node from Ubuntu 24.04's packages (v18.19.1); make check's renovate-check "
      "(renovate 42.99.0, needs Node ^22.13 or ^24.11) crashed on the first AWS trial. The laptop runner has v22.")
def runner_image_node_is_22_or_later_from_nodejs_org():
    setup = (P2 / "ami" / "setup.sh").read_text()
    code = "\n".join(l for l in setup.splitlines() if not l.lstrip().startswith("#"))
    problems = []
    # Any apt-get install line (with its continuation lines) that names nodejs or npm.
    for m in re.finditer(r"apt(?:-get)?\s+install(?:[^\n]*\\\n)*[^\n]*", code):
        if re.search(r"(?<![\w-])(nodejs|npm)(?![\w-])", m.group(0)):
            problems.append("setup.sh installs nodejs/npm from apt (Ubuntu 24.04's is v18)")
    # Since 2026-11-18 the version comes from spellbound3d's machine-requirements.txt (their range follows
    # renovate's engines), read by files/machine_requirements.py; the download is checked against its hash.
    if "machine_requirements.py" not in code or "machine-requirements.txt" not in code:
        problems.append("setup.sh doesn't take node's version from spellbound3d's machine-requirements.txt")
    if "nodejs.org/dist" not in code or not re.search(r'NODE_SHA256\}?  .*\| sha256sum -c', code):
        problems.append("setup.sh doesn't check the node download from nodejs.org against the file's sha256")
    reader = P2 / "ami" / "files" / "machine_requirements.py"
    if not reader.is_file():
        problems.append("files/machine_requirements.py is missing")
    else:
        import importlib.util
        spec = importlib.util.spec_from_file_location("mr_45", reader)
        mr = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mr)
        h = "0" * 64
        for v in ("18.19.1", "22.12.0", "23.1.0"):  # v18 crashed renovate 42; the others are outside its range
            text = (f"tool node 22.13 exact:{v}\nrange node ^22.13.0||^24.11.0 renovate@42.99.0\n"
                    f"sha256 node node-v{v}-linux-x64.tar.xz {h}\n")
            try:
                mr.parse(text)
                problems.append(f"the reader accepts node exact:{v}, which renovate 42.99.0 can't run on")
            except mr.Bad:
                pass
    return problems


@rule("2026-11-18 (#48): the bake's bundle travelled inside its EC2 user data and reached 16,364 of 16,384 bytes; "
      "the Node 22 change first overflowed it. The bundle now comes from S3, checked by SHA-256.")
def bake_user_data_is_small_and_the_bundle_is_checked():
    problems = []
    mud = P2 / "ami" / "make-user-data.py"
    sha = "0" * 64
    r = subprocess.run([sys.executable, "-I", str(mud), "example-bucket", f"bake/{sha}.txz", sha],
                       capture_output=True, text=True)
    if r.returncode:
        return [f"make-user-data.py BUCKET KEY SHA256 failed: {r.stderr.strip()[:200]}"]
    if len(r.stdout.encode()) > 4096:
        problems.append(f"the bake's user data is {len(r.stdout.encode())} bytes; keep it under 4 KB (the bundle goes via S3)")
    fetch = (P2 / "ami" / "fetch.py").read_text() if (P2 / "ami" / "fetch.py").is_file() else ""
    if "hashlib.sha256(data).hexdigest()" not in fetch or "!= want" not in fetch:
        problems.append("fetch.py doesn't check the bundle's SHA-256 before writing it")
    if "poweroff" not in r.stdout:
        problems.append("the user data doesn't power off when the fetch fails (bake.sh would wait 30 min)")
    role = (P2 / "ami" / "bake-role.sh").read_text()
    grants = re.findall(r'Action: "?\[?"?(s3:[A-Za-z*"]+[^}]*)', role)
    if 'arn:aws:s3:::${BUCKET}/bake/*' not in role or any(g.split('"')[0] != "s3:GetObject" for g in grants):
        problems.append("the bake role's S3 access must be s3:GetObject on the artifacts bucket's bake/* only")
    bake = (P2 / "ami" / "bake.sh").read_text()
    cleanup = bake[bake.find("cleanup() {"):bake.find("trap cleanup EXIT")]
    if "aws s3 rm" not in cleanup:
        problems.append("bake.sh's cleanup doesn't delete the bundle from S3")
    return problems


@rule("2026-11-18 (#47): DescribeInstances lagged RunInstances, so the launcher's next run (after the lock) "
      "didn't see a just-launched aws-heavy instance and launched a second one for the same job.")
def launcher_sees_its_own_launches_before_ec2_does():
    import contextlib
    import importlib.util
    import time as _time
    lam = P2 / "stack" / "lambda"
    sys.path.insert(0, str(lam))
    try:
        spec = importlib.util.spec_from_file_location("runner_47", lam / "runner.py")
        r = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(r)
    finally:
        sys.path.remove(str(lam))
    store, launched = {"/build/github/app-id": "1", "/build/github/app-private-key": "k"}, []

    class NotFound(Exception):
        response = {"Error": {"Code": "ParameterNotFound"}}

    def get(Name, **_):
        if Name not in store:
            raise NotFound()
        return {"Parameter": {"Value": store[Name]}}

    ssm = type("SSM", (), {"get_parameter": lambda self, **k: get(**k),
                           "put_parameter": lambda self, Name, Value, **k: store.__setitem__(Name, Value),
                           "delete_parameter": lambda self, Name: store.pop(Name, None)})()

    def launch(gh, job_id, pool):
        launched.append(job_id)
        return {"id": f"i-{job_id}-{len(launched)}", "name": f"aws-{job_id}-x", "runner_id": "1",
                "job_id": str(job_id), "pool": pool, "spot": True, "type": "t", "launched": _time.time()}

    r.aws = lambda service: ssm
    r.lock = lambda *a, **k: contextlib.nullcontext()
    r.parameter = lambda n: get(n)["Parameter"]["Value"]
    r.curfew_override = lambda: {}
    r.curfew.state = lambda *a, **k: {"active": False, "reason": "open"}
    r.ghapp.installation_token = lambda *a, **k: "tok"
    r.ghapp.revoke = lambda *a, **k: None
    r.GitHub = lambda token: type("G", (), {"paged": lambda self, *a: []})()
    heavy = next(p for p in r.POOLS if r.POOLS[p].get("class") == "heavy")
    r.queued_jobs = lambda gh: [{"id": 548, "pool": heavy, "created_at": "2026-11-18T02:12:00Z"}]
    r.live_instances = lambda: []  # EC2 doesn't list the first run's instance yet
    r.launch = launch
    r.terminate = lambda ids: None
    try:
        r.launcher({"source": "webhook", "job_id": 548, "pool": heavy}, None)
        r.launcher({"source": "webhook", "job_id": 548, "pool": heavy}, None)
    except Exception as e:
        return [f"launcher() failed in the replay: {type(e).__name__}: {e}"]
    if launched != [548]:
        return [f"one queued job got {len(launched)} launches across two runs while EC2 lagged (want 1)"]
    return []


@rule("2026-11-18 (#30 fix 6): a new account's Lambda limit (10) let a webhook flood use every execution, which "
      "could delay the reaper and the kill switch. The webhook is capped and the kill switch has executions reserved.")
def webhook_is_capped_and_kill_switch_reserved():
    problems = []
    tpl = (P2 / "stack" / "template.yaml").read_text()
    hook = tpl[tpl.find("  Webhook:\n"):tpl.find("\n  WebhookUrl:")]
    m = re.search(r"^\s+ReservedConcurrentExecutions:\s*(\d+)", hook, re.M)
    if not m or not 1 <= int(m.group(1)) <= 10:
        problems.append("template.yaml: the webhook needs ReservedConcurrentExecutions between 1 and 10")
    boot = (P1 / "bootstrap-budget-stop.sh").read_text()
    if not re.search(r'put-function-concurrency .*--function-name "\$FUNCTION" --reserved-concurrent-executions [1-9]', boot):
        problems.append("bootstrap-budget-stop.sh doesn't reserve concurrency for the kill switch")
    checklist = (P1 / "CHECKLIST.md").read_text()
    if re.search(r"delete-function-concurrency[^`]*build-runner-webhook|\(and `build-runner-webhook`\)", checklist):
        problems.append("CHECKLIST.md recovers the webhook by deleting its concurrency, which removes its flood cap")
    return problems


@rule("2026-11-18: a 12-second job's instance terminated inside the launcher's 5-minute memory of its own launches, "
      "so the next heavy job waited ~2.5 min: the gone instance still counted as a free runner.")
def finished_recent_launches_dont_hold_back_new_jobs():
    import importlib.util
    lam = P2 / "stack" / "lambda"
    sys.path.insert(0, str(lam))
    try:
        spec = importlib.util.spec_from_file_location("runner_done", lam / "runner.py")
        r = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(r)
    finally:
        sys.path.remove(str(lam))
    now = 1_000_000.0
    pool = next(iter(r.POOLS))
    gone = {"id": "i-gone", "name": "aws-1-gone", "job_id": "1", "pool": pool, "launched": now - 120}
    try:
        live = r.with_recent_launches([], [gone], now, set())   # EC2: nothing; GitHub: no such runner any more
    except TypeError:
        live = r.with_recent_launches([], [gone], now)          # the old signature can't be told
    plan = r.launches_needed([{"id": 2, "pool": pool, "created_at": "x"}], live, [], now, cap=4)
    return [] if plan == {pool: 1} else [f"a finished recent launch held back a new job's launch (plan {plan})"]


@rule("2026-11-21: the pipeline plan and ADR-0001 said 'the 8-runner limit' (the template's MaxValue), but the "
      "limit in force is policy.yaml's max_runners, 4.")
def docs_state_the_runner_limit_from_policy():
    m = re.search(r"^\s*max_runners:\s*(\d+)", (P1 / "policy.yaml").read_text(), re.M)
    if not m:
        return ["policy.yaml has no max_runners"]
    limit = m.group(1)
    docs = sorted(ROOT.glob("*.md")) + sorted((ROOT / "adr").rglob("*.md")) + sorted(P2.rglob("*.md"))
    pattern = r"\b(\d+)(-runner limit| runners at once|-runner cap)|\b(\d+) runners\b(?=[;,.)]| limit| at)"
    problems = []
    for path in docs:
        for n, line in enumerate(path.read_text().splitlines(), 1):
            nums = [a or c for a, _, c in re.findall(pattern, line)]
            if any(x != limit for x in nums):
                problems.append(f"says a runner limit other than max_runners ({limit}): "
                                f"{path.relative_to(ROOT)}:{n}: {line.strip()[:100]}")
    return problems


HOOK = P2 / "ami" / "files" / "job-started.sh"


def run_hook(hook, tmp, event, actor, triggering=None):
    """Run the job-started hook on a stand-in allowlist; True when it allows the job."""
    actors, repos, payload = tmp / "actors.txt", tmp / "repos.txt", tmp / "event.json"
    actors.write_text("# test list\nstickleprojects\nspellbound3d-claude[bot]\n")
    repos.write_text("stickleprojects/spellbound3d\n")
    payload.write_text("{}")
    env = {"PATH": "/usr/bin:/bin", "RUNNER_HOOK_ACTORS_FILE": str(actors), "RUNNER_HOOK_REPOS_FILE": str(repos),
           "GITHUB_REPOSITORY": "stickleprojects/spellbound3d", "GITHUB_ACTOR": actor,
           "GITHUB_TRIGGERING_ACTOR": triggering or actor, "GITHUB_EVENT_NAME": event,
           "GITHUB_EVENT_PATH": str(payload), "RUNNER_HOOK_STATE_DIR": str(tmp)}
    return subprocess.run(["bash", str(hook)], env=env, capture_output=True, text=True, cwd=tmp).returncode == 0


@rule("2026-11-22: spellbound3d's scheduled periodic check (run 44949388553) reached an AWS runner, but the hook "
      "refused it: on schedule runs GitHub gives the App's actor as 'spellbound3d-claude', without '[bot]'. Kieron "
      "chose option A: accept the bare login on schedule runs only, and only when its '[bot]' form is listed.")
def hook_accepts_an_apps_bare_login_on_schedule_runs_only(hook=HOOK):
    import tempfile
    cases = [  # (event, actor, triggering actor, should be allowed)
        ("schedule", "spellbound3d-claude", None, True),
        ("schedule", "spellbound3d-claude[bot]", None, True),
        ("schedule", "stickleprojects", None, True),
        ("push", "spellbound3d-claude[bot]", None, True),
        ("workflow_dispatch", "spellbound3d-claude[bot]", None, True),
        ("push", "spellbound3d-claude", None, False),               # bare login is not the App elsewhere
        ("workflow_dispatch", "spellbound3d-claude", None, False),
        ("issue_comment", "spellbound3d-claude", None, False),
        ("schedule", "someone-else", None, False),
        ("schedule", "stickleprojects[bot]", None, False),          # no stripping the other way round
        ("schedule", "spellbound3d-claude", "someone-else", False),  # a re-run by a stranger
        ("workflow_dispatch", "stickleprojects", "spellbound3d-claude", False),
    ]
    problems = []
    with tempfile.TemporaryDirectory() as d:
        for event, actor, trig, want in cases:
            got = run_hook(hook, Path(d), event, actor, trig)
            if got != want:
                problems.append(f"{event} by {actor} (triggered by {trig or actor}): "
                                f"{'allowed' if got else 'refused'}, should be {'allowed' if want else 'refused'}")
    return problems


@rule("2026-11-22: AC10's usage records showed 411-477 s for 4 jobs GitHub timed at 220-243 s, with avg_vcpu "
      "halved: the record timed from the runner's start, and those runners sat idle ~4 min until another job's "
      "runner swap handed them work. Halved vCPU could make the report call a heavy job 'could be light'.")
def job_usage_times_the_job_not_the_runner(files=P2 / "ami" / "files"):
    import importlib.util
    import os
    import tempfile
    spec = importlib.util.spec_from_file_location("job_usage", files / "job-usage.py")
    ju = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ju)
    with tempfile.TemporaryDirectory() as d:
        t = Path(d)
        (t / "job").touch()
        ready = int((t / "job").stat().st_mtime) - 240
        os.utime(t / "job", (ready, ready))
        # The job-started hook, allowing a job, then the job-completed hook: as on a runner.
        (t / "actors.txt").write_text("stickleprojects\n")
        (t / "repos.txt").write_text("stickleprojects/spellbound3d\n")
        (t / "event.json").write_text("{}")
        env = {"PATH": "/usr/bin:/bin", "RUNNER_HOOK_ACTORS_FILE": str(t / "actors.txt"),
               "RUNNER_HOOK_REPOS_FILE": str(t / "repos.txt"), "RUNNER_HOOK_STATE_DIR": d,
               "GITHUB_REPOSITORY": "stickleprojects/spellbound3d", "GITHUB_ACTOR": "stickleprojects",
               "GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_PATH": str(t / "event.json")}
        started = (files / "job-started.sh").read_text().replace("/run/build-runner", d)
        completed = (files / "job-completed.sh").read_text().replace("/run/build-runner", d)
        subprocess.run(["bash", "-c", started], env=env, capture_output=True, text=True)
        subprocess.run(["bash", "-c", completed], env=env, capture_output=True, text=True)
        info = ju.clean_job((t / "job-info.json").read_text()) if (t / "job-info.json").exists() else {}
        wall = ju.wall_seconds(info)
    if wall is None:
        return [f"no wall time in the job record: {info}"]
    return [] if wall < 60 else [f"a job that started 240 s after its runner was timed at {wall:.0f} s, "
                                 "not from its own start"]


TIME_SHIFT = """import runpy, sys, time
from datetime import datetime
from zoneinfo import ZoneInfo
hour, path = int(sys.argv[1]), sys.argv[2]
real = time.time
offset = datetime.now(ZoneInfo("Australia/Sydney")).replace(hour=hour, minute=30).timestamp() - real()
time.time = lambda: real() + offset
sys.argv = [path]
runpy.run_path(path, run_name="__main__")
"""


@rule("2026-11-22: PR #68's policy check failed at 23:27 Sydney, on a docs-only change: test_lambda.py's #47 tests "
      "ran on the real clock, so the runner curfew stopped their launches (found 2026-11-25). Every offline test "
      "must pass at any hour.")
def offline_tests_pass_inside_and_outside_the_curfew(tests=None):
    if tests is None:
        # test_cost_boundary.py runs these rules and edits files as it goes, so it isn't run from here.
        tests = [t for t in sorted((P1 / "tests").glob("test_*.py")) + sorted(P2.rglob("test_*.py"))
                 if t.name != "test_cost_boundary.py"]
    problems = []
    for path in tests:
        for hour in (3, 15, 23):  # the early hours, the open day, the late evening
            r = subprocess.run([sys.executable, "-I", "-c", TIME_SHIFT, str(hour), str(path)],
                               capture_output=True, text=True, cwd=ROOT)
            if r.returncode != 0:
                out = (r.stdout + r.stderr).strip().splitlines()
                why = next((x for x in out if x.startswith("FAIL")), out[-1] if out else "no output")
                problems.append(f"{path.name} at {hour}:30 Sydney: {why[:140]}")
    return problems


@rule("2026-11-26: Claude told spellbound3d's 0079 session the smoke test would check llvmpipe before a rebake, but it "
      "checked no driver at all: an image could install Mesa and still give their Chromium SwiftShader. Every driver "
      "a driver line may name needs its own smoke check, and the manifest must say which drivers the image has.")
def every_driver_has_a_smoke_check():
    names = re.search(r"^DRIVERS = \(([^)]*)\)", (P2 / "ami" / "files" / "machine_requirements.py").read_text(), re.M)
    if not names:
        return ["machine_requirements.py has no DRIVERS tuple"]
    smoke = (P2 / "ami" / "smoke-test.sh").read_text()
    setup = (P2 / "ami" / "setup.sh").read_text()
    problems = [f"smoke-test.sh has no check for driver {d}"
                for d in re.findall(r'"(\w+)"', names.group(1)) if not re.search(rf"^\s*{d}\) check ", smoke, re.M)]
    if not re.search(r"^\s*drivers: \(\$drivers", setup, re.M):
        problems.append("setup.sh doesn't write the image's drivers into the manifest")
    if not re.search(r'\*" libgl1-mesa-dri "\*\) DRIVERS="\$\{DRIVERS\} llvmpipe"', setup):
        problems.append("setup.sh doesn't count the web file's libgl1-mesa-dri as the llvmpipe driver (spellbound3d 0082)")
    if ".drivers" not in smoke:
        problems.append("smoke-test.sh doesn't read the manifest's drivers")
    if "llvmpipe" in names.group(1) and not re.search(r'"llvmpipe" in r and "swiftshader" not in r\.lower\(\)', smoke):
        problems.append("the llvmpipe check doesn't refuse SwiftShader")
    return problems

@rule("2026-11-26: November's usage (US$3.53 by the 24th) was all paid by credits, so guardrail-monthly, which counts "
      "what would be billed, read US$0.00: no budget saw any spending, and a slow leak could have used up the credits "
      "unnoticed. Kieron chose a second, alerts-only budget on usage before credits; the stop stays on the bill.")
def usage_before_credits_is_watched():
    problems = []
    apply = (P1 / "apply.sh").read_text()
    flat = apply.replace(" ", "")
    if "USAGE_BUDGET_NAME=guardrail-monthly-usage" not in flat:
        problems.append("apply.sh doesn't set up the guardrail-monthly-usage budget")
    if "IncludeCredit:false" not in flat:
        problems.append("apply.sh's usage budget doesn't count usage before credits (IncludeCredit:false)")
    if "IncludeCredit:true" not in flat:
        problems.append("apply.sh's stop budget no longer counts what would be billed (IncludeCredit:true)")
    stop = (P1 / "bootstrap-budget-stop.sh").read_text()
    if "guardrail-monthly-usage" in stop:
        problems.append("bootstrap-budget-stop.sh puts a stop on the usage budget; it's alerts only")
    role = (P1 / "bootstrap" / "apply-role-policy.json").read_text()
    if "budget/guardrail-monthly-usage" not in role:
        problems.append("the apply role can't create or update guardrail-monthly-usage")
    settings = (P1 / "policy.yaml").read_text()
    if not re.search(r"^  usage_alert_usd: \d+", settings, re.M):
        problems.append("policy.yaml has no account_settings.usage_alert_usd")
    check = (P2 / "laptop" / "daily_check.py").read_text()
    if 'USAGE_BUDGET = "guardrail-monthly-usage"' not in check or "before credits" not in check:
        problems.append("the daily check doesn't read usage before credits")
    return problems


@rule("2026-11-26: AWS refused to lower the Lambda limit to 110 (its minimum is 1,000). The invocation alarm misses "
      "a fault holding hundreds of long copies on few new calls (≈US$13/h, unseen by the budget for 8-36 h). "
      "Kieron chose a concurrency alarm and a cap on every one of our functions.")
def lambda_ceiling_without_the_quota():
    problems = []
    tpl = (P2 / "stack" / "template.yaml").read_text()
    for name in ("Launcher", "Webhook"):
        m0 = re.search(rf"^  {name}:\n(?:(?!^  \S).*\n?)*", tpl, re.M)
        block = m0.group(0) if m0 else ""
        m = re.search(r"^\s+ReservedConcurrentExecutions:\s*(\d+)", block, re.M)
        if not m or not 1 <= int(m.group(1)) <= 10:
            problems.append(f"template.yaml: the {name} needs ReservedConcurrentExecutions between 1 and 10")
    boot = (P1 / "bootstrap-budget-stop.sh").read_text()
    if not re.search(r"guardrail-fast-lambda-concurrency[\s\S]{0,300}--metric-name ConcurrentExecutions --statistic Maximum", boot):
        problems.append("bootstrap-budget-stop.sh doesn't create the Lambda concurrency alarm")
    mc = re.search(r"^      lambda_concurrent: (\d+)", (P1 / "policy.yaml").read_text(), re.M)
    if not mc or not 11 <= int(mc.group(1)) <= 50:
        problems.append("policy.yaml fast_stop.lambda_concurrent must be 11-50 (above the caps' 4+5+2, far below 1,000)")
    checklist = (P1 / "CHECKLIST.md").read_text()
    if re.search(r"delete-function-concurrency[^`]*build-runner-launcher", checklist):
        problems.append("CHECKLIST.md recovers the launcher by deleting its concurrency, which removes its cap")
    check = (P2 / "laptop" / "daily_check.py").read_text()
    if "far above the agreed 110" in check or "guardrail-fast-lambda-concurrency" not in check:
        problems.append("the daily check still wants 110, or doesn't check the concurrency alarm")
    return problems

@rule("2026-11-17 (#41): the bootstrap set the stop budget to US$25 when the decision was US$29, and only a pasted "
      "output showed it. 2026-11-27: applying #75 needed the apply role updated first, and nothing said so until a "
      "run failed. The guardrails-check workflow compares what's live with the repo daily and after every apply.")
def guardrails_drift_is_checked():
    problems = []
    wf_path = ROOT / ".github" / "workflows" / "guardrails-check.yml"
    if not wf_path.exists():
        return ["there's no guardrails-check workflow"]
    wf = wf_path.read_text()
    for need, why in (("schedule:", "it doesn't run daily"), ("workflows: [apply", "it doesn't run after apply"),
                      ("environment: guardrails-check", "it doesn't use the guardrails-check environment"),
                      ("guardrails_check.py", "it doesn't run guardrails_check.py"),
                      ("secrets.AWS_ACCOUNT_ID", "the account id isn't masked in its logs")):
        if need not in wf:
            problems.append(f"guardrails-check.yml: {why}")
    check = (P1 / "guardrails_check.py").read_text()
    for need in ("guardrail-monthly-usage", "lambda_concurrent",
                 "budget-stop-policy.json", "apply-role-policy.json", "kill_switch_env", "redact("):
        if need not in check:
            problems.append(f"guardrails_check.py doesn't cover {need}")
    trust = json.loads((P1 / "bootstrap" / "guardrails-check-role-trust.json").read_text())
    cond = trust["Statement"][0]["Condition"]["StringEquals"]
    if cond.get("token.actions.githubusercontent.com:ref") != "refs/heads/main" or \
            not str(cond.get("token.actions.githubusercontent.com:sub", "")).endswith(":environment:guardrails-check"):
        problems.append("the check role can be assumed from outside main's guardrails-check environment")
    pol = json.loads((P1 / "bootstrap" / "guardrails-check-role-policy.json").read_text())
    writes = [a for st in pol["Statement"] for a in ([st["Action"]] if isinstance(st["Action"], str) else st["Action"])
              if not re.match(r"[a-z]+:(Get|List|Describe|View)", a)]
    if writes:
        problems.append(f"the check role can do more than read: {writes}")
    boot = (P1 / "bootstrap-github-apply.sh").read_text()
    if "aws-build-runner-guardrails-check" not in boot or "check-policy.json" not in boot:
        problems.append("bootstrap-github-apply.sh doesn't create the check role")
    return problems


@rule("2026-11-27: the guardrails check's first run failed with AccessDenied on cloudwatch:DescribeAlarms. Its role "
      "allowed alarm:guardrail-fast-* only, but a describe-by-prefix call is authorized against alarm:*, which the "
      "IAM simulator (one alarm at a time) reported as allowed.")
def guardrails_check_can_list_alarms():
    pol = json.loads((P1 / "bootstrap" / "guardrails-check-role-policy.json").read_text())
    for st in pol["Statement"]:
        acts = [st["Action"]] if isinstance(st["Action"], str) else st["Action"]
        if "cloudwatch:DescribeAlarms" in acts:
            res = [st["Resource"]] if isinstance(st["Resource"], str) else st["Resource"]
            if not any(r.endswith(":alarm:*") or r == "*" for r in res):
                return [f"the check role's DescribeAlarms is scoped to {res}; prefix listing needs alarm:*"]
            return []
    return ["the check role can't describe alarms"]


@rule("2026-11-27 (#42 A): bootstrap-budget-stop.sh moves from CloudShell to the guardrails workflow. Its role can "
      "change the guardrails, so it must stay behind Kieron's approval on main, and must not reach anything that "
      "would let an approved run widen its own or Claude's permissions.")
def guardrails_workflow_is_fenced():
    problems = []
    wf_path = ROOT / ".github" / "workflows" / "guardrails.yml"
    if not wf_path.exists():
        return ["there's no guardrails workflow"]
    wf = wf_path.read_text()
    for need, why in (("environment: guardrails\n", "it doesn't wait for the guardrails environment's approval"),
                      ("bootstrap-budget-stop.sh", "it doesn't run bootstrap-budget-stop.sh"),
                      ("github.ref == 'refs/heads/main'", "it isn't limited to main")):
        if need not in wf:
            problems.append(f"guardrails.yml: {why}")
    if re.search(r"^\s+(push|pull_request|schedule|workflow_run):", wf, re.M):
        problems.append("guardrails.yml starts on its own; it must only be started by hand")
    trust = json.loads((P1 / "bootstrap" / "guardrails-role-trust.json").read_text())
    cond = trust["Statement"][0]["Condition"]["StringEquals"]
    if cond.get("token.actions.githubusercontent.com:ref") != "refs/heads/main" or \
            not str(cond.get("token.actions.githubusercontent.com:sub", "")).endswith(":environment:guardrails"):
        problems.append("the guardrails role can be assumed from outside main's guardrails environment")
    pol = json.loads((P1 / "bootstrap" / "guardrails-role-policy.json").read_text())
    for st in pol["Statement"]:
        acts = [st["Action"]] if isinstance(st["Action"], str) else st["Action"]
        res = [st["Resource"]] if isinstance(st["Resource"], str) else st["Resource"]
        if st["Effect"] != "Allow":
            continue
        if any(a.endswith(":*") or a == "*" for a in acts):
            problems.append(f"{st['Sid']}: a wildcard action ({acts})")
        if any(a.startswith("organizations:") or "User" in a or "AttachRolePolicy" in a or "AccessKey" in a for a in acts):
            problems.append(f"{st['Sid']}: reaches users, Organizations or managed-policy attachment ({acts})")
        if any("role/godmode" in r or ":user/" in r or "policy/claude" in r or r.endswith(":policy/*") for r in res):
            problems.append(f"{st['Sid']}: reaches godmode roles, users or Claude's policies ({res})")
        writes = [a for a in acts if a.startswith("iam:") and not re.match(r"iam:(Get|List)", a)]
        if writes and any(r.endswith("*") for r in res):
            problems.append(f"{st['Sid']}: IAM changes on a wildcard resource ({res})")
        if "iam:PassRole" in acts and "iam:PassedToService" not in json.dumps(st.get("Condition", {})):
            problems.append(f"{st['Sid']}: PassRole without an iam:PassedToService condition")
    boot = (P1 / "bootstrap-github-apply.sh").read_text()
    if "aws-build-runner-guardrails" not in boot or "guardrails-policy.json" not in boot:
        problems.append("bootstrap-github-apply.sh doesn't create the guardrails role")
    return problems


@rule("2026-11-27: #79's guardrails role looked scoped by name, but an edited script could escalate to admin: write "
      "any inline policy onto the kill-switch role, then replace and invoke its code; edit who may assume the helper "
      "roles; or rewrite budget-stop, which the budget action attaches to Claude's user. parliament and Cloudsplaining "
      "didn't flag it (each statement looked fine alone). Kieron caught it by asking what stops an edited script.")
def pipeline_roles_cannot_escalate():
    problems = []
    boundary_arn = "arn:aws:iam::{{ACCOUNT_ID}}:policy/guardrails/guardrail-roles-boundary"
    widening = {"iam:UpdateAssumeRolePolicy", "iam:CreatePolicy", "iam:CreatePolicyVersion", "iam:SetDefaultPolicyVersion",
                "iam:DeleteRolePermissionsBoundary", "iam:PutRolePermissionsBoundary", "iam:AttachRolePolicy",
                "iam:AttachUserPolicy", "iam:PutUserPolicy", "iam:CreateUser", "iam:CreateAccessKey",
                "iam:UpdateRole", "sts:AssumeRole"}
    for name in ("guardrails-role-policy.json", "guardrails-check-role-policy.json"):
        pol = json.loads((P1 / "bootstrap" / name).read_text())
        denied = set()
        for st in pol["Statement"]:
            acts = [st["Action"]] if isinstance(st["Action"], str) else st["Action"]
            if st["Effect"] == "Deny":
                denied |= set(acts)
                continue
            for a in sorted(widening & set(acts)):
                problems.append(f"{name} {st['Sid']}: allows {a}, which can widen permissions")
            for a in ("iam:PutRolePolicy", "iam:CreateRole"):
                if a in acts and st.get("Condition", {}).get("StringEquals", {}).get("iam:PermissionsBoundary") != boundary_arn:
                    problems.append(f"{name} {st['Sid']}: {a} without requiring guardrails/guardrail-roles-boundary")
        if name == "guardrails-role-policy.json":
            for a in ("iam:DeleteRolePermissionsBoundary", "iam:PutRolePermissionsBoundary", "iam:UpdateAssumeRolePolicy",
                      "iam:CreatePolicyVersion"):
                if a not in denied:
                    problems.append(f"{name}: doesn't explicitly deny {a}")
    bnd_path = P1 / "bootstrap" / "guardrail-roles-boundary.json"
    if not bnd_path.exists():
        problems.append("there's no bootstrap/guardrail-roles-boundary.json")
    else:
        for st in json.loads(bnd_path.read_text())["Statement"]:
            if st["Effect"] != "Allow":
                continue
            acts = [st["Action"]] if isinstance(st["Action"], str) else st["Action"]
            if any(a.endswith(":*") or a == "*" for a in acts):
                problems.append(f"boundary {st['Sid']}: a wildcard action")
            for a in acts:
                if a.startswith(("sts:", "iam:Pass", "iam:Put", "iam:Create", "iam:Update")) or a in (
                        "lambda:UpdateFunctionCode", "lambda:CreateFunction", "lambda:InvokeFunction"):
                    problems.append(f"boundary {st['Sid']}: allows {a}")
            if any(a.startswith("iam:") for a in acts) and "policy/guardrails/budget-stop" not in json.dumps(st.get("Condition", {})):
                problems.append(f"boundary {st['Sid']}: IAM attach not limited to guardrails/budget-stop")
    script = (P1 / "bootstrap-budget-stop.sh").read_text()
    for call in ("update-assume-role-policy", "create-policy-version", "create-policy ", "put-role-permissions-boundary"):
        if re.search(rf"^[^#]*aws iam {call}", script, re.M):
            problems.append(f"bootstrap-budget-stop.sh (run by the workflow) calls aws iam {call.strip()}")
    if script.count("--permissions-boundary") < 2:
        problems.append("bootstrap-budget-stop.sh creates a helper role without the boundary")
    return problems


@rule("2026-11-27: approving the guardrails run (#80) showed only a note and a commit; Kieron saw no 'this is what the "
      "approval will do'. Each approval now waits behind a job that shows the role's limits and the diff of the code "
      "the run executes since the last approved run.")
def approvals_show_what_will_run():
    problems = []
    for wf, env in (("guardrails.yml", "guardrails"), ("apply.yml", "aws")):
        text = (ROOT / ".github" / "workflows" / wf).read_text()
        if "release_preview.py" not in text:
            problems.append(f"{wf}: no release preview before the approval")
        if not re.search(r"^  describe:\n(?:(?!^  \S).*\n)*?\s+fetch-depth: 0", text, re.M):
            problems.append(f"{wf}: the describe job doesn't fetch the history it diffs")
        gated = re.search(r"^  (?!describe)[a-z-]+:\n(?:(?!^  \S).*\n)*?\s+needs: describe\n(?:(?!^  \S).*\n)*?\s+environment:",
                          text, re.M)
        if not gated:
            problems.append(f"{wf}: the approval job doesn't wait for the describe job")
        if f"environment={env}" not in text:
            problems.append(f"{wf}: the preview doesn't compare with the last approved {env} run")
    return problems


@rule("2026-11-27 (#30 fix 7): the organization's AWS-generated region SCP allowed EC2 in us-east-1 and Bedrock in "
      "every region. Kieron: nothing unless named, then only these services, in these regions; no double negatives. "
      "org-allowed-services replaces FullAWSAccess at Root: in Sydney only the services we use, in us-east-1 only the "
      "named global services (where AWS hosts them and Identity Center lives), nothing anywhere else.")
def region_lock_holds():
    problems = []
    path = P1 / "bootstrap" / "org-allowed-services-scp.json"
    if not path.exists():
        return ["there's no bootstrap/org-allowed-services-scp.json"]
    st = {x.get("Sid"): x for x in json.loads(path.read_text())["Statement"]}
    if any(x["Effect"] != "Allow" or "NotAction" in x or x.get("Resource") != "*" for x in st.values()):
        problems.append("org-allowed-services: only plain Allow statements (no Deny, no NotAction)")
    syd, east = st.get("InSydneyTheseServices", {}), st.get("InUsEast1TheseGlobalServices", {})
    if syd.get("Condition") != {"StringEquals": {"aws:RequestedRegion": "ap-southeast-2"}}:
        problems.append("org-allowed-services: the Sydney list isn't limited to Sydney")
    if east.get("Condition") != {"StringEquals": {"aws:RequestedRegion": ["us-east-1", "unspecified"]}}:
        problems.append("org-allowed-services: the global list isn't limited to us-east-1")
    if set(st) != {"InSydneyTheseServices", "InUsEast1TheseGlobalServices"}:
        problems.append(f"org-allowed-services has statements beyond the two lists: {sorted(st)}")
    allowed = {a.split(":")[0] for x in st.values() for a in x.get("Action", [])}
    if "*" in {a for x in st.values() for a in x.get("Action", [])}:
        problems.append("org-allowed-services allows everything")
    if {"bedrock", "bedrock-mantle", "sagemaker"} & allowed:
        problems.append(f"org-allowed-services allows what nobody uses: {sorted({'bedrock', 'bedrock-mantle', 'sagemaker'} & allowed)}")
    east_svcs = {a.split(":")[0] for a in east.get("Action", [])}
    for svc in ("ec2", "ssm", "lambda", "s3", "kms", "logs", "cloudwatch", "cloudshell", "cloudformation"):
        if svc in east_svcs:
            problems.append(f"org-allowed-services lets the regional service {svc} into us-east-1")
    if not {"iam", "kms", "sts"} <= {a.split(":")[0] for a in syd.get("Action", [])}:
        problems.append("the Sydney list must name iam, kms and sts: PassRole and encrypted disks are checked in Sydney")
    script = (P1 / "bootstrap-org-allowed-services.sh").read_text()
    if "p-FullAWSAccess" not in script or script.index("attach-policy --policy-id \"$id\"") > script.index("detach-policy --policy-id \"$FULL\""):
        problems.append("bootstrap-org-allowed-services.sh must attach ours before detaching FullAWSAccess from Root")
    if 'KS_REGIONS="ap-southeast-2"' not in (P1 / "bootstrap-budget-stop.sh").read_text():
        problems.append("the kill switch still sweeps a region other than Sydney")
    if "test_region_lock.py" not in (ROOT / ".github" / "workflows" / "policy.yml").read_text():
        problems.append("CI doesn't test the allow-list against our own calls")
    return problems


@rule("2026-11-27 (#23): GitHub couldn't tell Claude from Kieron (Claude's gh ran as his account), so 'Claude never "
      "merges or approves' was only Claude's word. Claude now acts as its own App; a policy change is approved only by "
      "Kieron's GitHub review on the latest commit; and Claude Code's settings deny Claude his token and the approve, "
      "merge and protection commands. 2026-11-27 (#85): the identity is the pinned agent-identity plugin.")
def only_kieron_approves_in_github():
    problems = []
    wf = (ROOT / ".github" / "workflows" / "approval.yml")
    if not wf.exists():
        return ["there's no approval.yml"]
    text = wf.read_text()
    for need, why in (("pull_request_review:", "it doesn't re-run when Kieron reviews"),
                      ("check_approvals.py", "it doesn't check Kieron's review"),
                      ("\n  approved:\n", "its job isn't called 'approved' (the required check)"),
                      ("pull-requests: read", "it can't read the reviews")):
        if need not in text:
            problems.append(f"approval.yml: {why}")
    if re.search(r"^  approved:\n", (ROOT / ".github" / "workflows" / "policy.yml").read_text(), re.M):
        problems.append("policy.yml still has its own 'approved' job: two checks would answer to one name")
    owners = (ROOT / ".github" / "CODEOWNERS")
    if not owners.exists() or not re.search(r"^\*\s+@stickleprojects\b", owners.read_text(), re.M):
        problems.append(".github/CODEOWNERS doesn't make Kieron (@stickleprojects) the owner of everything")
    check = (P1 / "check_approvals.py").read_text()
    if 'f"review on PR #{pr}"' not in check or 'commit_id") == head_sha' not in check:
        problems.append("check_approvals.py doesn't require 'review on PR #N' and a review of the latest commit")
    settings = ROOT / ".claude" / "settings.json"
    deny = set(json.loads(settings.read_text())["permissions"]["deny"]) if settings.exists() else set()
    for rule in ("Bash(gh auth token*)", "Bash(gh pr merge*)", "Bash(gh pr review*)", "Bash(gh api *branches/*/protection*)",
                 "Read(~/.ssh/**)", "Read(~/.config/gh/**)", "Edit(.claude/settings.json)", "Bash(git remote set-url*)"):
        if rule not in deny:
            problems.append(f".claude/settings.json doesn't deny {rule}")
    cfg = json.loads(settings.read_text()) if settings.exists() else {}
    if cfg.get("enabledPlugins", {}).get("agent-identity@stickleprojects-agent-identity") is not True:
        problems.append(".claude/settings.json doesn't enable the agent-identity plugin (#85)")
    ref = cfg.get("extraKnownMarketplaces", {}).get("stickleprojects-agent-identity", {}).get("source", {}).get("ref", "")
    if not re.fullmatch(r"[0-9a-f]{40}", ref):
        problems.append(f"the agent-identity plugin isn't pinned to a full commit (ref {ref!r}): a branch would run unreviewed code")
    arm = P1 / "arm-auto-merge.sh"
    if not arm.exists() or "reviewDecision" not in arm.read_text() or "SDLC_AGENT_IDENTITY" not in arm.read_text():
        problems.append("arm-auto-merge.sh is missing, or doesn't refuse without a required review or as Kieron")
    return problems


@rule("2026-11-27: approval.yml piped check_approvals.py into tee, and GitHub runs steps with bash -e but without "
      "pipefail, so a failed check would have passed. Caught before it shipped.")
def piped_checks_keep_their_exit_code():
    problems = []
    for wf in WORKFLOWS:
        text = wf.read_text()
        for m in re.finditer(r"run: \|\n((?:\s{10,}.*\n?)+)", text):
            block = m.group(1)
            # A command continued over lines with a trailing backslash is one command: join them first.
            for line in re.sub(r"\\\n\s*", " ", block).splitlines():
                if re.search(r"\b(python3|\./[\w./-]+\.(?:sh|py))\b.*\|\s*(tee|grep)\b", line) and "|| true" not in line \
                        and "pipefail" not in block and "shell: bash" not in text[max(0, m.start() - 200):m.start()]:
                    problems.append(f"{wf.name}: '{line.strip()[:80]}' pipes a check without set -o pipefail")
    return problems


@rule("2026-11-27: usage records misled sizing twice. A 4 s plan job read 3.99 busy vCPUs of 2 with 77% CPU stall: "
      "the box counters ran from boot, so the prewarm's CPU was charged to the job. And an 8.9 GB machine peak was "
      "mostly file cache; the job needed under 2 GB. Records now count from the job's start and keep real memory apart.")
def usage_records_count_the_job_and_real_memory():
    problems = []
    usage = (P2 / "ami" / "files" / "job-usage.py").read_text()
    if "since_start(cgroup_stats(SYSTEM)" not in usage:
        problems.append("job-usage.py doesn't count the box counters from the job's start")
    if "box_anon_peak_mb" not in usage:
        problems.append("job-usage.py doesn't record the real (anonymous) memory peak")
    hook = (P2 / "ami" / "files" / "job-started.sh").read_text()
    if "cpu.stat cpu.pressure memory.pressure" not in hook:
        problems.append("job-started.sh doesn't copy the counters at the job's start")
    setup = (P2 / "ami" / "setup.sh").read_text()
    if "memwatch.py" not in setup or "build-runner-memwatch.service" not in setup.split("systemctl enable", 1)[-1]:
        problems.append("setup.sh doesn't install and enable memwatch")
    if "memwatch" not in (P2 / "ami" / "smoke-test.sh").read_text():
        problems.append("the smoke test doesn't check memwatch runs")
    if 'j.get("anon_peak_mb")' not in (P2 / "usage" / "report.py").read_text():
        problems.append("report.py's 'too small?' still judges by a peak that counts file cache")
    return problems



@rule("2026-11-28: the weekly usage report still misled sizing after #87. Its job table showed only the peak "
      "with file cache (6.2 GB for a job that used 1 GB), 'Could be light?' said '0.95 busy vCPUs of 4' on a 2-vCPU "
      "machine, and the header said 'heavy (4 vCPU)'. It also mixed machine types and pre-#87 records. Now: real memory "
      "beside the cache, per machine type, from #87 records only.")
def usage_report_shows_real_sizes():
    problems = []
    report = (P2 / "usage" / "report.py").read_text()
    if "busy vCPUs of 4" in report or "heavy (4 vCPU)" in report or "light (2 vCPU)" in report:
        problems.append("report.py hardcodes the machines' vCPUs; pools differ, so use each job's nproc")
    if "anon_peak_mb'), '.0f')} / " not in report:
        problems.append("report.py's job table doesn't show real memory beside the cache-inclusive peak")
    if "max(nproc)" not in report or "by class, workflow, job, type" not in report:
        problems.append("report.py's query doesn't read each job's vCPUs per machine type")
    if "ispresent(box_counters_from)" not in report:
        problems.append("report.py still mixes in records from before #87 (CPU from boot, memory with cache)")
    return problems



@rule("2026-11-28: after spellbound3d #323 merged, the launcher's scheduled runs kept the old pools.json for ~40 min, "
      "and nothing showed it: an unchanged read logged nothing, so nobody could tell whether GitHub or the launcher was "
      "stale. Every read now logs and records the blob sha and when, and the daily check warns when reads stop.")
def pools_reads_are_visible():
    problems = []
    runner = (P2 / "stack" / "lambda" / "runner.py").read_text()
    if 'log("pools read"' not in runner or '"read_at": int(now)' not in runner or '"sha": sha' not in runner:
        problems.append("runner.py's load_pools doesn't log and record every read with its sha and read_at")
    if "hasn't read spellbound3d's pools.json" not in (P2 / "laptop" / "daily_check.py").read_text():
        problems.append("daily_check.py doesn't warn when the launcher stops reading pools.json")
    return problems



@rule("2026-11-28: spellbound3d listed ffmpeg, ffprobe and xvfb-run in machine-requirements.txt before any image had "
      "them, and its smoke test failed every CI job (PR #333). A new tool is baked first (setup.sh AHEAD_PACKAGES, "
      "checked by smoke-test.sh), and the consumer lists it after.")
def new_tools_baked_before_listed():
    problems = []
    setup = (P2 / "ami" / "setup.sh").read_text()
    m = re.search(r'^AHEAD_PACKAGES="([^"]*)"', setup, re.M)
    if not m or "$AHEAD_PACKAGES" not in setup:
        return ["setup.sh has no AHEAD_PACKAGES install, so a consumer can't get a new tool before it lists it"]
    smoke = (P2 / "ami" / "smoke-test.sh").read_text()
    tools = {"ffmpeg": ["ffmpeg", "ffprobe"], "xvfb": ["xvfb-run"]}
    for pkg in m.group(1).split():
        for tool in tools.get(pkg, [pkg]):
            if f"command -v" not in smoke or tool not in smoke:
                problems.append(f"smoke-test.sh doesn't check {tool} (from AHEAD_PACKAGES' {pkg}) runs for the job user")
    if "## Adding a tool" not in (P2 / "ami" / "MACHINE-REQUIREMENTS.md").read_text():
        problems.append("MACHINE-REQUIREMENTS.md doesn't say how a new tool gets onto the image before it's listed")
    return problems


def main():
    wanted = sys.argv[1:]  # rule names; none = all
    unknown = set(wanted) - {name for name, _, _ in RULES}
    if unknown:
        print(f"unknown rules: {', '.join(sorted(unknown))}")
        return 1
    rules = [r for r in RULES if not wanted or r[0] in wanted]
    failed = 0
    for name, incident, fn in rules:
        problems = fn()
        print(f"{'ok  ' if not problems else 'FAIL'} {name}")
        for p in problems:
            print(f"       {p}")
        if problems:
            print(f"       why: {incident}")
            failed += 1
    print(f"\n{len(rules)} regression rules, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
