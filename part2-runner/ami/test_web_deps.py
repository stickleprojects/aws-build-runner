#!/usr/bin/env python3
"""Offline tests for files/web_deps.py, the bake's reader of the web browsers' system libraries.

    python3 -I part2-runner/ami/test_web_deps.py
"""
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE / "files" / "web_deps.py"
spec = importlib.util.spec_from_file_location("web_deps", TOOL)
wd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wd)
failed = 0
# spellbound3d's file on 2026-11-24.
GOOD = """# What the web checks take from the machine.
#   browser <name> <apt packages the browser's system libraries need, or ->

browser chromium -
browser firefox -
# Found on Ubuntu 26.04 (2026-11-22)
browser webkit libmanette-0.2-0
"""


def check(name, ok, detail=""):
    global failed
    print(f"{'ok  ' if ok else 'FAIL'}  {name}{'' if ok else ': ' + str(detail)}")
    failed += 0 if ok else 1


def bad(fn, text):
    try:
        fn(text)
    except wd.Bad:
        return True
    return False


got = wd.browsers(GOOD)
check("file: browsers and extras", got == {"WEB_BROWSERS": "chromium firefox webkit", "WEB_EXTRA": "libmanette-0.2-0"}, got)
got = wd.browsers("browser webkit liba libb+\nbrowser firefox liba\n")
check("file: several packages, space-separated, once each", got["WEB_EXTRA"] == "liba libb+", got)
for name, text in {
    "an unknown kind": GOOD + "tool xvfb -\n",
    "an unknown browser": "browser opera -\n",
    "a browser twice": GOOD + "browser webkit -\n",
    "no packages field": "browser chromium\n",
    "no browser line": "# nothing\n",
    "a shell word": "browser chromium $(id)\n",
    "an apt option": "browser chromium -oDpkg::Pre-Invoke=x\n",
    "- beside a package": "browser chromium - libx\n",
    "comma-separated packages": "browser chromium liba,libb\n",
    "an end-of-line comment (whole lines only)": "browser chromium - # none\n",
}.items():
    check(f"file: {name} fails", bad(wd.browsers, text))

LIST = "Missing system dependencies (2):\n  libgtk-3-0t64\n  libstdc++6\n"
check("dry run: the names", wd.packages(LIST) == ["libgtk-3-0t64", "libstdc++6"])
check("dry run: nothing missing", wd.packages("All system dependencies are installed.\n") == [])
for name, text in {
    "empty output": "",
    "an error message": "Cannot install dependencies for this OS\n",
    "a header count that doesn't match": LIST.replace("(2)", "(3)"),
    "an apt option": LIST.replace("libstdc++6", "--allow-unauthenticated"),
    "a shell word": LIST.replace("libstdc++6", "x;reboot"),
    "a path": LIST.replace("libstdc++6", "/tmp/evil.deb"),
    "text before the list": "warning\n" + LIST,
}.items():
    check(f"dry run: {name} fails", bad(wd.packages, text))

with tempfile.TemporaryDirectory() as d:
    f = Path(d) / "r.txt"
    f.write_text(GOOD)
    run = lambda *a, i="": subprocess.run([sys.executable, "-I", str(TOOL), *a], input=i, capture_output=True, text=True)
    r = run("browsers", str(f))
    check("cli: browsers", r.returncode == 0 and "WEB_BROWSERS='chromium firefox webkit'" in r.stdout, r)
    check("cli: a missing file fails", run("browsers", d + "/none").returncode == 1)
    check("cli: a bad list fails and prints nothing", (r := run("packages", i="x")).returncode == 1 and not r.stdout, r)
sys.exit(1 if failed else 0)
