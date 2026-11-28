#!/usr/bin/env python3
"""Offline tests for config.py's targets (#98): which env file a target reads, that a named target never takes
the default account's GitHub variables, --env, and --check-account. No AWS, no GitHub.

    python3 -I part1-godmode/tests/test_config.py
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
P1 = HERE.parent
failed = 0


def check(name, ok, detail=""):
    global failed
    print(f"{'ok  ' if ok else 'FAIL'}  {name}{'' if ok else ': ' + str(detail)[:300]}")
    failed += 0 if ok else 1


def load(target, root):
    """config.py as if run with AWS_TARGET=target, reading env files from root."""
    for k in ("AWS_TARGET", "AWS_ACCOUNT_ID", "AWS_CLAUDE_USER", "AWS_PROFILE"):
        os.environ.pop(k, None)
    if target is not None:
        os.environ["AWS_TARGET"] = target
    spec = importlib.util.spec_from_file_location("config_under_test", P1 / "config.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.REPO_ROOT = root
    m.ENV_FILE = root / (f".env.{m.TARGET}.local" if m.TARGET else ".env.local")
    m._from_gh = lambda key: {"AWS_ACCOUNT_ID": "999999999999", "AWS_CLAUDE_USER": "gh-user"}.get(key)
    return m


def raises(fn):
    try:
        fn()
    except SystemExit as e:
        return str(e)
    return None


with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    (root / ".env.local").write_text("AWS_ACCOUNT_ID=111111111111\nAWS_CLAUDE_USER=identity-user\n")
    (root / ".env.build-runner.local").write_text(
        "AWS_ACCOUNT_ID=222222222222\nAWS_CLAUDE_USER=runner-user\nAWS_PROFILE=build-runner\n")

    m = load(None, root)
    check("default target reads .env.local", m.get("AWS_ACCOUNT_ID") == "111111111111" and
          m.lookup("AWS_ACCOUNT_ID")[1] == ".env.local", m.lookup("AWS_ACCOUNT_ID"))
    check("--env for the default target clears the target and profile",
          m.env_lines() == ["unset AWS_TARGET", "unset AWS_PROFILE"], m.env_lines())

    m = load("build-runner", root)
    check("build-runner reads .env.build-runner.local", m.get("AWS_ACCOUNT_ID") == "222222222222"
          and m.get("AWS_CLAUDE_USER") == "runner-user", m.settings())
    check("--env for build-runner sets the target and its profile, not the account",
          m.env_lines() == ["export AWS_TARGET=build-runner", "export AWS_PROFILE=build-runner"], m.env_lines())

    (root / ".env.build-runner.local").write_text("AWS_CLAUDE_USER=runner-user\n")
    m = load("build-runner", root)
    err = raises(lambda: m.get("AWS_ACCOUNT_ID"))
    check("a named target never falls back to the GitHub variables (the default account)",
          err is not None and "999999999999" not in err and "gh variable" not in err, err)
    m = load(None, root)
    os.environ.pop("AWS_ACCOUNT_ID", None)
    (root / ".env.local").write_text("AWS_CLAUDE_USER=identity-user\n")
    check("the default target still falls back to the GitHub variable", m.get("AWS_ACCOUNT_ID") == "999999999999")

    for bad in ("Build_Runner", "../x", "a b", "x" * 40):
        os.environ["AWS_TARGET"] = bad
        r = subprocess.run([sys.executable, "-I", str(P1 / "config.py")], capture_output=True, text=True)
        check(f"refuses AWS_TARGET={bad!r}", r.returncode != 0 and "AWS_TARGET must be" in r.stderr, r.stderr)
    os.environ.pop("AWS_TARGET", None)

    (root / ".env.build-runner.local").write_text("AWS_ACCOUNT_ID=222222222222\nAWS_PROFILE=bad profile!\n")
    m = load("build-runner", root)
    check("a bad AWS_PROFILE in the env file is refused", raises(m.env_lines) is not None)

    # --check-account: compares the credentials' account with the target's, without calling AWS.
    (root / ".env.build-runner.local").write_text("AWS_ACCOUNT_ID=222222222222\nAWS_CLAUDE_USER=runner-user\n")
    m = load("build-runner", root)

    def fake_sts(account, rc=0):
        return lambda *a, **k: subprocess.CompletedProcess(a, rc, stdout=account + "\n", stderr="denied" if rc else "")
    m.subprocess.run = fake_sts("222222222222")
    check("matching account passes", raises(m.check_account) is None)
    m.subprocess.run = fake_sts("111111111111")
    err = raises(m.check_account)
    check("the other account is refused, naming only the last 4 digits",
          err is not None and "…1111" in err and "…2222" in err and "111111111111" not in err, err)
    m.subprocess.run = fake_sts("", rc=255)
    check("no credentials: refused", "get-caller-identity failed" in (raises(m.check_account) or ""))
    m.subprocess.run = subprocess.run

root_ignore = (P1.parent / ".gitignore").read_text().splitlines()
check("the targets' env files are git-ignored", ".env.local" in root_ignore and ".env.*.local" in root_ignore)

print(f"\n{failed} failed")
sys.exit(1 if failed else 0)
