#!/usr/bin/env python3
"""Account-specific settings, kept out of the (public) repo.

Each setting comes from the first of these that has it:
  1. environment variable            AWS_ACCOUNT_ID, AWS_CLAUDE_USER
  2. .env.local at the repo root     KEY=value lines; git-ignored
  3. GitHub repo variable            gh variable list  (visible to collaborators only)

  config.py KEY            print one setting (exit 1 if it isn't set anywhere)
  config.py --render FILE  print FILE with the placeholders filled in
  config.py                show both and where each came from
  config.py --init         (re)write .env.local from `aws sts get-caller-identity`, run with
                           Claude's IAM user credentials (e.g. after a git clean wiped it)

In .env.local a value may refer to another environment variable: AWS_ACCOUNT_ID=$MY_AWS_ACCOUNT
(or ${MY_AWS_ACCOUNT}), with `export MY_AWS_ACCOUNT=...` in ~/.bashrc. Single quotes are literal.

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


_REFERENCE = re.compile(r"\$(?:\{(\w+)\}|(\w+))")


def _expand(value, key):
    """Fill in $NAME / ${NAME} from the environment, e.g. AWS_ACCOUNT_ID=$MY_AWS_ACCOUNT in .env.local
    with `export MY_AWS_ACCOUNT=...` in ~/.bashrc. A reference to an unset variable is an error."""
    def sub(m):
        name = m.group(1) or m.group(2)
        if name not in os.environ:
            raise SystemExit(f"{key} in {ENV_FILE.name} refers to ${name}, which isn't set. "
                             f"Is it exported (export {name}=...) in ~/.bashrc, and is this a new shell?")
        return os.environ[name]
    return _REFERENCE.sub(sub, value)


def _from_env_file(key):
    if not ENV_FILE.exists():
        return None
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if k.strip() == key:
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] == "'":   # single quotes: literal, as in a shell
                    return v[1:-1]
                return _expand(v.strip('"'), key)
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


def init_env_file():
    r = subprocess.run(["aws", "sts", "get-caller-identity", "--output", "json"], capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"aws sts get-caller-identity failed: {r.stderr.strip()}")
    import json
    ident = json.loads(r.stdout)
    arn = ident["Arn"]
    if ":user/" not in arn:
        raise SystemExit(f"These credentials are {arn}, not an IAM user. Run --init with Claude's IAM user "
                         "(the laptop's default profile), not godmode credentials.")
    user = arn.rsplit("/", 1)[-1]
    if ENV_FILE.exists():
        ENV_FILE.replace(ENV_FILE.with_name(ENV_FILE.name + ".bak"))
        print(f"kept the old file as {ENV_FILE.name}.bak")
    ENV_FILE.write_text(f"# Written by config.py --init from aws sts get-caller-identity. Not committed.\n"
                        f"AWS_ACCOUNT_ID={ident['Account']}\nAWS_CLAUDE_USER={user}\n")
    print(f"wrote {ENV_FILE}")


if __name__ == "__main__":
    if sys.argv[1:] == ["--init"]:
        init_env_file()
    elif len(sys.argv) == 3 and sys.argv[1] == "--render":
        sys.stdout.write(render(Path(sys.argv[2]).read_text(), settings()))
    elif len(sys.argv) > 1:
        print(get(sys.argv[1]))
    else:
        for k in PLACEHOLDERS:
            v, src = lookup(k)
            print(f"{k:16} {v or '(not set)'}  {('from ' + src) if src else ''}")
