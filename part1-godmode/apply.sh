#!/usr/bin/env bash
# Godmode: applies the CI-checked policy artifact for main's head commit to AWS, and records
# that it was applied. Run as an ADMIN, never as Claude's IAM user. Needs aws, gh, git, jq, python3+pyyaml.
#
#   ./apply.sh ALERT_EMAIL
#
# Lifecycle of a policy:
#   1. generated + checked   CI on main: build, lint, offline checks -> artifact "policies-<commit>"
#   2. not yet applied       a commit with that artifact but no successful GitHub deployment to "aws"
#   3. checked again here    3a artifact integrity and artifact == this checkout, 3b Access Analyzer,
#                            3c IAM simulator on the drafts. Nothing in AWS changes before these pass.
#   4. applied               4a policies, 4b account settings, 4c simulator on the live result
#   5. recorded              IAM tags on each policy (applied-commit/-at/-by), 5a drift check against
#                            them, then a GitHub deployment "success" saying who applied it and when
set -Eeuo pipefail   # -E: the ERR trap also fires inside functions
cd "$(dirname "$0")"

ALERT_EMAIL="${1:?usage: apply.sh ALERT_EMAIL}"
yaml_get() { python3 -I -c "import yaml; print(yaml.safe_load(open('policy.yaml'))$1)"; }
# Account id and user name live outside the repo (env, .env.local or gh variable): see config.py.
ACCOUNT_ID="$(python3 -I config.py AWS_ACCOUNT_ID)"
USER_NAME="$(python3 -I config.py AWS_CLAUDE_USER)"
REGION="$(yaml_get "['region']")"
BOUNDARY_PATH_NAME="$(yaml_get "['iam']['boundary']")"      # e.g. guardrails/claude-boundary
BUDGET_USD="$(yaml_get "['account_settings']['budget_usd']")"
BUDGET_NAME=guardrail-monthly
ENVIRONMENT=aws

# ---------- 0. who and where ----------
caller_arn="$(aws sts get-caller-identity --query Arn --output text)"
caller_account="$(aws sts get-caller-identity --query Account --output text)"
[[ "$caller_account" == "$ACCOUNT_ID" ]] || { echo "Refusing: credentials are for ${caller_account}, policy.yaml says ${ACCOUNT_ID}." >&2; exit 1; }
[[ "$caller_arn" != *":user/${USER_NAME}" ]] || { echo "Refusing: these are ${USER_NAME}'s own credentials. Use admin credentials." >&2; exit 1; }
gh_user="$(gh api user --jq .login)"
REPO="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"
echo "Account ${ACCOUNT_ID} as ${caller_arn}; GitHub ${gh_user} on ${REPO}"

# ---------- 1-2. the commit to apply: main's head, clean, with a checked artifact ----------
[[ -z "$(git status --porcelain -- .)" ]] || { echo "Refusing: part1-godmode has uncommitted changes." >&2; exit 1; }
git fetch --quiet origin main
commit="$(git rev-parse HEAD)"
[[ "$commit" == "$(git rev-parse origin/main)" ]] || { echo "Refusing: HEAD is not origin/main. Check out and pull main first." >&2; exit 1; }
echo "Commit to apply: ${commit}"

run_id="$(gh run list --repo "$REPO" --workflow policy.yml --commit "$commit" --status success \
  --json databaseId --jq '.[0].databaseId // empty')"
[[ -n "$run_id" ]] || { echo "Refusing: no successful 'policy' CI run for ${commit}. Wait for CI, or re-run it (workflow_dispatch on main)." >&2; exit 1; }

work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
gh run download "$run_id" --repo "$REPO" --name "policies-${commit}" --dir "$work/artifact" \
  || { echo "Refusing: artifact policies-${commit} not found (expired after 90 days?). Re-run CI on main." >&2; exit 1; }
art="$work/artifact"

echo "== 3a. check the artifact"
(cd "$art" && sha256sum --quiet -c SHA256SUMS) || { echo "Refusing: artifact checksums don't match." >&2; exit 1; }
[[ "$(jq -r .commit "$art/manifest.json")" == "$commit" ]] || { echo "Refusing: artifact is for another commit." >&2; exit 1; }
[[ "$(jq -r .pending_approval "$art/manifest.json")" == "false" ]] || { echo "Refusing: artifact has items pending approval." >&2; exit 1; }
diff -r "$art/out" out >/dev/null && cmp -s "$art/POLICY.md" POLICY.md && cmp -s "$art/policy.yaml" policy.yaml \
  || { echo "Refusing: artifact differs from this checkout." >&2; exit 1; }
