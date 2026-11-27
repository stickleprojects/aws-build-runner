# Claude's own GitHub identity (#23)

Claude acts in this repo as its own GitHub App, **`stickleprojects-claude`**, never as Kieron. So GitHub, not Claude's
word, enforces that only Kieron approves and merges.

| Piece | What it does |
|---|---|
| The App | Contents, pull requests, issues, Actions and workflows read/write; checks, statuses and deployments read. **No** Administration, Secrets, Environments or Variables, so it can't change protection, settings or secrets. Installed on `aws-build-runner` only. |
| `claude-github-token` | Signs a 1-hour installation token from `~/.config/claude-github-app/key.pem` (chmod 600, never in a repo), caches it in `~/.cache/claude-github-app/` |
| `claude-gh` | `gh` with that token and its own config dir, so Kieron's login is never used |
| `install.sh <APP_ID>` | Kieron, once: installs the two tools in `~/.local/bin`, and sets this clone to push over HTTPS with the App's token and commit as the App's bot user |
| `.github/CODEOWNERS` | `* @stickleprojects`: every PR needs Kieron's review |
| `approval.yml` / `check_approvals.py` | The required `approved` check: approval fields a PR changes must read `review on PR #N`, and Kieron must have approved its latest commit. It re-runs on each review. |
| `.claude/settings.json` | Claude Code denies Claude Kieron's gh token and SSH keys, `gh pr merge` / `gh pr review`, branch protection, and edits to these settings |

**Day to day:** Claude opens a PR as the App and arms auto-merge. Kieron reviews and clicks **Approve**, and the PR
merges once `policy` and `approved` pass. A new push after his approval needs a new one.

**Limits:** Claude runs as Kieron's Unix user, so the settings rules stop accidental use of his credentials, not a
determined bypass. Other repos (spellbound3d and others) still use Kieron's identity until the App is extended to them.

**Branch protection on `main`** (Kieron sets it; the App can't): required checks `policy` and `approved`; 1 approving
review; code owner review; dismiss stale approvals; approval of the most recent push; enforced for admins.
