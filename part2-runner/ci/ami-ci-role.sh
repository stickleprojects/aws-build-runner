#!/usr/bin/env bash
# The weekly AMI pipeline's AWS role, as Claude's IAM user (within the approved policy: path /claude/,
# the guardrails boundary, tagged resource-group=build-runner). Idempotent.
#
#   part2-runner/ci/ami-ci-role.sh
#
# Only a GitHub Actions job in this repo's "ami" environment, on main, can assume it (OIDC; the
# repo sends GitHub's immutable, ID-based subject). It can do what bake.sh, smoke-test.sh and
# prune-amis.sh need, in Sydney, and no IAM beyond passing the existing bake role to EC2.
set -euo pipefail
# shellcheck source=../lib/account.sh
. "$(dirname "$0")/../lib/account.sh" && require_account   # the target account only (#98)
cd "$(dirname "$0")"

NAME=build-runner-ami-ci
GROUP=build-runner
REGION=ap-southeast-2
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
BOUNDARY="arn:aws:iam::${ACCOUNT_ID}:policy/guardrails/claude-boundary"
# GitHub's immutable subject for this repo: owner and repo IDs (gh api repos/OWNER/REPO --jq '.owner.id, .id').
SUB="repo:stickleprojects@11067183/aws-build-runner@1459339065:environment:ami"

trust="$(jq -n --arg acct "$ACCOUNT_ID" --arg sub "$SUB" '{Version: "2012-10-17", Statement: [{
  Sid: "OnlyThisRepoAmiEnvironmentOnMain", Effect: "Allow",
  Principal: {Federated: "arn:aws:iam::\($acct):oidc-provider/token.actions.githubusercontent.com"},
  Action: "sts:AssumeRoleWithWebIdentity",
  Condition: {StringEquals: {"token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                             "token.actions.githubusercontent.com:sub": $sub,
                             "token.actions.githubusercontent.com:ref": "refs/heads/main"}}}]}')"

BUCKET="$(aws ssm get-parameter --name /build/runner/artifacts-bucket --query Parameter.Value --output text)"
policy="$(jq -n --arg acct "$ACCOUNT_ID" --arg r "$REGION" --arg bundles "arn:aws:s3:::${BUCKET}/bake/*" '{Version: "2012-10-17", Statement: [
  {Sid: "BakeTestAndPruneInSydney", Effect: "Allow", Resource: "*",
   Action: ["ec2:Describe*", "ec2:RunInstances", "ec2:TerminateInstances", "ec2:CreateImage",
            "ec2:DeregisterImage", "ec2:DeleteSnapshot", "ec2:CreateTags", "ec2:CreateSecurityGroup"],
   Condition: {StringEquals: {"aws:RequestedRegion": $r}}},
  {Sid: "BuildParameters", Effect: "Allow",
   Action: ["ssm:GetParameter", "ssm:GetParameters", "ssm:PutParameter", "ssm:DeleteParameter",
            "ssm:DeleteParameters", "ssm:AddTagsToResource"],
   Resource: ["arn:aws:ssm:\($r):\($acct):parameter/build/*", "arn:aws:ssm:\($r)::parameter/aws/service/canonical/*"]},
  {Sid: "SmokeTestThroughSystemsManager", Effect: "Allow",
   Action: ["ssm:SendCommand", "ssm:GetCommandInvocation", "ssm:ListCommandInvocations", "ssm:DescribeInstanceInformation"],
   Resource: "*", Condition: {StringEquals: {"aws:RequestedRegion": $r}}},
  {Sid: "SecureStringsThroughSsmOnly", Effect: "Allow", Action: ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey"],
   Resource: "*", Condition: {StringEquals: {"kms:ViaService": "ssm.\($r).amazonaws.com"}}},
  {Sid: "LaunchWithTheBakeRole", Effect: "Allow", Action: "iam:PassRole",
   Resource: "arn:aws:iam::\($acct):role/claude/build-runner-bake",
   Condition: {StringEquals: {"iam:PassedToService": "ec2.amazonaws.com"}}},
  {Sid: "BakeBundle", Effect: "Allow", Action: ["s3:PutObject", "s3:DeleteObject"], Resource: $bundles},
  {Sid: "Identity", Effect: "Allow", Action: "sts:GetCallerIdentity", Resource: "*"}]}')"

if aws iam get-role --role-name "$NAME" >/dev/null 2>&1; then
  aws iam update-assume-role-policy --role-name "$NAME" --policy-document "$trust"
  echo "trust policy updated"
else
  aws iam create-role --role-name "$NAME" --path /claude/ --permissions-boundary "$BOUNDARY" \
    --assume-role-policy-document "$trust" --max-session-duration 7200 \
    --description "Weekly AMI pipeline (GitHub Actions, environment ami, main): bake, smoke-test, prune" \
    --tags "Key=resource-group,Value=${GROUP}" --query Role.Arn --output text | sed 's/[0-9]\{12\}/<account>/; s/^/created /'
fi
aws iam put-role-policy --role-name "$NAME" --policy-name ami-pipeline --policy-document "$policy"
echo "role ${NAME} ready (for the GitHub environment secret AWS_AMI_ROLE_ARN)"
