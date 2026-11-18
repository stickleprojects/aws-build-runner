#!/usr/bin/env bash
# One-time setup, on Kieron's laptop, of the daily AWS check (daily_check.py), as Kieron (not root). Idempotent.
#
#   part2-runner/laptop/install.sh
#
#   1. Telegram: asks for the "AWS daily check" bot's token (hidden input; from @BotFather /newbot), then waits
#      for you to send the bot any message and takes that chat's ID. Both go in
#      ~/.config/aws-daily-check/notify.env (chmod 600). Skipped when the file already has both.
#   2. Sends a test message.
#   3. A systemd user timer: every day at 09:00 local time; Persistent, so a day the laptop was off runs at next login.
#   4. Runs the check once now.
# It installs a copy of daily_check.py and policy.yaml (for the cost limits) in ~/.local/share/aws-daily-check/.
# Uses the default AWS CLI profile (Claude's read-only user); every call it makes is free (no Cost Explorer).
set -Eeuo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
# The timer runs a copy, not the repo's working tree (a branch switch would change or remove it mid-schedule).
# Run this again after pulling a change to daily_check.py or the cost boundary in policy.yaml.
share="$HOME/.local/share/aws-daily-check"
check="$share/daily_check.py"
mkdir -p "$share"
install -m 755 "$here/daily_check.py" "$check"
install -m 644 "$here/../../part1-godmode/policy.yaml" "$share/policy.yaml"
conf_dir="$HOME/.config/aws-daily-check"
conf="$conf_dir/notify.env"
units="$HOME/.config/systemd/user"

get() { if [[ -f "$conf" ]]; then sed -n "s/^$1=//p" "$conf" | tail -1; fi; }

mkdir -p "$conf_dir" && chmod 700 "$conf_dir"
if [[ -z "$(get AWS_CHECK_TELEGRAM_BOT_TOKEN)" || -z "$(get AWS_CHECK_TELEGRAM_CHAT_ID)" ]]; then
  echo "Telegram: in the Telegram app, message @BotFather, send /newbot, and name it e.g. 'AWS daily check'."
  read -rsp "Paste the bot token it gives you (hidden), then Enter: " token; echo
  [[ "$token" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]] || { echo "That doesn't look like a bot token." >&2; exit 1; }
  ( umask 077; printf '# Daily AWS check (part2-runner/laptop). Private: chmod 600.\nAWS_CHECK_TELEGRAM_BOT_TOKEN=%s\n' "$token" > "$conf" )
  echo "Now open your new bot in Telegram and send it any message (e.g. hi). Waiting up to 3 minutes..."
  chat=""
  for _ in $(seq 1 36); do
    chat="$(python3 -I "$check" --telegram-chats | awk '$2 == "private" {print $1}' | tail -1)"
    [[ -n "$chat" ]] && break
    sleep 5
  done
  [[ -n "$chat" ]] || { echo "No message reached the bot; run this again." >&2; exit 1; }
  ( umask 077; printf 'AWS_CHECK_TELEGRAM_CHAT_ID=%s\n' "$chat" >> "$conf" )
  chmod 600 "$conf"
  echo "chat ${chat} saved in ${conf}"
fi

python3 -I - "$conf" <<'EOF'
import json, sys, urllib.parse, urllib.request
c = dict(l.split("=", 1) for l in open(sys.argv[1]).read().splitlines() if "=" in l and not l.startswith("#"))
data = urllib.parse.urlencode({"chat_id": c["AWS_CHECK_TELEGRAM_CHAT_ID"],
                               "text": "AWS daily check is set up: a report each day at 09:00."}).encode()
urllib.request.urlopen(f"https://api.telegram.org/bot{c['AWS_CHECK_TELEGRAM_BOT_TOKEN']}/sendMessage", data=data, timeout=20)
print("test message sent")
EOF

mkdir -p "$units"
cat > "$units/aws-daily-check.service" <<EOF
[Unit]
Description=Daily AWS cost and guardrail check (aws-build-runner part2-runner/laptop)

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -I ${check}
# An ALERT exits 1 on purpose; it's the report (already sent), not a fault of the service.
SuccessExitStatus=1
EOF
cat > "$units/aws-daily-check.timer" <<'EOF'
[Unit]
Description=Daily AWS cost and guardrail check at 09:00

[Timer]
OnCalendar=*-*-* 09:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now aws-daily-check.timer
systemctl --user start aws-daily-check.service
echo "installed; next run: $(systemctl --user list-timers aws-daily-check.timer --no-legend | awk '{print $1, $2, $3}')"
echo "log: ~/.local/state/aws-daily-check/log"
