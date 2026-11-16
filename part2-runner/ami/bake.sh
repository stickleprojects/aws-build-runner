#!/usr/bin/env bash
# Bakes the build-runner AMI in Sydney, as Claude's IAM user (all within the approved policy):
#   1. launches a t3.large from the current Ubuntu 24.04 image, with setup.sh as cloud-init user data
#   2. waits for it to power off, and checks the console says BAKE-OK
#   3. makes an AMI from it, then terminates the instance (also on any failure)
#   4. records the AMI id in SSM: /build/runner/ami-id
# Everything is tagged resource-group=build-runner. Takes ~10-15 min; costs ~US$0.05 plus the AMI's
# snapshot (~US$0.60-0.80/month; older AMIs can be deregistered).
#
#   part2-runner/ami/bake.sh
set -Eeuo pipefail
cd "$(dirname "$0")"

export AWS_REGION=ap-southeast-2
GROUP=build-runner
TYPE=t3.large
SG_NAME=build-runner-bake
STAMP="$(date -u +%Y%m%d-%H%M)"
TAGS="{Key=resource-group,Value=${GROUP}},{Key=Name,Value=build-runner-bake-${STAMP}}"

work="$(mktemp -d)"
instance=""
cleanup() {
  if [[ -n "$instance" ]]; then
    aws ec2 terminate-instances --instance-ids "$instance" >/dev/null && echo "terminated bake instance ${instance}"
  fi
  rm -rf "$work"
}
trap cleanup EXIT

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

python3 -I make-user-data.py > "$work/user-data"
instance="$(aws ec2 run-instances --image-id "$base" --instance-type "$TYPE" --security-group-ids "$sg" \
  --user-data "file://$work/user-data" --instance-initiated-shutdown-behavior stop \
  --metadata-options HttpTokens=required,HttpEndpoint=enabled \
  --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=30,VolumeType=gp3,DeleteOnTermination=true,Encrypted=true}' \
  --tag-specifications "ResourceType=instance,Tags=[${TAGS}]" "ResourceType=volume,Tags=[${TAGS}]" \
  --query 'Instances[0].InstanceId' --output text)"
echo "bake instance ${instance} (${TYPE}) started; waiting for setup.sh to finish and power off"

deadline=$(( $(date +%s) + 30 * 60 ))
while :; do
  state="$(aws ec2 describe-instances --instance-ids "$instance" --query 'Reservations[0].Instances[0].State.Name' --output text)"
  [[ "$state" == stopped ]] && break
  (( $(date +%s) < deadline )) || { echo "Timed out after 30 min (state ${state})" >&2; exit 1; }
  sleep 20
done

console="$(aws ec2 get-console-output --instance-id "$instance" --latest --query Output --output text 2>/dev/null || true)"
grep -E '^bake: ' <<<"$console" | tail -15 || true
if ! grep -q 'bake: BAKE-OK' <<<"$console"; then
  echo "The bake didn't report BAKE-OK; no AMI made." >&2
  exit 1
fi

ami="$(aws ec2 create-image --instance-id "$instance" --name "build-runner-${STAMP}" \
  --description "GitHub Actions runner for spellbound3d (part2-runner/ami/setup.sh)" \
  --tag-specifications "ResourceType=image,Tags=[{Key=resource-group,Value=${GROUP}},{Key=Name,Value=build-runner-${STAMP}}]" \
    "ResourceType=snapshot,Tags=[{Key=resource-group,Value=${GROUP}},{Key=Name,Value=build-runner-${STAMP}}]" \
  --query ImageId --output text)"
echo "making AMI ${ami}"
aws ec2 wait image-available --image-ids "$ami"

aws ssm put-parameter --name /build/runner/ami-id --type String --value "$ami" --overwrite \
  --description "Current build-runner AMI (part2-runner/ami/bake.sh)" >/dev/null
aws ssm add-tags-to-resource --resource-type Parameter --resource-id /build/runner/ami-id \
  --tags "Key=resource-group,Value=${GROUP}" >/dev/null
echo "AMI ${ami} is ready; recorded in SSM /build/runner/ami-id"
