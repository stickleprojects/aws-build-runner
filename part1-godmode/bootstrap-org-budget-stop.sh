#!/usr/bin/env bash
# Godmode, in the MANAGEMENT account (CloudShell), once and whenever it needs re-checking: the
# organization-wide budget stop, so the whole consolidated bill is capped. Idempotent; ends with a check.
#
#   export PATH=$HOME/bin:$PATH; cd ~/aws-build-runner/part1-godmode && git pull && ./bootstrap-org-budget-stop.sh
#
# Sets up (creating or correcting each):
#   - SCP org-budget-stop: bootstrap/budget-stop-policy.json (nothing new and billable)
#   - OU spend-capped under Root (the budget action's picker lists OUs, not Root). Member accounts
#     outside it are LISTED, not moved: moving accounts is your decision (Organizations → AWS accounts)
#   - role guardrails/org-budget-action-runner: only AWS Budgets can assume it; it can only attach/detach
#     org-budget-stop on spend-capped (not AWS's managed spend-limit role, whose permissions AWS controls
#     and which can also close accounts)
#   - budget org-guardrail-monthly: account_settings.budget_usd, monthly, all accounts, refunds excluded
#   - alerts: actual 50/80/100 %, forecast 100 %, to AWS_ALERT_EMAIL
#   - action: at 100 % actual, apply org-budget-stop to spend-capped, automatically
set -euo pipefail
cd "$(dirname "$0")"

BUDGET=org-guardrail-monthly
POLICY_NAME=org-budget-stop
OU_NAME=spend-capped
ROLE="org-budget-action-runner"
AMOUNT="$(python3 -I -c "import yaml; print(yaml.safe_load(open('policy.yaml'))['account_settings']['budget_usd'])")"

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
read -r ORG_ID MASTER <<<"$(aws organizations describe-organization --query 'Organization.[Id,MasterAccountId]' --output text)"
[[ "$ACCOUNT" == "$MASTER" ]] || { echo "Refusing: run this in the organization's management account (this is ${ACCOUNT})." >&2; exit 1; }
echo "Management account ${ACCOUNT}, organization ${ORG_ID}"

# The alert email: AWS_ALERT_EMAIL if set; else the address the existing org budget already alerts
# (so no email has to be typed or kept in this public repo); else config.py (env, .env.local, gh variable).
EMAIL="${AWS_ALERT_EMAIL:-}"
if [[ -z "$EMAIL" ]]; then
  # One line of JSON (jq -c): the CLI's JSON is multi-line, and reading just its first line ("{") broke
  # this on 2026-11-16.
  note="$(aws budgets describe-notifications-for-budget --account-id "$ACCOUNT" --budget-name "$BUDGET" \
    --query 'Notifications[0]' --output json 2>/dev/null | jq -c . 2>/dev/null || true)"
  if [[ -n "$note" && "$note" != null ]]; then
    EMAIL="$(aws budgets describe-subscribers-for-notification --account-id "$ACCOUNT" --budget-name "$BUDGET" \
      --notification "$note" --query "Subscribers[?SubscriptionType=='EMAIL'].Address | [0]" --output text 2>/dev/null || true)"
  fi
  [[ "$EMAIL" == None ]] && EMAIL=""
fi
[[ -n "$EMAIL" ]] || EMAIL="$(python3 -I config.py AWS_ALERT_EMAIL 2>/dev/null || true)"
[[ -n "$EMAIL" ]] || { echo "No alert email: set AWS_ALERT_EMAIL (the org budget doesn't exist yet)." >&2; exit 1; }
echo "alerts go to ${EMAIL%%@*}@…"

echo "== SCP ${POLICY_NAME}"
policy_id="$(aws organizations list-policies --filter SERVICE_CONTROL_POLICY \
  --query "Policies[?Name=='${POLICY_NAME}'].Id | [0]" --output text)"
if [[ -z "$policy_id" || "$policy_id" == None ]]; then
  policy_id="$(aws organizations create-policy --type SERVICE_CONTROL_POLICY --name "$POLICY_NAME" \
    --description "Attached automatically when the org budget is reached: nothing new and billable" \
    --content file://bootstrap/budget-stop-policy.json --query Policy.PolicySummary.Id --output text)"
  echo "created ${policy_id}"
