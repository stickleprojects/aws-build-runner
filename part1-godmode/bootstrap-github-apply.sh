#!/usr/bin/env bash
# Godmode, once (and again only if bootstrap/*.json changes): lets the "apply" GitHub Actions
# workflow sign in to AWS without stored keys. Run in CloudShell as admin:
#
#   cd ~/aws-build-runner/part1-godmode && git pull && ./bootstrap-github-apply.sh
#
# Creates, or updates to match bootstrap/:
#   - the GitHub OIDC identity provider (token.actions.githubusercontent.com)
#   - the role godmode/aws-build-runner-apply, which only jobs in this repo's GitHub environments
#     "aws" and "aws-rollback" can assume (those wait for Kieron's approval and accept main only),
#     with only the permissions apply.sh and rollback.sh use.
#   - the role godmode/aws-build-runner-guardrails-check (#42 C), which only the read-only
#     guardrails-check workflow on main can assume (environment "guardrails-check", no approval):
#     it reads the guardrails to report drift and can change nothing.
#   - the role godmode/aws-build-runner-guardrails (#42 A), which only the guardrails workflow on main can
#     assume, after Kieron's approval (environment "guardrails"). It runs bootstrap-budget-stop.sh, so it
#     can change the guardrails, by name; never Claude's user or policies, these godmode roles, or itself.
# Idempotent. Prints the role ARN for the GitHub environment secret AWS_APPLY_ROLE_ARN.
set -euo pipefail
cd "$(dirname "$0")"

ROLE_NAME=aws-build-runner-apply
ROLE_PATH=/godmode/
REGION=ap-southeast-2
ACCOUNT_ID="$(python3 -I config.py AWS_ACCOUNT_ID)"
USER_NAME="$(python3 -I config.py AWS_CLAUDE_USER)"

caller_arn="$(aws sts get-caller-identity --query Arn --output text)"
[[ "$(aws sts get-caller-identity --query Account --output text)" == "$ACCOUNT_ID" ]] \
  || { echo "Refusing: credentials are for another account than AWS_ACCOUNT_ID." >&2; exit 1; }
[[ "$caller_arn" != *":user/${USER_NAME}" ]] || { echo "Refusing: run this as admin, not as Claude's user." >&2; exit 1; }
echo "Account ${ACCOUNT_ID} as ${caller_arn}"

work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
python3 -I config.py --render bootstrap/apply-role-trust.json > "$work/trust.json"
python3 -I config.py --render bootstrap/apply-role-policy.json > "$work/policy.json"
python3 -I config.py --render bootstrap/guardrails-check-role-trust.json > "$work/check-trust.json"
python3 -I config.py --render bootstrap/guardrails-check-role-policy.json > "$work/check-policy.json"
python3 -I config.py --render bootstrap/guardrails-role-trust.json > "$work/guardrails-trust.json"
python3 -I config.py --render bootstrap/guardrails-role-policy.json > "$work/guardrails-policy.json"

echo "== validate with IAM Access Analyzer"
blocking=0
for pair in "IDENTITY_POLICY:policy.json" "RESOURCE_POLICY:trust.json" "IDENTITY_POLICY:check-policy.json" "RESOURCE_POLICY:check-trust.json" \
            "IDENTITY_POLICY:guardrails-policy.json" "RESOURCE_POLICY:guardrails-trust.json"; do
  type="${pair%%:*}"; file="${pair#*:}"
  extra=(); [[ "$type" == RESOURCE_POLICY ]] && extra=(--validate-policy-resource-type AWS::IAM::AssumeRolePolicyDocument)
  findings="$(aws accessanalyzer validate-policy --region "$REGION" --policy-type "$type" "${extra[@]}" \
    --policy-document "file://$work/$file" --query 'findings[].[findingType,issueCode,findingDetails]' --output text)"
  if [[ -n "$findings" ]]; then echo "-- $file"; echo "$findings"; grep -qE '^(ERROR|SECURITY_WARNING)' <<<"$findings" && blocking=1; fi
