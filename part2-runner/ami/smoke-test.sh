#!/usr/bin/env bash
# Boots an instance from a baked AMI without a job and checks it, through Systems Manager Run Command:
# the tools and versions, the hook and allowlists, and that the one-job service started, found no job,
# and didn't start a runner. Terminates the instance afterwards, also on failure. As Claude's IAM user.
#
#   part2-runner/ami/smoke-test.sh [AMI_ID]     (default: the candidate, SSM /build/runner/ami-candidate)
#
# Testing the candidate and passing: it becomes current (/build/runner/ami-id, /build/runner/manifest),
# which is what runners launch from. Failing: the current image stays as it is.
set -Eeuo pipefail
cd "$(dirname "$0")"

export AWS_REGION=ap-southeast-2
GROUP=build-runner
candidate="$(aws ssm get-parameter --name /build/runner/ami-candidate --query Parameter.Value --output text 2>/dev/null || true)"
ami="${1:-$candidate}"
[[ -n "$ami" && "$ami" != None ]] || { echo "No AMI given and no candidate in SSM" >&2; exit 1; }
sg="$(aws ec2 describe-security-groups --filters Name=group-name,Values=build-runner-bake \
  --query 'SecurityGroups[0].GroupId' --output text)"
TAGS="{Key=resource-group,Value=${GROUP}},{Key=Name,Value=build-runner-smoke-test}"

# Ubuntu's packaged AWS CLI on Python 3.14 can't run 'ssm send-command' at all: argparse rejects a "%"
# in the command's own help text ("badly formed help string"). Then use the official CLI container,
# pinned to the same version. GitHub's runners have a working CLI and never need it.
AWS_CLI_IMAGE=amazon/aws-cli:2.31.35
ssm_send_command() { # ssm_send_command DIR_WITH_params.json INSTANCE_ID
  local args=(ssm send-command --instance-ids "$2" --document-name AWS-RunShellScript
              --query Command.CommandId --output text)
  if aws ssm send-command --generate-cli-skeleton >/dev/null 2>&1; then
    aws "${args[@]}" --parameters "file://$1/params.json"
  else
    (
      eval "$(aws configure export-credentials --format env)"
      docker run --rm -v "$1:/w:ro" -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_SESSION_TOKEN \
        -e AWS_REGION "$AWS_CLI_IMAGE" "${args[@]}" --parameters file:///w/params.json
    )
  fi
}

instance=""
cleanup() {
  if [[ -n "$instance" ]]; then aws ec2 terminate-instances --instance-ids "$instance" >/dev/null && echo "terminated ${instance}"; fi
}
trap cleanup EXIT

instance="$(aws ec2 run-instances --image-id "$ami" --instance-type c7i.large --security-group-ids "$sg" \
  --iam-instance-profile Name=build-runner-bake --instance-initiated-shutdown-behavior terminate \
  --metadata-options HttpTokens=required,HttpEndpoint=enabled \
  --tag-specifications "ResourceType=instance,Tags=[${TAGS}]" "ResourceType=volume,Tags=[${TAGS}]" \
  --query 'Instances[0].InstanceId' --output text)"
echo "smoke-testing ${ami} on ${instance} (c7i.large, no job)"

deadline=$(( $(date +%s) + 10 * 60 ))
until [[ "$(aws ssm describe-instance-information --filters "Key=InstanceIds,Values=${instance}" \
  --query 'InstanceInformationList[0].PingStatus' --output text 2>/dev/null)" == Online ]]; do
  (( $(date +%s) < deadline )) || { echo "Instance never came online in Systems Manager" >&2; exit 1; }
  sleep 10
done
echo "online in Systems Manager"