else
  current="$(aws organizations describe-policy --policy-id "$policy_id" --query Policy.Content --output text | jq -S -c .)"
  if [[ "$current" != "$(jq -S -c . bootstrap/budget-stop-policy.json)" ]]; then
    aws organizations update-policy --policy-id "$policy_id" --content file://bootstrap/budget-stop-policy.json >/dev/null
    echo "updated ${policy_id} to match bootstrap/budget-stop-policy.json"
  else
    echo "${policy_id} matches bootstrap/budget-stop-policy.json"
  fi
fi

echo "== OU ${OU_NAME}"
root="$(aws organizations list-roots --query 'Roots[0].Id' --output text)"
ou="$(aws organizations list-organizational-units-for-parent --parent-id "$root" \
  --query "OrganizationalUnits[?Name=='${OU_NAME}'].Id | [0]" --output text)"
if [[ -z "$ou" || "$ou" == None ]]; then
  ou="$(aws organizations create-organizational-unit --parent-id "$root" --name "$OU_NAME" \
    --query OrganizationalUnit.Id --output text)"
  echo "created ${ou}"
else
  echo "${ou}"
fi
# --output text separates with tabs; the membership test below matches on spaces, so convert
# (2026-11-16: without it every account was reported as outside the OU).
in_ou="$(aws organizations list-accounts-for-parent --parent-id "$ou" --query 'Accounts[].Id' --output text | tr '\t' ' ')"
outside=0
while read -r id name; do
  [[ -z "$id" || "$id" == "$MASTER" ]] && continue
  if [[ " ${in_ou} " != *" ${id} "* ]]; then
    echo "WARNING: member account '${name}' (${id}) is NOT in ${OU_NAME}, so the org stop won't cover it."
    echo "         Move it: Organizations → AWS accounts → tick it → Actions → Move → ${OU_NAME}"
    outside=$((outside + 1))
  fi
done < <(aws organizations list-accounts --query "Accounts[?Status=='ACTIVE'].[Id,Name]" --output text)
(( outside == 0 )) && echo "every member account is in ${OU_NAME}"

echo "== role guardrails/${ROLE}"
trust="$(jq -n --arg a "$ACCOUNT" '{Version: "2012-10-17", Statement: [{Effect: "Allow",
  Principal: {Service: "budgets.amazonaws.com"}, Action: "sts:AssumeRole",
  Condition: {StringEquals: {"aws:SourceAccount": $a}}}]}')"
if aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  aws iam update-assume-role-policy --role-name "$ROLE" --policy-document "$trust"
else
  aws iam create-role --role-name "$ROLE" --path /guardrails/ --assume-role-policy-document "$trust" \
    --description "AWS Budgets: attach/detach org-budget-stop on spend-capped only" --query Role.Arn --output text \
    | sed 's/^/created /'
fi
perms="$(jq -n --arg a "$ACCOUNT" --arg o "$ORG_ID" --arg p "$policy_id" --arg ou "$ou" '{Version: "2012-10-17", Statement: [
  {Sid: "AttachDetachOrgBudgetStopOnSpendCappedOnly", Effect: "Allow",
   Action: ["organizations:AttachPolicy", "organizations:DetachPolicy"],
   Resource: ["arn:aws:organizations::\($a):policy/\($o)/service_control_policy/\($p)",
              "arn:aws:organizations::\($a):ou/\($o)/\($ou)"]},
  {Sid: "Describe", Effect: "Allow", Action: ["organizations:DescribePolicy", "organizations:ListPolicies",
   "organizations:DescribeOrganization"], Resource: "*"}]}')"
aws iam put-role-policy --role-name "$ROLE" --policy-name org-budget-stop-only --policy-document "$perms"
role_arn="arn:aws:iam::${ACCOUNT}:role/guardrails/${ROLE}"

echo "== budget ${BUDGET}: US\$${AMOUNT}/month, all accounts, refunds excluded"
budget="$(jq -n --arg n "$BUDGET" --arg amt "$AMOUNT" '{BudgetName: $n, BudgetType: "COST", TimeUnit: "MONTHLY",
  BudgetLimit: {Amount: $amt, Unit: "USD"},
  FilterExpression: {Not: {Dimensions: {Key: "RECORD_TYPE", Values: ["Refund"]}}}}')"
