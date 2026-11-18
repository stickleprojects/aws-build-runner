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
    r = subprocess.run([sys.executable, "-I", str(P2 / "ami" / "make-user-data.py")], capture_output=True, text=True)
    if r.returncode:
        problems.append(f"make-user-data.py failed: {r.stderr.strip()[:200]}")
    else:
        # The files travel compressed (one xz tar since 2026-11-16), so look inside the bundle, not just the text.
        import base64, io, lzma, tarfile
        texts = [r.stdout]
        m = re.search(r"^    content: (\S+)$", r.stdout, re.M)
        if not m:
            problems.append("can't find the bundle in the user data to check it")
        else:
            with tarfile.open(fileobj=io.BytesIO(lzma.decompress(base64.b64decode(m.group(1))))) as tar:
                texts += [tar.extractfile(x).read().decode(errors="replace") for x in tar.getmembers() if x.isfile()]
        if any(re.search(r"gh[sp]_[A-Za-z0-9]{20,}|github_pat_", x) for x in texts):
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
    major = re.search(r"^NODE_MAJOR=(\d+)\s*$", code, re.M)
    if not major:
        problems.append("setup.sh doesn't pin NODE_MAJOR")
    elif int(major.group(1)) < 22:
        problems.append(f"setup.sh pins NODE_MAJOR={major.group(1)}; make check needs 22 or later")
    if major and "nodejs.org/dist" not in code:
        problems.append("setup.sh doesn't install Node from nodejs.org")
    if "SHASUMS256.txt" not in code or "sha256sum -c" not in code:
        problems.append("setup.sh doesn't check the Node download against nodejs.org's SHASUMS256.txt")
    # Until spellbound3d's runner-smoke checks make check's prerequisites, the image's own smoke test does.
    smoke = (P2 / "ami" / "smoke-test.sh").read_text()
    if not re.search(r'check "node >= 22\.13', smoke) and "prereqs" not in smoke:
        problems.append("smoke-test.sh doesn't check the job's Node version (or spellbound3d's prereqs check)")
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
