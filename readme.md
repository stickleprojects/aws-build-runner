# aws-build-machine

An on-demand GitHub Actions runner on AWS (Sydney, ap-southeast-2) for the Godot game repo
`stickleprojects/spellbound3d`, set up so that Claude on this laptop does the AWS work, with an approved allow-list
of services that can't run up a big bill. **What Claude may do: [part1-godmode/POLICY.md](part1-godmode/POLICY.md).**

## Background

The original note: set up a GitHub runner hosted in AWS, allowing for how hard the CLI and
account setup is. A "godmode" Claude session sets up IAM, and a second, normal session
builds the image and the runner. AWS registration was done, but creating the CLI
account got confusing: the console said "you must log in as root", yet the name shown
was a team identity.

What we found since (2026-11-16):
- The CLI already works as Claude's IAM user, default region ap-southeast-2. The account id and
  user name are kept out of this public repo: see [part1-godmode/config.py](part1-godmode/config.py).
- The account is a member of the **kieron-wray-team** organization. It carries AWS-managed roles under
  `/managed/` (`AccountFullAccessRole` with AdministratorAccess, account-management roles trusted by the
  organization's **management account**) and a budgets spend-limit role. You sign in with GitHub and
  pick the account, which is why the console showed a team identity rather than root or an IAM user. See
  [part1-godmode/CHECKLIST.md](part1-godmode/CHECKLIST.md), "Sign in to AWS".
- The old `claude-assistant-policy` (v1) allowed EC2 only in us-east-1 (wrong region), couldn't make
  AMIs or use SSM, and let Claude create a `github-runner-*` role with any policy and pass it
  to EC2: a way round every limit. Part 1 replaces it.

## Two parts

| | Who runs it | What it does |
|---|---|---|
| **[Part 1: policy and godmode](part1-godmode/README.md)** | You approve; a godmode session (temporary admin credentials) applies | Claude's IAM user's everyday rights, as an allow-list. Approved services in Sydney only, with limits; Claude builds its own IAM roles under `/claude/`, capped by a boundary; everything else denied. Changes go through an approval process. Plus a monthly budget, public-access blocks and disk encryption. Not specific to the runner. |
| **[Part 2: runner](part2-runner/PLAN.md)** | Normal Claude session as Claude's IAM user | Baked Ubuntu AMI with Godot 4.7.2; GitHub webhook → Lambda → one spot EC2 instance per job via a just-in-time runner config; reaper for strays. Cut-over is one repo variable (`SDLC_RUNNER`). |

Decisions so far: Sydney region; on-demand (per-job) runner; one repo for now; a baked
AMI rather than Docker. AWS's own option, CodeBuild-hosted runners, was ruled out because its `runs-on` label must
contain `${{ github.run_id }}`, which the repo's `SDLC_RUNNER` variable can't hold without editing
the synced workflows.

## Cost boundary

Agreed with Kieron on 2026-11-17. All figures are in US$, the currency AWS bills in.

| | |
|---|---|
| **Ceiling** | a bill of at most **US$35 a month**, all in |
| **Automatic stop** | at **US$29** of actual spend: AWS Budgets blocks anything new and the kill switch stops what's running. The gap to the ceiling covers the 8–36 hours that billing data lags. A fast stop (CloudWatch alarms, about 10 minutes) catches runaway uploads, Lambda calls and logs without waiting for billing data. |
| **Expected** | ≈ **US$13** a typical month, ≈ **US$25** at the busiest load seen; about US$2 with no jobs (3 kept images, the weekly rebuild). Credits cover usage while they last. |

**What keeps it there.** CI fails any change that goes past one of these limits:

| Limit | Value | Where it's set |
|---|---|---|
| Runners at once, all pools together | 4 | `part2-runner/stack/config.json` `MaxRunners` |
| Runner size | up to `xlarge` (4 vCPU), spot first | `config.json` `Pools` |
| Runner lifetime | 90 min, or 20 min idle | `config.json` `MaxAgeMinutes`, `IdleMinutes` |
| Overnight curfew | at least 9 h (now 22:00–07:00 Sydney) | `config.json` `Curfew` |
| Kept runner images | 3 | `part2-runner/ami/prune-amis.sh`, `.github/workflows/ami.yml` |
| Root disk | 30 GiB gp3, deleted with the instance | `part2-runner/ami/bake.sh`, `stack/template.yaml` |
| Upload rate per job | at most 6250 KiB/s (now 2500, ≈20 Mbps) | `part2-runner/ami/files/build-runner-boot.sh` |
| Budget stop | at most US$29; anomaly email at US$3 or less | `part1-godmode/policy.yaml` `account_settings` |
| Cost Explorer API (US$0.01 a call) | never on a schedule; only the budget-stop bootstrap | the check scans every workflow and script |

**A quota isn't a cost.** EC2's vCPU quota (32 on-demand, 32 spot) is only a ceiling on what *could* run. Spend
comes from what does run, which the limits above keep in check.

**Changing a limit.** The values live in `cost_boundary` in
[part1-godmode/policy.yaml](part1-godmode/policy.yaml), and the rule `cost_boundary_holds`
([part1-godmode/tests/regressions.py](part1-godmode/tests/regressions.py)) checks the settings against them.
CI also checks that the rule catches each kind of breach (`tests/test_cost_boundary.py`). Raising a value is a
policy change: Claude opens it as `approved: pending`, with what it would cost a month, and only Kieron can
approve it. That holds whoever asks for it, including another Claude session.

Prices for every option considered: [PRICING.md](PRICING.md).

## Layout

```
part1-godmode/
  README.md            the model and the change process
  POLICY.md            what Claude can and can't do, in plain English (generated)
  policy.yaml          the definition: approved services, limits, approvals   <- the only file to edit
  build-policies.py    policy.yaml -> IAM JSON in out/ + POLICY.md
  out/                 generated IAM JSON (committed, so PRs show the real diff)
  verify.py            ~514 allow/deny checks (offline, or with the IAM simulator)
  check-live.py        live AWS vs the checkout: drift, applied commit, date, who
  apply.sh             admin only: applies main's CI-checked artifact, records who applied it and when
  CHECKLIST.md         the first godmode session, step by step
part2-runner/
  README.md            overview and status
  PLAN.md              design and steps for the spellbound3d runner
PRICING.md             estimates for every option
adr/                   architecture decision records; support/ holds each one's research
.github/workflows/policy.yml   CI: generate, lint, check, store artifact, report applied state
```

Policy changes go through pull requests: proposed (pending), then generated and checked by CI,
then approved and merged, then not yet applied, then applied (a GitHub deployment records the
date and who). See [part1-godmode/README.md](part1-godmode/README.md#lifecycle-of-a-policy-change).

## Status

- [x] Part 1 drafted as an allow-list, with the resource-group tag rule (2026-11-16).
- [x] Part 1 approved by Kieron (2026-11-16).
- [x] Part 1 applied (2026-11-16, commit `ef42718`), from CloudShell. Access Analyzer: no findings. IAM
      simulator: 442 of 442 checks pass on the live policies. Drift check clean; recorded as a GitHub
      deployment.
- [x] Automated apply and rollback from GitHub Actions, with Kieron's approval (first run 2026-11-16, `cec3774`).
- [x] Read-only listing is Sydney only (applied 2026-11-16).
- [x] Part 2's account: the identity delegated admin account for now; moving it is tech debt
      ([#17](https://github.com/stickleprojects/aws-build-runner/issues/17)).
- [ ] Part 2 built.
