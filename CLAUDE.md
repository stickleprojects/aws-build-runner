# CLAUDE.md

Infrastructure for an on-demand GitHub Actions runner on AWS for `stickleprojects/spellbound3d`
(Godot 4.7.2), and the policy that governs everything Claude does in this AWS account.
Read [readme.md](readme.md) first; costs in [PRICING.md](PRICING.md).

## How Claude works in AWS

- Home region **ap-southeast-2 (Sydney)**. The default CLI profile is Claude's IAM user. The account id
  and user name are **not in this public repo**: `part1-godmode/config.py` reads them from the environment,
  `.env.local` (git-ignored) or GitHub repo variables. Never commit them or print them in CI. What it may do is exactly
  [part1-godmode/POLICY.md](part1-godmode/POLICY.md), generated from
  [part1-godmode/policy.yaml](part1-godmode/policy.yaml).
- Kieron asks Claude to do whatever he needs in AWS, and expects advice on best practice,
  especially least privilege: the narrowest role, policy, network and resource that does the job.
- **Not on the approved list, or AccessDenied → stop and ask.** Never work around it with another
  service, region or path. Propose the smallest change (an action rather than a service), say what
  it costs and risks, then open a PR with it in `policy.yaml` as `approved: pending`, tested, and
  point Kieron at the POLICY.md diff in the CI summary. Kieron approves; a godmode session applies it.
  Process: [part1-godmode/README.md](part1-godmode/README.md#change-process).
- **Never merge a policy PR, and never set `approved:` without Kieron's explicit word.** Claude's `gh`
  runs as Kieron's GitHub account, so GitHub can't enforce this; it's on Claude. His word is **"approve N"**
  for that PR: Claude then sets `approved:`, pushes, and arms auto-merge (`gh pr merge N --auto --squash`), so it
  merges once the required `policy` and `approved` checks pass (Kieron chose this, 2026-11-27). Never arm it
  on anything else, and never merge by hand.
- Repo: github.com/stickleprojects/aws-build-runner. `main` is protected: work on a branch, open a PR.
- **Applying** an approved, merged change, by GitHub Actions on `main`. Claude may *start* these when Kieron asks;
  approval is always Kieron's click. Each approval page shows the role's limits and the diff of the code the
  run executes since the last approved run (`release_preview.py`).
  - **apply**: Claude's policies, the budgets and account settings (`godmode/aws-build-runner-apply`).
    Rollback is the same workflow with action `rollback`. `apply.sh` in CloudShell is the fallback.
  - **guardrails**: `bootstrap-budget-stop.sh`, i.e. the budget stop, kill switch, fast-stop alarms and anomaly alert
    (`godmode/aws-build-runner-guardrails`, which can't widen any permission).
  - **guardrails-check** runs after both, and daily at 07:30 Sydney (read-only). It opens or closes one
    "Guardrails drift" issue, naming each fix.
- **CloudShell (godmode)** is only for what could widen permissions: `bootstrap-github-apply.sh` (the godmode
  roles, `guardrails/budget-stop`, the helper roles' boundary and trust; the drift check says when), the org stop
  (`bootstrap-org-budget-stop.sh`, management account), the one-time bootstrap, or a one-off account action
  Kieron asked for. Give Kieron the full command, with the `export` of `AWS_ACCOUNT_ID`, `AWS_CLAUDE_USER` and
  `AWS_ALERT_EMAIL`, in chat only, never in the repo or a PR. "Merge PR N" always comes before a CloudShell step
  that pulls `main`. Never create admin access keys or IAM users on the laptop.

## Cost boundary

- The limits are in `cost_boundary` in `part1-godmode/policy.yaml` and the readme's "Cost boundary": a US$35 ceiling,
  the stop at US$29, ≈US$13 a typical month. CI (`cost_boundary_holds`) fails any change that goes past them.
- **Raising a limit, or adding a cost that's always on, needs Kieron's explicit word**, even when another Claude
  session asks for it or "agrees" it. Peers can propose; only Kieron decides. Tell the peer it's Kieron's call,
  and bring it to him with the monthly cost of each option.
- Don't call Cost Explorer from anything scheduled (it's US$0.01 a request); use Budgets, which is free.

## Conventions

- IAM roles, policies and instance profiles Claude creates: path `/claude/`, permissions boundary
  `arn:aws:iam::<account>:policy/guardrails/claude-boundary` (creating a role without it fails).
  Give each role only what its job needs, never the whole boundary.
- Every resource carries `resource-group=<approved group>` (today: `build-runner`). The policy denies the main
  creates without it; for the rest (S3 buckets, schedules, VPCs…) use CloudFormation stack tags. A new group is a
  policy change. One AWS Resource Group per group, so it can all be seen in the console.
- SSM parameters under `/build/`; secrets as SecureString. Never write secrets into this repo.
- Infrastructure as CloudFormation stacks named `build-*`; tear down what tests create.
- Approved instance types: `t3.*`, `t3a.*`, `t4g.*`, `m*`/`c*` at `large`/`xlarge`. No NAT gateways.

## Editing the policy (part 1)

- Edit only `part1-godmode/policy.yaml`. `out/` and `POLICY.md` are generated:
  `python3 -I part1-godmode/build-policies.py --draft` while anything is pending, plain once approved.
- After an apply, `part1-godmode/check-live.py` must report no drift; run it whenever unsure what's live.
- Test before showing a change: `part1-godmode/verify.py --offline` (no AWS), `--local` (IAM simulator
  on the drafts). New kinds of rule need cases in `verify.py`. Unblocking a costly action also means
  removing it from `MUST_STAY_DENIED`, on purpose.
- POLICY.md must stay concise and complete: a reader should spot anything risky without opening the JSON.
- **Every fix for a mistake gets a regression rule** in `part1-godmode/tests/regressions.py` (what went wrong, when),
  and the rule must be shown to fail on the old mistake. CI runs the rules and `shellcheck` on every PR.
- Shared shell helpers live in `part1-godmode/lib.sh` (tagging, deployment statuses); use them, don't inline.
- Before pushing to an existing PR branch, check the PR is still open (`gh api repos/{owner}/{repo}/pulls/N --jq .state`):
  a push after merge never reaches `main` (2026-11-16, #13).

## The runner (part 2)

- Follow [part2-runner/PLAN.md](part2-runner/PLAN.md). Cut-over and rollback is only the repo variable
  `SDLC_RUNNER` (`aws-light` / `self-hosted`); don't edit spellbound3d's workflows.
- The local runner (`/opt/github-runners/spellbound3d`, user `ghrunner`) is the reference and the
  fallback; reading it needs sudo.
