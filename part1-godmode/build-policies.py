#!/usr/bin/env python3
"""Build the IAM policies and the plain-English summary from policy.yaml.

  out/claude-assistant-policy.json  approved services, read-everywhere, limits   (on the user)
  out/claude-assistant-iam.json     roles/policies/instance profiles under /claude/ (on the user)
  out/claude-boundary.json          the boundary on every role Claude creates
  POLICY.md                         what all of that means, in plain English

The JSON in out/ uses the placeholder {{ACCOUNT_ID}}; config.render() fills in the real account
when policies are checked against AWS or applied, so the repo holds no account id.

Usage:
  build-policies.py            fails if anything in policy.yaml is still "approved: pending"
  build-policies.py --draft    builds anyway; POLICY.md is marked DRAFT and lists what's pending
  build-policies.py --check    builds to a temp dir and fails if POLICY.md is out of date

Also fails on a document over IAM's 6144-character managed-policy limit (whitespace excluded).
"""
import argparse
import datetime
import json
import re
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
MAX_POLICY_CHARS = 6144


def is_approved(value) -> bool:
    return value is not None and str(value).strip().lower() != "pending"


def pending_items(p: dict) -> list:
    items = [f"service: {s['title']}" for s in p["services"] if not is_approved(s.get("approved"))]
    items += [f"global: {g['title']}" for g in p["global"] if not is_approved(g.get("approved"))]
    for key, label in (("read_only", "read-only listing"), ("iam", "IAM for what Claude builds"),
                       ("resource_groups", "resource-group tag on everything Claude creates")):
        if key not in p:
            continue
        if not is_approved(p[key].get("approved")):
            items.append(label)
    items += [f"change {c['date']}: {c['what']}" for c in p.get("changes", []) if not is_approved(c.get("approved"))]
    return items


def resource_group_statements(p: dict) -> list:
    """Denies that make every tag-on-create resource carry an approved resource-group tag."""
    rg = p.get("resource_groups")
    if not rg:
        return []
    key, values = rg["tag_key"], sorted(rg["groups"])
    request_tag = f"aws:RequestTag/{key}"
    stmts = []
    for i, group in enumerate(rg["enforced_on"]):
        types = group["resources"]
        stmts.append({
            "Sid": f"CreateOnlyInApprovedGroup{i}", "Effect": "Deny", "Action": sorted(group["actions"]),
            "Resource": "*" if types == "*" else [f"arn:aws:ec2:*:*:{t}/*" for t in types],
            # Also true when the tag is missing: StringNotEquals on an absent key matches.
            "Condition": {"StringNotEquals": {request_tag: values}},
        })
    stmts.append({
        "Sid": "ResourceGroupTagCantBeRemoved", "Effect": "Deny", "Action": sorted(rg["untag_actions"]),
        "Resource": "*", "Condition": {"ForAnyValue:StringEquals": {"aws:TagKeys": [key]}},
    })
    stmts.append({
        "Sid": "ResourceGroupTagOnlyApprovedValues", "Effect": "Deny", "Action": sorted(rg["tag_actions"]),
        "Resource": "*",
        "Condition": {"ForAnyValue:StringEquals": {"aws:TagKeys": [key]},
                      "StringNotEquals": {request_tag: values}},
    })
    return stmts


