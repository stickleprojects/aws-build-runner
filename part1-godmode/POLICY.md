# What Claude can do in AWS

Generated from [policy.yaml](policy.yaml) by `build-policies.py`. Don't edit this file.

> **DRAFT, not approved yet.** Pending:
> - change 2026-11-26: Usage alert (#30): a second budget, guardrail-monthly-usage, counts usage before credits at US$29 and emails at 80 % and 100 % actual and 100 % forecast. It stops nothing; the stop stays on what would be billed. The apply role may view and change that budget too

Claude's IAM user (`AWS_CLAUDE_USER`) in the account `AWS_ACCOUNT_ID` · home region Sydney (`ap-southeast-2`). The account id and user name aren't in the repo: see [config.py](config.py).

## In short

- **Can build** with the 9 approved services below, **in Sydney only**.
- **Can list** (metadata only) in Sydney, plus IAM reads, which have no region.
- **Everything else is denied**, because nothing grants it: other services, other regions, IAM users,
  access keys, budgets, quotas, billing changes, Organizations, account settings.
- **Roles Claude creates** live under `/claude/` and can never do more than Claude can.
- **Anything new** goes through the change process ([README.md](README.md#change-process)).

## Approved services (Sydney only)

| Service | Used for | Allowed | Approved |
|---|---|---|---|
| EC2 and VPC | Build machines, AMIs, snapshots, volumes, launch templates; VPCs, subnets, security groups, internet gateways | everything, except 19 blocked actions (below); instance type and disk limits | 2026-11-16 Kieron |
| Systems Manager | Parameters and secrets (Parameter Store), Session Manager shell into instances, Run Command | everything | 2026-11-16 Kieron |
| Lambda | The runner webhook and the reaper that removes stray instances | everything, except 1 blocked action (below) | 2026-11-16 Kieron |
| EventBridge and Scheduler | Scheduled jobs (the reaper), event rules | everything | 2026-11-16 Kieron |
| CloudWatch Logs and metrics | Logs from Lambda and the runner instances; metrics and alarms | everything | 2026-11-16 Kieron |
| S3 | Lambda code and template packages, build artifacts | everything, except 2 blocked actions (below) | 2026-11-16 Kieron |
| CloudFormation | Deploying and tearing down stacks (runs with Claude's own rights, so the same limits apply) | everything | 2026-11-16 Kieron |
| Resource Groups | One saved tag search per approved resource group, to see everything in it (free) | everything | 2026-11-16 Kieron |
| KMS, through other services only | Encrypted disks, SecureString parameters, encrypted S3 objects and Lambda settings, all with AWS-managed keys. No creating keys | `kms:Decrypt`, `kms:Encrypt`, `kms:GenerateDataKey*`, `kms:ReEncrypt*`, `kms:DescribeKey`, `kms:CreateGrant`; only through ec2, ssm, s3, lambda | 2026-11-16 Kieron |

## Limits inside approved services

- **EC2 instance types**: only `t3.*`, `t3a.*`, `t4g.*`, `m*.large`, `m*.xlarge`, `c*.large`, `c*.xlarge`. Applies to launching and to resizing.
- **No dedicated instances or hosts**: shared tenancy only.
- **Disks**: at most 256 GiB each.
- **No provisioned-IOPS disks**: io1, io2 blocked.
- **No paid disk performance**: at most 3000 IOPS and 125 MiB/s per disk (gp3's free baseline; above it bills every month).
- **Blocked actions**:

  | Action | What it is, and why it's blocked |
  |---|---|
  | `ec2:CreateNatGateway` | NAT gateway (≈US$45/month each, plus data) |
  | `ec2:CreateTransitGateway` | Transit gateway (≈US$50/month per attachment) |
  | `ec2:CreateVpnConnection` | Site-to-site VPN (≈US$36/month) |
  | `ec2:CreateClientVpnEndpoint` | Client VPN (≈US$73/month per subnet) |
  | `ec2:CreateVerifiedAccess*` | Verified Access (≈US$200/month per app) |
  | `ec2:EnableFastSnapshotRestores` | Fast snapshot restore (≈US$650/month per snapshot per zone) |
  | `ec2:Purchase*` | Reserved instances, capacity blocks, host reservations (long commitments) |
  | `ec2:ModifyReservedInstances` | Reserved instance changes |
  | `ec2:AcceptReservedInstancesExchangeQuote` | Reserved instance exchanges |
  | `ec2:AllocateHosts` | Dedicated hosts |
  | `ec2:CreateCapacityReservation*` | Capacity reservations (billed whether used or not) |
  | `ec2:CreateFleet` | EC2 Fleet (launches many instances in one call, skipping the type limits) |
  | `ec2:RequestSpotFleet` | Spot Fleet (same) |
  | `ec2:RequestSpotInstances` | Legacy spot requests (spot goes through RunInstances instead) |
  | `ec2:DisableImageBlockPublicAccess` | Making AMIs public |
  | `ec2:DisableSnapshotBlockPublicAccess` | Making EBS snapshots public |
  | `ec2:DisableEbsEncryptionByDefault` | Turning off disk encryption by default |
  | `ec2:LockSnapshot` | Locking a snapshot (locked, it can't be deleted, so it bills until the lock ends, and the kill switch can't remove it) |
  | `ec2:EnableImageDeregistrationProtection` | Protecting an AMI from deletion (the kill switch couldn't remove it) |
  | `lambda:PutProvisionedConcurrencyConfig` | Provisioned concurrency (billed around the clock) |
  | `s3:PutAccountPublicAccessBlock` | Turning off the account-wide public access block |
  | `s3:PutBucketObjectLockConfiguration` | Object lock (locked data can't be deleted, so it is billed until the lock ends) |

## Global services and read-only access

Global services (no region):

- **Who am I**: `sts:GetCallerIdentity`
- **Costs and prices, read only**: `budgets:View*`, `budgets:Describe*`, `ce:Get*`, `ce:Describe*`, `ce:List*`, `pricing:*`

Read-only, Sydney only (lists and metadata, no data, no changes):

`ec2:Describe*`, `lambda:List*`, `lambda:GetFunction*`, `cloudformation:Describe*`, `cloudformation:List*`, `s3:ListAllMyBuckets`, `s3:GetBucketLocation`, `s3:GetBucketTagging`, `tag:GetResources`, `tag:GetTagKeys`, `tag:GetTagValues`, `logs:DescribeLogGroups`, `cloudtrail:LookupEvents`, `servicequotas:Get*`, `servicequotas:List*`, `access-analyzer:ValidatePolicy`, `access-analyzer:CheckNoNewAccess`, `access-analyzer:CheckAccessNotGranted`, `access-analyzer:CheckNoPublicAccess`, `sns:ListTopics`, `sns:ListSubscriptionsByTopic`

Read-only IAM (IAM has no regions; AWS evaluates every IAM request as us-east-1):

`iam:Get*`, `iam:List*`, `iam:GenerateServiceLastAccessedDetails`, `iam:SimulatePrincipalPolicy`, `iam:SimulateCustomPolicy`

## IAM: permissions for what Claude builds

- Creates, edits and deletes **roles, policies and instance profiles under `/claude/` only**.
- Every role **must carry the boundary `guardrails/claude-boundary`**, or creating or editing it fails. The boundary
  allows only the approved services in Sydney, with the same limits and blocked actions. Roles get
  **no IAM rights** apart from passing other roles under `/claude/`. So a role can never do more than Claude.
- Passes its roles only to: ec2.amazonaws.com, lambda.amazonaws.com, scheduler.amazonaws.com, events.amazonaws.com, cloudformation.amazonaws.com.
- Assumes its own `/claude/` roles, to test them.
- Service-linked roles only for: spot.amazonaws.com.
- **Cannot**: create IAM users, access keys or groups; change its own permissions; touch roles or policies
  outside `/claude/` (for example the account's `AccountFullAccessRole`).

## Resource groups

Everything Claude creates belongs to a resource group: the tag **`resource-group`** with one of these values.
Each group also has an AWS Resource Group of the same name (a saved tag search) to see it all in the console.

| Group | For |
|---|---|
| `build-runner` | The on-demand GitHub runner for spellbound3d (part 2) |

- **Enforced at creation**: `ec2:RunInstances`, `ec2:CreateVolume`, `ec2:CreateSnapshot`, `ec2:CreateImage`, `ec2:CreateLaunchTemplate`, `ec2:CreateSecurityGroup`, `lambda:CreateFunction`, `logs:CreateLogGroup`, `cloudformation:CreateStack`, `iam:CreateRole`, `iam:CreatePolicy`, `iam:CreateInstanceProfile`. Without an approved `resource-group` tag, these are denied.
- **Can't be removed or changed** to an unapproved group, on the resources above.
- **Not enforceable at creation** (AWS doesn't accept tags there, so requiring one would block them):
  S3 buckets, SSM parameter updates, EventBridge rules and schedules; also VPCs, subnets, route tables
  and gateways (their creation touches an existing VPC). These get the tag from their CloudFormation
  stack's tags; Claude creates them through stacks.
- **A new group** goes through the change process, like any other permission.

## Set by godmode, outside the policy

- Monthly budget **US$29**, email alerts at 50 %, 80 % and 100 % actual, and 100 % forecast. It counts **everything in the account**, every service (instances, logs, snapshots, data transfer…), as **what you'd be billed**: credits (applied automatically on the invoice) reduce it, refunds (paid back later, if ever) don't, and tax counts.
- **Usage alert, no stop:** budget `guardrail-monthly-usage` counts usage **before credits** (US$29), emailing at 80 % and 100 % actual and 100 % forecast. While credits cover the bill the budget above reads US$0 and can't fire; this one still sees the spending.
- **Automatic stop at 100 % of actual spend** (US$29):
  - **Nothing new:** AWS Budgets attaches `guardrails/budget-stop` to Claude's user and roles (`build-runner-bake`, `build-runner-ami-ci`, `build-runner-launcher`): no instances, spot/fleet, volumes, snapshots, images, IPs, NAT, endpoints, functions, buckets, stacks or passing roles; no resizing disks, copying snapshots, changing Lambdas or their concurrency, creating or changing schedules and rules, or creating log groups.
  - **Stop what's spending:** the kill switch (Lambda `guardrail-kill-switch`, Sydney) terminates **every instance** in the account and cancels open spot requests; sets **our Lambdas** (tagged `resource-group`) to 0 concurrency, so none runs; deletes the runner AMIs and their snapshots (the weekly pipeline rebuilds them); deletes **unattached disks** tagged `resource-group`; releases **Elastic IPs not attached** to anything; deletes **interface VPC endpoints**.
  - Billing data lags by 8-36 hours, so the stop fires at US$29 to keep the bill under the US$35 ceiling. Only Kieron can lift it (Billing → Budgets → Actions); Claude can't detach it or touch the kill switch.
- **Fast stop, without waiting for billing data:** CloudWatch alarms (Sydney, whole account) trigger the kill switch when, for 2 periods of 5 minutes in a row (≈10 minutes), instances send more than 1 GiB, Lambdas are called more than 1,000 times, or logs take in more than 100 MiB, per 5 minutes. It then terminates every instance, cancels spot requests and throttles our Lambdas, and emails Kieron. It keeps the AMIs, disks and IPs, so a false alarm costs only the interrupted jobs. Turning the Lambdas back on is Kieron's call.
- **Cost Anomaly Detection** (free) emails Kieron about unusual daily spend on any service of US$3 or more.
- **Protected resources** Claude and its roles can't touch, whatever the approved services allow: The budget kill switch (`arn:aws:lambda:*:ACCOUNT:function:guardrail-*`, lambda:*); The fast-stop alarms (`arn:aws:cloudwatch:*:ACCOUNT:alarm:guardrail-*`, cloudwatch:PutMetricAlarm, cloudwatch:DeleteAlarms, cloudwatch:DisableAlarmActions, cloudwatch:SetAlarmState, cloudwatch:TagResource, cloudwatch:UntagResource).
- **Cost boundary** (CI fails any change past it; raising a value needs Kieron's approval, whoever asks): a US$35 ceiling, the stop at most US$29 (expected ≈US$13 typical, ≈US$25 busiest a month); at most 4 runners, sizes up to xlarge (4 vCPU), spot first; runners end after 90 min or 20 min idle; a 9-hour overnight curfew; 3 kept images; 30 GiB disks; uploads at most 6250 KiB/s per job; no scheduled Cost Explorer calls (US$0.01 each).
- Public access blocked for: S3 (whole account), AMIs, EBS snapshots. Claude can't turn it off.
- EBS disk encryption on by default. Claude can't turn it off.
- Service quotas: read-only to Claude, so the EC2 vCPU quota caps how much can run at once.

## Change history

| Date | Change | Approved |
|---|---|---|
| 2026-11-16 | First allow-list, for building the runner; replaces the old claude-assistant-policy v1 | 2026-11-16 Kieron |
| 2026-11-16 | Every resource Claude creates must carry an approved resource-group tag (first group build-runner); adds AWS Resource Groups | 2026-11-16 Kieron |
| 2026-11-16 | Read-only listing becomes Sydney only (was every region); IAM reads stay global, as IAM has no regions | 2026-11-16 Kieron |
| 2026-11-16 | Budget US$25 (≈£20; the ceiling is a £30 bill), counted as what would be billed (credits reduce it, refunds don't). At 100%: AWS Budgets blocks anything new for Claude's user and roles, and the kill switch terminates every instance and deletes the runner AMIs. Claude can't touch the kill switch | 2026-11-16 Kieron |
| 2026-11-16 | The budget stop also covers the weekly AMI pipeline role (build-runner-ami-ci), so at the limit it can't launch bake instances either | 2026-11-16 Kieron |
| 2026-11-16 | The budget stop also covers the runner launcher's role (build-runner-launcher), so at the limit it can't launch runner instances either (the org-wide stop already did) | 2026-11-16 Kieron |
| 2026-11-17 | Disk limits (#30 fix 4): gp3 disks at most the free 3,000 IOPS and 125 MiB/s; snapshot locks and AMI deletion protection blocked, so the kill switch can always remove what it must | 2026-11-17 Kieron |
| 2026-11-26 | Usage alert (#30): a second budget, guardrail-monthly-usage, counts usage before credits at US$29 and emails at 80 % and 100 % actual and 100 % forecast. It stops nothing; the stop stays on what would be billed. The apply role may view and change that budget too | pending |
| 2026-11-17 | Fast stop (#30 fix 2): CloudWatch alarms on data sent by instances, Lambda calls and log ingestion trigger the kill switch within ≈10 minutes, without waiting for billing data. It terminates instances and throttles our Lambdas; AMIs, disks and IPs stay. Claude can't change or delete the alarms | 2026-11-17 Kieron |
| 2026-11-17 | Wider stop at the budget (#30 fix 3): the kill switch also throttles our Lambdas, deletes detached build disks, releases unused Elastic IPs and deletes interface VPC endpoints; the budget stop also blocks resizing disks, copying snapshots, changing Lambdas, creating or changing schedules and rules, and creating log groups | 2026-11-17 Kieron |
| 2026-11-17 | More headroom (#30 fix 5): the budget stop fires at US$29, ≈£23 (was US$25); Cost Anomaly Detection emails Kieron about unusual daily spend of US$3 or more | 2026-11-17 Kieron |
| 2026-11-17 | Cost boundary: the runners stay within a US$35 ceiling (stop at US$29; expected ≈US$13 typical, ≈US$25 busiest a month), at most 4 runners of at most 4 vCPU, spot first, 90-minute and 20-minute limits, a 9-hour curfew, 3 kept images, 30 GiB disks, a capped upload rate, and no scheduled Cost Explorer calls. CI fails any change past these; raising one needs Kieron's approval, whoever asks | 2026-11-17 Kieron |
| 2026-11-18 | SNS read-only (Sydney): list topics and their subscriptions, so Claude can check the alert emails are confirmed and the budget and fast stops still reach the kill switch. Free; no publishing, subscribing or changes | 2026-11-18 Kieron |

## Policy sizes

- `claude-assistant-policy.json`: 6086 of 6144 characters
- `claude-assistant-iam.json`: 1814 of 6144 characters
- `claude-boundary.json`: 5452 of 6144 characters