done
(( blocking )) && { echo "Blocking findings; nothing was changed." >&2; exit 1; }

echo "== GitHub OIDC provider"
provider="arn:aws:iam::${ACCOUNT_ID}:oidc-provider/token.actions.githubusercontent.com"
if aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$provider" >/dev/null 2>&1; then
  echo "exists"
else
  # AWS no longer checks thumbprints for GitHub's provider, but the API still wants one; these are GitHub's.
  aws iam create-open-id-connect-provider --url https://token.actions.githubusercontent.com \
    --client-id-list sts.amazonaws.com \
    --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1 1c58a3a8518e8759bf075b76b750d4f2df264fcd \
    --query OpenIDConnectProviderArn --output text | sed 's/^/created /'
fi

echo "== role ${ROLE_PATH}${ROLE_NAME}"
if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  aws iam update-assume-role-policy --role-name "$ROLE_NAME" --policy-document "file://$work/trust.json"
  echo "trust policy updated"
else
  aws iam create-role --role-name "$ROLE_NAME" --path "$ROLE_PATH" --max-session-duration 3600 \
    --description "GitHub Actions apply/rollback for stickleprojects/aws-build-runner (approved runs only)" \
    --assume-role-policy-document "file://$work/trust.json" --query Role.Arn --output text | sed 's/^/created /'
fi
aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name apply-and-rollback \
  --policy-document "file://$work/policy.json"
echo "permissions policy set"

CHECK_ROLE=aws-build-runner-guardrails-check
echo "== role ${ROLE_PATH}${CHECK_ROLE} (read-only)"
if aws iam get-role --role-name "$CHECK_ROLE" >/dev/null 2>&1; then
  aws iam update-assume-role-policy --role-name "$CHECK_ROLE" --policy-document "file://$work/check-trust.json"
  echo "trust policy updated"
else
  aws iam create-role --role-name "$CHECK_ROLE" --path "$ROLE_PATH" --max-session-duration 3600 \
    --description "GitHub Actions guardrails-check for stickleprojects/aws-build-runner (read-only)" \
    --assume-role-policy-document "file://$work/check-trust.json" --query Role.Arn --output text | sed 's/^/created /'
fi
aws iam put-role-policy --role-name "$CHECK_ROLE" --policy-name guardrails-check \
  --policy-document "file://$work/check-policy.json"
echo "permissions policy set"

GUARD_ROLE=aws-build-runner-guardrails
echo "== role ${ROLE_PATH}${GUARD_ROLE} (the guardrails workflow)"
if aws iam get-role --role-name "$GUARD_ROLE" >/dev/null 2>&1; then
  aws iam update-assume-role-policy --role-name "$GUARD_ROLE" --policy-document "file://$work/guardrails-trust.json"
  echo "trust policy updated"
else
  aws iam create-role --role-name "$GUARD_ROLE" --path "$ROLE_PATH" --max-session-duration 3600 \
    --description "GitHub Actions guardrails (bootstrap-budget-stop.sh) for stickleprojects/aws-build-runner (approved runs only)" \
    --assume-role-policy-document "file://$work/guardrails-trust.json" --query Role.Arn --output text | sed 's/^/created /'
fi
aws iam put-role-policy --role-name "$GUARD_ROLE" --policy-name guardrails \
  --policy-document "file://$work/guardrails-policy.json"
echo "permissions policy set"

echo
echo "Done. Role ARNs, for the GitHub environment secrets AWS_APPLY_ROLE_ARN, AWS_GUARDRAILS_CHECK_ROLE_ARN"
echo "and AWS_GUARDRAILS_ROLE_ARN:"
echo "arn:aws:iam::${ACCOUNT_ID}:role${ROLE_PATH}${ROLE_NAME}"
echo "arn:aws:iam::${ACCOUNT_ID}:role${ROLE_PATH}${CHECK_ROLE}"
echo "arn:aws:iam::${ACCOUNT_ID}:role${ROLE_PATH}${GUARD_ROLE}"
