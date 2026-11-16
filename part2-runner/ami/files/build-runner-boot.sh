#!/usr/bin/env bash
# Runs as root at every boot, before the runner (build-runner-boot.service):
#   1. /etc/build-runner/instance.json: this instance's type, vCPUs and memory, so jobs can size themselves
#   2. the job's just-in-time registration, from user data, to /run/build-runner/jitconfig (ghrunner, 0600)
#   3. blocks the ghrunner user from the instance metadata service (IMDS), so job code can't read the
#      user data or any instance-role credentials. Root (e.g. the Systems Manager agent) keeps access.
set -euo pipefail

# Backstop first, before anything can fail: this instance powers off (and is terminated) within 2 hours.
/usr/sbin/shutdown -h +120 "build-runner: 2-hour limit" || true

imds() {
  local token
  token="$(curl -sf -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')" || return 1
  curl -sf -H "X-aws-ec2-metadata-token: $token" "http://169.254.169.254/latest/$1"
}

mkdir -p /etc/build-runner
jq -n --arg type "$(imds meta-data/instance-type || echo unknown)" \
      --arg id "$(imds meta-data/instance-id || echo unknown)" \
      --argjson nproc "$(nproc)" \
      --argjson mem_mb "$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)" \
  '{instance_type: $type, instance_id: $id, nproc: $nproc, memory_mb: $mem_mb}' > /etc/build-runner/instance.json
chmod 644 /etc/build-runner/instance.json

install -d -m 750 -o ghrunner -g ghrunner /run/build-runner
jit="$(imds user-data 2>/dev/null | jq -r '.jitconfig // empty' 2>/dev/null || true)"
if [[ -n "$jit" ]]; then
  install -m 600 -o ghrunner -g ghrunner /dev/null /run/build-runner/jitconfig
  printf '%s' "$jit" > /run/build-runner/jitconfig
fi

# IPv4 and IPv6 metadata endpoints. -C first, so a restart doesn't add the rule twice.
for rule in "iptables -d 169.254.169.254" "ip6tables -d fd00:ec2::254"; do
  read -r cmd dflag addr <<<"$rule"
  "$cmd" -C OUTPUT -m owner --uid-owner ghrunner "$dflag" "$addr" -j REJECT 2>/dev/null \
    || "$cmd" -A OUTPUT -m owner --uid-owner ghrunner "$dflag" "$addr" -j REJECT
done
echo "build-runner boot: $(cat /etc/build-runner/instance.json); jitconfig $([[ -n "$jit" ]] && echo present || echo none); IMDS blocked for ghrunner"
