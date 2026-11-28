#!/usr/bin/env bash
# The daily usage report's AWS role, as Claude's IAM user (path /claude/, the guardrails boundary,
# tagged resource-group=build-runner). Idempotent.
#
#   part2-runner/ci/usage-role.sh
#
# Only a GitHub Actions job in this repo's "usage" environment, on main, can assume it (OIDC, immutable
# subject). Read-only: query the runner's logs (launcher, webhook, per-job usage) and read Sydney spot
# prices. Nothing else.
set -euo pipefail
# shellcheck source=../lib/account.sh
. "$(dirname "$0")/../lib/account.sh" && require_account   # the target account only (#98)

NAME=build-runner-usage
GROUP=build-runner
REGION=ap-southeast-2
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
BOUNDARY="arn:aws:iam::${ACCOUNT_ID}:policy/guardrails/claude-boundary"
# GitHub's immutable subject for this repo: owner and repo IDs (gh api repos/OWNER/REPO --jq '.owner.id, .id').
SUB="repo:stickleprojects@11067183/aws-build-runner@1459339065:environment:usage"

trust="$(jq -n --arg acct "$ACCOUNT_ID" --arg sub "$SUB" '{Version: "2012-10-17", Statement: [{
  Sid: "OnlyThisRepoUsageEnvironmentOnMain", Effect: "Allow",
  Principal: {Federated: "arn:aws:iam::\($acct):oidc-provider/token.actions.githubusercontent.com"},
  Action: "sts:AssumeRoleWithWebIdentity",
  Condition: {StringEquals: {"token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                             "token.actions.githubusercontent.com:sub": $sub,
                             "token.actions.githubusercontent.com:ref": "refs/heads/main"}}}]}')"

policy="$(jq -n --arg acct "$ACCOUNT_ID" --arg r "$REGION" '{Version: "2012-10-17", Statement: [
  {Sid: "QueryTheRunnerLogsOnly", Effect: "Allow", Action: "logs:StartQuery",
   Resource: ["arn:aws:logs:\($r):\($acct):log-group:/aws/lambda/build-runner-launcher:*",
              "arn:aws:logs:\($r):\($acct):log-group:/aws/lambda/build-runner-webhook:*",
              "arn:aws:logs:\($r):\($acct):log-group:/build/runner/jobs:*"]},
  {Sid: "ReadQueryResults", Effect: "Allow", Action: ["logs:GetQueryResults", "logs:StopQuery"], Resource: "*"},
  {Sid: "SpotPricesInSydney", Effect: "Allow", Action: "ec2:DescribeSpotPriceHistory", Resource: "*",
   Condition: {StringEquals: {"aws:RequestedRegion": $r}}},
  {Sid: "Identity", Effect: "Allow", Action: "sts:GetCallerIdentity", Resource: "*"}]}')"

if aws iam get-role --role-name "$NAME" >/dev/null 2>&1; then
  aws iam update-assume-role-policy --role-name "$NAME" --policy-document "$trust"
  echo "trust policy updated"
else
  aws iam create-role --role-name "$NAME" --path /claude/ --permissions-boundary "$BOUNDARY" \
    --assume-role-policy-document "$trust" \
    --description "Daily runner usage report (GitHub Actions, environment usage, main): read runner logs, spot prices" \
    --tags "Key=resource-group,Value=${GROUP}" --query Role.Arn --output text | sed 's/[0-9]\{12\}/<account>/; s/^/created /'
fi
aws iam put-role-policy --role-name "$NAME" --policy-name usage-report --policy-document "$policy"
echo "role ${NAME} ready (for the GitHub environment secret AWS_USAGE_ROLE_ARN)"
