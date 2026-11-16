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

## Design (as built, 2026-11-16: [stack/](stack/))

```
GitHub workflow_job webhook ─> build-runner-webhook  (Lambda, public function URL)
                                 checks the HMAC signature, keeps spellbound3d "queued" jobs labelled
                                 aws-spellbound3d, starts the launcher asynchronously (answers in < 1 s)
                                     │
EventBridge Scheduler, every 5 min ──┤
                                     v
                               build-runner-launcher (Lambda, one run at a time: an SSM-parameter lock)
                                 reconcile: queued jobs − runners not yet on a job = launches, up to MaxRunners
                                 per launch: JIT runner registration (GitHub App) → RunInstances from the
                                 launch template (current AMI via resolve:ssm), spot through each type and
                                 subnet, then on-demand
                                 on the schedule, also reaps: instances > 90 min, runners idle 20 min with
                                 nothing queued, registrations whose instance is gone
                                     │
                                     v
                               EC2 boots the baked AMI, runs ./run.sh --jitconfig …, one job, powers off
                               (shutdown = terminate; the image also powers off at 2 h whatever happens)
```

- **Idempotent**: the launcher never trusts a single event. Duplicate or missed webhooks don't matter: the
  next run counts again. It can't reserve Lambda concurrency (this account's limit is the minimum 10), so
  an SSM parameter created with "fail if it exists" is the lock.
- Label: `aws-spellbound3d`, and **only** that: a JIT runner has exactly the labels it's registered with,
  so it can never take a `self-hosted` job meant for the local runner. Cut-over = set `SDLC_RUNNER` to
  `aws-spellbound3d`; rollback = set it back to `self-hosted`.
- One instance per job; ephemeral, clean every time. Registered on GitHub ≈ 40 s after launch (measured).
- Instance: no SSH, no inbound rules (shell via Session Manager), IMDSv2 only, public IPv4 in a default-VPC
  subnet (no NAT gateway), 30 GiB gp3 encrypted. Its role (`build-runner-instance`) only runs the SSM agent.
- GitHub credential: the App `stickleprojects-aws-runner` (key in SSM); each launcher run gets a token for
  spellbound3d only, with only *administration: write* and *actions: read*, and revokes it at the end.
  The webhook secret is SSM `/build/github/webhook-secret` (SecureString), made by `deploy.sh`.
- Tags: everything `resource-group=build-runner`; runner instances also `build-runner-role=runner`, which
  is all the launcher may terminate.

## Instance types (decided 2026-11-16, revised when building)

Measured with spellbound3d's session: a cold `make check` (`FULL=1`, no `game/.godot`) **fails on 2 CPUs**
(438 s; a 5 s timing test took 7.6 s) and **passes on 4** (354 s, on laptop hyperthreads).

**Every runner is c7i.xlarge** (4 fixed vCPU, 8 GB) spot, then c6i.xlarge / m7i.xlarge spot, then
c7i.xlarge on-demand. **Never 2 vCPU or t3.** The plan was to give Claude jobs c7i.large, but GitHub hands
a queued job to *any* idle runner with matching labels: a JIT runner can't be reserved for the job it was
launched for. With one label for every job, a check job could land on a 2-vCPU runner and fail its timing
test. Per-job sizing needs spellbound3d to give heavy jobs a second label (their workflows, their call);
the launcher then registers runners per label. Costs: [PRICING.md](../PRICING.md#what-was-decided-2026-11-16-and-what-it-costs).

- **No pre-baked Godot import cache** (`game/.godot`): stale caches have hidden failures in spellbound3d.
  Every job imports cold. An exact-match `actions/cache` is spellbound3d's proposal, and up to Kieron.
- **8 vCPU** (c7i.2xlarge, ~US$0.194/h spot) is parked until spellbound3d's parallel gdUnit (0022) exists.
  It needs a policy change: xlarge is the current cap.
- Instances write `/etc/build-runner/instance.json` at boot (type, `nproc`, memory) so jobs can size themselves.

## Steps

1. [x] AMI bake, smoke test, weekly pipeline (`ami/`, `.github/workflows/ami.yml`).
2. [x] GitHub App key and id in SSM; webhook secret (`stack/deploy.sh`).
3. [x] Stack `build-runner` (`stack/`): webhook and launcher Lambdas, launch template, security group,
   5-minute schedule, log groups (14 days). Tested live: signatures, a spot launch, the runner online.
4. [ ] Add the spellbound3d webhook (*Workflow jobs* only) to the URL `deploy.sh` prints, with the secret.
5. [ ] Test with a throwaway workflow on a branch that targets `aws-spellbound3d`, then switch
   `SDLC_RUNNER`. Keep the local runner as the fallback until a week of green runs.

## Open questions

- ~~Which AWS account hosts the build machine?~~ **Decided 2026-11-16: the identity delegated admin account**,
  where Claude's IAM user is. Moving it to its own member account is tech debt, deferred until there's a budget
  line for another account: [#17](https://github.com/stickleprojects/aws-build-runner/issues/17).
- Is `~/.cache/godot` + the install dir enough for `install-godot.sh` to skip the download,
  or does the AMI also need the export templates?
- Does `claude-code-action` need Node/Bun preinstalled, or does it bring its own?
- GitHub App or PAT for runner registration?
