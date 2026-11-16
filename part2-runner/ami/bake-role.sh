#!/usr/bin/env bash
# The bake instance's role, as Claude's IAM user (within the approved policy: path /claude/, the
# guardrails boundary, tagged resource-group=build-runner). Idempotent; bake.sh runs it first.
#
# The role can only:
#   - report the bake's result: ssm:PutParameter on /build/bake/* (files/report.py)
#   - register with Systems Manager, so a running bake instance can be inspected (Run Command, Session Manager)
set -Eeuo pipefail
cd "$(dirname "$0")"

NAME=build-runner-bake
GROUP=build-runner
ROLE_PATH=/claude/
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
BOUNDARY="arn:aws:iam::${ACCOUNT_ID}:policy/guardrails/claude-boundary"
REGION="${AWS_REGION:-ap-southeast-2}"

trust='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
policy="$(jq -n --arg param "arn:aws:ssm:${REGION}:${ACCOUNT_ID}:parameter/build/bake/*" '{
  Version: "2012-10-17",
  Statement: [
    {Sid: "ReportBakeResult", Effect: "Allow", Action: "ssm:PutParameter", Resource: $param},
    {Sid: "SystemsManagerAgent", Effect: "Allow", Resource: "*",
     Action: ["ssm:UpdateInstanceInformation", "ssmmessages:CreateControlChannel", "ssmmessages:CreateDataChannel",
              "ssmmessages:OpenControlChannel", "ssmmessages:OpenDataChannel", "ec2messages:AcknowledgeMessage",
              "ec2messages:DeleteMessage", "ec2messages:FailMessage", "ec2messages:GetEndpoint",
              "ec2messages:GetMessages", "ec2messages:SendReply"]}
  ]}')"

if ! aws iam get-role --role-name "$NAME" >/dev/null 2>&1; then
  aws iam create-role --role-name "$NAME" --path "$ROLE_PATH" --permissions-boundary "$BOUNDARY" \
    --assume-role-policy-document "$trust" --description "build-runner AMI bake instance: report result, SSM agent" \
    --tags "Key=resource-group,Value=${GROUP}" --query Role.Arn --output text | sed 's/^/created role /'
fi
aws iam put-role-policy --role-name "$NAME" --policy-name bake --policy-document "$policy"

if ! aws iam get-instance-profile --instance-profile-name "$NAME" >/dev/null 2>&1; then
  aws iam create-instance-profile --instance-profile-name "$NAME" --path "$ROLE_PATH" \
    --tags "Key=resource-group,Value=${GROUP}" --query InstanceProfile.Arn --output text | sed 's/^/created instance profile /'
fi
if [[ "$(aws iam get-instance-profile --instance-profile-name "$NAME" --query 'length(InstanceProfile.Roles)' --output text)" == 0 ]]; then
  aws iam add-role-to-instance-profile --instance-profile-name "$NAME" --role-name "$NAME"
  echo "role added to instance profile; waiting for IAM to propagate"
  sleep 15
fi
echo "role and instance profile ${NAME} ready"
