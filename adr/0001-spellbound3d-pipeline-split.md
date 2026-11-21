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

## Revisit when

| Trigger (proposed numbers, Kieron to confirm) | Then consider |
| --- | --- |
| `check-pr`'s AWS median over 5 minutes (from the AC10 runs) | B: staged split, and reusing Godot passes |
| More than 2 editor-only Godot failures a month reach main past `check-pr` | B: a Godot stage on PRs |
| One Godot process peaks under about 3 GB, and faster feedback becomes the goal | C: parallel gdUnit on one machine |
| m7i.large is slower than c7i.xlarge by more than 20%, or runs out of memory | go back to c7i.xlarge for heavy |
| The laptop is the bottleneck, or Kieron wants it free | D: the review on AWS |

## Open for Kieron

1. **The goal:** lowest cost, fastest feedback, or keeping the laptop free. The proposal assumes lowest cost at today's
   speed.
2. **The trigger numbers** above.
3. **Which PR hit the paperwork problem**, to confirm the pr-title message is the cause.

## Action items

1. [ ] Kieron: accept, change or reject this ADR.
2. [ ] aws-build-machine: read check-pr's AWS timing and Godot memory from the AC10 runs (job ids from spellbound3d);
   add them to the research page.
3. [ ] spellbound3d, with aws-build-machine: trial m7i.large for heavy, and compare its time and memory with c7i.xlarge.
4. [ ] claude-sdlc: reword pr-title's error so a `## Plan changes` body edit is the first fix.
5. [ ] aws-build-machine: check the triggers monthly in the cost review; propose a new ADR if one fires.