python3 -I build-policies.py >/dev/null       # strict: fails on anything pending
git diff --quiet -- out POLICY.md || { echo "Refusing: rebuilding here gives different files from the artifact." >&2; exit 1; }
echo "artifact from run ${run_id}: checksums ok, same commit, nothing pending, identical to checkout"
jq -r .offline_checks "$art/manifest.json" | sed 's/^/CI: /'

# The artifact holds templates ({{ACCOUNT_ID}}); fill in this account for validating and applying.
mkdir -p "$work/rendered"
for f in "$art"/out/*.json; do
  python3 -I config.py --render "$f" > "$work/rendered/${f##*/}"
done
pol="$work/rendered"

echo "== 3b. validate with IAM Access Analyzer"
blocking=0
for f in "$pol"/*.json; do
  findings="$(aws accessanalyzer validate-policy --region "$REGION" --policy-type IDENTITY_POLICY \
    --policy-document "file://$f" --query 'findings[].[findingType,issueCode,findingDetails]' --output text)"
  if [[ -n "$findings" ]]; then
    echo "-- ${f##*/}"; echo "$findings"
    grep -qE '^(ERROR|SECURITY_WARNING)' <<<"$findings" && blocking=1
  fi
done
(( blocking )) && { echo "Blocking findings; nothing was changed." >&2; exit 1; }

echo "== 3c. simulate the artifact before applying"
python3 -I verify.py --local >"$work/verify-local.txt" || { tail -1 "$work/verify-local.txt"; grep '^FAIL' "$work/verify-local.txt"; echo "Nothing was changed." >&2; exit 1; }
tail -1 "$work/verify-local.txt"

# ---------- 4. apply (recorded as a GitHub deployment from here on) ----------
deployment_id="$(jq -n --arg ref "$commit" --arg env "$ENVIRONMENT" --arg by "$caller_arn" \
  '{ref:$ref, environment:$env, auto_merge:false, required_contexts:[], description:("IAM policies, applied by " + $by)}' \
  | gh api "repos/${REPO}/deployments" --input - --jq .id)"
deployment_status() {
  gh api "repos/${REPO}/deployments/${deployment_id}/statuses" -f state="$1" -f description="$2" \
    -f environment="$ENVIRONMENT" >/dev/null || echo "WARNING: couldn't record deployment status $1"
}
deployment_status in_progress "Applying, by ${gh_user} (${caller_arn})"
trap 'deployment_status failure "apply.sh failed; AWS may be partly updated. Re-run apply.sh."; rm -rf "$work"' ERR

