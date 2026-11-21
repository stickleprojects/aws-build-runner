# ADR-0001: How spellbound3d's pipelines use the laptop and AWS

- **Status:** Proposed
- **Date:** 21 Nov 2026
- **Deciders:** Kieron (all decisions). aws-build-machine, spellbound3d and claude-sdlc propose and build.
- **Supporting research:** [support/0001-pipeline-split-research.md](support/0001-pipeline-split-research.md) has the
  usage data, every option in detail, the peers' answers and the open questions. The live, commentable copy is the plan
  doc: https://claude.ai/artifact/4Qz2sZifCSyJTwPvz8sV6L (private to Kieron).

## Context

spellbound3d has two pipelines. A PR waits for the Claude review on the laptop, and then runs one `make check` on an AWS
heavy machine (c7i.xlarge spot). A push to main runs `make check FULL=1` on the same machine type. Kieron asked how to
make better use of AWS and the laptop: machine sizes, the software each job needs, mixing laptop and AWS jobs in one
pipeline, and running more in parallel on heavy.

What we measured (19–20 Nov):

- **Volume:** about 61 heavy and 196 laptop jobs a day.
- **Heavy machine:** about 1.1 of 4 vCPUs used on average, and a 4.4 GB memory peak out of 8 GB. The check is mostly gdUnit,
  which is single-threaded (10 of 18 minutes in a full run).
- **Cost:** about US$10–15 a month, inside the US$29 stop and covered by credits for now.

Two things changed on 21 Nov:

- **Faster checks on PRs.** spellbound3d's change 0048 (PR #234) merged. PRs now run the shorter `check-pr`, with only
  the core gdUnit tests. The full Godot check runs on main, every 3 hours within the 07:00–22:00 window, and as the
  release gate. That is close to claude-sdlc's "light PR, heavy main" counter-proposal.
- **Light jobs skip Godot.** claude-sdlc PR #250 (SDLC_STACK_NEEDS) merged.

Constraints:

- spellbound3d's workflows are synced from claude-sdlc and changed only through variables.
- One required `check` status.
- pr-title and claude-review are required checks, so whichever machine runs them must be on.
- Approved instance types only.
- Kieron's rule: a Godot pass is never thrown away over paperwork (the PR title, spec or descopes).

## Decision (proposed)

Keep 0048's pipelines as they are, and change only these two things now:

1. **Trial m7i.large for the heavy pool** (2 vCPU, 8 GB). The check needs memory, not CPU, and m7i.large costs about 40%
   less than c7i.xlarge on spot. It's an approved type, so no policy change. spellbound3d owns the machine definitions.
2. **Reword pr-title's error** so that its first suggested fix is a `## Plan changes` edit to the PR body. A body edit
   needs no new commit and no Godot run. Today the message reads as "split the docs out into a new PR", which costs a
   Godot run.

Everything else is deferred. Each deferred item has a measured trigger, under Revisit.

## Options considered

The monthly prices are rough runner costs. They include about US$2.50 of fixed costs, and are based on 20–30 busy days.

### A. Keep 0048, heavy on m7i.large (proposed)

| Dimension | Assessment |
| --- | --- |
| Complexity | Low: a machine-list change and a message change |
| Cost | about US$7–11 a month |
| Speed | `check-pr` on PRs (3 min 11 s on the laptop; AWS timing to come); full check on main |
| Effort | S: about half a day for AWS, S for claude-sdlc |

**Pros:** cheapest, nearly no build work, and 0048 is already live.

**Cons:**

- An editor-only Godot regression is found on main, not on the PR.
- The Godot stage may get no faster, because m7i.large has 2 vCPUs.

### B. Staged split, Godot stage on PRs

The review, the static checks on light, and the paperwork run side by side. Godot runs on heavy only after all three
pass, and an earlier pass for the same game code is reused.

| Dimension | Assessment |
| --- | --- |
| Complexity | Medium-high: a new claude-sdlc feature, a Godot-free tier, and a reuse key with a trusted recorder |
| Cost | about US$7–14 a month, depending on the heavy size |
| Speed | static checks fail in 1–2 minutes; Godot is skipped on docs-only PRs |
| Effort | about 3–4 days, plus a policy change for the S3 role that records passes |