read -r -d '' checks <<'EOF' || true
set -u
R=/opt/github-runners/spellbound3d
H=$R/home
fail=0
check() { if eval "$2" >/tmp/out 2>&1; then echo "ok    $1: $(head -c 120 /tmp/out | tr '\n' ' ')"; else echo "FAIL  $1: $(head -c 400 /tmp/out | tr '\n' ' ')"; fail=1; fi; }
# As the job user, with the job's environment (not root).
as_job() { sudo -u ghrunner -H env HOME=$H NPM_CONFIG_PREFIX=$H/.local PATH=$H/.local/bin:/usr/local/bin:/usr/bin:/bin bash -c "$1"; }
M=/etc/build-runner/manifest.json
CLAUDE_VER="$(jq -r '.tools.claude_code' $M | cut -d' ' -f1)"

# The boot step and the services. They start after boot, possibly after Systems Manager is online.
for _ in $(seq 1 36); do journalctl -u github-runner-jit.service --no-pager | grep -q 'No jitconfig in user data' && break; sleep 5; done
check "boot step ran"           "systemctl is-active build-runner-boot.service"
for _ in $(seq 1 36); do test -s /run/build-runner-prewarm.json && break; sleep 5; done
check "prewarm read the tools"  "jq -e '.files > 100 and .mb > 100 and .unreadable == 0 and any(.roots[]; . == \"$H/.local/opt\")' /run/build-runner-prewarm.json"
check "instance.json"           "jq -e '.instance_type == \"c7i.large\" and .nproc == 2' /etc/build-runner/instance.json"
check "no job, no runner"       "journalctl -u github-runner-jit.service --no-pager | grep -m1 'No jitconfig in user data' || { systemctl status github-runner-jit.service --no-pager; false; }"
check "no runner process"       "! pgrep -f Runner.Listener && echo none"
check "manifest"                "jq -e '.tools.godot and .tools.claude_code and .spellbound3d_commit' $M"

# Security: jobs run as ghrunner, can't escalate, can't reach the instance metadata service.
check "service user ghrunner"   "systemctl show -p User --value github-runner-jit.service | grep -x ghrunner"
check "NoNewPrivileges"         "systemctl show -p NoNewPrivileges --value github-runner-jit.service | grep -x yes"
check "ghrunner has no sudo"    "! sudo -u ghrunner sudo -n true 2>/dev/null && echo refused"
check "IMDS blocked for jobs"   "! as_job 'curl -sf -m 3 -X PUT http://169.254.169.254/latest/api/token -H X-aws-ec2-metadata-token-ttl-seconds:60' && echo blocked"
check "IMDS open for root"      "curl -sf -m 3 -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' >/dev/null && echo ok"
check "egress capped for jobs"  "iptables -S OUTPUT | grep -E -- '--uid-owner [0-9]+ .*--hashlimit-above [0-9]+kb/s .*-j DROP' && ip6tables -S OUTPUT | grep -q -- --hashlimit-above && echo capped"
check "hook"                    "bash -n /etc/github-runner/job-started.sh && test -x /etc/github-runner/job-started.sh && echo syntax ok"
check "job-completed hook"      "bash -n /etc/github-runner/job-completed.sh && grep -qx ACTIONS_RUNNER_HOOK_JOB_COMPLETED=/etc/github-runner/job-completed.sh $R/.env && echo installed"
check "job usage record"        "/opt/github-runners/job-usage.py --dry-run | jq -e '.msg == \"job\" and .nproc == 2 and .memory_mb > 0 and (.box_mem_peak_mb | type) == \"number\" and (.box_cpu_s | type) == \"number\"'"
# The job's real (anonymous) memory peak, without file cache (2026-11-27): memwatch samples it from boot.
for _ in $(seq 1 15); do test -s /run/build-runner-memwatch.json && break; sleep 2; done
check "memwatch samples real memory" "systemctl is-active build-runner-memwatch.service && jq -e '.boot_peak_mb > 0 and .updated > 0' /run/build-runner-memwatch.json"
check "hook refuses an unknown repo" "cd /tmp && GITHUB_REPOSITORY=someone/else GITHUB_ACTOR=stickleprojects GITHUB_EVENT_NAME=push GITHUB_EVENT_PATH=/dev/null /etc/github-runner/job-started.sh; test \$? -ne 0 && echo refused"

