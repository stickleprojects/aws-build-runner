#!/usr/bin/env bash
# Starts the one job this instance exists for, then the instance powers off (and, launched with
# shutdown behaviour "terminate", is deleted). Run by github-runner-jit.service as ghrunner.
#
# The webhook Lambda launches the instance with user data {"jitconfig": "<base64>"}: a
# just-in-time runner registration from GitHub, good for exactly one job. Without it (e.g. while
# baking or testing the AMI) this does nothing.
set -uo pipefail
R=/opt/github-runners/spellbound3d

imds() {
  local token
  token="$(curl -sf -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')" || return 1
  curl -sf -H "X-aws-ec2-metadata-token: $token" "http://169.254.169.254/latest/$1"
}

jit="$(imds user-data 2>/dev/null | jq -r '.jitconfig // empty' 2>/dev/null)"
if [[ -z "$jit" ]]; then
  echo "No jitconfig in user data: not starting a runner."
  exit 0
fi

# Tells the service there was a job, so it powers off afterwards (without one it doesn't, for testing).
touch /run/build-runner/job
cd "$R" || exit 1
# run.sh exits when its single job is done (or if registration fails); either way, we're finished.
./run.sh --jitconfig "$jit"
echo "Runner exited with $?; powering off."
