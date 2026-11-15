# Part 2: on-demand GitHub runner for spellbound3d (plan)

Built in a normal session as Claude's IAM user, after part 1 is applied. Nothing here
needs godmode: roles go under `/claude/` with the `guardrails/claude-boundary` boundary.

## What the repo needs (found 2026-11-16)

- Repo `stickleprojects/spellbound3d`, private. Every job uses
  `runs-on: ${{ vars.SDLC_RUNNER ... || 'ubuntu-latest' }}`; `SDLC_RUNNER` is `self-hosted`
  today. **Switching runners = changing that one repo variable**; the workflows are synced
  from a template and should not be edited.
- Fork PRs already go to `ubuntu-latest`; the self-hosted runner only gets same-repo work.
- Toolchain is installed by the jobs (`.github/actions/setup-stack`): uv 0.13.4 from PyPI
  into a venv, Godot 4.7.2 (zip cached in `~/.cache/godot`, SHA-512 checked by
  `scripts/install-godot.sh`), then `make setup`. The machine needs `git`, `curl`, `unzip`,
  `make`, `python3` + `python3-venv`.
- Claude jobs use `anthropics/claude-code-action@v1` and `.sdlc/scripts/install-claude.sh`.
- The local runner (`/opt/github-runners/spellbound3d`, user `ghrunner`, labels
  `self-hosted, Linux, X64, spellbound3d`) runs a `job-started.sh` hook that fails a job
  unless its repo and actors are allowlisted and its PR head is not a fork. Port this.

## Design

```
GitHub workflow_job "queued" webhook
  └─> Lambda function URL  (build-runner-webhook; checks the HMAC signature and the job's labels)
        ├─ asks GitHub for a just-in-time (JIT) runner config  (single job, then it unregisters)
        └─ RunInstances from launch template build-runner  (spot, on-demand fallback)
              └─ EC2 boots the baked AMI, runs ./run.sh --jitconfig …, one job, then powers off
                 (instance-initiated shutdown = terminate)
EventBridge Scheduler, every 15 min ─> reaper Lambda: terminates build-runner instances
  older than 90 min, so nothing can run on unnoticed
```

- Label: `aws-spellbound3d` (unique, so the local runner and AWS never compete).
  Cut-over = set `SDLC_RUNNER` to `aws-spellbound3d`; rollback = set it back to `self-hosted`.
- One instance per job; ephemeral, clean every time. Costs ≈ 1–2 min of boot per job;
  if that hurts, add a warm instance later (see [PRICING.md](../PRICING.md), option A5).
- Instance: t3.large, Ubuntu 24.04 x86-64, 30 GiB gp3, IMDSv2 required, no SSH and no
  inbound rules (shell via Session Manager), public IPv4 in a default-VPC public subnet
  (no NAT gateway: the guardrails deny it, and it would cost ≈ US$45/month).
- GitHub credential: a fine-grained PAT, or better a GitHub App, with repository
  *Administration: read & write* on spellbound3d only (that is what JIT runner configs need),
  stored as an SSM SecureString under `/build/github/`. The webhook secret is stored there too.
- Tags: everything `managed-by=build-machine`, so cost reports and the reaper can find it.
- IaC: one CloudFormation stack `build-runner` (Lambda code packaged to an S3 bucket).

## Steps

1. AMI bake script (`part2-runner/ami/`): Ubuntu 24.04 + base packages + Actions runner +
   Godot 4.7.2 pre-placed where `install-godot.sh` looks + the `job-started.sh` guard;
   bake on a t3.large, `CreateImage`, terminate. Check it against the local runner
   first (needs sudo: `/opt/github-runners/spellbound3d`, `/etc/github-runner/`).
2. Store the GitHub credential and webhook secret in SSM.
3. CloudFormation stack: roles (`/claude/`, with the boundary), launch template, security
   group, webhook Lambda + function URL, reaper Lambda + schedule, log groups with 14-day retention.
4. Add the repo webhook (*Workflow jobs* events only) pointing at the function URL.
5. Test with a throwaway workflow on a branch that targets `aws-spellbound3d`, then switch
   `SDLC_RUNNER`. Keep the local runner as the fallback until a week of green runs.

## Open questions

- Is `~/.cache/godot` + the install dir enough for `install-godot.sh` to skip the download,
  or does the AMI also need the export templates?
- Does `claude-code-action` need Node/Bun preinstalled, or does it bring its own?
- GitHub App or PAT for runner registration?
