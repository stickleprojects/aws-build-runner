# shellcheck shell=bash
# require_account: stop unless the current AWS credentials are in the target account (#98). With two accounts
# during the move (#17), a profile left pointing at the wrong one would deploy or change things there; a second
# launcher on the same labels would give one job two runners. The target is part1-godmode/config.py's
# (AWS_TARGET, its env file, or AWS_ACCOUNT_ID in the environment, as in CI). Source it, then call it.
require_account() {
  python3 -I "$(dirname "${BASH_SOURCE[0]}")/../../part1-godmode/config.py" --check-account >&2
}
