#!/usr/bin/env bash
# Godmode, once (and again when the list of roles changes): the automatic stop at the budget.
# Run in CloudShell as admin:
#
#   export PATH=$HOME/bin:$PATH; cd ~/aws-build-runner/part1-godmode && git pull && ./bootstrap-budget-stop.sh
#
# Sets up, idempotently:
#   - the guardrail-monthly budget counts GROSS cost: every service in the account, with credits and
#     refunds NOT offsetting it (so free-plan credits can't hide real usage)
#   - policy guardrails/budget-stop (bootstrap/budget-stop-policy.json): denies starting or creating anything
#     billable (instances, spot/fleet, volumes, snapshots, images, IPs, NAT, endpoints, functions, buckets,
#     stacks, passing roles)
#   - role guardrails/budget-action-runner: only AWS Budgets can assume it; it can only attach/detach
#     budget-stop, and only on the targets below
#   - the budget action: at 100% of ACTUAL spend, automatically attach budget-stop to Claude's IAM user and
#     to the roles in policy.yaml account_settings.budget_stop.roles that exist
# Billing data lags (up to ~a day), so it stops new spending within about a day, not to the cent.
# It doesn't stop running instances (runners end within 2 hours anyway). Undo: Billing → Budgets →
# guardrail-monthly → Actions → reverse, once you've decided.
set -euo pipefail
cd "$(dirname "$0")"

ACCOUNT_ID="$(python3 -I config.py AWS_ACCOUNT_ID)"
USER_NAME="$(python3 -I config.py AWS_CLAUDE_USER)"
EMAIL="$(python3 -I config.py AWS_ALERT_EMAIL)"
yaml_get() { python3 -I -c "import yaml, json; print(json.dumps(yaml.safe_load(open('policy.yaml'))$1))"; }
BUDGET=guardrail-monthly
PERCENT="$(yaml_get "['account_settings']['budget_stop']['at_percent']")"
POLICY_ARN="arn:aws:iam::${ACCOUNT_ID}:policy/guardrails/budget-stop"
RUNNER_ROLE="budget-action-runner"

caller_arn="$(aws sts get-caller-identity --query Arn --output text)"
[[ "$(aws sts get-caller-identity --query Account --output text)" == "$ACCOUNT_ID" ]] \
  || { echo "Refusing: credentials are for another account than AWS_ACCOUNT_ID." >&2; exit 1; }
[[ "$caller_arn" != *":user/${USER_NAME}" ]] || { echo "Refusing: run this as admin, not as Claude's user." >&2; exit 1; }
echo "Account ${ACCOUNT_ID} as ${caller_arn}"

# Targets: Claude's user, and the listed /claude/ roles that exist (the budget action rejects missing ones).
roles=()
while read -r role; do
  [[ -z "$role" ]] && continue
  if aws iam get-role --role-name "$role" >/dev/null 2>&1; then roles+=("$role"); else echo "role ${role} doesn't exist yet; skipped (rerun this when it does)"; fi
done < <(yaml_get "['account_settings']['budget_stop']['roles']" | jq -r '.[]')
echo "targets: user ${USER_NAME}; roles: ${roles[*]:-(none)}"

echo "== budget ${BUDGET}: gross cost (credits and refunds don't offset it)"
budget="$(aws budgets describe-budget --account-id "$ACCOUNT_ID" --budget-name "$BUDGET" --query Budget --output json)"
budget="$(jq '.CostTypes.IncludeCredit = false | .CostTypes.IncludeRefund = false | del(.CalculatedSpend, .LastUpdatedTime)' <<<"$budget")"
aws budgets update-budget --account-id "$ACCOUNT_ID" --new-budget "$budget"
aws budgets describe-budget --account-id "$ACCOUNT_ID" --budget-name "$BUDGET" \
  --query 'Budget.{limit: BudgetLimit.Amount, filters: CostFilters, credits: CostTypes.IncludeCredit, refunds: CostTypes.IncludeRefund}' --output json

echo "== policy guardrails/budget-stop"
if aws iam get-policy --policy-arn "$POLICY_ARN" >/dev/null 2>&1; then
  current="$(aws iam get-policy-version --policy-arn "$POLICY_ARN" \
    --version-id "$(aws iam get-policy --policy-arn "$POLICY_ARN" --query Policy.DefaultVersionId --output text)" \
    --query PolicyVersion.Document --output json | jq -S -c .)"
  if [[ "$current" == "$(jq -S -c . bootstrap/budget-stop-policy.json)" ]]; then
    echo "unchanged"
  else
    old="$(aws iam list-policy-versions --policy-arn "$POLICY_ARN" \
      --query 'Versions[?!IsDefaultVersion] | sort_by(@, &CreateDate)[0].VersionId' --output text)"
    n="$(aws iam list-policy-versions --policy-arn "$POLICY_ARN" --query 'length(Versions)' --output text)"
    if (( n >= 5 )); then aws iam delete-policy-version --policy-arn "$POLICY_ARN" --version-id "$old"; fi
    aws iam create-policy-version --policy-arn "$POLICY_ARN" --policy-document file://bootstrap/budget-stop-policy.json \
      --set-as-default --query PolicyVersion.VersionId --output text | sed 's/^/updated -> /'
  fi
