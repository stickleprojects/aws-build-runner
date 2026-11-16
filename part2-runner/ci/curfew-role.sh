#!/usr/bin/env bash
# The runner curfew workflow's AWS role, as Claude's IAM user (path /claude/, the guardrails boundary,
# tagged resource-group=build-runner). Idempotent.
#
#   part2-runner/ci/curfew-role.sh
#
# Only a GitHub Actions job in this repo's "curfew" environment, on main, can assume it (OIDC, immutable
# subject). It can read and write ONE parameter, /build/runner/curfew (Kieron's extend / stop), which the
# launcher reads on every run. Nothing else: it can't start, stop or see any instance itself.
set -euo pipefail

NAME=build-runner-curfew
GROUP=build-runner
REGION=ap-southeast-2
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
BOUNDARY="arn:aws:iam::${ACCOUNT_ID}:policy/guardrails/claude-boundary"
# GitHub's immutable subject for this repo: owner and repo IDs (gh api repos/OWNER/REPO --jq '.owner.id, .id').
SUB="repo:stickleprojects@11067183/aws-build-runner@1459339065:environment:curfew"

trust="$(jq -n --arg acct "$ACCOUNT_ID" --arg sub "$SUB" '{Version: "2012-10-17", Statement: [{
  Sid: "OnlyThisRepoCurfewEnvironmentOnMain", Effect: "Allow",
  Principal: {Federated: "arn:aws:iam::\($acct):oidc-provider/token.actions.githubusercontent.com"},
  Action: "sts:AssumeRoleWithWebIdentity",
  Condition: {StringEquals: {"token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                             "token.actions.githubusercontent.com:sub": $sub,
                             "token.actions.githubusercontent.com:ref": "refs/heads/main"}}}]}')"

policy="$(jq -n --arg acct "$ACCOUNT_ID" --arg r "$REGION" '{Version: "2012-10-17", Statement: [
  {Sid: "TheCurfewOverridesOnly", Effect: "Allow", Action: ["ssm:GetParameter", "ssm:PutParameter"],
   Resource: "arn:aws:ssm:\($r):\($acct):parameter/build/runner/curfew"}]}')"

if aws iam get-role --role-name "$NAME" >/dev/null 2>&1; then
  aws iam update-assume-role-policy --role-name "$NAME" --policy-document "$trust"
  echo "trust policy updated"
else
  aws iam create-role --role-name "$NAME" --path /claude/ --permissions-boundary "$BOUNDARY" \
    --assume-role-policy-document "$trust" \
    --description "Runner curfew (GitHub Actions, environment curfew, main): read and write /build/runner/curfew" \
    --tags "Key=resource-group,Value=${GROUP}" --query Role.Arn --output text | sed 's/[0-9]\{12\}/<account>/; s/^/created /'
fi
aws iam put-role-policy --role-name "$NAME" --policy-name curfew --policy-document "$policy"
echo "role ${NAME} ready (for the GitHub environment secret AWS_CURFEW_ROLE_ARN)"
