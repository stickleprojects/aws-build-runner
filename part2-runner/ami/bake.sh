#!/usr/bin/env bash
# Bakes the build-runner AMI in Sydney, as Claude's IAM user (all within the approved policy):
#   1. uploads setup.sh and files/ (one xz tar) to the artifacts bucket under bake/<sha256>.txz, and launches a
#      t3.large from the current Ubuntu 24.04 image whose user data fetches and checks it (fetch.py, #48)
#   2. waits for it to power off, and reads setup.sh's result from SSM (/build/bake/result/<instance>)
#   3. makes an AMI from it, then terminates the instance (also on any failure)
#   4. records the AMI as the CANDIDATE in SSM (/build/runner/ami-candidate); smoke-test.sh promotes it to
#      /build/runner/ami-id only if every check passes, so an untested image is never used for jobs
# Everything is tagged resource-group=build-runner. Takes ~10-15 min; costs ~US$0.05 plus the AMI's
# snapshot (~US$0.60-0.80/month; older AMIs can be deregistered).
#
#   part2-runner/ami/bake.sh
set -Eeuo pipefail
# shellcheck source=../lib/account.sh
. "$(dirname "$0")/../lib/account.sh" && require_account   # the target account only (#98)
cd "$(dirname "$0")"
# shellcheck source=../lib/github-app.sh
. ../lib/github-app.sh

export AWS_REGION=ap-southeast-2
GROUP=build-runner
TYPE=t3.large
SG_NAME=build-runner-bake
STAMP="$(date -u +%Y%m%d-%H%M)"
TAGS="{Key=resource-group,Value=${GROUP}},{Key=Name,Value=build-runner-bake-${STAMP}}"

work="$(mktemp -d)"
instance=""
token=""
bundle_key=""
BUCKET="$(aws ssm get-parameter --name /build/runner/artifacts-bucket --query Parameter.Value --output text)"
cleanup() {
  if [[ -n "$instance" ]]; then
    aws ec2 terminate-instances --instance-ids "$instance" >/dev/null && echo "terminated bake instance ${instance}"
  fi
  # The bake's GitHub token: out of SSM and revoked, whatever happened.
  aws ssm delete-parameter --name /build/bake/github-token >/dev/null 2>&1 || true
  if [[ -n "$token" ]]; then github_app_revoke "$token" && echo "revoked the bake's GitHub token"; fi
  if [[ -n "$bundle_key" ]]; then aws s3 rm --only-show-errors "s3://${BUCKET}/${bundle_key}" || true; fi
  rm -rf "$work"
}
trap cleanup EXIT

# The bake role already exists for the pipeline, whose role can't touch IAM (the boundary); refresh it by hand.
if [[ "${GITHUB_ACTIONS:-}" != true ]]; then ./bake-role.sh; fi

base="$(aws ssm get-parameter --name /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id \
  --query Parameter.Value --output text)"
vpc="$(aws ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)"
echo "base image ${base} (Ubuntu 24.04), default VPC ${vpc}"

# A security group with no inbound rules (the default outbound rule lets it download packages).
sg="$(aws ec2 describe-security-groups --filters "Name=group-name,Values=${SG_NAME}" "Name=vpc-id,Values=${vpc}" \
  --query 'SecurityGroups[0].GroupId' --output text)"
if [[ "$sg" == None || -z "$sg" ]]; then
  sg="$(aws ec2 create-security-group --group-name "$SG_NAME" --vpc-id "$vpc" \
    --description "build-runner AMI bake: no inbound access" \
    --tag-specifications "ResourceType=security-group,Tags=[{Key=resource-group,Value=${GROUP}},{Key=Name,Value=${SG_NAME}}]" \
    --query GroupId --output text)"
  echo "created security group ${sg}"
fi

# A 1-hour, read-only GitHub App token so the instance can check out spellbound3d for its pinned
# versions. In SSM (encrypted), never in user data; deleted and revoked in cleanup.
token="$(github_app_token)"
aws ssm put-parameter --name /build/bake/github-token --type SecureString --value "$token" --overwrite \
  --description "Short-lived token for the running AMI bake; deleted when it ends" >/dev/null

