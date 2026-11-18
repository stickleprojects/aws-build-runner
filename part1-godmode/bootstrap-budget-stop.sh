#!/usr/bin/env bash
# Godmode, once (and again when the list of roles changes): the automatic stop at the budget.
# Run in CloudShell as admin:
#
#   export PATH=$HOME/bin:$PATH; cd ~/aws-build-runner/part1-godmode && git pull && ./bootstrap-budget-stop.sh
#
# Sets up, idempotently:
#   - the guardrail-monthly budget counts what would be BILLED: every service in the account; credits
#     reduce it (AWS applies them on the invoice), refunds don't (they come later, if at all), tax counts
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
#     instance, cancels spot requests, throttles our Lambdas, deletes the runner AMIs, detached build disks,
#     unused Elastic IPs and interface endpoints (each per policy.yaml kill_switch). Claude's policy denies it
#     any access to guardrail-* functions.
#   - the fast stop (#30): CloudWatch alarms guardrail-fast-* (data out, Lambda calls, log ingestion; thresholds
#     in policy.yaml fast_stop) -> SNS topic guardrail-fast-stop -> the kill switch in "fast" mode (instances and
#     Lambdas only), and an email to Kieron (confirm the subscription the first time)
#   - Cost Anomaly Detection: a monitor per service and a daily email above policy.yaml anomaly_alert_usd
# Ends with a DRY RUN of both modes: what they would stop right now, without stopping anything.
# Billing data lags 8-36 hours, which is why the budget stop is at US$29 (≈£23) for a £30 ceiling, and why the fast stop
# doesn't use billing data at all.
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
# The limit comes from policy.yaml, every run: before 2026-11-17 this kept whatever the budget had (US$25
# stayed live after budget_usd went to 29).
AMOUNT="$(yaml_get "['account_settings']['budget_usd']")"

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

echo "== budget ${BUDGET}: US\$${AMOUNT} a month (policy.yaml budget_usd); credits reduce it, refunds don't, tax counts"
budget="$(aws budgets describe-budget --account-id "$ACCOUNT_ID" --budget-name "$BUDGET" --query Budget --output json)"
budget="$(jq --arg amt "$AMOUNT" '.BudgetLimit.Amount = $amt | .CostTypes.IncludeCredit = true | .CostTypes.IncludeRefund = false | .CostTypes.IncludeTax = true | del(.CalculatedSpend, .LastUpdatedTime)' <<<"$budget")"
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
ks_flag() { yaml_get "['account_settings']['budget_stop']['kill_switch'].get('$1', False)"; }
KS_LAMBDAS="$(ks_flag throttle_lambdas)"
KS_VOLUMES="$(ks_flag delete_detached_volumes)"
KS_ADDRESSES="$(ks_flag release_unused_addresses)"
KS_ENDPOINTS="$(ks_flag delete_interface_endpoints)"
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
  {Sid: "FindLeftovers", Effect: "Allow", Resource: "*",
   Action: ["ec2:DescribeVolumes", "ec2:DescribeAddresses", "ec2:DescribeVpcEndpoints", "tag:GetResources"]},
  {Sid: "DeleteDetachedGroupVolumes", Effect: "Allow", Action: "ec2:DeleteVolume", Resource: "*",
   Condition: {Null: {"ec2:ResourceTag/resource-group": "false"}}},
  {Sid: "ReleaseAddressesDeleteEndpoints", Effect: "Allow", Resource: "*",
   Action: ["ec2:ReleaseAddress", "ec2:DeleteVpcEndpoints"]},
  {Sid: "ThrottleLambdas", Effect: "Allow", Action: "lambda:PutFunctionConcurrency",
   Resource: "arn:aws:lambda:\($region):\($acct):function:*"},
  {Sid: "NeverTheGuardrails", Effect: "Deny", Action: "lambda:PutFunctionConcurrency",
   Resource: "arn:aws:lambda:*:\($acct):function:guardrail-*"},
  {Sid: "OwnLogs", Effect: "Allow", Action: ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
   Resource: "arn:aws:logs:\($region):\($acct):log-group:/aws/lambda/\($fn):*"}]}')"
aws iam put-role-policy --role-name "$KS_ROLE" --policy-name kill-switch --policy-document "$ks_policy"

