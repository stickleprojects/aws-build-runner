#!/usr/bin/env bash
# Runner job-started hook (ACTIONS_RUNNER_HOOK_JOB_STARTED).
# Runs before every job on this self-hosted runner, before checkout. A non-zero
# exit fails the job, so none of its steps run.
#
# The job is allowed only when ALL of these hold:
#   - its repository is on the allowed repos list;
#   - the user who started it (GITHUB_ACTOR) and the user who triggered this run,
#     e.g. a re-run (GITHUB_TRIGGERING_ACTOR), are both on the allowlist;
#   - for an event with a pull request, the PR's head branch is in this
#     repository, not in a fork.
# Anything missing or unreadable fails closed.
set -uo pipefail

# One entry per line; '#' comments and blank lines are ignored. Managed by
# runner-guard.sh and installed root-owned by runner-root.sh.
ACTORS_FILE="${RUNNER_HOOK_ACTORS_FILE:-/etc/github-runner/allowed-actors.txt}"
REPOS_FILE="${RUNNER_HOOK_REPOS_FILE:-/etc/github-runner/allowed-repos.txt}"

deny() {
	echo "::error::Runner hook refused this job: $*"
	exit 1
}

read_list() { # read_list FILE ARRAYNAME
	local line
	local -n out="$2"
	out=()
	[ -r "$1" ] || deny "allowlist $1 is not readable"
	while IFS= read -r line || [ -n "$line" ]; do
		line="${line%%#*}"
		line="${line//[[:space:]]/}"
		[ -n "$line" ] && out+=("$line")
	done < "$1"
	[ "${#out[@]}" -gt 0 ] || deny "allowlist $1 is empty"
}

read_list "$ACTORS_FILE" ALLOWED_ACTORS
read_list "$REPOS_FILE" ALLOWED_REPOS

in_array() { # in_array VALUE ITEM...
	local v="$1" a
	shift
	for a in "$@"; do
		[ "$v" = "$a" ] && return 0
	done
	return 1
}
is_allowed_actor() { in_array "$1" "${ALLOWED_ACTORS[@]}"; }

repo="${GITHUB_REPOSITORY:-}"
actor="${GITHUB_ACTOR:-}"
triggering="${GITHUB_TRIGGERING_ACTOR:-$actor}"
event="${GITHUB_EVENT_NAME:-}"
payload="${GITHUB_EVENT_PATH:-}"

[ -n "$repo" ] || deny "GITHUB_REPOSITORY is not set"
[ -n "$actor" ] || deny "GITHUB_ACTOR is not set"
[ -n "$event" ] || deny "GITHUB_EVENT_NAME is not set"
in_array "$repo" "${ALLOWED_REPOS[@]}" || deny "repository '$repo' is not on the allowed repos list"
is_allowed_actor "$actor" || deny "actor '$actor' is not allowed"
is_allowed_actor "$triggering" || deny "triggering actor '$triggering' is not allowed"

[ -n "$payload" ] && [ -r "$payload" ] || deny "event payload GITHUB_EVENT_PATH is not readable"
command -v jq >/dev/null || deny "jq is not installed"
# A payload jq cannot parse must refuse the job, not quietly skip the checks below.
jq -e 'type == "object"' "$payload" >/dev/null 2>&1 || deny "event payload is not a JSON object"

# Any event carrying a pull request (pull_request, pull_request_target,
# pull_request_review, pull_request_review_comment): the head must be this repo.
# A PR from a deleted fork has head.repo = null, which is refused too.
if jq -e 'has("pull_request")' "$payload" >/dev/null; then
	head_repo="$(jq -r '.pull_request.head.repo.full_name // ""' "$payload")"
	# Not "// true": jq's // also replaces false, which would mark every PR a fork.
	is_fork="$(jq -r '.pull_request.head.repo.fork | if . == false then "false" else "true" end' "$payload")"
	[ "$head_repo" = "$repo" ] || deny "pull request head '$head_repo' is not $repo"
	[ "$is_fork" = "false" ] || deny "pull request comes from a fork"
fi

# The user who wrote the comment, for comment-triggered workflows. This is
# normally the same as GITHUB_ACTOR, but it is checked from the payload as well.
commenter="$(jq -r '.comment.user.login // ""' "$payload")"
if [ -n "$commenter" ]; then
	is_allowed_actor "$commenter" || deny "comment author '$commenter' is not allowed"
fi

# The workspace is kept between jobs (so game/.godot stays warm), but the
# pull-request merge refs an earlier job fetched stay with it, and gitleaks in
# `make security` scans every ref. Drop them so a secret on a closed PR can't
# fail every later job. The job's own checkout fetches what it needs again.
ws="${GITHUB_WORKSPACE:-}"
if [ -n "$ws" ] && git -C "$ws" rev-parse --git-dir >/dev/null 2>&1; then
	git -C "$ws" for-each-ref --format='%(refname)' refs/remotes/pull |
		xargs -r -n1 git -C "$ws" update-ref -d ||
		deny "could not drop stale pull-request refs in $ws"
fi

echo "Runner hook: allowed $event by $actor ($triggering) on $repo"
exit 0
