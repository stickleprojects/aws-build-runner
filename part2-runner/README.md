# Part 2: the spellbound3d runner

An on-demand GitHub Actions runner on AWS for `stickleprojects/spellbound3d`. Each queued
job gets its own fresh EC2 instance, which runs that one job and terminates itself.
It is built in a normal Claude session as Claude's IAM user, after
[part 1](../part1-godmode/README.md) has been applied; no godmode needed.

**Design, steps and open questions: [PLAN.md](PLAN.md).**

## In one picture

```
job queued on GitHub ──webhook──> Lambda ──> launcher ──> a spot instance sized by label
                                                             runs one job (just-in-time runner), terminates
every 5 min ──> launcher: catches missed webhooks; reaps runners > 90 min, or idle 20 min
```

- **Switch over**: set the spellbound3d repo variable `SDLC_RUNNER` to `aws-light`.
  **Roll back**: set it back to `self-hosted` (the local runner). The workflows don't change.
- **Image**: Ubuntu 24.04 baked with the Actions runner, Godot 4.7.2 and the local runner's
  `job-started.sh` safety check.
- **Cost**: ≈ US$16–20/month at the busiest observed load (spot), ≈ US$33 on demand only;
  every alternative is priced in [../PRICING.md](../PRICING.md).

## Status

- [x] **AMI bake** (`ami/bake.sh`): versions from spellbound3d's own pins and install scripts; reports through SSM.
- [x] **Smoke test** (`ami/smoke-test.sh`): 23 checks (tools work, security, boot step), plus spellbound3d's Godot smoke test once it's on their main. A new AMI is only a
      *candidate* until it passes; then it becomes current (SSM `/build/runner/ami-id`). Current: `ami-0d1c0c1e4fbf2a0dd`.
- [x] **Image contract**: [ami/IMAGE.md](ami/IMAGE.md) (and `/etc/build-runner/manifest.json` on every instance).
- [ ] **Machine requirements from spellbound3d**: setup.sh installs Node and the OS tools listed in their `tools/runner-smoke/machine-requirements.txt` (format: [ami/MACHINE-REQUIREMENTS.md](ami/MACHINE-REQUIREMENTS.md)). Waits on [#48](https://github.com/stickleprojects/aws-build-runner/issues/48), the user-data limit.
- [x] **Weekly rebuild** (`.github/workflows/ami.yml`): Monday 08:30 Sydney (07:30 in winter: after the curfew), or by hand. Bake → smoke test → promote →
      prune (keep 3) → GitHub Release with IMAGE.md and manifest.json. Role `/claude/build-runner-ami-ci` via OIDC (`ci/ami-ci-role.sh`).
      **The latest image's contract is always the latest [release](https://github.com/stickleprojects/aws-build-runner/releases/latest).**
- [x] **The runner** (`stack/deploy.sh`): webhook and launcher Lambdas, launch template, 5-minute reconcile and reaper,
      at most 4 runners at once. Deployed and tested live (spot launch → runner online on GitHub in ≈ 40 s).
- [x] **Usage per class** (`usage/report.py`, `.github/workflows/usage.yml`, daily 08:00 Sydney): hours and estimated
      cost for light and heavy runners, a weekly summary on the *Runner usage* issue, and an issue assigned to Kieron when
      runners are projected over US$15 with heavy over half the hours ("spellbound3d is shredding your budget"), or
      projected over the US$25 stop. Read-only role `/claude/build-runner-usage` (`ci/usage-role.sh`).
- [x] **Nightly curfew** (`stack/lambda/curfew.py`, `.github/workflows/curfew.yml`, skill `runner-curfew`): no
      runners 22:00-07:00 Sydney. At 21:00 a notice on the *AWS runner curfew* issue mentions Kieron. He replies
      `/extend 1h` or `/extend 2h` (or `/stop`, `/resume`, `/status`), or uses Actions → curfew. No answer: the
      launcher terminates every runner at 22:00 and launches none until 07:00; queued jobs wait. Role
      `/claude/build-runner-curfew` (`ci/curfew-role.sh`) can only read and write `/build/runner/curfew`.
- [ ] **Job fit** (we own the runners, so sizing is ours to watch and suggest): each runner sends one line per job
      (`ami/files/job-usage.py`: CPU, memory, CPU/memory wait) and the webhook records each finished job's wait for a
      runner. The daily report adds a *Job fit* table with notes: too small, CPU-starved, could be light, near the
      90-minute limit, waiting for runners. Needs the next image and a redeploy.
- [ ] spellbound3d: a second label for check jobs (4 vCPU; everything else 2 vCPU), then the webhook, a test
      workflow and the cut-over (PLAN.md steps 4–6).

| Path | Purpose |
|---|---|
| [PLAN.md](PLAN.md) | Design, instance types per job, steps |
| [ami/IMAGE.md](ami/IMAGE.md) | **What the image comes with** (generated from `ami/manifest.json`) |
| `ami/setup.sh` | How the image is built (runs on the bake instance) |
| [ami/MACHINE-REQUIREMENTS.md](ami/MACHINE-REQUIREMENTS.md) | The format of spellbound3d's `machine-requirements.txt` (what `make check` needs from the machine), agreed with spellbound3d |
| `ami/files/` | The local runner's hook and allowlists; our job-completed hook and `job-usage.py` (one usage line per job); the boot step (instance info, job registration, IMDS block, upload cap); the one-job service; the bake reporter |
| `ami/bake.sh`, `ami/bake-role.sh` | Bake an AMI as Claude's user; the bake instance's role can only report, read its one GitHub token, and talk to Systems Manager |
| `ami/smoke-test.sh` | Check a baked AMI; promote the candidate if it passes |
| `lib/github-app.sh` | Short-lived GitHub App tokens (the key stays in SSM) |
| `ami/prune-amis.sh` | Keep the newest 3 AMIs (always the current one); delete the rest and their snapshots |
| `ci/ami-ci-role.sh` | The pipeline's AWS role (Claude creates it: `/claude/`, boundary, tagged) |
| `ci/publish-release.sh` | Publish the current image as a GitHub Release: what changed, then IMAGE.md |
| `stack/deploy.sh` | Deploy or update the runner (`--delete` tears it down); runs `test_lambda.py` first |
| `stack/template.yaml`, `stack/artifacts.yaml` | Stack `build-runner`; stack `build-runner-artifacts` (the Lambda zip bucket) |
| `stack/lambda/runner.py` | The webhook and the launcher (reconcile, launch with spot fallback, reap) |
| `stack/lambda/ghapp.py` | GitHub App sign-in with the standard library only (RS256 JWT) |
| `stack/runner-role.sh` | The instances' role (SSM agent, and writing to `/build/runner/jobs` only); outside the stack, as CloudFormation can't tag instance profiles |
| `stack/config.json` | The stack's settings (labels and their instance types, cap, ages); deploy.sh passes every one |
| `usage/report.py`, `usage/test_report.py` | Daily usage report (cost per class, job fit) and its offline tests |
| `ami/test_job_usage.py` | Offline tests for the per-job usage record and hook |
| `ci/usage-role.sh` | The usage workflow's read-only AWS role |
| `stack/lambda/curfew.py`, `stack/test_curfew.py` | The nightly curfew's rules (shared by the launcher, the workflow and the skill) and their tests |
| `curfew/curfew.py`, `ci/curfew-role.sh` | The curfew by hand (status, extend, stop, resume), and the curfew workflow's AWS role |
| `stack/test_lambda.py` | Offline tests: signatures, filtering, planning, fallback, the lock (CI runs them) |
