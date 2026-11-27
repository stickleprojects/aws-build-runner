# Applying the policy (godmode), step by step

How a merged, approved policy change gets into AWS: exactly what worked on 2026-11-16. Every
command is complete; paste it as it is. First time through, do every step; after that, only
**Each apply**.

Before starting: the change is merged to `main` (see the [change process](README.md#change-process)),
and the `policy` CI run on `main` has finished green. `apply.sh` refuses otherwise.

## Automated: apply from GitHub (once set up)

1. Open https://github.com/stickleprojects/aws-build-runner/actions/workflows/apply.yml
2. **Run workflow**. Branch `main`, action **apply** (or **rollback**). Then **Run workflow**.
3. The run waits for approval: open it, then **Review deployments** → tick the environment → **Approve and deploy**.
4. It runs `apply.sh` (or `rollback.sh`) exactly as below and shows the same output in the log, with
   the account details masked. The run *is* the deployment record: who started it, who approved it, when.

It signs in to AWS through GitHub's OIDC as the role `godmode/aws-build-runner-apply`, with no stored
keys. That role can only manage Claude's three policies and the account safety settings, and only a
job in this repo's `aws` or `aws-rollback` environment, from `main`, after your approval, can use it.

**Rollback** puts the previous version of each policy back, or detaches a policy that was new. It marks
the last apply's deployment inactive, and opens an issue to revert the change in git.

### One-time setup (godmode)

- In CloudShell (steps 1–2 below first, if CloudShell isn't set up yet):
  ```bash
  export PATH=$HOME/bin:$PATH; cd ~/aws-build-runner/part1-godmode && git pull && ./bootstrap-github-apply.sh
  ```
  Validates the role's policies with Access Analyzer, then creates the GitHub OIDC provider and the role.
  Ends with the role ARN. Run it again only if `bootstrap/*.json` changes.
- Claude (from the laptop, as Kieron's GitHub account) sets up the GitHub side: environments `aws`
  and `aws-rollback` requiring Kieron's approval, from `main` only; the environment secrets
  `AWS_APPLY_ROLE_ARN`, `AWS_ACCOUNT_ID`, `AWS_CLAUDE_USER` and `AWS_ALERT_EMAIL`.

The manual route below still works and is the fallback.

### Automatic stop at the budget (the guardrails workflow; again when budget_stop or bootstrap/ changes)

Normally: Actions → **guardrails** → Run workflow on `main`, with a note (Claude starts it when you ask), then
you approve the run on its page, after reading what it will do. The drift check runs straight after.

It stops before changing anything if `guardrails/budget-stop`, the boundary `guardrails/guardrail-roles-boundary`,
or the helper roles' trust or boundary differ from the repo. Those could widen permissions, so only CloudShell
sets them:
```bash
export AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)" AWS_CLAUDE_USER=<Claude's user> AWS_ALERT_EMAIL=<alert email> && cd ~/aws-build-runner && git pull && cd part1-godmode && ./bootstrap-github-apply.sh
```
(Claude gives you this with the real values, in chat.) As a fallback, `./bootstrap-budget-stop.sh` also runs in
CloudShell with the same exports.
Makes the budget count what would be billed (credits reduce it, refunds don't, tax counts), and creates `guardrails/budget-stop` and the
role AWS Budgets uses to attach it. Then sets the budget action: at 100 % of actual spend, attach it automatically to
Claude's user and roles. Ends by listing the action. To lift a stop once you've decided: Billing → Budgets →
`guardrail-monthly` → Actions → reverse.

Since #30 it also sets up the **fast stop** (CloudWatch alarms `guardrail-fast-*` → SNS `guardrail-fast-stop` →
the kill switch in fast mode) and **Cost Anomaly Detection**. The first run emails a subscription confirmation for
the fast-stop alerts: **click Confirm** in that email, or the alarms stop runners without telling you. It ends with
a dry run of both modes. After a fast stop, once you've found the cause: turn our Lambdas back on with
`aws lambda put-function-concurrency --function-name build-runner-launcher --reserved-concurrent-executions 4` and
`aws lambda put-function-concurrency --function-name build-runner-webhook --reserved-concurrent-executions 5`
(each keeps its cap, #30 fix 6: AWS won't set the account limit below 1,000; don't delete them), or tell Claude to.

### Region lock (management account, once; again when bootstrap/org-region-lock-scp.json changes)

`./bootstrap-org-region-lock.sh` in the management account's CloudShell attaches two SCPs to Root: `org-region-lock` (outside Sydney) and `org-sydney-services` (in Sydney):
in Sydney only the named services we use; outside Sydney only a named list of global services (IAM, Budgets, billing,
sign-in...), and only in us-east-1; nothing anywhere else (#30 fix 7). SCPs never apply to the management account, so they can be
detached there; the script prints the command. Then run
the **guardrails** workflow, so the kill switch sweeps Sydney only.

When [bootstrap/budget-stop-policy.json](bootstrap/budget-stop-policy.json) or `budget_usd` changes, rerun
`./bootstrap-org-budget-stop.sh` in the management account (below): it updates the `org-budget-stop` SCP
and the org budget from the repo, so there's nothing to paste.

### The whole organization's bill (management account, once, by hand)

The bill is the organization's consolidated bill, paid by the **kieron-wray-team management account**.
The steps above protect the identity account. These protect the **whole bill**: every account in
the organization. Claude has no access to the management account; do this in the console.

**The easy way: a script.** Sign in, choose the **kieron-wray-team management account**, open CloudShell.
No `gh` or token needed here: the repo is public, and the alert email is taken from the org budget's
existing alerts (set `AWS_ALERT_EMAIL` only if the budget doesn't exist yet). First time:
```bash
git clone https://github.com/stickleprojects/aws-build-runner.git ~/aws-build-runner
```
Then, every time:
```bash
cd ~/aws-build-runner/part1-godmode && git pull && ./bootstrap-org-budget-stop.sh
```
It refuses to run anywhere but the management account. It creates or corrects everything below (the SCP,
the `spend-capped` OU, a dedicated role, the budget and its four alerts, and the action), lists any member
account outside the OU, and ends with a check. Safe to rerun.

The same steps by hand, for reference:

1. Sign in (https://signin.aws.amazon.com → GitHub) and choose **kieron-wray-team management account**.
2. **Organizations → Policies → Service control policies.** If they're disabled, **Enable**. Then **Create policy**:
   - Name: `org-budget-stop`
   - Description: `Attached automatically when the org budget is reached: nothing new and billable`
   - Policy: paste the contents of [bootstrap/budget-stop-policy.json](bootstrap/budget-stop-policy.json)
     (the same deny list as the identity account's stop).
   - **Create policy**. Don't attach it to anything: the budget action does that.
3. **An OU for the action to target** (the budget's action picker lists OUs, never Root itself):
   Organizations → **AWS accounts** → tick **Root** → **Actions → Organizational unit → Create new**:
   `spend-capped`. (Not *Add an AWS account*: that makes a new account and fails with
   `EMAIL_ALREADY_EXISTS`.) Then tick every **member** account (identity delegated admin, building
   momentum) → **Actions → AWS account → Move** → `spend-capped`. The management account stays at Root;
   SCPs never apply to it. Accounts keep everything they inherited from Root (checked 2026-11-16: the
   organization's region rules still apply after the move). **Put every new account into `spend-capped`.**
4. **Billing and Cost Management → Budgets → Create budget → Customize (advanced) → Cost budget**:
   - Name `org-guardrail-monthly`; period **Monthly**; recurring; fixed amount **25** (USD, ≈£20).
   - Budget scope: **Filter specific AWS cost dimensions** → **Add filter** → dimension **Charge type** →
     **Exclude** → select **Refund** only. Leave Credit, Tax and Usage in. (The console no longer has
     refund/credit/tax checkboxes under Advanced options; this filter does the same. In the management
     account the budget still covers every account in the organization.)
   - Advanced options: aggregate by **Unblended**.
5. Alerts: **Actual 50 %**, **Actual 80 %**, **Actual 100 %**, **Forecasted 100 %**, each emailing you.
   (No SNS or Chatbot.) **Next** → the **Attach actions** page.
6. **Add action** on the **Actual 100 %** alert:
   - IAM role: **Create new role**. Not `AWSManagedBudgetsSpendLimitManagementRole`: AWS manages that one
     for its own feature, and its permissions can change under us.
   - Action type **Apply service control policy**; policy `org-budget-stop`; organizational unit `spend-capped`.
   - **Run automatically**. **Next** → **Create budget**.
7. Check: the budget lists 4 alerts and 1 action, status *Standby*.

What it does: at US$25 across the whole organization, every account in `spend-capped` (identity,
building momentum) can't start or create anything billable. Running instances in the identity account
are terminated by its own kill switch (above).

Limits:
- **SCPs don't apply to the management account itself**, so keep nothing running there.
- Billing data lags up to about a day.
- To lift the stop: the budget's Actions → **Reverse**.

## Manual: from CloudShell

### 1. Sign in to AWS

1. Go to https://signin.aws.amazon.com and choose **GitHub**. There is no root-user sign-in for this
   account; it's in the **kieron-wray-team** organization, and GitHub is the identity. (AWS
   Builder ID is a different thing and doesn't open the account.)
2. On the **AWS sign-ins** screen, choose **kieron-wray-team identity delegated admin**. That's
   where Claude's IAM user lives. The session is full admin, through the account's AWS-managed
   `AccountFullAccessRole`; that is godmode.
3. Top right, set the region to **Asia Pacific (Sydney)**.
4. Open **CloudShell** (the `>_` icon, top bar).

### 2. First time only: set up CloudShell

CloudShell keeps your home folder between sessions, so this is once.

**a. Install `gh`** (the GitHub CLI) and keep it on the PATH for future sessions:

```bash
curl -sL https://github.com/cli/cli/releases/download/v2.63.2/gh_2.63.2_linux_amd64.tar.gz | tar xz && mkdir -p ~/bin && mv gh_2.63.2_linux_amd64/bin/gh ~/bin/ && rm -rf gh_2.63.2_linux_amd64 && (grep -q 'HOME/bin' ~/.bashrc || echo 'export PATH=$HOME/bin:$PATH' >> ~/.bashrc) && export PATH=$HOME/bin:$PATH && gh --version
```

**b. Create a GitHub token for this repo only**, in your laptop's browser: open
https://github.com/settings/personal-access-tokens/new and set

| Field | Value |
|---|---|
| Token name | `cloudshell-apply` |
| Expiration | 7 days (make a new one next time) |
| Repository access | *Only select repositories* → `stickleprojects/aws-build-runner` |
| Repository permissions → **Actions** | Read-only (find the CI run, download the artifact) |
| Repository permissions → **Deployments** | Read and write (record the apply) |
| Repository permissions → **Variables** | Read-only (account id, user name, alert email) |
| Metadata | Read-only (added automatically) |

**Generate token**, copy it.

Don't use `gh auth login --web`: it asks for *full control of all your repositories*, and
CloudShell can't open a browser anyway.

**c. Log `gh` in with the token.** Paste this, press Enter, paste the token (nothing shows),
press Enter, then **Ctrl-D**:

```bash
gh auth login --with-token
```

Check it: this should say "Logged in … stickleprojects", then `3`.

```bash
gh auth status && gh api repos/stickleprojects/aws-build-runner/actions/variables --jq '.total_count'
```

**d. Get the repo and check the tools.** PyYAML and `jq` are normally already there. Don't use
`pip install --user`: CloudShell's Python runs in a virtualenv that refuses it.

```bash
(python3 -I -c 'import yaml' 2>/dev/null || pip3 install pyyaml) && (command -v jq || sudo dnf install -y jq) && git clone https://github.com/stickleprojects/aws-build-runner.git && cd aws-build-runner/part1-godmode && python3 -I config.py
```

It should end by listing `AWS_ACCOUNT_ID` and `AWS_CLAUDE_USER` "from GitHub repo variable". These
values, and the budget alert email, are kept out of this public repo: see [config.py](config.py).

### 3. Each apply

In CloudShell:

```bash
export PATH=$HOME/bin:$PATH; cd ~/aws-build-runner/part1-godmode && git pull && ./apply.sh
```

- Start it soon after opening CloudShell. CloudShell ends a session after ~20–30 minutes **without
  keyboard or mouse activity**. The apply itself takes about 5 minutes, so leave the tab open and
  don't touch it.
- If the token has expired (7 days), `gh` fails. Make a new one (step 2b) and log in again (2c).

What it does, and what you should see:

| Step | What happens | Good output |
|---|---|---|
| 3a | The CI artifact for `main`'s head commit: checksums, same commit, nothing pending, identical to the checkout | `checksums ok, same commit, nothing pending, identical to checkout` |
| 3b | AWS's own validator (Access Analyzer) on the three policies | nothing printed |
| 3c | IAM simulator on the artifact, ~1–2 min with a progress counter | `442 checks (simulator, local drafts), 0 failed` |
| 4a | Policies created or updated; unchanged ones are left alone | `created …` / `updated … -> vN` / `unchanged …` |
| 4b | Budget, public-access blocks, disk encryption | five lines |
| 4c | IAM simulator on the live policies | `… (simulator, live policies), 0 failed` |
| 5a | Drift check, with the applied commit, time and who in each policy's tags | `AWS has this checkout's commit applied.` |
| end | GitHub deployment to `aws` marked success | `Applied <commit> at <time> by stickleprojects (AccountFullAccessRole).` |

Nothing in AWS changes before 4a, so a stop in 3a–3c is harmless. If it stops at 4a or later,
GitHub records a failed deployment. Rerunning is safe: it finishes the job, and policies that are
already right show as `unchanged`.

Checks that pass in your account but are **blocked by the organization**: the kieron-wray-team
organization's SCPs block some regions entirely (seen: eu-west-2, ap-southeast-4). `verify.py`
reports those as "blocked by the organization's SCPs", not as failures.

### 4. Afterwards

- Back on the laptop, as Claude's user: `python3 -I part1-godmode/check-live.py` reports
  "AWS has this checkout's commit applied".
- Optional: delete the token at https://github.com/settings/personal-access-tokens, or let it expire.

## Things that didn't work (2026-11-16)

- **Exporting CloudShell's credentials to a laptop terminal** (`aws configure export-credentials`):
  they last only **~15 minutes** and expired mid-run twice. `apply.sh` now refuses to start
  changing AWS if exported credentials have less than 10 minutes left. Run it in CloudShell.
- `gh auth login --web`: asks for full control of all repos; no browser in CloudShell.
- `pip3 install --user`: refused inside CloudShell's virtualenv.

## Optional, once

- [ ] **GitHub two-factor authentication is on.** GitHub is the key to every AWS account in the organization.
- [ ] Billing: is a **spend limit** set? If so, note it. It's the hard cap; the budget only emails.
- [ ] Cost Anomaly Detection (Billing): add an email subscription to the default monitor (e.g. ≥ US$5). Free.
- [ ] Automatic stop: a budget action on `guardrail-monthly` that, at 100 %, attaches a policy to Claude's
      IAM user denying `ec2:RunInstances` and `ec2:StartInstances`. Free for two budgets.
- [ ] Note the EC2 vCPU quota (Service Quotas → EC2 → L-1216C47A). Claude can't raise it, so it caps
      how much can run at once.

## Emergency: undo

As admin, in CloudShell:

- **Kill switch** (Claude's user can then do nothing at all):
  ```bash
  cd ~/aws-build-runner/part1-godmode && ACCT=$(python3 -I config.py AWS_ACCOUNT_ID) && U=$(python3 -I config.py AWS_CLAUDE_USER) && for p in claude-assistant-policy claude-assistant-iam; do aws iam detach-user-policy --user-name "$U" --policy-arn "arn:aws:iam::$ACCT:policy/$p"; done; aws iam list-attached-user-policies --user-name "$U"
  ```
- **Rollback** to the previous policy versions: the **apply** workflow with action **rollback** (above), or
  in CloudShell:
  ```bash
  export PATH=$HOME/bin:$PATH; cd ~/aws-build-runner/part1-godmode && git pull && ./rollback.sh
  ```
- **Back to the previous approved policy, properly**: revert the change in a PR, merge, apply.
