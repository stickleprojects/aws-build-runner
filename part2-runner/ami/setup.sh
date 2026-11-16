#!/usr/bin/env bash
# Builds the runner image. Runs once, as root, on the bake instance (cloud-init runcmd), with the
# files from files/ already in /opt/bake/files. Reports STARTED, then OK (with the manifest) or FAILED
# (with its log) to SSM via files/report.py, and powers off; bake.sh then makes the AMI.
#
# Versions come from spellbound3d itself, so the image always matches what its CI pins: the bake
# checks out spellbound3d's main (a short-lived, read-only GitHub App token that bake.sh puts in SSM
# /build/bake/github-token) and runs spellbound3d's own install scripts as ghrunner.
#
# Layout matches the local runner: /opt/github-runners/spellbound3d, user ghrunner, HOME inside it, the
# job-started hook and allowlists in /etc/github-runner. Tools are also on the PATH for everyone.
#
#   BAKE_IN_CONTAINER=1 GITHUB_TOKEN=... ./setup.sh   test run in a plain ubuntu:24.04 container
set -Eeuo pipefail

R=/opt/github-runners/spellbound3d
H="$R/home"
FILES=/opt/bake/files
SB3D=/opt/bake/spellbound3d
MANIFEST=/etc/build-runner/manifest.json
IN_CONTAINER="${BAKE_IN_CONTAINER:-}"
# Reports through the bake role; runs from /run because the clean-up deletes /opt/bake first.
REPORTER=/run/bake-report.py
[[ -n "$IN_CONTAINER" ]] || cp "$FILES/report.py" "$REPORTER"

say() { echo "bake: $*"; }
report() { [[ -n "$IN_CONTAINER" ]] || python3 "$REPORTER" "$@" || true; }
as_runner() { sudo -u ghrunner -H env HOME="$H" PATH="$H/.local/bin:/usr/local/bin:/usr/bin:/bin" bash -c "$1"; }
finish() {
  local result="$1"
  say "$result"
  if [[ "$result" == BAKE-OK ]]; then report OK "$MANIFEST"; else report FAILED /var/log/cloud-init-output.log; fi
  [[ -n "$IN_CONTAINER" ]] && exit "$([[ "$result" == BAKE-OK ]] && echo 0 || echo 1)"
  sync; poweroff
}
trap 'finish "BAKE-FAILED at line $LINENO: $BASH_COMMAND"' ERR
report STARTED

say "packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
[[ -n "$IN_CONTAINER" ]] || apt-get -y -q upgrade
# git/curl/jq/unzip/make/python3-venv: the stack (setup-stack, Makefile, the hook); nodejs/npm: Claude Code;
# build-essential: native npm/pip builds; libfontconfig1: Godot needs it even headless; iptables: the
# boot step blocks jobs from the instance metadata service.
apt-get install -y -q --no-install-recommends ca-certificates curl git jq unzip zip make \
  python3 python3-venv python3-pip nodejs npm build-essential sudo libfontconfig1 iptables

say "user ghrunner, home $H"
mkdir -p /opt/github-runners /etc/build-runner
id ghrunner >/dev/null 2>&1 || useradd --system --create-home --home-dir "$H" --shell /bin/bash ghrunner
mkdir -p "$R"

say "spellbound3d main (for its pinned versions and install scripts)"
token="${GITHUB_TOKEN:-}"
[[ -n "$token" ]] || token="$(python3 "$REPORTER" --get /build/bake/github-token)"
rm -rf "$SB3D"
git clone --quiet --depth 1 "https://x-access-token:${token}@github.com/stickleprojects/spellbound3d.git" "$SB3D"
unset token
git -C "$SB3D" remote set-url origin https://github.com/stickleprojects/spellbound3d.git
sb3d_commit="$(git -C "$SB3D" rev-parse HEAD)"
chown -R ghrunner:ghrunner "$SB3D"
# shellcheck disable=SC1091
. "$SB3D/tools/godot/version.env"
CLAUDE_CODE_VERSION="$(sed -nE 's/.*CLAUDE_CODE_VERSION:-([0-9][0-9.]*)\}.*/\1/p' "$SB3D/.sdlc/scripts/install-claude.sh" | head -1)"
UV_VERSION="$(sed -nE 's/^ *UV_VERSION: *"?([0-9][0-9.]*)"?.*/\1/p' "$SB3D/.github/actions/setup-stack/action.yml" | head -1)"
[[ -n "${GODOT_VERSION:-}" && -n "$CLAUDE_CODE_VERSION" && -n "$UV_VERSION" ]]
say "pins from spellbound3d ${sb3d_commit:0:12}: Godot ${GODOT_VERSION}, Claude Code ${CLAUDE_CODE_VERSION}, uv ${UV_VERSION}"

say "GitHub Actions runner (latest release, SHA-256 checked)"
release="$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest)"
RUNNER_VERSION="$(jq -r '.tag_name | ltrimstr("v")' <<<"$release")"
asset="actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
RUNNER_SHA256="$(jq -r --arg a "$asset" '.assets[] | select(.name == $a) | .digest | ltrimstr("sha256:")' <<<"$release")"
[[ "$RUNNER_SHA256" =~ ^[0-9a-f]{64}$ ]]
curl -fsSL --retry 3 -o /tmp/runner.tgz "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${asset}"
echo "${RUNNER_SHA256}  /tmp/runner.tgz" | sha256sum -c --quiet
tar xzf /tmp/runner.tgz -C "$R"
rm -f /tmp/runner.tgz
"$R/bin/installdependencies.sh" >/dev/null
cat > "$R/.env" <<EOF
LANG=en_US.UTF-8
HOME=$H
NPM_CONFIG_PREFIX=$H/.local
ACTIONS_RUNNER_HOOK_JOB_STARTED=/etc/github-runner/job-started.sh
EOF
echo "$H/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" > "$R/.path"
chown -R ghrunner:ghrunner "$R"

