---
name: runner-curfew
description: Check or change the AWS runners' nightly curfew (22:00-07:00 Sydney). Use when Kieron says "stop the runners", "lights off", "kill the runners now", "extend the runners 1h/2h", "keep the runners on", "resume the runners", or asks whether the runners are on.
---

# Runner curfew

AWS runners for spellbound3d stop every night from 22:00 to 07:00 Sydney (`part2-runner/stack/lambda/curfew.py`).
At 21:00 a notice goes to Kieron on the *AWS runner curfew* issue. If he doesn't extend, the launcher
terminates every runner at 22:00 and launches none; queued jobs wait and start at 07:00. This skill does the
same things by hand, as Claude's IAM user, through one SSM parameter (`/build/runner/curfew`).

Run from the repo root, in Sydney, as the default CLI profile:

| Kieron says | Run |
|---|---|
| "are the runners on?" | `python3 -I part2-runner/curfew/curfew.py status` |
| "extend 1h" / "extend 2h" | `python3 -I part2-runner/curfew/curfew.py extend 1` (or `2`) |
| "stop / kill the runners now", "lights off" | `python3 -I part2-runner/curfew/curfew.py stop` |
| "turn the runners back on" | `python3 -I part2-runner/curfew/curfew.py resume`, but during the quiet hours `resume` only clears overrides and they stay off; use `extend 1` or `extend 2` |

- **Extending:** 1 or 2 hours at a time, past tonight's stop, or from now if they've already stopped. Never past
  07:00. Each extension gets its own notice an hour before the new stop. Repeat it to go longer.
- **Stopping:** `stop` lasts until 07:00. The launcher acts on its next run, within 5 minutes: it terminates every
  runner and the jobs on them fail. Before stopping while jobs are running, tell Kieron which jobs will be killed
  (`gh run list -R stickleprojects/spellbound3d --status in_progress`), and ask him to confirm.
- **After each command:** quote the line the script prints (it says when runners next stop or start) back to
  Kieron, as Sydney time.
- **Checking it took effect:** `aws ec2 describe-instances --filters Name=tag:build-runner-role,Values=runner
  Name=instance-state-name,Values=pending,running --query 'length(Reservations[].Instances[])'` should be 0
  within 5 minutes of a stop.
- **Other ways in:** Kieron can do the same from his phone. He comments `/extend 1h`, `/extend 2h`, `/stop`,
  `/resume` or `/status` on the curfew issue, or uses Actions → curfew → Run workflow. Only his comments count.
- **Changing the hours** (22:00 / 07:00 / Sydney) is a design change: give Kieron the options with prices,
  then edit `Curfew` in `part2-runner/stack/config.json` and run `part2-runner/stack/deploy.sh`.
