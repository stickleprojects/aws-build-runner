#!/usr/bin/env python3
"""What an approval will actually run: for the summary a workflow shows before its approval gate (#42).

    release_preview.py --last SHA --head SHA --role bootstrap/ROLE.json [--role-name NAME] FILE...

Prints Markdown:
  1. the most the run can change: every statement of the role it signs in as, read from the role file
     (what AWS will enforce, whatever the code says; the drift check confirms the live role matches);
  2. the code it executes that changed since the last approved run (SHA), as a diff, per file;
  3. or that none of it changed.
FILEs are paths or directories relative to the repo root. Read-only; needs git history (fetch-depth 0).
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MAX_DIFF_LINES = 400   # per run; longer diffs are cut, with a link to the full comparison


def git(*args) -> str:
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, check=True).stdout


def role_limits(role_file: Path) -> list[str]:
    doc = json.loads(role_file.read_text())
    lines = []
    for st in doc["Statement"]:
        acts = [st["Action"]] if isinstance(st["Action"], str) else st["Action"]
        res = st.get("Resource", "*")
        res = [res] if isinstance(res, str) else res
        res = [r.replace("{{ACCOUNT_ID}}", "<account>").replace("{{CLAUDE_USER}}", "<claude-user>") for r in res]
        cond = " (only " + json.dumps(st["Condition"]).replace("{{ACCOUNT_ID}}", "<account>") + ")" if st.get("Condition") else ""
        verb = "**Never**" if st["Effect"] == "Deny" else "May"
        lines.append(f"- {verb}: `{'`, `'.join(acts)}` on `{'`, `'.join(res)}`{cond}")
    return lines


def preview(last: str, head: str, files: list[str], role_file: Path, role_name: str, repo_url: str) -> str:
    out = [f"### The most this run can change (role `{role_name}`)", "",
           "AWS enforces this whatever the code does. The drift check confirms the live role matches this file.", ""]
    out += role_limits(role_file)
    out += [""]
    known = bool(last) and subprocess.run(["git", "-C", str(REPO), "cat-file", "-e", f"{last}^{{commit}}"],
                                          capture_output=True).returncode == 0
    if not known:
        out += ["### Code it executes", "", "No earlier approved run to compare with: **review all of it.**"]
        return "\n".join(out)
    changed = [f for f in git("diff", "--name-only", last, head, "--", *files).split() if f]
    out += [f"### Code it executes, changed since the last approved run (`{last[:12]}` → `{head[:12]}`)", ""]
    if not changed:
        out += ["**None.** Every file this run executes is the same as at the last approved run."]
        return "\n".join(out)
    out += ["```", git("diff", "--stat", last, head, "--", *files).rstrip(), "```", ""]
    budget = MAX_DIFF_LINES
    for f in changed:
        diff = git("--no-pager", "diff", last, head, "--", f).splitlines()[4:]   # drop the git header
        out += [f"<details><summary><code>{f}</code> ({len(diff)} lines)</summary>", "", "```diff"]
        if budget <= 0:
            out += ["(cut: see the full comparison below)"]
        else:
            out += diff[:budget]
            if len(diff) > budget:
                out += ["(cut: see the full comparison below)"]
        budget -= len(diff)
        out += ["```", "</details>", ""]
    out += [f"Full comparison: {repo_url}/compare/{last[:12]}...{head[:12]}"]
    return "\n".join(out)


def main(argv) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--last", default="")
    p.add_argument("--head", required=True)
    p.add_argument("--role", required=True)
    p.add_argument("--role-name", default="")
    p.add_argument("--repo-url", default="https://github.com/stickleprojects/aws-build-runner")
    p.add_argument("files", nargs="+")
    a = p.parse_args(argv[1:])
    role = REPO / "part1-godmode" / a.role
    print(preview(a.last, a.head, a.files, role, a.role_name or role.stem, a.repo_url))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
