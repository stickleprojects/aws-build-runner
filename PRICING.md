# Pricing estimates

USD, ap-southeast-2 (Sydney), checked 2026-11-16. EC2 on-demand and CodeBuild prices are
from the AWS price list; spot prices are live from `describe-spot-price-history` and move
by the hour. Treat every figure as an estimate.

## The workload these are based on

From `stickleprojects/spellbound3d`'s last 100 workflow runs (all on 2026-11-15):
100 runs in 11.5 hours, 0 s to 38 min each, **6.4 min on average**, mostly short Claude
review / PR-title / descope jobs plus CI (`make check` with Godot 4.7.2).

Assumed month: **20 days like that** ≈ 12,800 job-minutes ≈ 213 job-hours.
On-demand EC2 runners also boot per job (~2 min with a baked AMI), so ≈ **280 instance-hours**.
Scale every monthly figure linearly if real use is lighter.

## Part 1: godmode setup

| Item | Cost |
|---|---|
| IAM users, roles, policies, boundary, policy simulator, Access Analyzer validation | free |
| AWS Budgets (plain budgets; first 2 action-enabled budgets) | free |
| Cost Anomaly Detection | free |
| CloudShell | free |
| **Total** | **US$0** |

## Part 2: where the runner runs

| Option | Unit price | Est. per month | Notes |
|---|---|---|---|
| **A1. EC2 on demand per job, t3.large** (2 vCPU, 8 GiB) | US$0.1056/h + US$0.005/h public IPv4 | **≈ US$33** | Recommended starting point; no workflow changes |
| **A2. Same on spot, t3.large** | ≈ US$0.042–0.046/h + IPv4 | **≈ US$16** | Can be interrupted (rare for short jobs); fall back to on-demand |
| A3. EC2 on demand per job, t3.xlarge (4 vCPU, 16 GiB) | US$0.2112/h + IPv4 | ≈ US$62 | Only if Godot exports need it |
| A4. Same on spot, t3.xlarge | ≈ US$0.068–0.073/h + IPv4 | ≈ US$22 | |
| A5. One always-on t3.large (no on-demand plumbing) | US$0.1106/h × 730 h | ≈ US$81 (spot ≈ US$36) | Simplest, but you pay while idle |
| B. AWS CodeBuild-hosted runner, general1.small (2 vCPU, 3 GB) | US$0.005/min | ≈ US$64 | Needs `runs-on: codebuild-<project>-${{ github.run_id }}-${{ github.run_attempt }}`: can't go in the `SDLC_RUNNER` variable, so every synced workflow would need editing |
| B'. CodeBuild general1.medium (4 vCPU, 7 GB) | US$0.01/min | ≈ US$128 | |
| C. GitHub-hosted `ubuntu-latest` (private repo, 2 vCPU) | ≈ US$0.006/min after the free minutes (2,000 Free plan / 3,000 Pro or Team) | ≈ US$59–65 | Zero maintenance; what jobs fall back to today when `SDLC_RUNNER` is unset |
| D. Current local runner | — | ≈ US$0 extra | Only works while this PC is on |

Running costs on top of A1–A5, all small:

| Item | Price | Est. per month |
|---|---|---|
| Root volume, 30 GiB gp3, only while an instance exists | US$0.096/GB-month | ≈ US$1 (A5: ≈ US$3) |
| Baked AMI snapshot, ~10–15 GB | US$0.055/GB-month | ≈ US$0.60–0.80 |
| Webhook Lambda + reaper Lambda | free tier covers ~3,000 calls/month | ≈ US$0 |
| SSM Parameter Store (standard), Session Manager | free | US$0 |
| CloudWatch Logs (Lambda + runner logs) | US$0.50–0.75/GB ingested | < US$1 |
| Data out to GitHub (artifacts, logs) | first 100 GB/month free, then ≈ US$0.114/GB | ≈ US$0 |

**Totals for the recommended path: A2 spot with on-demand fallback ≈ US$16–20/month;
A1 on-demand only ≈ US$33/month.** *(Superseded: see "What was decided" below.)*

### What was decided (2026-11-16), and what it costs

