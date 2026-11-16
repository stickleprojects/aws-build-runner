#!/usr/bin/env bash
# The runner instances' role and instance profile, as Claude's IAM user (path /claude/, the guardrails
# boundary, tagged resource-group=build-runner). Outside the stack because CloudFormation can't tag an
# instance profile at creation, and the policy denies creating one untagged (same as ami/bake-role.sh).
# Idempotent; deploy.sh runs it first.
#
# The role can only register with Systems Manager (Run Command, Session Manager, as root). Jobs run as
# ghrunner, which the boot step blocks from the instance metadata service, so jobs can't use it at all.
set -Eeuo pipefail

NAME=build-runner-instance
GROUP=build-runner
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
BOUNDARY="arn:aws:iam::${ACCOUNT_ID}:policy/guardrails/claude-boundary"

trust='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
policy='{"Version":"2012-10-17","Statement":[{"Sid":"SystemsManagerAgent","Effect":"Allow","Resource":"*",
  "Action":["ssm:UpdateInstanceInformation","ssmmessages:CreateControlChannel","ssmmessages:CreateDataChannel",
            "ssmmessages:OpenControlChannel","ssmmessages:OpenDataChannel","ec2messages:AcknowledgeMessage",
            "ec2messages:DeleteMessage","ec2messages:FailMessage","ec2messages:GetEndpoint",
            "ec2messages:GetMessages","ec2messages:SendReply"]}]}'

if ! aws iam get-role --role-name "$NAME" >/dev/null 2>&1; then
  aws iam create-role --role-name "$NAME" --path /claude/ --permissions-boundary "$BOUNDARY" \
    --assume-role-policy-document "$trust" --description "build-runner instance: Systems Manager agent only" \
    --tags "Key=resource-group,Value=${GROUP}" --query Role.Arn --output text | sed 's/^/created role /'
fi
aws iam put-role-policy --role-name "$NAME" --policy-name ssm-agent --policy-document "$policy"

if ! aws iam get-instance-profile --instance-profile-name "$NAME" >/dev/null 2>&1; then
  aws iam create-instance-profile --instance-profile-name "$NAME" --path /claude/ \
    --tags "Key=resource-group,Value=${GROUP}" --query InstanceProfile.Arn --output text | sed 's/^/created instance profile /'
fi
if [[ "$(aws iam get-instance-profile --instance-profile-name "$NAME" --query 'length(InstanceProfile.Roles)' --output text)" == 0 ]]; then
  aws iam add-role-to-instance-profile --instance-profile-name "$NAME" --role-name "$NAME"
  echo "role added to instance profile; waiting for IAM to propagate"
  sleep 15
fi
echo "role and instance profile ${NAME} ready"
