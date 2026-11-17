#!/usr/bin/env bash
# Runs as root at every boot, before the runner (build-runner-boot.service):
#   1. /etc/build-runner/instance.json: this instance's type, vCPUs and memory, so jobs can size themselves,
#      and the label and class it was launched for (user data), for the per-job usage record
#   2. the job's just-in-time registration, from user data, to /run/build-runner/jitconfig (ghrunner, 0600)
#   3. blocks the ghrunner user from the instance metadata service (IMDS), so job code can't read the
#      user data or any instance-role credentials. Root (e.g. the Systems Manager agent) keeps access.
#   4. caps what ghrunner sends off the machine at about 20 Mbps, so a job can't run up a data-transfer bill
set -euo pipefail

# Backstop first, before anything can fail: this instance powers off (and is terminated) within 2 hours.
/usr/sbin/shutdown -h +120 "build-runner: 2-hour limit" || true

imds() {
  local token
  token="$(curl -sf -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')" || return 1
  curl -sf -H "X-aws-ec2-metadata-token: $token" "http://169.254.169.254/latest/$1"
}

mkdir -p /etc/build-runner
userdata="$(imds user-data 2>/dev/null || true)"
# Label and class: short, plain words only (the launcher sets them; anything else becomes "unknown").
word() { local v; v="$(jq -r --arg k "$1" '.[$k] // empty' 2>/dev/null <<<"$userdata" || true)"; [[ "$v" =~ ^[A-Za-z0-9._-]{1,40}$ ]] && echo "$v" || echo unknown; }
jq -n --arg label "$(word label)" --arg class "$(word class)" --arg type "$(imds meta-data/instance-type || echo unknown)" \
      --arg id "$(imds meta-data/instance-id || echo unknown)" \
      --argjson nproc "$(nproc)" \
      --argjson mem_mb "$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)" \
  '{instance_type: $type, instance_id: $id, nproc: $nproc, memory_mb: $mem_mb, label: $label, class: $class}' > /etc/build-runner/instance.json
chmod 644 /etc/build-runner/instance.json

install -d -m 750 -o ghrunner -g ghrunner /run/build-runner
jit="$(jq -r '.jitconfig // empty' 2>/dev/null <<<"$userdata" || true)"
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

# 4. Egress cap: everything ghrunner (the runner and its jobs) sends off the machine shares one bucket of
# EGRESS_KBPS. Data out of AWS costs US$0.114/GB, so an uncapped runner could cost ≈US$250/h; capped, at most
# ≈US$1/h each (#30). Downloads (inbound) are free and not capped. Jobs have no sudo, so they can't lift it.
EGRESS_KBPS=2500   # KiB/s, ≈ 20 Mbps
for cmd in iptables ip6tables; do
  cap=(OUTPUT ! -o lo -m owner --uid-owner ghrunner -m hashlimit --hashlimit-name "egress-${cmd}"
       --hashlimit-above "${EGRESS_KBPS}kb/s" --hashlimit-burst "$((EGRESS_KBPS * 2))kb" -j DROP)
  "$cmd" -C "${cap[@]}" 2>/dev/null || "$cmd" -A "${cap[@]}"
done
echo "build-runner boot: $(cat /etc/build-runner/instance.json); jitconfig $([[ -n "$jit" ]] && echo present || echo none); IMDS blocked for ghrunner; egress capped at ${EGRESS_KBPS} KiB/s"
