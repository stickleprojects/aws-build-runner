# shellcheck shell=bash
# Shared helpers for apply.sh and rollback.sh (source it). Each exists because of a real failure;
# tests/regressions.py makes sure nothing bypasses them.

# IAM tag values allow only letters, digits, spaces and _ . : / = + - @, at most 256 characters.
# (2026-11-16: "(approved in environment aws)" made tag-policy fail after an apply.)
iam_tag_value() { LC_ALL=C sed 's#[^A-Za-z0-9 _.:/=+@-]# #g; s#  *# #g' <<<"${1:0:256}"; }

# tag_applied POLICY_ARN COMMIT AT BY: the applied-commit/-at/-by tags, values cleaned.
tag_applied() {
  aws iam tag-policy --policy-arn "$1" --tags \
    Key=applied-commit,Value="$(iam_tag_value "$2")" \
    Key=applied-at,Value="$(iam_tag_value "$3")" \
    Key=applied-by,Value="$(iam_tag_value "$4")"
}

# gh_deployment_status REPO DEPLOYMENT_ID STATE ENVIRONMENT DESCRIPTION
# GitHub rejects deployment status descriptions over 140 characters with HTTP 422.
# (2026-11-16: the first apply's success status was rejected.)
gh_deployment_status() {
  gh api "repos/$1/deployments/$2/statuses" -f state="$3" -f environment="$4" \
    -f description="${5:0:140}" >/dev/null
}
