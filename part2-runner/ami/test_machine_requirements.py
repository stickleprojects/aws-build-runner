#!/usr/bin/env python3
"""Offline tests for files/machine_requirements.py, the bake's reader of spellbound3d's machine-requirements.txt.

    python3 -I part2-runner/ami/test_machine_requirements.py
"""
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("mr", HERE / "files" / "machine_requirements.py")
mr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mr)
failed = 0
H = "cdeb8670a0c81f51b4dc4ac61919f9b1ad917b304bf8221eb939570663d240f8"
# spellbound3d's file as agreed (their PR #207, 2026-11-18), comments and all.
GOOD = f"""# What `make check` takes from the machine.
#   tool <name> <minimum, or -> <install>
tool node 22.13 exact:22.23.5
range node ^22.13.0||^24.11.0 renovate@42.99.0
sha256 node node-v22.23.5-linux-x64.tar.xz {H}
tool npx - with:node

tool python3 - distro:python3   # setup-stack's venv
module venv distro:python3-venv
module ensurepip distro:python3-venv
tool git - distro:git
tool make - distro:make
tool curl - distro:curl
tool unzip - distro:unzip
"""


def check(name, ok, detail=""):
    global failed
    print(f"{'ok  ' if ok else 'FAIL'}  {name}{'' if ok else ': ' + str(detail)}")
    failed += 0 if ok else 1


def bad(text):
    try:
        mr.parse(text)
    except mr.Bad as e:
        return str(e)
    return None


out = mr.parse(GOOD)
check("good: node pin, file and hash", (out["NODE_VERSION"], out["NODE_FILE"], out["NODE_SHA256"])
      == ("22.23.5", "node-v22.23.5-linux-x64.tar.xz", H), out)
check("good: every distro package once, in order",
      out["APT_PACKAGES"] == "python3 git make curl unzip python3-venv", out["APT_PACKAGES"])

swap = GOOD.replace

check("no node line fails", bad("tool git - distro:git\n") is not None)
check("node not exact fails", bad(swap("tool node 22.13 exact:22.23.5", "tool node 22.13 distro:nodejs")) is not None)
check("an unknown kind fails, naming the line", "line" in (bad(GOOD + "pin node 22\n") or ""))
check("a malformed line fails", bad(GOOD + "tool git distro:git\n") is not None)
check("exact: for a tool the bake can't install fails", bad(GOOD + "tool git - exact:2.43.0\n") is not None)
check("exact below its minimum fails", "minimum" in (bad(swap("tool node 22.13", "tool node 22.30")) or ""))
check("exact outside its range fails (23.x passes >= 22.13 but not ^22.13||^24.11)",
      "range" in (bad(swap("exact:22.23.5", "exact:23.1.0").replace("node-v22.23.5", "node-v23.1.0")) or ""))
check("24.0 is outside ^24.11.0", not mr.in_range("24.0.1", "^22.13.0||^24.11.0"))
check("24.11.0 is inside", mr.in_range("24.11.0", "^22.13.0||^24.11.0"))
check("22.12 is outside ^22.13.0", not mr.in_range("22.12.9", "^22.13.0||^24.11.0"))
check("no sha256 line fails", "sha256" in (bad("\n".join(l for l in GOOD.splitlines() if not l.startswith("sha256"))) or ""))
check("a sha256 for another version fails",
      bad(swap("node-v22.23.5-linux-x64.tar.xz", "node-v22.23.4-linux-x64.tar.xz")) is not None)
check("a malformed hash fails", bad(swap(H, H.upper())) is not None)
check("a bad apt package name fails", bad(GOOD + "tool x - distro:Bad;rm\n") is not None)
check("with: a tool that isn't listed fails", bad(GOOD + "tool npm - with:nothing\n") is not None)
check("a 0.x caret range is refused, not guessed", bad(GOOD.replace("^22.13.0||^24.11.0", "^0.1.0")) is not None)

GH = "4c806978ffce1cec70853302a56d70ac6da4f19bae7adca4e1a8ab00633dd91d"
withgh = GOOD + f"tool gh 2.46 exact:2.103.3\nsha256 gh gh_2.103.3_linux_amd64.tar.gz {GH}\n"
o = mr.parse(withgh)
check("gh exact: pinned with its release file and hash",
      (o.get("GH_VERSION"), o.get("GH_FILE"), o.get("GH_SHA256")) == ("2.103.3", "gh_2.103.3_linux_amd64.tar.gz", GH), o)
check("gh is optional: no GH_ lines without it", not any(k.startswith("GH_") for k in mr.parse(GOOD)))
check("gh without its sha256 line fails", bad(GOOD + "tool gh 2.46 exact:2.103.3\n") is not None)
check("gh with the wrong release file fails",
      bad(GOOD + f"tool gh 2.46 exact:2.103.3\nsha256 gh gh_2.103.3_linux_arm64.tar.gz {GH}\n") is not None)
check("gh below its minimum fails", "minimum" in (bad(GOOD + f"tool gh 2.200 exact:2.103.3\nsha256 gh gh_2.103.3_linux_amd64.tar.gz {GH}\n") or ""))
check("another exact: tool is still refused", bad(GOOD + "tool jq - exact:1.7.1\n") is not None)

# The CLI prints shell assignments that eval safely; a missing file exits 1.
with tempfile.TemporaryDirectory() as d:
    f = Path(d) / "machine-requirements.txt"
    f.write_text(GOOD)
    r = subprocess.run([sys.executable, "-I", str(HERE / "files" / "machine_requirements.py"), str(f)],
                       capture_output=True, text=True)
    sh = subprocess.run(["bash", "-c", f'set -eu; eval "$1"; echo "$NODE_VERSION|$APT_PACKAGES"', "_", r.stdout],
                        capture_output=True, text=True)
    check("cli: output evals in bash", r.returncode == 0 and sh.stdout.strip() == "22.23.5|python3 git make curl unzip python3-venv",
          (r.stderr, sh.stdout, sh.stderr))
    r = subprocess.run([sys.executable, "-I", str(HERE / "files" / "machine_requirements.py"), str(Path(d) / "missing")],
                       capture_output=True, text=True)
    check("cli: a missing file exits 1", r.returncode == 1 and "can't read" in r.stderr, r.stderr)

print(f"\n{failed} failed")
sys.exit(1 if failed else 0)
