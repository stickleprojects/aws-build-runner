# Part 1: what Claude may do in AWS

**Read [POLICY.md](POLICY.md)**: the plain-English list of what Claude can and can't do.
It is generated from [policy.yaml](policy.yaml), the one file that defines it, together with
the IAM policies, so the description and what AWS enforces can't drift apart.

## The model

- **Allow-list.** Claude's IAM user can build only with approved services, only in
  Sydney. Nothing else is granted, so everything else is denied: other services, other
  regions, IAM users and keys, budgets, quotas, billing, account settings.
- **Limits inside approved services**: small instance types, small disks, and a short list of
  costly actions blocked (NAT gateways, reservations, fleets…).
- **Claude builds its own IAM**: it creates the roles, policies and instance profiles a build machine
  needs, under `/claude/` only. Each one carries a boundary that repeats the allow-list and limits, so
  nothing Claude makes can do more than Claude can.
- **No AWS-managed policies.** AWS changes those without asking (its `ViewOnlyAccess` is on
  version 49 and now includes paid AI-model calls). Every action is listed in `policy.yaml`.
- **Godmode is temporary**: an admin session only for what could widen permissions (see step 5 below), or
  a one-off account action. Day to day, Claude works as its IAM user, and changes reach AWS through
  workflows that wait for Kieron's approval.
