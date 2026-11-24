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

**Per label, not per job** (Kieron, 2026-11-16): GitHub gives a queued job to any idle runner with its
labels, so check jobs get their own label (`aws-heavy`; light jobs use `aws-light`), being agreed with spellbound3d. These
figures hold once that's in place. Without it, the only safe choice would be 4 vCPU for every job:
≈ US$31 at the busiest load (over the stop), ≈ US$16 at half.

**Watch out:** at the **busiest observed load all month**, this lands just under the **US$29 budget stop**, so a
busier month would trip it before month end. The "busiest load" was a single day of 100 runs in 11.5
hours; a typical month should be well under. If months really run that hot, the options are:
- cap concurrent runners in the launcher (planned anyway),
- keep light jobs on GitHub-hosted runners,
- or raise the budget towards the £30 (≈ US$38) ceiling, keeping headroom for the billing lag.

On-demand fallback (when spot is short) costs about 2.5× spot for those hours.

### Web pool (`aws-spellbound3d-web`), checked 2026-11-24

For spellbound3d's "Web release" and "Web browser tests". Spot order: m7i.xlarge, c6i.xlarge, c7i.xlarge;
on-demand fallback c7i.xlarge. No always-on cost beyond the shared image. Sydney prices:

| Type (4 vCPU) | Spot, range over the 3 zones | On demand |
|---|---|---|
| m7i.xlarge (16 GiB) | US$0.098–0.109/h | US$0.252/h |
| c6i.xlarge (8 GiB) | US$0.103–0.112/h | US$0.222/h |
| c7i.xlarge (8 GiB) | US$0.089–0.099/h | US$0.2331/h |

Add US$0.005/h for the public IPv4 address and ≈ US$0.004/h for the 30 GiB root volume. Per-run figures
allow ≈ 1 minute of boot and shutdown; they are estimates from measured times, not billed amounts.

| Job | Measured (2026-11-24) | Spot, each | On-demand c7i.xlarge, each | 20 a month, spot | 20 a month, on demand |
|---|---|---|---|---|---|
| Web release (v0.1.0) | 384 s on m7i.xlarge | ≈ US$0.013 | ≈ US$0.030 | ≈ US$0.26 | ≈ US$0.60 |
| Web browser tests | 595 s on c7i.xlarge | ≈ US$0.02 | ≈ US$0.044 | ≈ US$0.40 | ≈ US$0.88 |
| **Total** | | | | **≈ US$0.66** | **≈ US$1.48** |

- One run of each, so treat them as rough. The browser suite hasn't been timed on m7i.xlarge, and the
  on-demand figures assume the same times as measured.
- Reclaimed attempts are extra: 1 of the pool's first 5 launches was reclaimed (c7i.xlarge, before the
  current order). Even paying for every job twice would be ≈ US$1.32 a month.
- The browser libraries' effect on image size, and so on snapshot cost, hasn't been measured.

### Actual spend, 1–24 November 2026

From Cost Explorer on 2026-11-24, whole account, estimated (billing lags by about a day, so the web pool's
runs are mostly not in it):

| Service | Usage |
|---|---|
| EC2 instances | US$2.46 |
| EC2 other (volumes, snapshots, regional data transfer) | US$0.55 |
| CloudWatch (fast-stop alarms) | US$0.20 |
| Public IPv4 | US$0.17 |
| Cost Explorer (requests by hand, US$0.01 each) | US$0.15 |
| **Total usage** | **US$3.53** |
| Credits | −US$3.53 |
| **Net** | **US$0.00** |

The daily average for 19–23 November was US$0.57 (≈ US$17 over 30 such days). **The US$29 budget counts spend
after credits, so it read US$0.00**: while credits cover the usage, the budget stop can't fire.

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

**Corrected 2026-11-17 (#30).** This section used to say the realistic worst case was about £22–25. That was
wrong. The budget stop isn't a hard cap: it fires on billing data that is 8–36 hours late, and it only
stopped instances. Something that spends fast, or that isn't an instance, could run well past it. The review
in #30 put the exposure at thousands of US$ for outbound data from a runner (≈ US$1,000/h for 4 runners).

What's in place, once the #30 fixes are applied:

| Guardrail | What it limits | Worst case it leaves |
|---|---|---|
| Upload cap on every runner (≈ 20 Mbps, [ami/files/build-runner-boot.sh](part2-runner/ami/files/build-runner-boot.sh)) | Data out, the biggest risk | ≈ US$1.05/h per runner, ≈ US$4.20/h for 4 |
| Fast stop: CloudWatch alarms → kill switch within ≈ 10 minutes | Data out, Lambda loops, log floods | ≈ 10 minutes of the fastest leak: well under US$5 |
| Budget stop at **US$29** (≈ £23, tax included; Kieron, 2026-11-17) | Everything, a day late | The lag: a few US$ at the capped rates |
| Kill switch at the budget | Instances, our Lambdas, AMIs, detached disks, unused IPs, interface endpoints | — |
| Disk limits | No paid IOPS/throughput, no locked snapshots or protected AMIs | — |
| Cost Anomaly Detection (daily email from US$3) | Slow leaks the alarms don't see (e.g. S3 requests) | Noticed within about a day |
| Org-wide budget with an SCP (management account) | Every account on the bill | — |

So the bill should end below the £30 ceiling even with a mistake, but it is still not a legal guarantee: AWS offers
no hard spend cap. Not covered by a fast alarm: S3 request floods (no free account-wide metric). They're slow at
today's scale, and Cost Anomaly Detection and the budget stop catch them.

At the busiest load seen so far, a month of runners costs ≈ US$24–25 (above), so a busy month fits under the
US$29 stop with a little room; a month busier than that would trip it and stop builds until Kieron lifts it.
