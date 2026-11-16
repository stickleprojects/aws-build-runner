# shellcheck shell=bash
# GitHub App helpers (source this). The App stickleprojects-aws-runner is installed on
# stickleprojects/spellbound3d only; its id and private key are in SSM (/build/github/app-id,
# /build/github/app-private-key). Needs aws, curl, jq, openssl. Never prints the key or tokens.

# github_app_token: prints a fresh installation token (valid 1 hour) for the App's installation.
github_app_token() {
  local key id now header payload signature jwt installation
  key="$(mktemp)"; chmod 600 "$key"
  aws ssm get-parameter --name /build/github/app-private-key --with-decryption --output json \
    --query Parameter.Value | jq -r . > "$key"
  id="$(aws ssm get-parameter --name /build/github/app-id --query Parameter.Value --output text)"
  b64() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }
  now="$(date +%s)"
  header="$(printf '{"alg":"RS256","typ":"JWT"}' | b64)"
  payload="$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' $((now - 60)) $((now + 540)) "$id" | b64)"
  signature="$(printf '%s.%s' "$header" "$payload" | openssl dgst -sha256 -sign "$key" | b64)"
  rm -f "$key"
  jwt="${header}.${payload}.${signature}"
  installation="$(curl -fsS -H "Authorization: Bearer ${jwt}" -H "Accept: application/vnd.github+json" \
    https://api.github.com/app/installations | jq -r '.[0].id')"
  curl -fsS -X POST -H "Authorization: Bearer ${jwt}" -H "Accept: application/vnd.github+json" \
    "https://api.github.com/app/installations/${installation}/access_tokens" | jq -r .token
}

# github_app_revoke TOKEN: revokes an installation token early.
github_app_revoke() {
  curl -fsS -o /dev/null -X DELETE -H "Authorization: token $1" https://api.github.com/installation/token
}
