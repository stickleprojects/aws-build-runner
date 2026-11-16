# Part 2: the spellbound3d runner

An on-demand GitHub Actions runner on AWS for `stickleprojects/spellbound3d`. Each queued
job gets its own fresh EC2 instance, which runs that one job and terminates itself.
It is built in a normal Claude session as Claude's IAM user, after
[part 1](../part1-godmode/README.md) has been applied; no godmode needed.

**Design, steps and open questions: [PLAN.md](PLAN.md).**

## In one picture

```
job queued on GitHub ──webhook──> Lambda ──> spot t3.large from a baked AMI
                                               runs one job (just-in-time runner), then terminates
reaper every 15 min ──> terminates anything older than 90 min
```

- **Switch over**: set the spellbound3d repo variable `SDLC_RUNNER` to `aws-spellbound3d`.
  **Roll back**: set it back to `self-hosted` (the local runner). The workflows don't change.
- **Image**: Ubuntu 24.04 baked with the Actions runner, Godot 4.7.2 and the local runner's
  `job-started.sh` safety check.
- **Cost**: ≈ US$16–20/month at the busiest observed load (spot), ≈ US$33 on demand only;
  every alternative is priced in [../PRICING.md](../PRICING.md).

## Status

- [x] **AMI bake** (`ami/bake.sh`): versions from spellbound3d's own pins and install scripts; reports through SSM.
- [x] **Smoke test** (`ami/smoke-test.sh`): 25 checks (tools work, security, boot step). A new AMI is only a
      *candidate* until it passes; then it becomes current (SSM `/build/runner/ami-id`). Current: `ami-0d1c0c1e4fbf2a0dd`.
- [x] **Image contract**: [ami/IMAGE.md](ami/IMAGE.md) (and `/etc/build-runner/manifest.json` on every instance).
- [x] **Weekly rebuild** (`.github/workflows/ami.yml`): Monday 03:00 Sydney, or by hand. Bake → smoke test → promote →
      prune (keep 3) → GitHub Release with IMAGE.md and manifest.json. Role `/claude/build-runner-ami-ci` via OIDC (`ci/ami-ci-role.sh`).
      **The latest image's contract is always the latest [release](https://github.com/stickleprojects/aws-build-runner/releases/latest).**
- [ ] Webhook and launcher Lambda, per-job instance types, reaper: the runner itself.

| Path | Purpose |
|---|---|
| [PLAN.md](PLAN.md) | Design, instance types per job, steps |
| [ami/IMAGE.md](ami/IMAGE.md) | **What the image comes with** (generated from `ami/manifest.json`) |
| `ami/setup.sh` | How the image is built (runs on the bake instance) |
| `ami/files/` | The local runner's hook and allowlists; the boot step (instance info, job registration, IMDS block); the one-job service; the bake reporter |
| `ami/bake.sh`, `ami/bake-role.sh` | Bake an AMI as Claude's user; the bake instance's role can only report, read its one GitHub token, and talk to Systems Manager |
| `ami/smoke-test.sh` | Check a baked AMI; promote the candidate if it passes |
| `lib/github-app.sh` | Short-lived GitHub App tokens (the key stays in SSM) |
| `ami/prune-amis.sh` | Keep the newest 3 AMIs (always the current one); delete the rest and their snapshots |
| `ci/ami-ci-role.sh` | The pipeline's AWS role (Claude creates it: `/claude/`, boundary, tagged) |
| `ci/publish-release.sh` | Publish the current image as a GitHub Release: what changed, then IMAGE.md |