else
  aws iam create-policy --policy-name budget-stop --path /guardrails/ --policy-document file://bootstrap/budget-stop-policy.json \
    --description "Attached automatically when guardrail-monthly reaches its limit" --query Policy.Arn --output text | sed 's/^/created /'
fi

echo "== role guardrails/${RUNNER_ROLE} (AWS Budgets applies the stop with it)"
trust="$(jq -n --arg acct "$ACCOUNT_ID" '{Version: "2012-10-17", Statement: [{Effect: "Allow",
  Principal: {Service: "budgets.amazonaws.com"}, Action: "sts:AssumeRole",
  Condition: {StringEquals: {"aws:SourceAccount": $acct}}}]}')"
if aws iam get-role --role-name "$RUNNER_ROLE" >/dev/null 2>&1; then
  aws iam update-assume-role-policy --role-name "$RUNNER_ROLE" --policy-document "$trust"
else
  aws iam create-role --role-name "$RUNNER_ROLE" --path /guardrails/ --assume-role-policy-document "$trust" \
    --description "AWS Budgets: attach/detach guardrails/budget-stop on Claude's user and roles" \
    --query Role.Arn --output text | sed 's/^/created /'
fi
targets="$(jq -n --arg acct "$ACCOUNT_ID" --arg user "$USER_NAME" '$ARGS.positional as $roles |
  ["arn:aws:iam::\($acct):user/\($user)"] + ($roles | map("arn:aws:iam::\($acct):role/claude/\(.)"))' --args "${roles[@]}")"
runner_policy="$(jq -n --argjson targets "$targets" --arg policy "$POLICY_ARN" '{Version: "2012-10-17", Statement: [{
  Sid: "AttachDetachBudgetStopOnTargetsOnly", Effect: "Allow",
  Action: ["iam:AttachUserPolicy", "iam:DetachUserPolicy", "iam:AttachRolePolicy", "iam:DetachRolePolicy"],
  Resource: $targets, Condition: {ArnEquals: {"iam:PolicyARN": $policy}}}]}')"
aws iam put-role-policy --role-name "$RUNNER_ROLE" --policy-name budget-stop-only --policy-document "$runner_policy"
runner_arn="arn:aws:iam::${ACCOUNT_ID}:role/guardrails/${RUNNER_ROLE}"

echo "== budget action: at ${PERCENT}% of actual spend, attach budget-stop automatically"
definition="$(jq -n --arg policy "$POLICY_ARN" --arg user "$USER_NAME" '$ARGS.positional as $roles |
  {IamActionDefinition: ({PolicyArn: $policy, Users: [$user]} + (if ($roles | length) > 0 then {Roles: $roles} else {} end))}' \
  --args "${roles[@]}")"
existing="$(aws budgets describe-budget-actions-for-budget --account-id "$ACCOUNT_ID" --budget-name "$BUDGET" \
  --query "Actions[?ActionType=='APPLY_IAM_POLICY'].ActionId | [0]" --output text)"
if [[ -n "$existing" && "$existing" != None ]]; then
  aws budgets update-budget-action --account-id "$ACCOUNT_ID" --budget-name "$BUDGET" --action-id "$existing" \
    --notification-type ACTUAL --action-threshold "ActionThresholdValue=${PERCENT},ActionThresholdType=PERCENTAGE" \
    --definition "$definition" --execution-role-arn "$runner_arn" --approval-model AUTOMATIC \
    --subscribers "SubscriptionType=EMAIL,Address=${EMAIL}" >/dev/null
  echo "updated action ${existing}"
else
  # IAM can take a few seconds before AWS Budgets may use a new role.
  for attempt in 1 2 3 4 5 6; do
    if aws budgets create-budget-action --account-id "$ACCOUNT_ID" --budget-name "$BUDGET" \
      --notification-type ACTUAL --action-type APPLY_IAM_POLICY \
      --action-threshold "ActionThresholdValue=${PERCENT},ActionThresholdType=PERCENTAGE" \
      --definition "$definition" --execution-role-arn "$runner_arn" --approval-model AUTOMATIC \
      --subscribers "SubscriptionType=EMAIL,Address=${EMAIL}" --query ActionId --output text; then break; fi
    (( attempt < 6 )) || exit 1
    echo "retrying in 10 s (IAM propagation)"; sleep 10
  done
fi
aws budgets describe-budget-actions-for-budget --account-id "$ACCOUNT_ID" --budget-name "$BUDGET" \
  --query 'Actions[].{id: ActionId, status: Status, at: ActionThreshold.ActionThresholdValue, approval: ApprovalModel, users: Definition.IamActionDefinition.Users, roles: Definition.IamActionDefinition.Roles}' --output json
echo "Done."