echo "== Lambda ${FUNCTION}"
zipdir="$(mktemp -d)"
python3 -I -c 'import sys, zipfile; z = zipfile.ZipFile(sys.argv[1], "w"); z.write(sys.argv[2], "kill_switch.py"); z.close()' \
  "$zipdir/kill_switch.zip" bootstrap/kill_switch.py
env_json="$(jq -n --arg r "$KS_REGIONS" --arg t "$KS_TERMINATE" --arg a "$KS_AMIS" --arg l "$KS_LAMBDAS" \
  --arg v "$KS_VOLUMES" --arg e "$KS_ADDRESSES" --arg p "$KS_ENDPOINTS" \
  '{Variables: {REGIONS: $r, TERMINATE_INSTANCES: $t, DELETE_RUNNER_AMIS: $a, THROTTLE_LAMBDAS: $l,
                DELETE_DETACHED_VOLUMES: $v, RELEASE_UNUSED_ADDRESSES: $e, DELETE_INTERFACE_ENDPOINTS: $p}}')"
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
# #30 fix 6: 2 executions always free for the kill switch, whatever else floods the account. Lambda keeps 100
# unreserved, so this needs the account limit raised from a new account's 10 (quota L-B99A9384, to 1,000).
limit="$(aws lambda get-account-settings --region "$REGION" --query AccountLimit.ConcurrentExecutions --output text)"
if (( limit >= 102 )); then
  aws lambda put-function-concurrency --region "$REGION" --function-name "$FUNCTION" --reserved-concurrent-executions 2 >/dev/null
  echo "reserved 2 concurrent executions for ${FUNCTION}"
else
  echo "WARNING: the account's Lambda limit is ${limit}; request 1000 (quota L-B99A9384) and rerun to reserve the kill switch's 2" >&2
fi
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

# ---------- the fast stop (#30): CloudWatch alarms, no waiting for billing data ----------
FAST_TOPIC="guardrail-fast-stop"
fast() { yaml_get "['account_settings']['budget_stop']['fast_stop']['$1']"; }
echo "== SNS topic ${FAST_TOPIC}: CloudWatch alarms → the kill switch (fast mode) and an email"
fast_arn="$(aws sns create-topic --region "$REGION" --name "$FAST_TOPIC" --query TopicArn --output text)"
fast_policy="$(jq -n --arg topic "$fast_arn" --arg acct "$ACCOUNT_ID" '{Version: "2012-10-17", Statement: [{
  Sid: "AlarmsMayPublish", Effect: "Allow", Principal: {Service: "cloudwatch.amazonaws.com"}, Action: "SNS:Publish",
  Resource: $topic, Condition: {StringEquals: {"aws:SourceAccount": $acct}}}]}')"
aws sns set-topic-attributes --region "$REGION" --topic-arn "$fast_arn" --attribute-name Policy --attribute-value "$fast_policy"
aws lambda add-permission --region "$REGION" --function-name "$FUNCTION" --statement-id fast-sns \
  --action lambda:InvokeFunction --principal sns.amazonaws.com --source-arn "$fast_arn" >/dev/null 2>&1 || true
aws sns subscribe --region "$REGION" --topic-arn "$fast_arn" --protocol lambda --notification-endpoint "$fn_arn" \
  --query SubscriptionArn --output text | sed 's/^/subscribed: /'
if aws sns list-subscriptions-by-topic --region "$REGION" --topic-arn "$fast_arn" \
     --query "Subscriptions[?Protocol=='email'].Endpoint" --output text | grep -qF "$EMAIL"; then
  echo "email already subscribed (or awaiting confirmation)"
else
  aws sns subscribe --region "$REGION" --topic-arn "$fast_arn" --protocol email --notification-endpoint "$EMAIL" >/dev/null
  echo "email subscription sent: confirm it from your inbox"
fi
gib="$(fast network_out_gib)"; calls="$(fast lambda_invocations)"; mib="$(fast log_ingest_mib)"
alarm=(--region "$REGION" --evaluation-periods 2 --datapoints-to-alarm 2 --treat-missing-data notBreaching
       --comparison-operator GreaterThanThreshold --alarm-actions "$fast_arn")
