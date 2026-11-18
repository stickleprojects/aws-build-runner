#!/usr/bin/env bash
# Deploys the runner (stack build-runner) as Claude's IAM user. Idempotent: run it again after any change.
#
#   part2-runner/stack/deploy.sh            deploy or update
#   part2-runner/stack/deploy.sh --delete   tear both stacks down (the webhook secret stays in SSM)
#
#   0. role and instance profile build-runner-instance (runner-role.sh; CloudFormation can't tag profiles)
#   1. stack build-runner-artifacts: a private bucket for the Lambda zip
#   2. SSM /build/github/webhook-secret (SecureString), generated once; never printed
#   3. the Lambda zip (lambda/*.py, deterministic, so an unchanged zip changes nothing), uploaded by its sha256
#   4. stack build-runner: template.yaml with the settings in config.json, in the default VPC's public subnets
#   5. prints the webhook URL, for the spellbound3d webhook (Workflow jobs events only)
#
# Runners aren't used until spellbound3d's SDLC_RUNNER says aws-light; this only makes them possible.
set -Eeuo pipefail
cd "$(dirname "$0")"
export AWS_REGION=ap-southeast-2
GROUP=build-runner
SECRET=/build/github/webhook-secret

if [[ "${1:-}" == --delete ]]; then
  bucket="$(aws cloudformation describe-stacks --stack-name build-runner-artifacts \
    --query "Stacks[0].Outputs[?OutputKey=='Bucket'].OutputValue" --output text 2>/dev/null || true)"
  aws cloudformation delete-stack --stack-name build-runner
  aws cloudformation wait stack-delete-complete --stack-name build-runner
  if [[ -n "$bucket" && "$bucket" != None ]]; then aws s3 rm "s3://${bucket}" --recursive --quiet; fi
  aws cloudformation delete-stack --stack-name build-runner-artifacts
  aws cloudformation wait stack-delete-complete --stack-name build-runner-artifacts
  echo "deleted build-runner and build-runner-artifacts"
  exit 0
fi

python3 -I test_lambda.py >/dev/null || { echo "test_lambda.py fails; not deploying" >&2; exit 1; }

echo "== 0. the instances' role and instance profile"
./runner-role.sh

echo "== 1. stack build-runner-artifacts"
aws cloudformation deploy --stack-name build-runner-artifacts --template-file artifacts.yaml \
  --tags "resource-group=${GROUP}" --no-fail-on-empty-changeset
bucket="$(aws cloudformation describe-stacks --stack-name build-runner-artifacts \
  --query "Stacks[0].Outputs[?OutputKey=='Bucket'].OutputValue" --output text)"

echo "== 2. webhook secret"
if aws ssm get-parameter --name "$SECRET" --query Parameter.Name --output text >/dev/null 2>&1; then
  echo "${SECRET}: present"
else
  tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
  ( umask 077; openssl rand -hex 32 | tr -d '\n' > "$tmp/secret" )
  aws ssm put-parameter --name "$SECRET" --type SecureString --value "file://$tmp/secret" \
    --description "HMAC secret for the spellbound3d workflow_job webhook (build-runner-webhook checks it)" \
    --tags "Key=resource-group,Value=${GROUP}" >/dev/null
  rm -f "$tmp/secret"
  echo "${SECRET}: created"
fi

echo "== 3. Lambda code"
zip="$(mktemp --suffix .zip)"
python3 -I - "$zip" lambda <<'EOF'
import sys, zipfile
from pathlib import Path
out, src = sys.argv[1], Path(sys.argv[2])
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for f in sorted(src.glob("*.py")):
        info = zipfile.ZipInfo(f.name, date_time=(2020, 1, 1, 0, 0, 0))
        info.external_attr = 0o644 << 16
        z.writestr(info, f.read_bytes(), zipfile.ZIP_DEFLATED)
EOF
key="lambda/$(sha256sum "$zip" | cut -c1-16).zip"
aws s3 cp "$zip" "s3://${bucket}/${key}" --quiet
rm -f "$zip"
echo "s3://…/${key}"

echo "== 4. stack build-runner"
# Every other parameter, always, from config.json: cloudformation deploy keeps a parameter's previous
# value when it isn't passed, so a setting changed only in the template never reached the stack (2026-11-16).
mapfile -t settings < <(jq -r 'to_entries[] | "\(.key)=\(.value | if type == "string" then . else tojson end)"' config.json)
vpc="$(aws ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)"
subnets="$(aws ec2 describe-subnets --filters "Name=vpc-id,Values=${vpc}" Name=default-for-az,Values=true \
  --query 'Subnets[].SubnetId' --output text | tr '\t' ',')"
[[ -n "$subnets" ]] || { echo "No default subnets in ${vpc}" >&2; exit 1; }
# The cost boundary a consumer's pools.json must stay within (#54), straight from policy.yaml.
boundary="$(python3 -I - ../../part1-godmode/policy.yaml <<'PY'
import json, sys, yaml
p = yaml.safe_load(open(sys.argv[1]))
cb = p["cost_boundary"]
ec2 = next(s for s in p["services"] if s["id"] == "ec2")
print(json.dumps({"instance_types": ec2["limits"]["instance_types"], "instance_sizes": cb["instance_sizes"],
                  "max_runners": cb["max_runners"], "max_instance_minutes": cb["max_instance_minutes"],
                  "max_idle_minutes": cb["max_idle_minutes"]}, separators=(",", ":")))
PY
)"
aws cloudformation deploy --stack-name build-runner --template-file template.yaml \
  --capabilities CAPABILITY_NAMED_IAM --tags "resource-group=${GROUP}" --no-fail-on-empty-changeset \
  --parameter-overrides "CodeBucket=${bucket}" "CodeKey=${key}" "VpcId=${vpc}" "Subnets=${subnets}" "Boundary=${boundary}" "${settings[@]}"

echo "== 5. webhook URL"
aws cloudformation describe-stacks --stack-name build-runner \
  --query "Stacks[0].Outputs[?OutputKey=='WebhookUrl'].OutputValue" --output text