if aws budgets describe-budget --account-id "$ACCOUNT" --budget-name "$BUDGET" >/dev/null 2>&1; then
  aws budgets update-budget --account-id "$ACCOUNT" --new-budget "$budget"
  echo "updated"
else
  aws budgets create-budget --account-id "$ACCOUNT" --budget "$budget"
  echo "created"
fi

echo "== alerts"
for spec in ACTUAL:50 ACTUAL:80 ACTUAL:100 FORECASTED:100; do
  type="${spec%%:*}"; pct="${spec#*:}"
  note="{\"NotificationType\":\"${type}\",\"ComparisonOperator\":\"GREATER_THAN\",\"Threshold\":${pct},\"ThresholdType\":\"PERCENTAGE\"}"
  if aws budgets describe-notifications-for-budget --account-id "$ACCOUNT" --budget-name "$BUDGET" \
       --query "Notifications[?NotificationType=='${type}' && Threshold==\`${pct}\`]" --output text | grep -q "$type"; then
    echo "${type} ${pct}%: present"
  else
    aws budgets create-notification --account-id "$ACCOUNT" --budget-name "$BUDGET" --notification "$note" \
      --subscribers "SubscriptionType=EMAIL,Address=${EMAIL}"
    echo "${type} ${pct}%: added"
  fi
done

echo "== action: at 100% actual, apply ${POLICY_NAME} to ${OU_NAME}, automatically"
definition="$(jq -n --arg p "$policy_id" --arg ou "$ou" '{ScpActionDefinition: {PolicyId: $p, TargetIds: [$ou]}}')"
action="$(aws budgets describe-budget-actions-for-budget --account-id "$ACCOUNT" --budget-name "$BUDGET" \
  --query "Actions[?ActionType=='APPLY_SCP_POLICY'].ActionId | [0]" --output text)"
if [[ -n "$action" && "$action" != None ]]; then
  aws budgets update-budget-action --account-id "$ACCOUNT" --budget-name "$BUDGET" --action-id "$action" \
    --notification-type ACTUAL --action-threshold "ActionThresholdValue=100,ActionThresholdType=PERCENTAGE" \
    --definition "$definition" --execution-role-arn "$role_arn" --approval-model AUTOMATIC \
    --subscribers "SubscriptionType=EMAIL,Address=${EMAIL}" >/dev/null
  echo "updated ${action} (role: ${ROLE})"
else
  for attempt in 1 2 3 4 5 6; do
    if aws budgets create-budget-action --account-id "$ACCOUNT" --budget-name "$BUDGET" \
      --notification-type ACTUAL --action-type APPLY_SCP_POLICY \
      --action-threshold "ActionThresholdValue=100,ActionThresholdType=PERCENTAGE" \
      --definition "$definition" --execution-role-arn "$role_arn" --approval-model AUTOMATIC \
      --subscribers "SubscriptionType=EMAIL,Address=${EMAIL}" --query ActionId --output text; then break; fi
    (( attempt < 6 )) || exit 1
    echo "retrying in 10 s (IAM propagation)"; sleep 10
  done
fi

echo "== check"
aws budgets describe-budget --account-id "$ACCOUNT" --budget-name "$BUDGET" \
  --query 'Budget.{limit: BudgetLimit.Amount, period: TimeUnit, filter: FilterExpression}' --output json
aws budgets describe-notifications-for-budget --account-id "$ACCOUNT" --budget-name "$BUDGET" \
  --query 'Notifications[].[NotificationType, Threshold]' --output text | sort
aws budgets describe-budget-actions-for-budget --account-id "$ACCOUNT" --budget-name "$BUDGET" \
  --query 'Actions[].{status: Status, type: ActionType, at: ActionThreshold.ActionThresholdValue, approval: ApprovalModel,
            policy: Definition.ScpActionDefinition.PolicyId, targets: Definition.ScpActionDefinition.TargetIds,
            role: ExecutionRoleArn}' --output json
echo "Done."