sha="$(python3 -I make-user-data.py --bundle "$work/bundle.txz")"
bundle_key="bake/${sha}.txz"
aws s3 cp --only-show-errors "$work/bundle.txz" "s3://${BUCKET}/${bundle_key}"
python3 -I make-user-data.py "$BUCKET" "$bundle_key" "$sha" > "$work/user-data"
instance="$(aws ec2 run-instances --image-id "$base" --instance-type "$TYPE" --security-group-ids "$sg" \
  --user-data "file://$work/user-data" --instance-initiated-shutdown-behavior stop \
  --iam-instance-profile Name=build-runner-bake \
  --metadata-options HttpTokens=required,HttpEndpoint=enabled \
  --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=30,VolumeType=gp3,DeleteOnTermination=true,Encrypted=true}' \
  --tag-specifications "ResourceType=instance,Tags=[${TAGS}]" "ResourceType=volume,Tags=[${TAGS}]" \
  --query 'Instances[0].InstanceId' --output text)"
echo "bake instance ${instance} (${TYPE}) started; waiting for setup.sh to finish and power off"

deadline=$(( $(date +%s) + 30 * 60 ))
started=""
while :; do
  if [[ -z "$started" ]] && aws ssm get-parameter --name "/build/bake/result/${instance}" >/dev/null 2>&1; then
    started=1; echo "setup.sh reported STARTED (the report channel works)"
  fi
  state="$(aws ec2 describe-instances --instance-ids "$instance" --query 'Reservations[0].Instances[0].State.Name' --output text)"
  [[ "$state" == stopped ]] && break
  (( $(date +%s) < deadline )) || { echo "Timed out after 30 min (state ${state})" >&2; exit 1; }
  sleep 20
done

# setup.sh reports through SSM (files/report.py): STARTED early, then OK or FAILED with its log tail.
param="/build/bake/result/${instance}"
result="$(aws ssm get-parameter --name "$param" --query Parameter.Value --output text 2>/dev/null || echo '{}')"
aws ssm delete-parameter --name "$param" >/dev/null 2>&1 || true
status="$(jq -r '.status // "none"' <<<"$result")"
echo "bake result: ${status}"
if [[ "$status" != OK ]]; then
  echo "The bake didn't report OK; no AMI made." >&2
  if [[ "$status" == none ]]; then
    echo "Nothing was reported at all: setup.sh didn't start, or report.py couldn't reach SSM." >&2
  else
    echo "Last lines of the bake log:" >&2; jq -r '.detail // ""' <<<"$result" | tail -40 >&2
  fi
  exit 1
fi

ami="$(aws ec2 create-image --instance-id "$instance" --name "build-runner-${STAMP}" \
  --description "GitHub Actions runner for spellbound3d (part2-runner/ami/setup.sh)" \
  --tag-specifications "ResourceType=image,Tags=[{Key=resource-group,Value=${GROUP}},{Key=Name,Value=build-runner-${STAMP}}]" \
    "ResourceType=snapshot,Tags=[{Key=resource-group,Value=${GROUP}},{Key=Name,Value=build-runner-${STAMP}}]" \
  --query ImageId --output text)"
echo "making AMI ${ami} (a 30 GB image takes 10-20 min)"
# Not 'aws ec2 wait image-available': it gives up after 10 minutes, before a 30 GB image is ready.
deadline=$(( $(date +%s) + 45 * 60 ))
until [[ "$(aws ec2 describe-images --image-ids "$ami" --query 'Images[0].State' --output text)" == available ]]; do
  (( $(date +%s) < deadline )) || { echo "AMI ${ami} not available after 45 min" >&2; exit 1; }
  sleep 20
done

# The manifest (what's installed, from the instance itself) goes with the AMI.
jq -r '.detail' <<<"$result" > "$work/manifest.json"
jq -e .tools "$work/manifest.json" >/dev/null || { echo "The bake's manifest is missing or invalid" >&2; exit 1; }
aws ssm put-parameter --name /build/runner/candidate-manifest --type String --value "$(jq -c . "$work/manifest.json")" \
  --overwrite --description "Manifest of the build-runner AMI awaiting its smoke test" >/dev/null
cp "$work/manifest.json" ./manifest.json && python3 -I make-image-md.py ./manifest.json ./IMAGE.md
aws ssm put-parameter --name /build/runner/ami-candidate --type String --value "$ami" --overwrite \
  --description "build-runner AMI awaiting its smoke test (bake.sh); smoke-test.sh promotes it" >/dev/null
echo "AMI ${ami} is ready; it's the candidate (SSM /build/runner/ami-candidate) until smoke-test.sh passes it"
