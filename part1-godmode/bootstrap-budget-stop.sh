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
#   - the kill switch, in Sydney (the organization blocks Lambda and SNS elsewhere): Lambda
#     guardrail-kill-switch (bootstrap/kill_switch.py) with role guardrails/budget-kill-switch, triggered by
#     the budget's 100% ACTUAL notification through SNS topic guardrail-budget-stop. It terminates every
#     instance, cancels spot requests and deletes the runner AMIs. Claude's policy denies it any access to
#     guardrail-* functions. Ends with a DRY RUN: what it would stop right now, without stopping anything.
# Billing data lags (up to ~a day), which is why the budget is US$25 for a £30 ceiling.
# Undo the block on new things: Billing → Budgets → guardrail-monthly → Actions → reverse, once you've decided.
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

# ---------- the kill switch (Sydney) ----------
REGION="ap-southeast-2"
FUNCTION="guardrail-kill-switch"
TOPIC="guardrail-budget-stop"
KS_ROLE="budget-kill-switch"
KS_TERMINATE="$(yaml_get "['account_settings']['budget_stop']['kill_switch']['terminate_instances']")"
KS_AMIS="$(yaml_get "['account_settings']['budget_stop']['kill_switch']['delete_runner_amis']")"
# Instances can only exist where the organization allows EC2: Sydney, and us-east-1 (seen 2026-11-16).
KS_REGIONS="ap-southeast-2,us-east-1"

echo "== kill switch role guardrails/${KS_ROLE}"
ks_trust='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
if ! aws iam get-role --role-name "$KS_ROLE" >/dev/null 2>&1; then
  aws iam create-role --role-name "$KS_ROLE" --path /guardrails/ --assume-role-policy-document "$ks_trust" \
    --description "guardrail-kill-switch: stop what's spending when the budget is reached" --query Role.Arn --output text | sed 's/^/created /'
  sleep 10   # IAM propagation, before Lambda may use it
fi
ks_policy="$(jq -n --arg acct "$ACCOUNT_ID" --arg region "$REGION" --arg fn "$FUNCTION" '{Version: "2012-10-17", Statement: [
  {Sid: "StopInstancesAndSpot", Effect: "Allow", Resource: "*",
   Action: ["ec2:DescribeInstances", "ec2:TerminateInstances", "ec2:DescribeSpotInstanceRequests", "ec2:CancelSpotInstanceRequests"]},
  {Sid: "DeleteRunnerAmis", Effect: "Allow", Resource: "*",
   Action: ["ec2:DescribeImages", "ec2:DeregisterImage", "ec2:DeleteSnapshot"]},
  {Sid: "OwnLogs", Effect: "Allow", Action: ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
   Resource: "arn:aws:logs:\($region):\($acct):log-group:/aws/lambda/\($fn):*"}]}')"
aws iam put-role-policy --role-name "$KS_ROLE" --policy-name kill-switch --policy-document "$ks_policy"

echo "== Lambda ${FUNCTION}"
zipdir="$(mktemp -d)"
python3 -I -c 'import sys, zipfile; z = zipfile.ZipFile(sys.argv[1], "w"); z.write(sys.argv[2], "kill_switch.py"); z.close()' \
  "$zipdir/kill_switch.zip" bootstrap/kill_switch.py
env_json="$(jq -n --arg r "$KS_REGIONS" --arg t "$KS_TERMINATE" --arg a "$KS_AMIS" \
  '{Variables: {REGIONS: $r, TERMINATE_INSTANCES: $t, DELETE_RUNNER_AMIS: $a}}')"
if aws lambda get-function --region "$REGION" --function-name "$FUNCTION" >/dev/null 2>&1; then
  aws lambda update-function-code --region "$REGION" --function-name "$FUNCTION" --zip-file "fileb://$zipdir/kill_switch.zip" >/dev/null
  aws lambda wait function-updated --region "$REGION" --function-name "$FUNCTION"
  aws lambda update-function-configuration --region "$REGION" --function-name "$FUNCTION" --environment "$env_json" >/dev/null
  echo "updated"