spellbound3d measured a cold `make check`: it **fails on 2 vCPU** (a timing test) and passes on 4, and
t3's CPU credits made timings flaky. So jobs get fixed-performance c7i types, chosen per job
([part2-runner/PLAN.md](part2-runner/PLAN.md)). Sydney spot prices, checked 2026-11-16:

| Item | Price | Busiest observed load (≈280 instance-hours/month) | About half that |
|---|---|---|---|
| Check jobs: c7i.xlarge spot (4 vCPU), ≈ half the hours | ≈ US$0.092/h + US$0.005/h IPv4 | ≈ US$13.60 | ≈ US$6.80 |
| Claude jobs: c7i.large spot (2 vCPU), ≈ half the hours | ≈ US$0.048/h + US$0.005/h IPv4 | ≈ US$7.40 | ≈ US$3.70 |
| Root volumes (30 GiB gp3, only while instances exist) | US$0.096/GB-month | ≈ US$1 | ≈ US$0.50 |
| 3 kept AMIs (≈10.5 GB of snapshot each) | US$0.055/GB-month | ≈ US$1.75 | ≈ US$1.75 |
| Weekly AMI rebuild (bake + smoke test, ≈30 min) | ≈ US$0.05 each | ≈ US$0.20 | ≈ US$0.20 |
| Lambdas, SSM, Logs, data out | mostly free tier | < US$1 | < US$1 |
| **Total** | | **≈ US$24–25** | **≈ US$13** |

**As built, every runner is c7i.xlarge** (a JIT runner can take any queued job with its label, so a
2-vCPU runner could end up with a check job: [PLAN.md](part2-runner/PLAN.md#instance-types-decided-2026-11-16-revised-when-building)).
The Claude-job half then costs ≈ US$13.60 instead of ≈ US$7.40 at the busiest load: **total ≈ US$31
busiest, ≈ US$16 at half**. Per-job sizing comes back (≈ US$24–25 / ≈ US$13) if spellbound3d gives
heavy jobs their own label.

**Watch out:** at the **busiest observed load all month**, this lands at the **US$25 budget**, so the
automatic stop would fire near month end. The "busiest load" was a single day of 100 runs in 11.5
hours; a typical month should be well under. If months really run that hot, the options are:
- cap concurrent runners in the launcher (planned anyway),
- keep light jobs on GitHub-hosted runners,
- or raise the budget towards the £30 (≈ US$38) ceiling, keeping headroom for the billing lag.

On-demand fallback (when spot is short) costs about 2.5× spot for those hours.

### GitHub's self-hosted runner fee (A, B and D)

GitHub announced a US$0.002/min platform charge for self-hosted runners in private repos from
March 2026, then [postponed it](https://resources.github.com/actions/2026-pricing-changes-for-github-actions/);
as of mid-2026 it had [not taken effect](https://openmetal.io/resources/blog/the-real-cost-math-behind-self-hosted-github-actions-runners/).
If it ever does: +≈ US$26/month at this workload, for any self-hosted option (CodeBuild included).
GitHub-hosted prices already include it.

## Image: AMI vs Docker

| Option | Cost | Notes |
|---|---|---|
| **Baked AMI** (Ubuntu 24.04 + runner + Godot 4.7.2) | snapshot ≈ US$0.60–0.80/month | Recommended: fastest start, nothing to pull per job |
| Docker image in ECR on a plain AMI | ECR ≈ US$0.10/GB-month (≈ US$0.20–0.50) | Pull per job adds time; useful if you want the same image locally |
| Stock Ubuntu AMI, install at boot | US$0 | Adds minutes to every job |

## Worst case under the guardrails

Since 2026-11-16 the budget **stops spending automatically** (part1-godmode/POLICY.md, "Set by godmode"):
- at **US$25** (≈£20; the ceiling is a £30 bill), counting what would be billed,
- **nothing new** can be started by Claude's user or roles,
- the **kill switch terminates every instance** and deletes the runner AMIs,
- and an **org-wide budget with an SCP** in the management account covers every account on the bill.

Billing data lags by up to about a day, so the stop fires after spend crosses US$25. The guardrails
(small instance types, no purchases, no quota increases) and the 32-vCPU quota cap how fast it can
grow meanwhile: the realistic worst case is about £22–25. It isn't a legal guarantee; AWS offers no
hard spend cap.
