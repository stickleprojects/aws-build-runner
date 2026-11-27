#!/usr/bin/env bash
# Godmode, in the MANAGEMENT account (CloudShell): the organization's allow-list of services and regions
# (#30 fix 7; Kieron, 2026-11-27).
#
#   cd ~/aws-build-runner && git pull && cd part1-godmode && ./bootstrap-org-allowed-services.sh
#
# Nothing is allowed unless named. At Root, the SCP org-allowed-services (bootstrap/org-allowed-services-scp.json)
# replaces AWS's FullAWSAccess, so only what it allows can happen in any account:
#   - in Sydney: the named services we use (EC2, Lambda, S3, SSM, CloudShell...)
#   - in us-east-1: the named global services (IAM, Organizations, Budgets, billing, support, sign-in...): AWS
#     hosts them there, and so is this organization's Identity Center (checked 2026-11-27)
#   - anywhere else, or any other service: nothing
# SCPs never grant, they only limit: an action must be allowed at every level (Root, OU, account), so replacing
# FullAWSAccess at Root alone is enough. It stays on the OUs and accounts. AWS's AdvancedModeRegionRestriction
# SCP stays attached too. SCPs never apply to the management account, so this can always be undone there; the
# script ends by printing the exact commands. tests/test_region_lock.py checks every AWS call our code makes is
# allowed. Idempotent. The kill switch sweeps Sydney only (bootstrap-budget-stop.sh, via the guardrails workflow).
set -euo pipefail
cd "$(dirname "$0")"

NAME=org-allowed-services
FILE=bootstrap/org-allowed-services-scp.json
FULL=p-FullAWSAccess
master="$(aws organizations describe-organization --query Organization.MasterAccountId --output text)"
[[ "$(aws sts get-caller-identity --query Account --output text)" == "$master" ]] \
  || { echo "Refusing: run this in the organization's management account." >&2; exit 1; }
root="$(aws organizations list-roots --query 'Roots[0].Id' --output text)"
on_root() { aws organizations list-policies-for-target --target-id "$root" --filter SERVICE_CONTROL_POLICY --query 'Policies[].Id' --output text; }

echo "== SCP ${NAME}"
id="$(aws organizations list-policies --filter SERVICE_CONTROL_POLICY --query "Policies[?Name=='${NAME}'].Id | [0]" --output text)"
if [[ -z "$id" || "$id" == None ]]; then
  id="$(aws organizations create-policy --type SERVICE_CONTROL_POLICY --name "$NAME" \
    --description "Only these services: Sydney's we use, and the global ones in us-east-1 (aws-build-runner #30)" \
    --content "file://${FILE}" --query Policy.PolicySummary.Id --output text)"
  echo "created ${id}"
elif [[ "$(aws organizations describe-policy --policy-id "$id" --query Policy.Content --output text | jq -S -c .)" == "$(jq -S -c . "$FILE")" ]]; then
  echo "unchanged (${id})"
else
  aws organizations update-policy --policy-id "$id" --content "file://${FILE}" >/dev/null
  echo "updated (${id})"
fi

# Ours first, then take away FullAWSAccess: Root is never left with nothing, or with only the allow-all.
echo "== Root (${root})"
if grep -qw "$id" <<<"$(on_root)"; then echo "${NAME} attached already"; else
  aws organizations attach-policy --policy-id "$id" --target-id "$root"; echo "${NAME} attached"; fi
if grep -qw "$FULL" <<<"$(on_root)"; then
  aws organizations detach-policy --policy-id "$FULL" --target-id "$root"; echo "FullAWSAccess detached from Root"
else echo "FullAWSAccess not on Root already"; fi

echo "== check: SCPs on Root"
aws organizations list-policies-for-target --target-id "$root" --filter SERVICE_CONTROL_POLICY --query 'Policies[].Name' --output text
echo "Done. Only the named services work: in Sydney ours, in us-east-1 the global ones; nothing anywhere else."
echo "To undo, here in the management account (SCPs never apply to it), in this order:"
echo "  aws organizations attach-policy --policy-id ${FULL} --target-id ${root}"
echo "  aws organizations detach-policy --policy-id ${id} --target-id ${root}"