- **Nothing a pipeline runs can widen permissions.** The guardrails role may change the helper roles only
  while `guardrails/guardrail-roles-boundary` is on them, and may never remove it, edit trust or edit any
  managed policy (#80, 2026-11-27). The regression rule `pipeline_roles_cannot_escalate` checks it.

## Lifecycle of a policy change

| Stage | Where you see it | What has happened |
|---|---|---|
| **Proposed** | A pull request; `approved: pending` in policy.yaml | Claude drafted it; the `approved` check is red |
| **Generated and checked** | The `policy` check on the PR or commit; its run summary shows the POLICY.md diff | `out/*.json` and POLICY.md regenerated (pushed back to the PR if stale); lint (parliament); ≈514 allow/deny checks for scope and cost; stored as the artifact `policies-<commit>` with checksums |
| **Approved** | `approved: YYYY-MM-DD Kieron`; the `approved` check goes green; you merge | Only approved policy reaches main |
| **Not yet applied** | Run summary on main: "AWS: not yet applied"; `./check-live.py` | Main is ahead of AWS |
| **Applied** | GitHub deployment to the `aws` environment (repo sidebar → Deployments): *"Applied ⟨date⟩ by ⟨GitHub user⟩ as ⟨AWS identity⟩"*. IAM tags `applied-commit`, `applied-at` and `applied-by` on each policy. Run summary: "this commit is applied" | `apply.sh` re-checked the artifact, applied exactly it, and confirmed AWS matches |

`apply.sh` applies only the artifact CI made for main's head commit. Before changing anything, it checks:
- the artifact's checksums, and that it's for this commit with nothing pending.
- that it's byte-identical to the checkout and to a fresh rebuild.
- AWS's own validator (Access Analyzer): no ERROR or SECURITY_WARNING.
- the IAM simulator on the artifact.

After applying, it re-runs the simulator on the live policies and a drift check. A failure
part-way is recorded as a failed deployment.

## Change process

1. **Claude stops and asks** when something isn't approved (AccessDenied, or known in advance).
   It says what it needs, why, the cost, and the smallest permission that would do. It doesn't look
   for another way round.
2. **Claude opens a PR**: a branch with the change in `policy.yaml` (`approved: pending`, plus a `changes:`
   line). If it removes a block, it also removes that action from `MUST_STAY_DENIED` in `verify.py`
   (deliberately a second edit). The PR template asks for need, smallest permission, worst-case cost, risk.
3. **CI generates and checks it**; the run summary shows the POLICY.md diff and what's pending.
4. **You approve, in GitHub**: Claude (as its App, `stickleprojects-claude`) writes `approved: review on PR #N` and arms
   auto-merge. You click **Approve** on the PR, and it merges once `policy` and `approved` pass. `approved` checks
   that your review is on the latest commit; a later push needs a new approval. Only your review approves.
5. **It gets applied**, by the workflow for what changed. You approve each run on its page, which shows the
   most its role can change and the diff of the code it will execute since the last approved run:
   - **apply**: Claude's policies, budgets and account settings. Or by hand in CloudShell with `./apply.sh`.
   - **guardrails**: the budget stop, kill switch, alarms and anomaly alert (`bootstrap-budget-stop.sh`).
   - **CloudShell**, only for what could widen permissions: `./bootstrap-github-apply.sh` (the godmode roles,
     `guardrails/budget-stop`, the helper roles' boundary and trust), and the org stop in the management account.

   Step by step: [CHECKLIST.md](CHECKLIST.md). The deployment record shows it applied.
6. **The drift check confirms it**: **guardrails-check** runs after every apply and guardrails run, and daily at
   07:30 Sydney. If anything live differs from the repo it opens one "Guardrails drift" issue, with the fix
   for each item, and closes it once everything matches.

Removing permissions works the same way and is always welcome.

Commands for drafting: `python3 -I build-policies.py --draft`, `./verify.py --offline`
(no AWS) or `./verify.py --local` (IAM simulator on the drafts, changes nothing), `./check-live.py`
(what's live vs this checkout).

## Files

| File | What it is |
|---|---|
| [policy.yaml](policy.yaml) | **The definition.** Change only through a PR |
| [POLICY.md](POLICY.md) | Plain-English summary, generated. Read this |
| [out/](out/) | Generated IAM JSON. Committed, so PRs show the real diff |
| [build-policies.py](build-policies.py) | `policy.yaml` → `out/*.json` + `POLICY.md`. Refuses pending items unless `--draft`; `--check` reports a stale POLICY.md |
| [verify.py](verify.py) | ≈514 allow/deny checks, for the user and for the worst role Claude could create. `--offline`: local evaluator. `--local`: IAM simulator on the drafts. Default: IAM simulator on the live policies |
| [config.py](config.py) | Account id and IAM user name, which are kept out of this public repo. Read from env vars, `.env.local` or GitHub repo variables. Committed JSON uses `{{ACCOUNT_ID}}`, filled in only when checking against AWS or applying |
| [check-live.py](check-live.py) | Live AWS vs this checkout: drift, applied commit, date, who |
| [guardrails_check.py](guardrails_check.py) | The rest of the guardrails, live vs the repo (#42 C): both budgets, the stop action and policy, the kill switch, alarms, SNS wiring, anomaly alerts, the apply and check roles, our Lambdas' caps. Run daily and after every apply by the **guardrails-check** workflow (read-only role `godmode/aws-build-runner-guardrails-check`), which opens or closes one "Guardrails drift" issue |
| [release_preview.py](release_preview.py) | What an approval will actually run, shown before the approval gate of **apply** and **guardrails**: every permission of the role it signs in as (from the role file, which the drift check confirms is live), and the diff of the code it executes since the last approved run |
| [apply.sh](apply.sh) | **Admin only.** Applies main's checked artifact and records it. Run by the **apply** workflow, or in CloudShell |
| [lib.sh](lib.sh) | Shared helpers for apply/rollback: IAM tag values cleaned, deployment status descriptions capped |
| [tests/regressions.py](tests/regressions.py) | One rule per past mistake, each with what went wrong; runs in CI with `shellcheck` |
| [rollback.sh](rollback.sh) | **Admin only.** Puts the previous policy versions back, marks the deployment inactive, opens a "revert in git" issue |
| [bootstrap-github-apply.sh](bootstrap-github-apply.sh), [bootstrap/](bootstrap/) | **Godmode, once.** GitHub OIDC provider plus the role `godmode/aws-build-runner-apply` that the **apply** workflow uses, with only these scripts' permissions |
| [CHECKLIST.md](CHECKLIST.md) | The first godmode session, step by step |
| [../.github/workflows/policy.yml](../.github/workflows/policy.yml) | CI: generate, check, store the artifact, report the applied state. Never touches AWS |
| [../.github/workflows/apply.yml](../.github/workflows/apply.yml) | Apply or roll back, run by hand on `main`, after your approval |

## Status

**Applied** 2026-11-16 (commit `ef42718`), from CloudShell by Kieron. All 442 checks pass in AWS's
real IAM simulator on the live policies, Access Analyzer has no findings, and `check-live.py` shows
no drift. The kieron-wray-team organization's SCPs also block some regions (eu-west-2,
ap-southeast-4) on top of this policy.

Cost: free (CI is a minute or so per run). See [../PRICING.md](../PRICING.md).
