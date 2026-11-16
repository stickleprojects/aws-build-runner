#!/usr/bin/env bash
# Boots an instance from a baked AMI without a job and checks it, through Systems Manager Run Command:
# the tools and versions, the hook and allowlists, and that the one-job service started, found no job,
# and didn't start a runner. Terminates the instance afterwards, also on failure. As Claude's IAM user.
#
#   part2-runner/ami/smoke-test.sh [AMI_ID]     (default: SSM /build/runner/ami-id)
set -Eeuo pipefail
cd "$(dirname "$0")"

export AWS_REGION=ap-southeast-2
GROUP=build-runner
ami="${1:-$(aws ssm get-parameter --name /build/runner/ami-id --query Parameter.Value --output text)}"
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
fail=0
check() { if eval "$2" >/tmp/out 2>&1; then echo "ok    $1: $(head -c 120 /tmp/out | tr '\n' ' ')"; else echo "FAIL  $1: $(head -c 300 /tmp/out | tr '\n' ' ')"; fail=1; fi; }
check "godot"             "sudo -u ghrunner -H $R/home/.local/opt/godot-4.7.2/godot --headless --version 2>&1 | grep -E '^4\.7\.2'"
check "godot cache zip"   "test -s $R/home/.cache/godot/Godot_v4.7.2-stable_linux.x86_64.zip && echo present"
check "fontconfig"        "ldconfig -p | grep -m1 libfontconfig.so.1"
check "runner version"    "sudo -u ghrunner -H bash -c 'cd $R && ./config.sh --version'"
check "runner env"        "grep -E '^(HOME|NPM_CONFIG_PREFIX|ACTIONS_RUNNER_HOOK_JOB_STARTED)=' $R/.env | tr '\n' ' ' | grep -q ACTIONS_RUNNER_HOOK_JOB_STARTED && echo 3 settings"
check "hook"              "bash -n /etc/github-runner/job-started.sh && test -x /etc/github-runner/job-started.sh && echo syntax ok"
check "allowlists"        "grep -qx stickleprojects/spellbound3d /etc/github-runner/allowed-repos.txt && grep -qx stickleprojects /etc/github-runner/allowed-actors.txt && echo present"
check "hook refuses a fork-less unknown repo" "cd /tmp && GITHUB_REPOSITORY=someone/else GITHUB_ACTOR=stickleprojects GITHUB_EVENT_NAME=push GITHUB_EVENT_PATH=/dev/null /etc/github-runner/job-started.sh; test \$? -ne 0 && echo refused"
check "one-job service"   "systemctl is-enabled github-runner-jit.service"
# The service starts after cloud-init's final stage, which can be after Systems Manager is online: wait for it.
for _ in $(seq 1 36); do journalctl -u github-runner-jit.service --no-pager | grep -q 'No jitconfig in user data' && break; sleep 5; done
check "no job, no runner" "journalctl -u github-runner-jit.service --no-pager | grep -m1 'No jitconfig in user data' || { systemctl status github-runner-jit.service --no-pager; false; }"
check "no runner process" "! pgrep -f Runner.Listener && echo none"
check "node/npm/python/jq" "echo node \$(node --version) npm \$(npm --version) \$(python3 --version) \$(jq --version)"
check "owner"             "test \"\$(ls -ld $R/home | awk '{print \$3}')\" = ghrunner && echo ghrunner"
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
