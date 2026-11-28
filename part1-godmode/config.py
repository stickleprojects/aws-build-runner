#!/usr/bin/env python3
"""Account-specific settings, kept out of the (public) repo.

Each setting comes from the first of these that has it:
  1. environment variable            AWS_ACCOUNT_ID, AWS_CLAUDE_USER (and AWS_ALERT_EMAIL for apply.sh)
  2. the env file at the repo root   KEY=value lines; git-ignored
  3. GitHub repo variable            gh variable list  (visible to collaborators only); default target only

Two accounts (#17, #98): AWS_TARGET picks which. Unset, the env file is .env.local (the identity account,
until the move). AWS_TARGET=build-runner reads .env.build-runner.local instead, which may also set
AWS_PROFILE. A named target never falls back to the GitHub variables, which hold the default account.

  config.py KEY             print one setting (exit 1 if it isn't set anywhere)
  config.py --render FILE   print FILE with the placeholders filled in
  config.py                 show the settings, the target, and where each came from
  config.py --env           print export lines for the target: eval "$(python3 -I part1-godmode/config.py --env)"
  config.py --check-account exit 1 unless the current AWS credentials are in the target's AWS_ACCOUNT_ID
  config.py --init          (re)write the target's env file from `aws sts get-caller-identity`, run with
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
TARGET = os.environ.get("AWS_TARGET", "")
if TARGET and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,30}", TARGET):
    raise SystemExit(f"AWS_TARGET must be a short lowercase name such as build-runner, not {TARGET!r}")
ENV_FILE = REPO_ROOT / (f".env.{TARGET}.local" if TARGET else ".env.local")
PLACEHOLDERS = {"AWS_ACCOUNT_ID": "{{ACCOUNT_ID}}", "AWS_CLAUDE_USER": "{{CLAUDE_USER}}"}
VALID = {"AWS_ACCOUNT_ID": re.compile(r"\d{12}"), "AWS_CLAUDE_USER": re.compile(r"[\w+=,.@-]{1,64}"),
         "AWS_ALERT_EMAIL": re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+"),  # budget alerts; apply.sh only
         "AWS_PROFILE": re.compile(r"[\w.+-]{1,64}")}  # the CLI profile for a named target
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
    sources = [("environment", lambda k: os.environ.get(k)), (ENV_FILE.name, _from_env_file)]
    if not TARGET:   # the repo variables hold the default account: never let them fill in another target
        sources.append(("GitHub repo variable", _from_gh))
    for source, fn in sources:
        value = fn(key)
        if value:
            if key in VALID and not VALID[key].fullmatch(value):
                raise SystemExit(f"{key} from {source} is not valid: {value!r}")
            return value, source
    return None, None


def get(key, dummy=False):
    if dummy:
        return DUMMY[key]
    value, _ = lookup(key)
    if value is None:
        where = f"put it in {ENV_FILE.name}" + ("" if TARGET else f", or `gh variable set {key}`")
        raise SystemExit(f"{key} is not set for target {TARGET or 'default'}: export it, {where}")
    return value


def settings(dummy=False):
    return {k: get(k, dummy) for k in PLACEHOLDERS}


def render(text, values):
    for key, placeholder in PLACEHOLDERS.items():
        text = text.replace(placeholder, values[key])
    if "{{" in text:
        raise SystemExit(f"unreplaced placeholder left: {re.findall(r'{{[A-Z_]+}}', text)}")
    return text


def env_lines():
    """export lines that point a shell at the target: AWS_TARGET and its CLI profile. Not the account or user:
    an exported one would override the env file and go stale when the target changes; scripts read them here."""
    get("AWS_ACCOUNT_ID")   # fail now if the target isn't set up
    profile = _from_env_file("AWS_PROFILE")
    if profile and not VALID["AWS_PROFILE"].fullmatch(profile):
        raise SystemExit(f"AWS_PROFILE in {ENV_FILE.name} is not valid: {profile!r}")
    return [f"export AWS_TARGET={TARGET}" if TARGET else "unset AWS_TARGET",
            f"export AWS_PROFILE={profile}" if profile else "unset AWS_PROFILE"]


def check_account():
    """Refuse when the current credentials are in another account than the target's (#98): with two accounts,
    a profile left pointing at the wrong one would deploy or change things there."""
    want = get("AWS_ACCOUNT_ID")
    r = subprocess.run(["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"],
                       capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"aws sts get-caller-identity failed: {r.stderr.strip()[:300]}")
    have = r.stdout.strip()
    if have != want:
        raise SystemExit(f"Refusing: these AWS credentials are in account …{have[-4:]}, but the target "
                         f"({TARGET or 'default'}, {ENV_FILE.name}) is …{want[-4:]}. Check AWS_PROFILE and AWS_TARGET, "
                         f'or run: eval "$(python3 -I part1-godmode/config.py --env)"')
    print(f"account …{have[-4:]} matches the target ({TARGET or 'default'})")


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
    profile = f"AWS_PROFILE={os.environ['AWS_PROFILE']}\n" if TARGET and os.environ.get("AWS_PROFILE") else ""
    ENV_FILE.write_text(f"# Written by config.py --init from aws sts get-caller-identity. Not committed.\n"
                        f"AWS_ACCOUNT_ID={ident['Account']}\nAWS_CLAUDE_USER={user}\n{profile}")
    print(f"wrote {ENV_FILE}")


if __name__ == "__main__":
    if sys.argv[1:] == ["--init"]:
        init_env_file()
    elif sys.argv[1:] == ["--env"]:
        print("\n".join(env_lines()))
    elif sys.argv[1:] == ["--check-account"]:
        check_account()
    elif len(sys.argv) == 3 and sys.argv[1] == "--render":
        sys.stdout.write(render(Path(sys.argv[2]).read_text(), settings()))
    elif len(sys.argv) > 1:
        print(get(sys.argv[1]))
    else:
        print(f"{'target':16} {TARGET or 'default'}  ({ENV_FILE.name})")
        for k in PLACEHOLDERS:
            v, src = lookup(k)
            print(f"{k:16} {v or '(not set)'}  {('from ' + src) if src else ''}")
