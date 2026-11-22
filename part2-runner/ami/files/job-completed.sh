#!/usr/bin/env bash
# Runner job-completed hook (ACTIONS_RUNNER_HOOK_JOB_COMPLETED), as ghrunner, after every job.
# Notes which job this was, for job-usage.py (root, at power-off) to report with the job's CPU and
# memory use. Never fails the job: any error is ignored.
set -u
mkdir -p /run/build-runner 2>/dev/null
# ready: start-jit.sh touched /run/build-runner/job just before the runner started.
# started: the job-started hook's note of when this job really began (falls back to ready).
ready="$(stat -c %Y /run/build-runner/job 2>/dev/null || echo "")"
started="$(cat /run/build-runner/job-started 2>/dev/null || echo "")"
[[ "$started" =~ ^[0-9]+$ ]] || started="$ready"
jq -n --arg repository "${GITHUB_REPOSITORY:-}" --arg workflow "${GITHUB_WORKFLOW:-}" --arg job "${GITHUB_JOB:-}" \
      --arg run_id "${GITHUB_RUN_ID:-}" --arg run_attempt "${GITHUB_RUN_ATTEMPT:-}" \
      --arg event "${GITHUB_EVENT_NAME:-}" --arg ready "$ready" --arg started "$started" --arg ended "$(date +%s)" \
  '{repository: $repository, workflow: $workflow, job: $job, run_id: $run_id, run_attempt: $run_attempt,
    event: $event, ready: $ready, started: $started, ended: $ended}' > /run/build-runner/job-info.json 2>/dev/null || true
exit 0
