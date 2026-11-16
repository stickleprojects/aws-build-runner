#!/usr/bin/env bash
# Builds the runner image. Runs once, as root, on the bake instance (cloud-init runcmd), with the
# files from files/ already written to /opt/bake/files. Ends by printing BAKE-OK (or BAKE-FAILED)
# to the console and powering off; bake.sh then makes the AMI.
#
# Layout matches the local runner: /opt/github-runners/spellbound3d, user ghrunner, HOME inside it,
# the job-started hook and allowlists in /etc/github-runner. Every download is pinned and checked.
#
#   BAKE_IN_CONTAINER=1 ./setup.sh   test run in a plain ubuntu:24.04 container (no systemd, no power off)
set -Eeuo pipefail

RUNNER_VERSION=2.338.2
RUNNER_SHA256=baebf204307bac6193480852732f0aba2ae97b6c5597c244f4d5558390147c40
# Must match stickleprojects/spellbound3d tools/godot/version.env, or jobs download Godot again.
GODOT_VERSION=4.7.2
GODOT_SHA512=9aa00f7a605200940bce3027a567b782f49bd8e940dd06ae9e987bd65aee1b1467edd56ed84fcdcbdd44354bf613bdbb4e5d2913e925850368e150c59ed54c65

R=/opt/github-runners/spellbound3d
FILES=/opt/bake/files
IN_CONTAINER="${BAKE_IN_CONTAINER:-}"

# EC2 captures the serial port (ttyS0) as the console output bake.sh reads.
say() {
  echo "bake: $*"
  [[ -n "$IN_CONTAINER" ]] || { echo "bake: $*" > /dev/ttyS0; echo "bake: $*" > /dev/console; } 2>/dev/null || true
}
# Reports to SSM (/build/bake/result/<instance-id>) through the bake role; bake.sh reads it.
# Runs from /run: the clean-up deletes /opt/bake before the final report.
REPORTER=/run/bake-report.py
[[ -n "$IN_CONTAINER" ]] || cp "$FILES/report.py" "$REPORTER"
report() { [[ -n "$IN_CONTAINER" ]] || python3 "$REPORTER" "$@" || true; }
finish() {
  local result="$1"
  say "$result"
  if [[ "$result" == BAKE-OK ]]; then report OK; else report FAILED /var/log/cloud-init-output.log; fi
  [[ -n "$IN_CONTAINER" ]] && exit "$([[ "$result" == BAKE-OK ]] && echo 0 || echo 1)"
  # Give EC2's console capture time to pick up the result before the power goes.
  sync; sleep 20; poweroff
}
trap 'finish "BAKE-FAILED at line $LINENO"' ERR
report STARTED

say "packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
[[ -n "$IN_CONTAINER" ]] || apt-get -y -q upgrade
# git/curl/jq/unzip/make/python3-venv: the stack (setup-stack, Makefile, the hook);
# nodejs/npm: install-claude.sh (npm, per job); build-essential: native npm/pip builds;
# libfontconfig1: Godot needs it even headless (the container test showed the error without it).
apt-get install -y -q --no-install-recommends ca-certificates curl git jq unzip zip make \
  python3 python3-venv python3-pip nodejs npm build-essential sudo libfontconfig1

say "user ghrunner, home $R/home"
mkdir -p /opt/github-runners
id ghrunner >/dev/null 2>&1 || useradd --system --create-home --home-dir "$R/home" --shell /bin/bash ghrunner
mkdir -p "$R"

say "GitHub Actions runner $RUNNER_VERSION"
curl -fsSL --retry 3 -o /tmp/runner.tgz \
  "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
echo "${RUNNER_SHA256}  /tmp/runner.tgz" | sha256sum -c --quiet
tar xzf /tmp/runner.tgz -C "$R"
rm -f /tmp/runner.tgz
"$R/bin/installdependencies.sh" >/dev/null
cat > "$R/.env" <<EOF
LANG=en_US.UTF-8
HOME=$R/home
NPM_CONFIG_PREFIX=$R/home/.local
ACTIONS_RUNNER_HOOK_JOB_STARTED=/etc/github-runner/job-started.sh
EOF
echo "$R/home/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" > "$R/.path"

say "Godot $GODOT_VERSION, where spellbound3d's install-godot.sh looks for it"
name="Godot_v${GODOT_VERSION}-stable_linux.x86_64"
cache="$R/home/.cache/godot"
dest="$R/home/.local/opt/godot-${GODOT_VERSION}"
mkdir -p "$cache" "$dest"
curl -fsSL --retry 3 -o "$cache/$name.zip" \
  "https://github.com/godotengine/godot/releases/download/${GODOT_VERSION}-stable/$name.zip"
echo "${GODOT_SHA512}  $cache/$name.zip" | sha512sum -c --quiet
unzip -q -o "$cache/$name.zip" -d "$dest"
chmod +x "$dest/$name"
ln -sfn "$name" "$dest/godot"
echo "$GODOT_SHA512" > "$dest/.installed"

say "job-started hook and allowlists (copied from the local runner)"
install -d -m 755 /etc/github-runner
install -m 755 "$FILES/job-started.sh" /etc/github-runner/job-started.sh
install -m 644 "$FILES/allowed-actors.txt" /etc/github-runner/allowed-actors.txt
install -m 644 "$FILES/allowed-repos.txt" /etc/github-runner/allowed-repos.txt

say "one-job service"
install -m 755 "$FILES/start-jit.sh" /opt/github-runners/start-jit.sh
install -m 644 "$FILES/github-runner-jit.service" /etc/systemd/system/github-runner-jit.service
[[ -n "$IN_CONTAINER" ]] || systemctl enable github-runner-jit.service

chown -R ghrunner:ghrunner "$R"

say "checks"
sudo -u ghrunner -H "$dest/godot" --headless --version
sudo -u ghrunner -H bash -c "cd '$R' && ./config.sh --version"
node --version; npm --version; python3 --version; jq --version
bash -n /etc/github-runner/job-started.sh

if [[ -z "$IN_CONTAINER" ]]; then
  say "clean up for imaging"
  apt-get clean
  rm -rf /opt/bake /var/lib/apt/lists/* /tmp/* /var/tmp/*
  cloud-init clean --logs
  truncate -s 0 /etc/machine-id
fi
finish BAKE-OK
