# What Claude can do in AWS

Generated from [policy.yaml](policy.yaml) by `build-policies.py`. Don't edit this file.

> **DRAFT, not approved yet.** Pending:
> - read-only listing
> - change 2026-11-16: Read-only listing becomes Sydney only (was every region); IAM reads stay global, as IAM has no regions

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
| EC2 and VPC | Build machines, AMIs, snapshots, volumes, launch templates; VPCs, subnets, security groups, internet gateways | everything, except 17 blocked actions (below); instance type and disk limits | 2026-11-16 Kieron |
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
  | `lambda:PutProvisionedConcurrencyConfig` | Provisioned concurrency (billed around the clock) |
  | `s3:PutAccountPublicAccessBlock` | Turning off the account-wide public access block |
  | `s3:PutBucketObjectLockConfiguration` | Object lock (locked data can't be deleted, so it is billed until the lock ends) |

## Global services and read-only access

Global services (no region):

- **Who am I**: `sts:GetCallerIdentity`
- **Costs and prices, read only**: `budgets:View*`, `budgets:Describe*`, `ce:Get*`, `ce:Describe*`, `ce:List*`, `pricing:*`

Read-only, Sydney only (lists and metadata, no data, no changes):

`ec2:Describe*`, `lambda:List*`, `lambda:GetFunction*`, `cloudformation:Describe*`, `cloudformation:List*`, `s3:ListAllMyBuckets`, `s3:GetBucketLocation`, `s3:GetBucketTagging`, `tag:GetResources`, `tag:GetTagKeys`, `tag:GetTagValues`, `logs:DescribeLogGroups`, `cloudtrail:LookupEvents`, `servicequotas:Get*`, `servicequotas:List*`, `access-analyzer:ValidatePolicy`, `access-analyzer:CheckNoNewAccess`, `access-analyzer:CheckAccessNotGranted`, `access-analyzer:CheckNoPublicAccess`

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

- Monthly budget **US$30**, email alerts at 50 %, 80 % and 100 % actual, and 100 % forecast.
- Public access blocked for: S3 (whole account), AMIs, EBS snapshots. Claude can't turn it off.
- EBS disk encryption on by default. Claude can't turn it off.
- Service quotas: read-only to Claude, so the EC2 vCPU quota caps how much can run at once.

## Change history

| Date | Change | Approved |
|---|---|---|
| 2026-11-16 | First allow-list, for building the runner; replaces the old claude-assistant-policy v1 | 2026-11-16 Kieron |
| 2026-11-16 | Every resource Claude creates must carry an approved resource-group tag (first group build-runner); adds AWS Resource Groups | 2026-11-16 Kieron |
| 2026-11-16 | Read-only listing becomes Sydney only (was every region); IAM reads stay global, as IAM has no regions | pending |

## Policy sizes

- `claude-assistant-policy.json`: 5152 of 6144 characters
- `claude-assistant-iam.json`: 1814 of 6144 characters
- `claude-boundary.json`: 4566 of 6144 characters