def statements(p: dict) -> dict:
    acct, region = "{{ACCOUNT_ID}}", p["region"]  # placeholder: the repo holds no account id
    services = p["services"]
    kms = [s for s in services if s["id"] == "kms"]
    regional = [s for s in services if s["id"] != "kms"]
    boundary_arn = f"arn:aws:iam::{acct}:policy/{p['iam']['boundary']}"
    path = p["iam"]["path"]

    sydney_actions = sorted({a for s in regional for a in s["actions"]})
    allow_sydney = {
        "Sid": "ApprovedServicesInHomeRegion",
        "Effect": "Allow",
        "Action": sydney_actions,
        "Resource": "*",
        "Condition": {"StringEquals": {"aws:RequestedRegion": region}},
    }
    allow_kms = [
        {
            "Sid": "KmsOnlyThroughApprovedServices",
            "Effect": "Allow",
            "Action": k["actions"],
            "Resource": "*",
            "Condition": {"StringEquals": {"kms:ViaService": [f"{v}.{region}.amazonaws.com" for v in k["via_services"]]}},
        }
        for k in kms
    ]
    identity = [g for g in p["global"] if g["id"] == "identity"]
    allow_global = {
        "Sid": "GlobalIdentityAndBilling",
        "Effect": "Allow",
        "Action": sorted({a for g in p["global"] for a in g["actions"]}),
        "Resource": "*",
    }
    allow_identity_only = {
        "Sid": "Identity",
        "Effect": "Allow",
        "Action": sorted({a for g in identity for a in g["actions"]}),
        "Resource": "*",
    }
    see = [
        {"Sid": "ReadOnlyInHomeRegion", "Effect": "Allow", "Action": sorted(p["read_only"]["home_region"]),
         "Resource": "*", "Condition": {"StringEquals": {"aws:RequestedRegion": region}}},
        {"Sid": "ReadOnlyGlobalIam", "Effect": "Allow", "Action": sorted(p["read_only"]["global"]),
         "Resource": "*"},
    ]

    denies = []
    blocked = sorted({a for s in services for a in (s.get("blocked") or {})})
    if blocked:
        denies.append({"Sid": "BlockedActions", "Effect": "Deny", "Action": blocked, "Resource": "*"})
    ec2 = next((s for s in services if s["id"] == "ec2"), None)
    limits = (ec2 or {}).get("limits") or {}
    if "instance_types" in limits:
        types = limits["instance_types"]
        denies.append({
            "Sid": "OnlyApprovedInstanceTypes", "Effect": "Deny", "Action": "ec2:RunInstances",
            "Resource": "arn:aws:ec2:*:*:instance/*",
            "Condition": {"StringNotLike": {"ec2:InstanceType": types}},
        })
        denies.append({
            "Sid": "NoResizingPastApprovedTypes", "Effect": "Deny", "Action": "ec2:ModifyInstanceAttribute",
            "Resource": "*",
            "Condition": {"Null": {"ec2:Attribute/InstanceType": "false"},
                          "StringNotLike": {"ec2:Attribute/InstanceType": types}},
        })
    if limits.get("shared_tenancy_only"):
        denies.append({
            "Sid": "SharedTenancyOnly", "Effect": "Deny", "Action": "ec2:RunInstances",
            "Resource": "arn:aws:ec2:*:*:instance/*",
            "Condition": {"StringNotEquals": {"ec2:Tenancy": "default"}},
        })
    volume_actions = ["ec2:CreateVolume", "ec2:ModifyVolume", "ec2:RunInstances"]
    if "max_volume_gib" in limits:
        denies.append({
            "Sid": "MaxVolumeSize", "Effect": "Deny", "Action": volume_actions,
            "Resource": "arn:aws:ec2:*:*:volume/*",
            "Condition": {"NumericGreaterThan": {"ec2:VolumeSize": str(limits["max_volume_gib"])}},
        })
    if limits.get("blocked_volume_types"):
        denies.append({
            "Sid": "BlockedVolumeTypes", "Effect": "Deny", "Action": volume_actions,
            "Resource": "arn:aws:ec2:*:*:volume/*",
            "Condition": {"StringEquals": {"ec2:VolumeType": limits["blocked_volume_types"]}},
        })

    denies += resource_group_statements(p)

    role_arn = f"arn:aws:iam::{acct}:role{path}*"
    pass_roles = {
        "Sid": "PassOwnRolesToApprovedServices", "Effect": "Allow", "Action": "iam:PassRole",
        "Resource": role_arn,
        "Condition": {"StringEquals": {"iam:PassedToService": p["iam"]["pass_roles_to"]}},
    }
    iam = [
        {
            "Sid": "CreateRolesOnlyWithTheBoundary", "Effect": "Allow",
            "Action": ["iam:CreateRole", "iam:PutRolePermissionsBoundary", "iam:PutRolePolicy",
                       "iam:DeleteRolePolicy", "iam:AttachRolePolicy", "iam:DetachRolePolicy"],
            "Resource": role_arn,
            "Condition": {"StringEquals": {"iam:PermissionsBoundary": boundary_arn}},
        },
        {
            "Sid": "ManageOwnRoles", "Effect": "Allow",
            "Action": ["iam:DeleteRole", "iam:UpdateRole", "iam:UpdateRoleDescription",
                       "iam:UpdateAssumeRolePolicy", "iam:TagRole", "iam:UntagRole"],
            "Resource": role_arn,
        },
        {
            "Sid": "OwnPoliciesAndInstanceProfiles", "Effect": "Allow",
            "Action": ["iam:CreatePolicy", "iam:CreatePolicyVersion", "iam:DeletePolicy",
                       "iam:DeletePolicyVersion", "iam:SetDefaultPolicyVersion", "iam:TagPolicy",
                       "iam:UntagPolicy", "iam:CreateInstanceProfile", "iam:DeleteInstanceProfile",
                       "iam:AddRoleToInstanceProfile", "iam:RemoveRoleFromInstanceProfile",
                       "iam:TagInstanceProfile", "iam:UntagInstanceProfile"],
            "Resource": [f"arn:aws:iam::{acct}:policy{path}*", f"arn:aws:iam::{acct}:instance-profile{path}*"],
        },
        pass_roles,
        {
            "Sid": "AssumeOwnRolesToTestThem", "Effect": "Allow",
            "Action": ["sts:AssumeRole", "sts:TagSession"], "Resource": role_arn,
        },
        {
            "Sid": "ApprovedServiceLinkedRoles", "Effect": "Allow", "Action": "iam:CreateServiceLinkedRole",
            "Resource": "*",
            "Condition": {"StringEquals": {"iam:AWSServiceName": p["iam"]["service_linked_roles"]}},
        },
    ]

    def doc(stmts):
        return {"Version": "2012-10-17", "Statement": stmts}

    return {
        "claude-assistant-policy.json": doc([allow_sydney, *allow_kms, allow_global, *see, *denies]),
        "claude-assistant-iam.json": doc(iam),
        "claude-boundary.json": doc([allow_sydney, *allow_kms, allow_identity_only, pass_roles, *denies]),
    }


