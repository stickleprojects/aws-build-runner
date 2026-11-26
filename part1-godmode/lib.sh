# shellcheck shell=bash
# Shared helpers for apply.sh and rollback.sh (source it). Each exists because of a real failure;
# tests/regressions.py makes sure nothing bypasses them.

# IAM tag values allow only letters, digits, spaces and _ . : / = + - @, at most 256 characters.
# (2026-11-16: "(approved in environment aws)" made tag-policy fail after an apply.)
# Make managed policy ARN match the rendered FILE: create it (PATH, NAME, DESCRIPTION) or add a default version,
# deleting the oldest non-default first when all 5 are in use. Prints created / unchanged / updated.
put_managed_policy() {
  local arn="$1" path="$2" name="$3" file="$4" description="$5" current old n
  if aws iam get-policy --policy-arn "$arn" >/dev/null 2>&1; then
    current="$(aws iam get-policy-version --policy-arn "$arn" \
      --version-id "$(aws iam get-policy --policy-arn "$arn" --query Policy.DefaultVersionId --output text)" \
      --query PolicyVersion.Document --output json | jq -S -c .)"
    if [[ "$current" == "$(jq -S -c . "$file")" ]]; then echo "unchanged"; return; fi
    n="$(aws iam list-policy-versions --policy-arn "$arn" --query 'length(Versions)' --output text)"
    if (( n >= 5 )); then
      old="$(aws iam list-policy-versions --policy-arn "$arn" \
        --query 'Versions[?!IsDefaultVersion] | sort_by(@, &CreateDate)[0].VersionId' --output text)"
      aws iam delete-policy-version --policy-arn "$arn" --version-id "$old"
    fi
    aws iam create-policy-version --policy-arn "$arn" --policy-document "file://$file" --set-as-default \
      --query PolicyVersion.VersionId --output text | sed 's/^/updated -> /'
  else
    aws iam create-policy --policy-name "$name" --path "$path" --policy-document "file://$file" \
      --description "$description" --query Policy.Arn --output text | sed 's/^/created /'
  fi
}

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