**Pros:** catches editor-only Godot failures before merge, and fails fast on cheap checks.

**Cons:**

- Most of the build work of any option.
- Needs a new OIDC role.
- The reuse of passes only pays off if `check-pr` turns out slow.

### C. B plus parallel or sharded gdUnit

gdUnit runs as 2–3 processes on one c7i.xlarge, or as 2–3 shards on m7i.large.

| Dimension | Assessment |
| --- | --- |
| Complexity | High: suite groups, separate report folders, and merged warnings and slow_suites |
| Cost | about US$8–14 a month |
| Speed | the fastest: the Godot stage is maybe 30–50% shorter |
| Effort | B, plus about 1 day |

**Pros:** the fastest feedback.

**Cons:**

- Memory-bound, and one Godot process's peak memory isn't measured yet.
- Shards use more of the 8-runner limit.

### D. Claude review on AWS (add-on to any option)

| Dimension | Assessment |
| --- | --- |
| Cost | +US$4–5 a month (the subscription token costs the same anywhere) |
| Effort | S |

**Pros:** frees the laptop, and AWS covers the hours the laptop is off.

**Cons:** costs money for a job the laptop does for free today. A spot reclaim re-runs the review.

## Trade-off analysis

0048 already delivers most of what the split was for. Godot-heavy work moved off PRs, and the full check runs on main
and on a schedule. B and C still add two things: catching editor-only Godot failures before merge, and faster feedback.
Both are worth days of work only if one of these turns out true:

- `check-pr` is slow on AWS.
- Editor-only failures keep reaching main.

Neither has been measured yet. The 10 PR-tier runs that 0048 dispatches after merge (AC10) answer the first. Main-red
issues answer the second.

A is the cheapest and the least work, and it leaves B and C open.

## Consequences

- **Easier:** about US$3–4 a month less on heavy; fewer "move the docs to a new PR" Godot re-runs; no new framework
  feature to maintain.
- **Harder:**
  - Main can go red from a Godot editor regression that a PR didn't catch, and main-red means fix it first.
  - The Godot stage doesn't get faster.
- **Unchanged:**
  - The laptop keeps the review, the gate and the paperwork.
  - pr-title and claude-review still need the laptop to be on.

## What this decision gives: cost, laptop load and speed

The decision aims for the lowest cost while keeping the laptop's load small. Those two goals only conflict on the
Claude review: moving it to AWS (D) would take about two-thirds of the laptop's load off it, for about US$4–5 a month.
The gate and the paperwork stay on the laptop, because on a fresh AWS machine most of their cost would be boot time.

| Measure | Expected under this decision | Source of the figure |
| --- | --- | --- |
| Runner cost | about US$7–11 a month | the research's options table |
| Laptop load | about 1.9 h of small jobs a day: review about 65 min, gate about 31 min, paperwork about 19 min | 19–20 Nov job counts |
| First PR response: paperwork | 15–30 s after the push (laptop) | 19–20 Nov |
| First PR response: Claude review verdict | about 2.6 min typical, 6.5 min slow (laptop) | 19–20 Nov |
| PR complete (`check` green): review, gate, AWS boot (about 1 min), `check-pr` | about 7 min typical, 11–12 min slow | estimate: `check-pr` took 3 min 11 s on the laptop; its AWS time isn't measured yet |
| Release: the full `make check FULL=1` on main | about 10–15 min on c7i.xlarge; possibly 12–18 min on m7i.large | estimate: today's 478 s median check, plus FULL's Godot coverage modules |
| Release gate when you release | immediate, if main's full run on that commit has passed | 0048's release gate reads main's existing result |

The full run starts on main when a PR merges, and the 3-hourly full check covers anything the push run missed. So a
release normally waits for nothing.

m7i.large shouldn't slow the Godot tests, which use one core. The steps that run in parallel, coverage among them,
get 2 cores instead of 4. That's where any slowdown in the full run would show.

The estimates are replaced by measured figures from:

- the AC10 runs: 10 PR-tier dispatches of "Periodic full check" after #234's merge;
- the m7i.large trial.

## When to re-evaluate

**Scheduled checkpoints:**