# upsert_policy NAME PATH FILE: create, or add a version and make it the default
# (IAM keeps at most 5 versions, so the oldest non-default one goes first).
upsert_policy() {
  local name="$1" path="$2" file="$3" arn="arn:aws:iam::${ACCOUNT_ID}:policy${2}${1}"
  if aws iam get-policy --policy-arn "$arn" >/dev/null 2>&1; then
    local old
    read -r -a old <<<"$(aws iam list-policy-versions --policy-arn "$arn" \
      --query 'Versions[?!IsDefaultVersion] | sort_by(@, &CreateDate)[].VersionId' --output text)"
    if (( ${#old[@]} >= 4 )); then aws iam delete-policy-version --policy-arn "$arn" --version-id "${old[0]}"; fi
    aws iam create-policy-version --policy-arn "$arn" --policy-document "file://$file" --set-as-default \
      --query PolicyVersion.VersionId --output text | sed "s|^|updated ${arn} -> |"
  else
    aws iam create-policy --policy-name "$name" --path "$path" --policy-document "file://$file" \
      --description "Generated from ${REPO} part1-godmode/policy.yaml" \
      --query Policy.Arn --output text | sed 's|^|created |'
  fi
}

echo "== 4a. policies"
boundary_name="${BOUNDARY_PATH_NAME##*/}"
boundary_dir="${BOUNDARY_PATH_NAME%/*}"
upsert_policy "$boundary_name" "/${boundary_dir}/" "$pol/claude-boundary.json"
upsert_policy claude-assistant-policy / "$pol/claude-assistant-policy.json"
upsert_policy claude-assistant-iam / "$pol/claude-assistant-iam.json"

want=("arn:aws:iam::${ACCOUNT_ID}:policy/claude-assistant-policy" "arn:aws:iam::${ACCOUNT_ID}:policy/claude-assistant-iam")
for arn in "${want[@]}"; do aws iam attach-user-policy --user-name "$USER_NAME" --policy-arn "$arn"; done
for arn in $(aws iam list-attached-user-policies --user-name "$USER_NAME" --query 'AttachedPolicies[].PolicyArn' --output text); do
  if [[ " ${want[*]} " != *" $arn "* ]]; then
    aws iam detach-user-policy --user-name "$USER_NAME" --policy-arn "$arn"; echo "detached $arn (not in policy.yaml)"
  fi
done
inline="$(aws iam list-user-policies --user-name "$USER_NAME" --query PolicyNames --output text)"
[[ -z "$inline" ]] || echo "WARNING: ${USER_NAME} has inline policies not managed here: ${inline}. Remove them by hand."
groups="$(aws iam list-groups-for-user --user-name "$USER_NAME" --query 'Groups[].GroupName' --output text)"
[[ -z "$groups" ]] || echo "WARNING: ${USER_NAME} is in groups that may grant more: ${groups}"

echo "== 4b. account settings"
budget_json="$(jq -n --arg n "$BUDGET_NAME" --arg a "$BUDGET_USD" \
  '{BudgetName:$n, BudgetType:"COST", TimeUnit:"MONTHLY", BudgetLimit:{Amount:$a, Unit:"USD"}}')"
if aws budgets describe-budget --account-id "$ACCOUNT_ID" --budget-name "$BUDGET_NAME" >/dev/null 2>&1; then
  aws budgets update-budget --account-id "$ACCOUNT_ID" --new-budget "$budget_json"
  echo "budget ${BUDGET_NAME}: limit US\$${BUDGET_USD} (existing alert recipients kept)"
else
  notifications="$(jq -n --arg e "$ALERT_EMAIL" '
    [ {t:"ACTUAL",p:50}, {t:"ACTUAL",p:80}, {t:"ACTUAL",p:100}, {t:"FORECASTED",p:100} ]
    | map({Notification:{NotificationType:.t, ComparisonOperator:"GREATER_THAN", Threshold:.p, ThresholdType:"PERCENTAGE"},
           Subscribers:[{SubscriptionType:"EMAIL", Address:$e}]})')"
  aws budgets create-budget --account-id "$ACCOUNT_ID" --budget "$budget_json" --notifications-with-subscribers "$notifications"
  echo "budget ${BUDGET_NAME}: created, US\$${BUDGET_USD}/month, alerts to ${ALERT_EMAIL}"
fi
aws s3control put-public-access-block --account-id "$ACCOUNT_ID" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
echo "S3 public access: blocked for the whole account"
aws ec2 enable-image-block-public-access --region "$REGION" --image-block-public-access-state block-new-sharing >/dev/null
echo "AMI public sharing: blocked (${REGION})"
aws ec2 enable-snapshot-block-public-access --region "$REGION" --state block-all-sharing >/dev/null
echo "EBS snapshot public sharing: blocked (${REGION})"
aws ec2 enable-ebs-encryption-by-default --region "$REGION" >/dev/null
echo "EBS encryption by default: on (${REGION})"

echo "== 4c. simulate the live policies"
python3 -I verify.py >"$work/verify-live.txt" || { tail -1 "$work/verify-live.txt"; grep '^FAIL' "$work/verify-live.txt"; false; }
tail -1 "$work/verify-live.txt"

# ---------- 5. record ----------
applied_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
applied_by="${gh_user} as ${caller_arn}"
applied_by="${applied_by:0:256}"
for arn in "${want[@]}" "arn:aws:iam::${ACCOUNT_ID}:policy/${BOUNDARY_PATH_NAME}"; do
  aws iam tag-policy --policy-arn "$arn" --tags \
    Key=applied-commit,Value="$commit" Key=applied-at,Value="$applied_at" Key=applied-by,Value="$applied_by"
done

echo "== 5a. drift check"
python3 -I check-live.py --expect-commit "$commit"

trap 'rm -rf "$work"' ERR
deployment_status success "Applied ${applied_at} by ${applied_by}"
echo
echo "Applied ${commit:0:12} at ${applied_at} by ${applied_by}."
echo "Recorded: IAM tags on the three policies, and GitHub deployment ${deployment_id} (${REPO}, environment '${ENVIRONMENT}')."
