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
A1 on-demand only ≈ US$33/month.**

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

Budget alerts email you; they don't stop anything. The hard ceilings are the guardrails
(small instance types, no purchases, no quota increases) and the EC2 vCPU quota. Example:
with a 32-vCPU quota, forgetting 8 × c5.xlarge for a whole month ≈ US$1,300; the 50 %
budget alert (US$15 on a US$30 budget) would arrive on day 1. Turn on the optional budget action in
[part1-godmode/CHECKLIST.md](part1-godmode/CHECKLIST.md) for an automatic stop.
