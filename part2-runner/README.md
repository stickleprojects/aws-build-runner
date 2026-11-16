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

- [x] **AMI bake** (`ami/bake.sh`): a t3.large runs `ami/setup.sh` from Ubuntu 24.04 and reports through SSM; the AMI is
      recorded in SSM `/build/runner/ami-id`. First good image: `ami-01d8f1eb7885ab15a` (2026-11-16).
- [x] **Smoke test** (`ami/smoke-test.sh`): boots the AMI without a job and runs 13 checks through Systems Manager. All pass.
- [ ] Image manifest (`IMAGE.md`, `/etc/build-runner/manifest.json`), versions from spellbound3d's pins.
- [ ] Weekly rebuild pipeline (bake, smoke test, keep the last 3 AMIs).
- [ ] Webhook and launcher Lambda, per-job instance types, reaper: the runner itself.

| Path | Purpose |
|---|---|
| [PLAN.md](PLAN.md) | Design, instance types per job, steps |
| `ami/setup.sh` | What the image contains, and how it's installed (runs on the bake instance) |
| `ami/files/` | The local runner's hook and allowlists, the one-job service, the bake reporter |
| `ami/bake.sh`, `ami/bake-role.sh` | Bake an AMI as Claude's user (the instance's role can only report and talk to Systems Manager) |
| `ami/smoke-test.sh` | Check a baked AMI |