# Tools work, as the job user (not just report a version).
# Tools baked ahead of the consumer listing them (setup.sh AHEAD_PACKAGES) each get a check here that they run as
# the job user, e.g. (2026-11-28, until spellbound3d listed them):
#   check "ffmpeg, ffprobe, xvfb-run"  "as_job 'command -v ffmpeg ffprobe xvfb-run >/dev/null && ffmpeg -version'"
check "godot runs a script"     "as_job 'printf \"extends SceneTree\nfunc _init():\n\tprint(\\\"smoke-ok\\\")\n\tquit()\n\" > /tmp/smoke.gd && godot --headless -s /tmp/smoke.gd' | grep -m1 smoke-ok"
check "godot where install-godot.sh looks" "as_job 'test -x \$HOME/.local/opt/godot-*/godot && test -s \$HOME/.cache/godot/*.zip && echo present'"
# spellbound3d's own Godot smoke test (their change 0034, on their main since ed2b2a8), offline, as ghrunner.
# Fails the image on a non-zero exit, a last line not starting PASS:, or the test missing from the image.
# POSIX sh only in this block: Run Command runs it with /bin/sh (dash), where [[ ]] doesn't exist.
if [ "$(jq -r '.runner_smoke // "absent"' $M)" = present ]; then
  check "spellbound3d godot-smoke" "as_job '/opt/build-runner/runner-smoke/godot-smoke.sh \"$(jq -r .godot_pinned $M)\" godot' > /tmp/gs 2>&1; rc=\$?; tail -1 /tmp/gs; test \$rc -eq 0 && tail -1 /tmp/gs | grep -q '^PASS:'"
else
  echo "FAIL  spellbound3d godot-smoke: tools/runner-smoke/ isn't in the image (manifest runner_smoke absent)"; fail=1
fi
check "claude from npm cache, offline" "as_job \"rm -rf /tmp/c && npm install --prefix /tmp/c --offline --no-fund --no-audit --loglevel=error @anthropic-ai/claude-code@$CLAUDE_VER >/dev/null && /tmp/c/node_modules/.bin/claude --version\""
check "claude global"           "as_job 'claude --version'"
check "uv runs python"          "as_job 'uv run --no-project python3 -c \"print(42)\"' | grep -x 42"
check "python venv"             "as_job 'python3 -m venv /tmp/v && /tmp/v/bin/python -c \"import sys; print(sys.version.split()[0])\"'"
check "git commit"              "as_job 'rm -rf /tmp/g && git init -q /tmp/g && git -C /tmp/g -c user.name=x -c user.email=x@x commit -q --allow-empty -m x && git -C /tmp/g log --oneline | wc -l'"
# The web browsers start and draw, as jobs run them (spellbound3d change 0030): downloaded here, only their
# libraries are in the image. Firefox has no WebGL 2 without a display, even headless (2026-11-24), so xvfb.
cat > /tmp/browsers.py <<'PY'
import sys
from playwright.sync_api import sync_playwright
ARGS = ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]
JS = "() => { const g = document.createElement('canvas').getContext('webgl2'); return g ? g.getParameter(g.VERSION) : null }"
with sync_playwright() as p:
    name = sys.argv[1]
    b = getattr(p, name).launch(**({"args": ARGS} if name == "chromium" else {}))
    gl = b.new_page().evaluate(JS)
    print(name, b.version, gl)
    b.close()
    sys.exit(0 if gl else 1)
PY
chmod 644 /tmp/browsers.py
WEB="$(jq -r '.web.libraries_for | join(" ")' $M)"
check "web browsers listed"     "test -n \"$WEB\" && test \"$WEB\" != null && echo $WEB"
check "web browsers downloaded" "as_job 'python3 -m venv /tmp/pw && /tmp/pw/bin/pip install -q playwright==$(jq -r .web.playwright $M) && PLAYWRIGHT_BROWSERS_PATH=/tmp/pwb /tmp/pw/bin/playwright install $WEB'"
for b in $WEB; do
  run=""; [ "$b" = firefox ] && run="xvfb-run -a"
  check "$b starts, WebGL 2"    "as_job 'PLAYWRIGHT_BROWSERS_PATH=/tmp/pwb $run /tmp/pw/bin/python /tmp/browsers.py $b'"
