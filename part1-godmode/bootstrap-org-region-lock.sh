#!/usr/bin/env bash
# Godmode, in the MANAGEMENT account (CloudShell): the organization's region lock (#30 fix 7, 2026-11-27).
#
#   cd ~/aws-build-runner && git pull && cd part1-godmode && ./bootstrap-org-region-lock.sh
#
# Creates or updates two SCPs and attaches both to Root. An explicit allow-list (Kieron, 2026-11-27): named services
# in named regions only, everything else denied. Two short policies, so each list is in one place:
#   - org-region-lock (bootstrap/org-region-lock-scp.json): outside Sydney, only a named list of global services
#     (IAM, STS, Organizations, Budgets, Cost Explorer, billing, support, Health, CloudTrail lookups, sign-in /
#     Identity Center, console settings), and only in us-east-1, where AWS hosts them and where this organization's
#     Identity Center lives (checked 2026-11-27). Nothing at all in any other region.
#   - org-sydney-services (bootstrap/org-sydney-services-scp.json): in Sydney, only the named services we use.
#     Everything else (Bedrock, SageMaker, RDS...) is denied there too.
# It adds to AWS's AdvancedModeRegionRestriction SCP (still attached); the stricter always wins. SCPs never apply
# to the management account, so a mistake here can always be undone there:
#   the script ends by printing the exact detach commands.
# tests/test_region_lock.py checks every AWS call our code makes stays allowed. Idempotent; ends with a check.
# The kill switch sweeps Sydney only once this is attached (bootstrap-budget-stop.sh, via the guardrails workflow).
set -euo pipefail
cd "$(dirname "$0")"

master="$(aws organizations describe-organization --query Organization.MasterAccountId --output text)"
[[ "$(aws sts get-caller-identity --query Account --output text)" == "$master" ]] \
  || { echo "Refusing: run this in the organization's management account." >&2; exit 1; }
root="$(aws organizations list-roots --query 'Roots[0].Id' --output text)"

# AWS allows at most 5 SCPs on a target; Root had 3 on 2026-11-27, so these two make 5.
attached="$(aws organizations list-policies-for-target --target-id "$root" --filter SERVICE_CONTROL_POLICY \
  --query 'Policies[].Name' --output text)"
new=0
for name in org-region-lock org-sydney-services; do grep -qw "$name" <<<"$attached" || new=$((new + 1)); done
(( $(wc -w <<<"$attached") + new <= 5 )) \
  || { echo "Refusing: Root has ${attached// /, }; ${new} more would pass AWS's limit of 5 SCPs." >&2; exit 1; }

ids=()
for pair in "org-region-lock:bootstrap/org-region-lock-scp.json:Outside Sydney only named global services, only in us-east-1 (aws-build-runner #30)" \
            "org-sydney-services:bootstrap/org-sydney-services-scp.json:In Sydney only the named services we use (aws-build-runner #30)"; do
  IFS=: read -r name file description <<<"$pair"
  echo "== SCP ${name}"
  id="$(aws organizations list-policies --filter SERVICE_CONTROL_POLICY \
    --query "Policies[?Name=='${name}'].Id | [0]" --output text)"
  if [[ -z "$id" || "$id" == None ]]; then
    id="$(aws organizations create-policy --type SERVICE_CONTROL_POLICY --name "$name" --description "$description" \
      --content "file://${file}" --query Policy.PolicySummary.Id --output text)"
    echo "created ${id}"
  elif [[ "$(aws organizations describe-policy --policy-id "$id" --query Policy.Content --output text | jq -S -c .)" \
          == "$(jq -S -c . "$file")" ]]; then
    echo "unchanged (${id})"
  else
    aws organizations update-policy --policy-id "$id" --content "file://${file}" >/dev/null
    echo "updated (${id})"
  fi
  if aws organizations list-targets-for-policy --policy-id "$id" --query 'Targets[].TargetId' --output text | grep -qw "$root"; then
    echo "attached to Root already"
  else
    aws organizations attach-policy --policy-id "$id" --target-id "$root"
    echo "attached to Root"
  fi
  ids+=("$id")
done

echo "== check: SCPs on Root"
aws organizations list-policies-for-target --target-id "$root" --filter SERVICE_CONTROL_POLICY --query 'Policies[].Name' --output text
echo "Done. Outside Sydney only the named global services work (in us-east-1); in Sydney only the named services."
echo "To undo, here in the management account (SCPs never apply to it):"
for id in "${ids[@]}"; do echo "  aws organizations detach-policy --policy-id ${id} --target-id ${root}"; done