1. **After the AC10 runs (this week).** They give `check-pr`'s real AWS time and one Godot process's memory peak.
   - Confirm or correct the speed table above.
   - Settle the triggers about `check-pr` and memory below.
2. **After a week of m7i.large on heavy.** Compare its time and memory with c7i.xlarge. Keep it, or roll it back.
3. **Monthly, in the cost review.** aws-build-machine checks every trigger below and reports the numbers to Kieron.
   If one fires, it proposes a new ADR that supersedes or amends this one, with a price for each option.

**Triggers** (the numbers are proposals; Kieron to confirm):

| Trigger | Then consider |
| --- | --- |
| `check-pr`'s AWS median over 5 minutes, or PR complete regularly over 12 minutes | B: the staged split, and reusing Godot passes |
| More than 2 editor-only Godot failures a month reach main past `check-pr` | B: a Godot stage on PRs |
| One Godot process peaks under about 3 GB, and faster feedback becomes the goal | C: parallel gdUnit on one machine |
| m7i.large is more than 20% slower than c7i.xlarge, or runs out of memory | go back to c7i.xlarge for heavy |
| The laptop queues jobs: a laptop job regularly waits more than 2 minutes to start, or Kieron wants the laptop free | D: the review on AWS |
| The month's runner cost is projected above US$20, or Budgets warns as it nears the US$29 stop | re-price; first look at what grew |
| Heavy jobs a day double (over about 120), or jobs queue at the 8-runner limit | re-check the runner limit and C's shards |
| A paperwork fix still costs a Godot re-run after the pr-title reword | B's reuse of Godot passes |

## Metrics to check

All of these exist today, at no cost or nearly none. Cost Explorer is used by hand only, never on a schedule.

| Metric | Supports which trigger | Where it comes from |
| --- | --- | --- |
| `check-pr` and the full check's run time, per job (`wall_s`), split by machine type | `check-pr` median, m7i.large vs c7i.xlarge, release time | CloudWatch log group `/build/runner/jobs`, read with Logs Insights (a tiny scan, under a cent) |
| Memory peak per job (`mem_peak_mb`) and per machine (`box_mem_peak_mb`) | m7i.large out of memory; Godot process memory for C | `/build/runner/jobs` |
| Time from a PR push until `check` is green | PR complete over 12 minutes | GitHub: the commit's statuses and check runs, through `gh api` |
| Claude review job time, and each laptop job's wait before it starts | the review verdict time; the laptop queues jobs | GitHub Actions job timings (`created_at`, `started_at`, `completed_at`) for the self-hosted runner |
| Main-red issues caused by an editor-only Godot failure | more than 2 a month reach main | spellbound3d's main-red issues from 0048; spellbound3d says which were editor-only |
| PRs that re-ran Godot only for a paperwork fix | the pr-title reword didn't remove them | pr-title failures followed by a new commit that changes no game files (GitHub; spellbound3d can list them) |
| Heavy jobs a day, and jobs waiting at the 8-runner limit | volume doubled | `/build/runner/jobs` job counts; the launcher's logs |
| Month-to-date and projected runner cost | cost above US$20, or nearing the stop | AWS Budgets `guardrail-monthly` (free); the monthly cost review |

## Open for Kieron

1. **The goal:** lowest cost, fastest feedback, or keeping the laptop free. The proposal assumes lowest cost at today's
   speed.
2. **The trigger numbers** under When to re-evaluate.
3. **Which PR hit the paperwork problem**, to confirm the pr-title message is the cause.

## Action items

1. [ ] Kieron: accept, change or reject this ADR.
2. [ ] aws-build-machine: read check-pr's AWS timing and Godot memory from the AC10 runs (job ids from spellbound3d);
   add them to the research page.
3. [ ] spellbound3d, with aws-build-machine: trial m7i.large for heavy, and compare its time and memory with c7i.xlarge.
4. [ ] claude-sdlc: reword pr-title's error so a `## Plan changes` body edit is the first fix.
5. [ ] aws-build-machine: replace the speed estimates with measured figures after the AC10 runs and the m7i.large trial.
6. [ ] aws-build-machine: check every trigger monthly in the cost review, with the metrics above; propose a new ADR if
   one fires.