done
# Every software graphics driver spellbound3d's driver lines name must work, not just be installed
# (2026-11-26, their 0079: we said this test checked llvmpipe; it didn't). llvmpipe: Chromium run the way their
# render jobs run it (headed under xvfb-run, --use-angle=gl) must report Mesa's llvmpipe and never SwiftShader,
# its own fallback. lavapipe: Mesa's Vulkan driver is registered. A driver with no check fails the image.
cat > /tmp/renderer.py <<'PY'
import sys
from playwright.sync_api import sync_playwright
ARGS = ["--ignore-gpu-blocklist", "--use-angle=gl"]
JS = ("() => { const g = document.createElement('canvas').getContext('webgl2');"
      " const d = g && g.getExtension('WEBGL_debug_renderer_info');"
      " return d ? g.getParameter(d.UNMASKED_RENDERER_WEBGL) : null }")
with sync_playwright() as p:
    b = p.chromium.launch(headless=False, args=ARGS)
    r = b.new_page().evaluate(JS) or "no WebGL 2 renderer"
    b.close()
print(r)
sys.exit(0 if "llvmpipe" in r and "swiftshader" not in r.lower() else 1)
PY
chmod 644 /tmp/renderer.py
DRIVERS="$(jq -r '(.drivers // []) | join(" ")' $M)"
for d in $DRIVERS; do
  case $d in
    llvmpipe) check "driver llvmpipe draws (Chromium, --use-angle=gl)" "as_job 'PLAYWRIGHT_BROWSERS_PATH=/tmp/pwb xvfb-run -a -s \"-screen 0 1600x2000x24\" /tmp/pw/bin/python /tmp/renderer.py'" ;;
    lavapipe) check "driver lavapipe registered (Vulkan ICD)" "ls /usr/share/vulkan/icd.d/lvp_icd.*.json" ;;
    *) echo "FAIL  driver $d: the smoke test has no check for it"; fail=1 ;;
  esac
done
check "gitleaks"                "as_job 'gitleaks version'"
check "jq"                      "as_job 'echo {\\\"a\\\":1} | jq -e .a'"
check "runner version"          "as_job 'cd $R && ./config.sh --version'"
check "owner"                   "test \"\$(ls -ld $H | awk '{print \$3}')\" = ghrunner && echo ghrunner"
exit $fail
EOF

params_dir="$(mktemp -d)"
jq -n --arg c "$checks" '{commands: [$c]}' > "$params_dir/params.json"
cmd="$(ssm_send_command "$params_dir" "$instance")"
rm -rf "$params_dir"
until status="$(aws ssm get-command-invocation --command-id "$cmd" --instance-id "$instance" \
  --query Status --output text 2>/dev/null)" && [[ "$status" != Pending && "$status" != InProgress && "$status" != Delayed ]]; do
  sleep 5
done
aws ssm get-command-invocation --command-id "$cmd" --instance-id "$instance" --query StandardOutputContent --output text
[[ "$status" == Success ]] || { echo "Smoke test FAILED (${status})" >&2; exit 1; }
echo "Smoke test passed: ${ami}"
if [[ "$ami" == "$candidate" ]]; then
  # aws:ec2:image: the runner launch template resolves it at each launch (resolve:ssm), and only takes that type.
  aws ssm put-parameter --name /build/runner/ami-id --type String --data-type aws:ec2:image --value "$ami" --overwrite \
    --description "Current build-runner AMI: baked by bake.sh, passed smoke-test.sh" >/dev/null
  aws ssm put-parameter --name /build/runner/manifest --type String --overwrite \
    --value "$(aws ssm get-parameter --name /build/runner/candidate-manifest --query Parameter.Value --output text)" \
    --description "Manifest of the current build-runner AMI" >/dev/null
  aws ssm delete-parameters --names /build/runner/ami-candidate /build/runner/candidate-manifest >/dev/null
  echo "promoted ${ami} to current (SSM /build/runner/ami-id)"
fi