def policy_size(d: dict) -> int:
    return len(re.sub(r"\s", "", json.dumps(d)))


def summary(p: dict, pending: list, sizes: dict) -> str:
    region, name = p["region"], p["region_name"]
    path, boundary = p["iam"]["path"], p["iam"]["boundary"]
    ec2 = next((s for s in p["services"] if s["id"] == "ec2"), {})
    limits = ec2.get("limits") or {}
    settings = p.get("account_settings") or {}
    out = []
    w = out.append
    w("# What Claude can do in AWS")
    w("")
    w("Generated from [policy.yaml](policy.yaml) by `build-policies.py`. Don't edit this file.")
    w("")
    if pending:
        w("> **DRAFT, not approved yet.** Pending:")
        for item in pending:
            w(f"> - {item}")
        w("")
    w(f"Claude's IAM user (`AWS_CLAUDE_USER`) in the account `AWS_ACCOUNT_ID` · home region {name} (`{region}`). "
      "The account id and user name aren't in the repo: see [config.py](config.py).")
    w("")
    w("## In short")
    w("")
    w(f"- **Can build** with the {len(p['services'])} approved services below, **in {name} only**.")
    w(f"- **Can list** (metadata only) in {name}, plus IAM reads, which have no region.")
    w("- **Everything else is denied**, because nothing grants it: other services, other regions, IAM users,")
    w("  access keys, budgets, quotas, billing changes, Organizations, account settings.")
    w(f"- **Roles Claude creates** live under `{path}` and can never do more than Claude can.")
    w("- **Anything new** goes through the change process ([README.md](README.md#change-process)).")
    w("")
    w(f"## Approved services ({name} only)")
    w("")
    w("| Service | Used for | Allowed | Approved |")
    w("|---|---|---|---|")
    for s in p["services"]:
        n_blocked = len(s.get("blocked") or {})
        if any(a.endswith(":*") for a in s["actions"]):
            allowed = "everything"
            if n_blocked:
                allowed += f", except {n_blocked} blocked action{'s' if n_blocked > 1 else ''} (below)"
            if s["id"] == "ec2":
                allowed += "; instance type and disk limits"
        else:
            allowed = ", ".join(f"`{a}`" for a in s["actions"])
        if s.get("via_services"):
            allowed += f"; only through {', '.join(s['via_services'])}"
        w(f"| {s['title']} | {s['use']} | {allowed} | {s.get('approved')} |")
    w("")
    w("## Limits inside approved services")
    w("")
    if "instance_types" in limits:
        w(f"- **EC2 instance types**: only {', '.join(f'`{t}`' for t in limits['instance_types'])}. Applies to launching and to resizing.")
    if limits.get("shared_tenancy_only"):
        w("- **No dedicated instances or hosts**: shared tenancy only.")
    if "max_volume_gib" in limits:
        w(f"- **Disks**: at most {limits['max_volume_gib']} GiB each.")
    if limits.get("blocked_volume_types"):
        w(f"- **No provisioned-IOPS disks**: {', '.join(limits['blocked_volume_types'])} blocked.")
    w("- **Blocked actions**:")
    w("")
    w("  | Action | What it is, and why it's blocked |")
    w("  |---|---|")
    for s in p["services"]:
        for action, why in (s.get("blocked") or {}).items():
            w(f"  | `{action}` | {why} |")
    w("")
    w("## Global services and read-only access")
    w("")
    w("Global services (no region):")
    w("")
    for g in p["global"]:
        w(f"- **{g['title']}**: {', '.join(f'`{a}`' for a in g['actions'])}")
    w("")
    w(f"Read-only, {name} only (lists and metadata, no data, no changes):")
    w("")
    w(", ".join(f"`{a}`" for a in p["read_only"]["home_region"]))
    w("")
    w("Read-only IAM (IAM has no regions; AWS evaluates every IAM request as us-east-1):")
    w("")
    w(", ".join(f"`{a}`" for a in p["read_only"]["global"]))
    w("")
    w("## IAM: permissions for what Claude builds")
    w("")
    w(f"- Creates, edits and deletes **roles, policies and instance profiles under `{path}` only**.")
    w(f"- Every role **must carry the boundary `{boundary}`**, or creating or editing it fails. The boundary")
    w(f"  allows only the approved services in {name}, with the same limits and blocked actions. Roles get")
    w("  **no IAM rights** apart from passing other roles under `/claude/`. So a role can never do more than Claude.")
    w(f"- Passes its roles only to: {', '.join(p['iam']['pass_roles_to'])}.")
    w(f"- Assumes its own `{path}` roles, to test them.")
    w(f"- Service-linked roles only for: {', '.join(p['iam']['service_linked_roles'])}.")
    w(f"- **Cannot**: create IAM users, access keys or groups; change its own permissions; touch roles or policies")
    w(f"  outside `{path}` (for example the account's `AccountFullAccessRole`).")
    w("")
    rg = p.get("resource_groups")
    if rg:
        key = rg["tag_key"]
        w("## Resource groups")
        w("")
        w(f"Everything Claude creates belongs to a resource group: the tag **`{key}`** with one of these values.")
        w("Each group also has an AWS Resource Group of the same name (a saved tag search) to see it all in the console.")
        w("")
        w("| Group | For |")
        w("|---|---|")
        for name, what in rg["groups"].items():
            w(f"| `{name}` | {what} |")
        w("")
        enforced = ", ".join(f"`{a}`" for g in rg["enforced_on"] for a in g["actions"])
        w(f"- **Enforced at creation**: {enforced}. Without an approved `{key}` tag, these are denied.")
        w(f"- **Can't be removed or changed** to an unapproved group, on the resources above.")
        w("- **Not enforceable at creation** (AWS doesn't accept tags there, so requiring one would block them):")
        w("  S3 buckets, SSM parameter updates, EventBridge rules and schedules; also VPCs, subnets, route tables")
        w("  and gateways (their creation touches an existing VPC). These get the tag from their CloudFormation")
        w("  stack's tags; Claude creates them through stacks.")
        w("- **A new group** goes through the change process, like any other permission.")
        w("")
    w("## Set by godmode, outside the policy")
    w("")
    if "budget_usd" in settings:
        scope = (" It counts **everything in the account**, every service (instances, logs, snapshots, data transfer…), "
                 "**gross**: credits and refunds don't offset it.") if settings.get("budget_counts_gross") else ""
        w(f"- Monthly budget **US${settings['budget_usd']}**, email alerts at 50 %, 80 % and 100 % actual, and 100 % forecast.{scope}")
    stop = settings.get("budget_stop")
    if stop:
        roles = ", ".join(f"`{r}`" for r in stop["roles"]) or "none yet"
        w(f"- **Automatic stop at {stop['at_percent']} % of actual spend:** AWS Budgets attaches `guardrails/budget-stop` to "
          f"Claude's user and roles ({roles}). It denies starting or creating anything billable: instances, spot/fleet, "
          "volumes, snapshots, images, IPs, NAT, endpoints, functions, buckets, stacks, passing roles. Billing data lags, "
          "so it acts within about a day. Running instances aren't stopped (runners end within 2 hours). Only Kieron can "
          "reverse it (Billing → Budgets → Actions); Claude can't detach it.")
    if settings.get("block_public_access"):
        w(f"- Public access blocked for: {', '.join(settings['block_public_access'])}. Claude can't turn it off.")
    if settings.get("ebs_encryption_by_default"):
        w("- EBS disk encryption on by default. Claude can't turn it off.")
    w("- Service quotas: read-only to Claude, so the EC2 vCPU quota caps how much can run at once.")
    w("")
    w("## Change history")
    w("")
    w("| Date | Change | Approved |")
    w("|---|---|---|")
    for c in p.get("changes", []):
        w(f"| {c['date']} | {c['what']} | {c.get('approved')} |")
    w("")
    w("## Policy sizes")
    w("")
    for n, size in sizes.items():
        w(f"- `{n}`: {size} of {MAX_POLICY_CHARS} characters")
    w("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--draft", action="store_true", help="allow pending approvals; mark POLICY.md DRAFT")
    ap.add_argument("--check", action="store_true", help="fail if POLICY.md is not what this would generate")
    ap.add_argument("--policy", default=str(HERE / "policy.yaml"))
    args = ap.parse_args()

    p = yaml.safe_load(Path(args.policy).read_text())
    if isinstance(p.get("changes"), list):
        for c in p["changes"]:
            if isinstance(c.get("date"), datetime.date):
                c["date"] = c["date"].isoformat()
    for key in ("account_id", "user"):
        if key in p:
            raise SystemExit(f"policy.yaml: remove '{key}'; it comes from config.py (env, .env.local or gh variable)")

    pending = pending_items(p)
    if pending and not (args.draft or args.check):
        print("Not approved yet (set  approved: YYYY-MM-DD Name  in policy.yaml, or build with --draft):")
        for item in pending:
            print(f"  - {item}")
        return 2

    docs = statements(p)
    for name, d in docs.items():
        sids = [s["Sid"] for s in d["Statement"]]
        if len(sids) != len(set(sids)):
            raise SystemExit(f"{name}: duplicate Sids")

    sizes = {name: policy_size(d) for name, d in docs.items()}
    too_big = [n for n, s in sizes.items() if s > MAX_POLICY_CHARS]
    text = summary(p, pending, sizes)

    if args.check:
        current = (HERE / "POLICY.md").read_text() if (HERE / "POLICY.md").exists() else ""
        if current != text:
            print("POLICY.md is out of date: run build-policies.py (with --draft if anything is pending)")
            return 1
        print("POLICY.md is up to date")
        return 1 if too_big else 0

    out = HERE / "out"
    out.mkdir(exist_ok=True)
    for name, d in docs.items():
        (out / name).write_text(json.dumps(d, indent=2) + "\n")
    (HERE / "POLICY.md").write_text(text)
    for name, size in sizes.items():
        print(f"{name:32} {size:5} / {MAX_POLICY_CHARS} chars  {'TOO BIG' if size > MAX_POLICY_CHARS else 'ok'}")
    print(f"POLICY.md written{' (DRAFT: ' + str(len(pending)) + ' pending)' if pending else ''}")
    return 1 if too_big else 0


if __name__ == "__main__":
    sys.exit(main())
