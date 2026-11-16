# Part 1: first godmode session

One-off. Afterwards Claude works as its IAM user with exactly what
[POLICY.md](POLICY.md) says. Later changes follow the [change process](README.md#change-process).

## 0. Approve the policy

- [ ] Read [POLICY.md](POLICY.md). Anything you don't want, say so; Claude removes it.
- [ ] Approve: tell Claude, which sets every `approved: pending` to `approved: 2026-MM-DD Kieron` in a PR.
      Once the `policy` and `approved` checks are green, merge it.

## 1. Work out how you sign in (the "log in as root" confusion)

The account has AWS-managed roles under `/managed/` (`AccountFullAccessRole` with
AdministratorAccess, trusted by `account-access.amazonaws.com`; account-management roles trusted by
an AWS-owned account) and `AWSServiceRoleForBudgetsSpendLimitMember`, all created 2026-10-25. The
console probably signs you in through that managed access, not as root or an IAM user. That would
explain why it shows a team identity while root-only pages ask for root.

- [ ] Console, account menu (top right): note what it says you are. A role session such as
      `AccountFullAccessRole/...` is already admin. That session is godmode; root isn't needed.
- [ ] If root sign-in works ("Root user" + the registration email), add MFA to root, then stop using it.
- [ ] Billing: is a **spend limit** set? If so, note it. It's the hard cap; the budget only emails.

## 2. Admin credentials for the session (no long-lived keys)

`apply.sh` needs `aws`, `gh` (to fetch the CI artifact and record the deployment), `git`, `jq` and
python3 with PyYAML. The laptop has them all, so run it there with temporary admin credentials:

1. Open **AWS CloudShell** in ap-southeast-2 (the `>_` icon in the console). It runs as your console identity.
2. In CloudShell: `aws configure export-credentials --format env`.
3. Paste the `export` lines into a **new** laptop terminal. They expire by themselves (≈1 h).
4. In that terminal, start Claude and ask it to run step 3, or run it yourself. Close the terminal afterwards.

If `export-credentials` doesn't work in CloudShell, run everything in CloudShell instead. Install `gh`
there (a single binary from its GitHub releases), then `gh auth login`, `git clone`, and `pip install --user pyyaml`.

Never create admin access keys or IAM users on the laptop.

The account id and Claude's IAM user name aren't in the repo. The scripts take them from the
environment, `.env.local` at the repo root (git-ignored), or the repo variables `AWS_ACCOUNT_ID` and
`AWS_CLAUDE_USER` (`gh variable list`). Check with `python3 -I part1-godmode/config.py`. Values in
`.env.local` may refer to variables exported in `~/.bashrc` (see `.env.local.example`). If the file
is lost (`git clean -xfd`), the repo variables still work. To rebuild it, run
`python3 -I part1-godmode/config.py --init` with the laptop's normal profile.

## 3. Apply

```bash
git checkout main && git pull
cd part1-godmode
./apply.sh you@example.com        # budget alert address; the amount comes from policy.yaml
```

It applies only the artifact CI generated and checked for main's head commit. It refuses if:
- there's no such artifact.
- the artifact's checksums or contents don't match the checkout.
- anything is pending approval.
- Access Analyzer reports an ERROR or SECURITY_WARNING.
- the IAM simulator disagrees.

Then it:
- applies the three policies, replacing the old `claude-assistant-policy` v1.
- detaches anything else from the user.
- creates the budget.
- blocks public S3, AMIs and snapshots, and turns on EBS encryption by default.
- simulates the live result and checks for drift.
- records the apply: IAM tags on the policies, and a GitHub deployment saying who applied it and when.

- [ ] Ends with "Applied ⟨commit⟩ at ⟨time⟩ by ⟨you⟩".

## 4. Optional

- [ ] Cost Anomaly Detection (Billing): add an email subscription to the default monitor (e.g. ≥ US$5). Free.
- [ ] Automatic stop: a budget action on `guardrail-monthly` that, at 100 %, attaches a policy to
      Claude's IAM user denying `ec2:RunInstances` and `ec2:StartInstances`. Free for two budgets.
- [ ] Note the EC2 vCPU quota (Service Quotas → EC2 → L-1216C47A). Claude can't raise it, so it's the
      ceiling on how much can run at once.

## 5. Back to normal

New terminal, laptop profile: `aws sts get-caller-identity` shows Claude's IAM user;
`part1-godmode/verify.py` passes and `part1-godmode/check-live.py` reports no drift.

## Undo

As admin, detach `claude-assistant-policy` and `claude-assistant-iam` from the user. That leaves
the user with no rights. The account settings can stay.
