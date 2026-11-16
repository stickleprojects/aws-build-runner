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
check "hook"                    "bash -n /etc/github-runner/job-started.sh && test -x /etc/github-runner/job-started.sh && echo syntax ok"
check "hook refuses an unknown repo" "cd /tmp && GITHUB_REPOSITORY=someone/else GITHUB_ACTOR=stickleprojects GITHUB_EVENT_NAME=push GITHUB_EVENT_PATH=/dev/null /etc/github-runner/job-started.sh; test \$? -ne 0 && echo refused"

# Tools work, as the job user (not just report a version).
check "godot runs a script"     "as_job 'printf \"extends SceneTree\nfunc _init():\n\tprint(\\\"smoke-ok\\\")\n\tquit()\n\" > /tmp/smoke.gd && godot --headless -s /tmp/smoke.gd' | grep -m1 smoke-ok"
check "godot where install-godot.sh looks" "as_job 'test -x \$HOME/.local/opt/godot-*/godot && test -s \$HOME/.cache/godot/*.zip && echo present'"
# spellbound3d's own Godot smoke test (their change 0034), offline, as ghrunner. Fails the image on a
# non-zero exit or a last line not starting PASS:. Skipped (with a note) until it's on their main.
if [[ "$(jq -r '.runner_smoke // "absent"' $M)" == present ]]; then
  check "spellbound3d godot-smoke" "as_job '/opt/build-runner/runner-smoke/godot-smoke.sh \"$(jq -r .godot_pinned $M)\" godot' > /tmp/gs 2>&1; rc=\$?; tail -1 /tmp/gs; test \$rc -eq 0 && tail -1 /tmp/gs | grep -q '^PASS:'"
else
  echo "skip  spellbound3d godot-smoke: tools/runner-smoke/ isn't on spellbound3d main yet"
fi
check "claude from npm cache, offline" "as_job \"rm -rf /tmp/c && npm install --prefix /tmp/c --offline --no-fund --no-audit --loglevel=error @anthropic-ai/claude-code@$CLAUDE_VER >/dev/null && /tmp/c/node_modules/.bin/claude --version\""
check "claude global"           "as_job 'claude --version'"
check "uv runs python"          "as_job 'uv run --no-project python3 -c \"print(42)\"' | grep -x 42"
check "python venv"             "as_job 'python3 -m venv /tmp/v && /tmp/v/bin/python -c \"import sys; print(sys.version.split()[0])\"'"
check "git commit"              "as_job 'rm -rf /tmp/g && git init -q /tmp/g && git -C /tmp/g -c user.name=x -c user.email=x@x commit -q --allow-empty -m x && git -C /tmp/g log --oneline | wc -l'"
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
  aws ssm put-parameter --name /build/runner/ami-id --type String --value "$ami" --overwrite \
    --description "Current build-runner AMI: baked by bake.sh, passed smoke-test.sh" >/dev/null
  aws ssm put-parameter --name /build/runner/manifest --type String --overwrite \
    --value "$(aws ssm get-parameter --name /build/runner/candidate-manifest --query Parameter.Value --output text)" \
    --description "Manifest of the current build-runner AMI" >/dev/null
  aws ssm delete-parameters --names /build/runner/ami-candidate /build/runner/candidate-manifest >/dev/null
  echo "promoted ${ami} to current (SSM /build/runner/ami-id)"
fi
