#!/usr/bin/env bash
# Deletes old runner AMIs and their snapshots, keeping the newest KEEP (default 3) and always the
# current one (SSM /build/runner/ami-id) and any candidate. Each kept AMI costs ≈US$0.58/month
# (≈10.5 GB of snapshot). As Claude's IAM user or the pipeline's role.
#
#   part2-runner/ami/prune-amis.sh [KEEP]
set -Eeuo pipefail
# shellcheck source=../lib/account.sh
. "$(dirname "$0")/../lib/account.sh" && require_account   # the target account only (#98)
export AWS_REGION=ap-southeast-2
KEEP="${1:-3}"

current="$(aws ssm get-parameter --name /build/runner/ami-id --query Parameter.Value --output text 2>/dev/null || true)"
candidate="$(aws ssm get-parameter --name /build/runner/ami-candidate --query Parameter.Value --output text 2>/dev/null || true)"
images="$(aws ec2 describe-images --owners self --filters Name=tag:resource-group,Values=build-runner \
  --query 'reverse(sort_by(Images, &CreationDate))[].[ImageId, CreationDate, BlockDeviceMappings[0].Ebs.SnapshotId]' --output text)"

n=0
while read -r ami created snap; do
  [[ -z "$ami" ]] && continue
  n=$((n + 1))
  if (( n <= KEEP )) || [[ "$ami" == "$current" || "$ami" == "$candidate" ]]; then
    echo "keep   ${ami} (${created})$([[ "$ami" == "$current" ]] && echo ', current')"
    continue
  fi
  aws ec2 deregister-image --image-id "$ami" >/dev/null
  if [[ -n "$snap" && "$snap" != None ]]; then aws ec2 delete-snapshot --snapshot-id "$snap"; fi
  echo "delete ${ami} (${created}) and ${snap}"
done <<<"$images"
