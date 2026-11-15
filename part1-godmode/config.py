#!/usr/bin/env python3
"""Account-specific settings, kept out of the (public) repo.

Each setting comes from the first of these that has it:
  1. environment variable            AWS_ACCOUNT_ID, AWS_CLAUDE_USER
  2. .env.local at the repo root     KEY=value lines; git-ignored
  3. GitHub repo variable            gh variable list  (visible to collaborators only)

  config.py KEY            print one setting (exit 1 if it isn't set anywhere)
  config.py --render FILE  print FILE with the placeholders filled in
  config.py                show both and where each came from

Committed files use the placeholders below instead of the real values; render() fills them in.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env.local"
PLACEHOLDERS = {"AWS_ACCOUNT_ID": "{{ACCOUNT_ID}}", "AWS_CLAUDE_USER": "{{CLAUDE_USER}}"}
VALID = {"AWS_ACCOUNT_ID": re.compile(r"\d{12}"), "AWS_CLAUDE_USER": re.compile(r"[\w+=,.@-]{1,64}")}
# Used where no real account is needed (offline checks, CI). Never a real account.
DUMMY = {"AWS_ACCOUNT_ID": "000000000000", "AWS_CLAUDE_USER": "example-claude-user"}


def _from_env_file(key):
    if not ENV_FILE.exists():
        return None
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if k.strip() == key:
                return v.strip().strip("'\"")
    return None


def _from_gh(key):
    try:
        # The API rather than `gh variable get`, which older gh versions lack.
        r = subprocess.run(["gh", "api", f"repos/{{owner}}/{{repo}}/actions/variables/{key}", "--jq", ".value"],
                           capture_output=True, text=True, cwd=REPO_ROOT, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def lookup(key):
    """(value, source) or (None, None)."""
    for source, fn in (("environment", lambda k: os.environ.get(k)), (".env.local", _from_env_file),
                       ("GitHub repo variable", _from_gh)):
        value = fn(key)
        if value:
            if not VALID[key].fullmatch(value):
                raise SystemExit(f"{key} from {source} is not valid: {value!r}")
            return value, source
    return None, None


def get(key, dummy=False):
    if dummy:
        return DUMMY[key]
    value, _ = lookup(key)
    if value is None:
        raise SystemExit(f"{key} is not set: export it, put it in {ENV_FILE.name}, or "
                         f"`gh variable set {key}`")
    return value


def settings(dummy=False):
    return {k: get(k, dummy) for k in PLACEHOLDERS}


def render(text, values):
    for key, placeholder in PLACEHOLDERS.items():
        text = text.replace(placeholder, values[key])
    if "{{" in text:
        raise SystemExit(f"unreplaced placeholder left: {re.findall(r'{{[A-Z_]+}}', text)}")
    return text


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--render":
        sys.stdout.write(render(Path(sys.argv[2]).read_text(), settings()))
    elif len(sys.argv) > 1:
        print(get(sys.argv[1]))
    else:
        for k in PLACEHOLDERS:
            v, src = lookup(k)
            print(f"{k:16} {v or '(not set)'}  {('from ' + src) if src else ''}")
