#!/usr/bin/env bash
# Sets up Claude's own GitHub identity on this laptop (#23), for this repo only. Run once, as Kieron:
#
#   part1-godmode/claude-identity/install.sh <APP_ID>
#
# Needs the App's key at ~/.config/claude-github-app/key.pem (chmod 600). It:
#   - installs claude-github-token and claude-gh into ~/.local/bin
#   - writes ~/.config/claude-github-app/app.env (the App id and this repo; not secret)
#   - in this clone only: pushes go over HTTPS with the App's token (not Kieron's SSH key), and commits are
#     authored as the App's bot user
# Prints the bot identity and a token check. Never prints the key or a token.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo_dir="$(git -C "$here" rev-parse --show-toplevel)"
app_id="${1:-}"
[[ "$app_id" =~ ^[0-9]+$ ]] || { echo "usage: $0 <APP_ID> (the number on the App's settings page)" >&2; exit 1; }
dir="${HOME}/.config/claude-github-app"
[[ -f "$dir/key.pem" ]] || { echo "Put the App's private key at $dir/key.pem first." >&2; exit 1; }
chmod 700 "$dir"; chmod 600 "$dir/key.pem"

mkdir -p "${HOME}/.local/bin"
install -m 755 "$here/claude-github-token" "$here/claude-gh" "${HOME}/.local/bin/"
printf 'APP_ID=%s\nREPO=%s\n' "$app_id" "stickleprojects/aws-build-runner" > "$dir/app.env"
chmod 600 "$dir/app.env"
rm -f "${HOME}/.cache/claude-github-app/token"

"${HOME}/.local/bin/claude-github-token" >/dev/null && echo "token: ok (installation token minted)"
slug="$("${HOME}/.local/bin/claude-gh" api /repos/stickleprojects/aws-build-runner --jq .full_name)"
echo "repo access: ${slug}"
bot_login="stickleprojects-claude[bot]"
bot_id="$(gh api "/users/stickleprojects-claude%5Bbot%5D" --jq .id)"

git -C "$repo_dir" remote set-url --push origin https://github.com/stickleprojects/aws-build-runner.git
# shellcheck disable=SC2016 # git runs the helper later; it must stay unexpanded here
git -C "$repo_dir" config --local credential.https://github.com.helper \
  '!f() { test "$1" = get && echo username=x-access-token && echo "password=$(claude-github-token)"; }; f'
git -C "$repo_dir" config --local user.name "$bot_login"
git -C "$repo_dir" config --local user.email "${bot_id}+${bot_login}@users.noreply.github.com"
echo "this clone: pushes as the App over HTTPS; commits by ${bot_login}"
