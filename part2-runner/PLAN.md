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
                                 aws-light, starts the launcher asynchronously (answers in < 1 s)
                                     │
EventBridge Scheduler, every 5 min ──┤
                                     v
                               build-runner-launcher (Lambda, one run at a time: an SSM-parameter lock)
                                 reconcile: queued jobs − runners not yet on a job = launches, up to MaxRunners
                                 per launch: JIT runner registration (GitHub App) → RunInstances from the
                                 launch template (current AMI via resolve:ssm), spot through each type and
                                 subnet, then on-demand; sized per label
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
- Labels: `aws-light` and `aws-heavy`, one per runner and nothing else: a JIT runner
  has exactly the labels it's registered with, so it can never take a `self-hosted` job meant for the local
  runner. Cut-over = set `SDLC_RUNNER` (and `SDLC_RUNNER_HEAVY`) to them; rollback = back to `self-hosted`.
- One instance per job; ephemeral, clean every time. Registered on GitHub ≈ 40 s after launch (measured).
- Instance: no SSH, no inbound rules (shell via Session Manager), IMDSv2 only, public IPv4 in a default-VPC
  subnet (no NAT gateway), 30 GiB gp3 encrypted. Its role (`build-runner-instance`) only runs the SSM agent.
- GitHub credential: the App `stickleprojects-aws-runner` (key in SSM); each launcher run gets a token for
  spellbound3d only, with only *administration: write* and *actions: read*, and revokes it at the end.
  The webhook secret is SSM `/build/github/webhook-secret` (SecureString), made by `deploy.sh`.
- Tags: everything `resource-group=build-runner`; runner instances also `build-runner-role=runner`, which
  is all the launcher may terminate.

## Instance types per label (decided 2026-11-16, Kieron)

Measured with spellbound3d's session: a cold `make check` (`FULL=1`, no `game/.godot`) **fails on 2 CPUs**
(438 s; a 5 s timing test took 7.6 s) and **passes on 4** (354 s, on laptop hyperthreads).

GitHub gives a queued job to *any* idle runner with matching labels, so a runner can't be kept for the job
it was launched for (with one local runner this never mattered). So sizes go **per label**, and
spellbound3d's heavy jobs get their own label (being agreed with spellbound3d: their workflows, their change):

| Label (repo variable) | Jobs | Instance, tried in order | Why |
|---|---|---|---|
| `aws-heavy` (`SDLC_RUNNER_HEAVY`) | CI `check`, `make check (after review)` | c7i.xlarge → c6i.xlarge → m7i.xlarge spot → c7i.xlarge on-demand. **Never 2 vCPU or t3** | Timing tests fail on 2 vCPU; t3 throttles when CPU credits run out |
| `aws-light` (`SDLC_RUNNER`) | Everything else (Claude jobs: review, mention, spec, descopes, PR title, evals) | c7i.large → t3.large spot → c7i.large on-demand | Mostly waiting on the API |

Which class a job uses is spellbound3d's choice, and its responsibility. A daily report
(`usage/`) tracks hours and estimated cost per class, and opens an issue for Kieron if heavy takes over the
budget. We own the runners, so whether they're the right size is ours to watch: each runner also records
every job's CPU, memory and CPU/memory wait (`/build/runner/jobs`), the webhook records each job's wait for a runner,
and the report's *Job fit* section suggests changes (too small, CPU-starved, could be light, waiting for runners).
Size changes still go to Kieron with prices. spellbound3d measured light on 2026-11-16 (2 CPUs, 4 GiB): `evals` 33 s,
a review-sized Claude run 67 s at ≈ 280 MB; plenty of headroom. The launcher plans each label separately (an idle 2-vCPU runner never counts as covering a check job), with
one cap for both. Until spellbound3d has the second label, nothing cuts over. The labels and types are the
stack parameter `Pools`.

- **Nightly curfew (Kieron, 2026-11-17): no runners 22:00-07:00 Sydney.** At 21:00 a notice on the *AWS runner
  curfew* issue mentions him. `/extend 1h` or `/extend 2h` pushes the stop back, with a new notice an hour before.
  No answer: the launcher terminates every runner (running jobs are killed, his choice) and launches none; queued
  jobs wait and start at 07:00. Overrides live in one SSM parameter, `/build/runner/curfew`. The rules are
  `stack/lambda/curfew.py`, shared by the launcher, `.github/workflows/curfew.yml` and the `runner-curfew` skill.
  The weekly image rebuild moved to Monday 08:30 (07:30 in winter) so it stays outside the curfew.
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
5. [ ] spellbound3d: a second label for its check jobs (`SDLC_RUNNER_HEAVY`, falling back to `SDLC_RUNNER`; names decided 2026-11-17, Kieron).
6. [ ] Test with a throwaway workflow on a branch that targets `aws-light`, then switch
   `SDLC_RUNNER`. Keep the local runner as the fallback until a week of green runs.

## Open questions

- ~~Which AWS account hosts the build machine?~~ **Decided 2026-11-16: the identity delegated admin account**,
  where Claude's IAM user is. Moving it to its own member account is tech debt, deferred until there's a budget
  line for another account: [#17](https://github.com/stickleprojects/aws-build-runner/issues/17).
- Is `~/.cache/godot` + the install dir enough for `install-godot.sh` to skip the download,
  or does the AMI also need the export templates?
- Does `claude-code-action` need Node/Bun preinstalled, or does it bring its own?
- GitHub App or PAT for runner registration?
