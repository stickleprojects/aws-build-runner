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

Planned, nothing built yet. Planned contents of this folder:

| Path | Purpose |
|---|---|
| [PLAN.md](PLAN.md) | Design and build steps |
| `ami/` | Script to bake the runner AMI |
| `stack/` | CloudFormation stack `build-runner`: launch template, security group, webhook and reaper Lambdas, roles under `/claude/` |
| `lambda/` | Webhook and reaper code |