say "Godot ${GODOT_VERSION}: spellbound3d's install-godot.sh, as ghrunner; also /usr/local/bin/godot"
as_runner "cd '$SB3D' && scripts/install-godot.sh"
ln -sfn "$H/.local/opt/godot-${GODOT_VERSION}/godot" /usr/local/bin/godot

say "uv ${UV_VERSION}: /usr/local/bin/uv, plus ghrunner's pip cache for setup-stack's own install"
python3 -m venv /opt/uv
/opt/uv/bin/pip install --quiet --disable-pip-version-check "uv==${UV_VERSION}"
ln -sfn /opt/uv/bin/uv /usr/local/bin/uv
ln -sfn /opt/uv/bin/uvx /usr/local/bin/uvx
as_runner "python3 -m venv /tmp/uv-warm && /tmp/uv-warm/bin/pip install --quiet --disable-pip-version-check 'uv==${UV_VERSION}'; rm -rf /tmp/uv-warm"

say "Python tools from spellbound3d's uv.lock (warms ghrunner's uv cache)"
as_runner "cd '$SB3D' && uv sync --frozen --quiet"

say "gitleaks: spellbound3d's install-gitleaks.sh, as ghrunner; also /usr/local/bin/gitleaks"
as_runner "cd '$SB3D' && scripts/install-gitleaks.sh"
ln -sfn "$H/.local/bin/gitleaks" /usr/local/bin/gitleaks

say "Claude Code ${CLAUDE_CODE_VERSION}: global, plus ghrunner's npm cache for install-claude.sh"
npm install -g --no-fund --no-audit --loglevel=error "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" >/dev/null
# A real throwaway install, not 'npm cache add': only an install also caches the platform-native binary
# Claude Code pulls in, so install-claude.sh can install offline (the smoke test caught this).
as_runner "npm install --prefix /tmp/claude-warm --no-fund --no-audit --loglevel=error '@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}' >/dev/null; rm -rf /tmp/claude-warm"

say "job-started hook and allowlists (copied from the local runner)"
install -d -m 755 /etc/github-runner
install -m 755 "$FILES/job-started.sh" /etc/github-runner/job-started.sh
install -m 644 "$FILES/allowed-actors.txt" /etc/github-runner/allowed-actors.txt
install -m 644 "$FILES/allowed-repos.txt" /etc/github-runner/allowed-repos.txt

say "boot step and one-job service"
install -m 755 "$FILES/build-runner-boot.sh" /opt/github-runners/build-runner-boot.sh
install -m 755 "$FILES/start-jit.sh" /opt/github-runners/start-jit.sh
install -m 644 "$FILES/build-runner-boot.service" /etc/systemd/system/build-runner-boot.service
install -m 644 "$FILES/github-runner-jit.service" /etc/systemd/system/github-runner-jit.service
[[ -n "$IN_CONTAINER" ]] || systemctl enable build-runner-boot.service github-runner-jit.service

chown -R ghrunner:ghrunner "$H"

say "manifest: what's actually installed"
v() { as_runner "$1" 2>/dev/null | head -1 | tr -d '\r' || echo "missing"; }
base_ami=container
if [[ -z "$IN_CONTAINER" ]]; then
  base_ami="$(curl -sf -H "X-aws-ec2-metadata-token: $(curl -sf -X PUT http://169.254.169.254/latest/api/token \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')" http://169.254.169.254/latest/meta-data/ami-id || echo unknown)"
fi
jq -n -c \
  --arg built "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg base "$base_ami" \
  --arg os "$(sed -n 's/^PRETTY_NAME="\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' /etc/os-release)" --arg sb3d "$sb3d_commit" --arg runner "$RUNNER_VERSION" \
  --arg godot "$(v 'godot --headless --version')" --arg claude "$(v 'claude --version')" \
  --arg uv "$(v 'uv --version')" --arg gitleaks "$(v 'gitleaks version')" \
  --arg node "$(v 'node --version')" --arg npm "$(v 'npm --version')" \
  --arg python "$(v 'python3 --version')" --arg git "$(v 'git --version')" --arg jq "$(v 'jq --version')" \
  '{built: $built, base_ami: $base, os: $os, spellbound3d_commit: $sb3d,
    runner: {version: $runner, dir: "/opt/github-runners/spellbound3d", user: "ghrunner",
             home: "/opt/github-runners/spellbound3d/home"},
    tools: {godot: $godot, claude_code: $claude, uv: $uv, gitleaks: $gitleaks, node: $node, npm: $npm,
            python: $python, git: $git, jq: $jq},
    on_path: ["godot", "claude", "uv", "uvx", "gitleaks", "node", "npm", "python3", "git", "jq", "make"],
    caches: ["~/.cache/godot (Godot zip)", "~/.local/opt/godot-<ver>", "~/.cache/uv (uv.lock)",
             "~/.cache/pip (uv wheel)", "~/.npm (Claude Code)"],
    not_included: ["Godot import cache (game/.godot): every job imports cold", "sudo for jobs", "Docker",
                   "AWS credentials for jobs (IMDS is blocked for ghrunner)"]}' > "$MANIFEST"
chmod 644 "$MANIFEST"
cat "$MANIFEST"; echo
if grep -q '"missing"' "$MANIFEST"; then say "a tool is missing from the manifest"; false; fi

if [[ -z "$IN_CONTAINER" ]]; then
  say "clean up for imaging"
  rm -rf "$SB3D" /opt/bake
  apt-get clean
  rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*
  cloud-init clean --logs
  truncate -s 0 /etc/machine-id
fi
finish BAKE-OK
