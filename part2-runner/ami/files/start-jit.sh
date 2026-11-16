#!/usr/bin/env bash
# Starts the one job this instance exists for, then the instance powers off (and, launched with
# shutdown behaviour "terminate", is deleted). Run by github-runner-jit.service as ghrunner.
#
# build-runner-boot.sh (root, earlier) put the job's just-in-time runner registration in
# /run/build-runner/jitconfig and blocked ghrunner from the instance metadata service, so this
# script and the job never touch IMDS. No file (e.g. while baking or testing the image): do nothing.
set -uo pipefail
R=/opt/github-runners/spellbound3d
JIT=/run/build-runner/jitconfig

if [[ ! -s "$JIT" ]]; then
  echo "No jitconfig in user data: not starting a runner."
  exit 0
fi
jit="$(cat "$JIT")"
rm -f "$JIT"   # single use; don't leave it lying around for the job

# Tells the service there was a job, so it powers off afterwards (without one it doesn't, for testing).
touch /run/build-runner/job
cd "$R" || exit 1
# run.sh exits when its single job is done (or if registration fails); either way, we're finished.
./run.sh --jitconfig "$jit"
echo "Runner exited with $?; powering off."