# EC2 publishes NetworkOut per instance only (an account total needs paid detailed monitoring), so a
# Metrics Insights query sums every instance's.
network_query="$(jq -c -n '[{Id: "out", Period: 300, ReturnData: true,
  Expression: "SELECT SUM(NetworkOut) FROM SCHEMA(\"AWS/EC2\", InstanceId)"}]')"
aws cloudwatch put-metric-alarm "${alarm[@]}" --alarm-name guardrail-fast-network-out \
  --alarm-description "Instances sent > ${gib} GiB in 5 min, twice: kill switch (fast). policy.yaml fast_stop" \
  --threshold "$(( gib * 1024 * 1024 * 1024 ))" --metrics "$network_query"
aws cloudwatch put-metric-alarm "${alarm[@]}" --alarm-name guardrail-fast-lambda-invocations --period 300 \
  --alarm-description "Lambdas called > ${calls} times in 5 min, twice: kill switch (fast). policy.yaml fast_stop" \
  --namespace AWS/Lambda --metric-name Invocations --statistic Sum --threshold "$calls"
aws cloudwatch put-metric-alarm "${alarm[@]}" --alarm-name guardrail-fast-log-ingest --period 300 \
  --alarm-description "Logs took in > ${mib} MiB in 5 min, twice: kill switch (fast). policy.yaml fast_stop" \
  --namespace AWS/Logs --metric-name IncomingBytes --statistic Sum --threshold "$(( mib * 1024 * 1024 ))"
aws cloudwatch describe-alarms --region "$REGION" --alarm-name-prefix guardrail-fast- \
  --query 'MetricAlarms[].{alarm: AlarmName, state: StateValue, threshold: Threshold}' --output table

# ---------- Cost Anomaly Detection (free) ----------
# Cost Explorer is a global service: the CLI calls its one endpoint, in us-east-1. Nothing runs or is stored there.
ANOMALY_USD="$(yaml_get "['account_settings']['anomaly_alert_usd']")"
echo "== Cost Anomaly Detection: per-service monitor, daily email above US\$${ANOMALY_USD}"
monitor="$(aws ce get-anomaly-monitors --query "AnomalyMonitors[?MonitorName=='guardrail-services'].MonitorArn | [0]" --output text)"
if [[ -z "$monitor" || "$monitor" == None ]]; then
  monitor="$(aws ce create-anomaly-monitor --anomaly-monitor \
    '{"MonitorName": "guardrail-services", "MonitorType": "DIMENSIONAL", "MonitorDimension": "SERVICE"}' \
    --query MonitorArn --output text)"
  echo "created monitor guardrail-services"
fi
threshold="$(jq -c -n --arg usd "$ANOMALY_USD" '{Dimensions: {Key: "ANOMALY_TOTAL_IMPACT_ABSOLUTE",
  MatchOptions: ["GREATER_THAN_OR_EQUAL"], Values: [$usd]}}')"
sub="$(aws ce get-anomaly-subscriptions --query "AnomalySubscriptions[?SubscriptionName=='guardrail-daily'].SubscriptionArn | [0]" --output text)"
if [[ -z "$sub" || "$sub" == None ]]; then
  aws ce create-anomaly-subscription --anomaly-subscription "$(jq -n --arg m "$monitor" --arg e "$EMAIL" --argjson t "$threshold" \
    '{SubscriptionName: "guardrail-daily", Frequency: "DAILY", MonitorArnList: [$m],
      Subscribers: [{Type: "EMAIL", Address: $e}], ThresholdExpression: $t}')" >/dev/null
  echo "created daily subscription guardrail-daily"
else
  aws ce update-anomaly-subscription --subscription-arn "$sub" --threshold-expression "$threshold" >/dev/null
  echo "updated daily subscription guardrail-daily"
fi

for mode in budget fast; do
  echo "== DRY RUN (${mode} mode): what the kill switch would stop right now (nothing is stopped)"
  out="$(mktemp)"
  aws lambda invoke --region "$REGION" --function-name "$FUNCTION" --cli-binary-format raw-in-base64-out \
    --payload "{\"dry_run\": true, \"mode\": \"${mode}\"}" "$out" >/dev/null
  jq . "$out"; rm -f "$out"
done
echo "Done."