else
  for attempt in 1 2 3 4 5 6; do
    if aws lambda create-function --region "$REGION" --function-name "$FUNCTION" --runtime python3.12 \
      --handler kill_switch.handler --role "arn:aws:iam::${ACCOUNT_ID}:role/guardrails/${KS_ROLE}" \
      --zip-file "fileb://$zipdir/kill_switch.zip" --timeout 300 --environment "$env_json" \
      --description "Stops what's spending when guardrail-monthly hits its limit (bootstrap-budget-stop.sh)" \
      --query FunctionArn --output text; then break; fi
    (( attempt < 6 )) || exit 1
    echo "retrying in 10 s (IAM propagation)"; sleep 10
  done
fi
rm -r "$zipdir"
aws lambda wait function-active-v2 --region "$REGION" --function-name "$FUNCTION"
aws logs create-log-group --region "$REGION" --log-group-name "/aws/lambda/${FUNCTION}" 2>/dev/null || true
aws logs put-retention-policy --region "$REGION" --log-group-name "/aws/lambda/${FUNCTION}" --retention-in-days 90

echo "== SNS topic ${TOPIC}: the budget's 100% ACTUAL notification → the kill switch"
topic_arn="$(aws sns create-topic --region "$REGION" --name "$TOPIC" --query TopicArn --output text)"
topic_policy="$(jq -n --arg topic "$topic_arn" --arg acct "$ACCOUNT_ID" '{Version: "2012-10-17", Statement: [{
  Sid: "BudgetsMayPublish", Effect: "Allow", Principal: {Service: "budgets.amazonaws.com"}, Action: "SNS:Publish",
  Resource: $topic, Condition: {StringEquals: {"aws:SourceAccount": $acct}}}]}')"
aws sns set-topic-attributes --region "$REGION" --topic-arn "$topic_arn" --attribute-name Policy --attribute-value "$topic_policy"
fn_arn="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${FUNCTION}"
aws lambda add-permission --region "$REGION" --function-name "$FUNCTION" --statement-id budget-sns \
  --action lambda:InvokeFunction --principal sns.amazonaws.com --source-arn "$topic_arn" >/dev/null 2>&1 || true
aws sns subscribe --region "$REGION" --topic-arn "$topic_arn" --protocol lambda --notification-endpoint "$fn_arn" \
  --query SubscriptionArn --output text | sed 's/^/subscribed: /'
notification="{\"NotificationType\":\"ACTUAL\",\"ComparisonOperator\":\"GREATER_THAN\",\"Threshold\":${PERCENT},\"ThresholdType\":\"PERCENTAGE\"}"
if ! aws budgets describe-notifications-for-budget --account-id "$ACCOUNT_ID" --budget-name "$BUDGET" \
     --query "Notifications[?NotificationType=='ACTUAL' && Threshold==\`${PERCENT}\`]" --output text | grep -q ACTUAL; then
  aws budgets create-notification --account-id "$ACCOUNT_ID" --budget-name "$BUDGET" --notification "$notification" \
    --subscribers "SubscriptionType=EMAIL,Address=${EMAIL}"
fi
if aws budgets describe-subscribers-for-notification --account-id "$ACCOUNT_ID" --budget-name "$BUDGET" \
     --notification "$notification" --query "Subscribers[?SubscriptionType=='SNS'].Address" --output text | grep -q "$topic_arn"; then
  echo "the budget already notifies the kill switch"
else
  aws budgets create-subscriber --account-id "$ACCOUNT_ID" --budget-name "$BUDGET" --notification "$notification" \
    --subscriber "SubscriptionType=SNS,Address=${topic_arn}"
  echo "the budget now notifies the kill switch at ${PERCENT}% of actual spend"
fi

echo "== DRY RUN: what the kill switch would stop right now (nothing is stopped)"
out="$(mktemp)"
aws lambda invoke --region "$REGION" --function-name "$FUNCTION" --cli-binary-format raw-in-base64-out \
  --payload '{"dry_run": true}' "$out" >/dev/null
jq . "$out"; rm -f "$out"
echo "Done."
