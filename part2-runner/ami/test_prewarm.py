#!/usr/bin/env python3
"""Offline tests for the boot prewarm (ami/files/prewarm.py) and its fields in the per-job usage record.
No AWS, no root; a fake tree in a temp dir.

    python3 -I part2-runner/ami/test_prewarm.py
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, HERE / "files" / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pw = load("prewarm", "prewarm.py")
ju = load("job_usage", "job-usage.py")
failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failed
    print(f"{'ok  ' if ok else 'FAIL'}  {name}{'' if ok else ': ' + detail}")
    failed += 0 if ok else 1


with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    (root / "godot").mkdir()
    (root / "godot" / "godot").write_bytes(b"x" * (3 * pw.CHUNK + 5))   # more than one chunk
    (root / "cache" / "uv").mkdir(parents=True)
    (root / "cache" / "uv" / "a.whl").write_bytes(b"y" * 100)
    (root / "cache" / "uv" / "link").symlink_to(root / "godot" / "godot")  # symlinks aren't read twice
    os.link(root / "cache" / "uv" / "a.whl", root / "cache" / "hard.whl")   # nor hard links
    (root / "cache" / "secret").write_bytes(b"z")
    os.chmod(root / "cache" / "secret", 0)

    roots = [str(root / "godot"), str(root / "cache" / "uv"), str(root / "cache"), str(root / "missing")]
    fs = pw.files(roots)
    check("files: in root order, Godot first", fs[0].endswith("godot/godot"), str(fs))
    check("files: each file once (overlapping roots, hard link, symlink skipped)",
          len(fs) == 3 and not any(f.endswith("link") for f in fs), str(fs))
    check("read_all: whole file, across chunks", pw.read_all(fs[0]) == 3 * pw.CHUNK + 5)
    check("read_all: missing is -1", pw.read_all(str(root / "nope")) == -1)

    r = pw.prewarm(roots, workers=4)
    as_root = os.geteuid() == 0
    check("prewarm: counts and size", r["files"] == (3 if as_root else 2)
          and r["unreadable"] == (0 if as_root else 1) and r["mb"] == round((3 * pw.CHUNK + 105 + (1 if as_root else 0)) / 2**20, 1), str(r))
    check("prewarm: only existing roots listed", r["roots"] == roots[:3], str(r["roots"]))
    check("prewarm: has an end time", isinstance(r["ended"], int) and r["ended"] > 0)

check("hot paths: the runner first, then the Godot install, Claude Code's cache last",
      pw.HOT[0].endswith("/bin") and pw.HOT.index(f"{pw.H}/.local/opt") < pw.HOT.index(f"{pw.H}/.cache/uv")
      and pw.HOT[-1].endswith("/.npm"))
check("result file is root's, not in the job user's /run/build-runner",
      str(pw.OUT) == "/run/build-runner-prewarm.json" and str(ju.PREWARM) == str(pw.OUT))

# The usage record's fields.
good = json.dumps({"seconds": 12.3, "mb": 640.0, "ended": 1000})
check("record: prewarm time, size and lead", ju.prewarm_fields(good, {"started": "1030"})
      == {"prewarm_s": 12.3, "prewarm_mb": 640.0, "prewarm_lead_s": 30.0})
check("record: still reading when the job began is negative",
      ju.prewarm_fields(good, {"started": "990"})["prewarm_lead_s"] == -10.0)
check("record: no job start, no lead", "prewarm_lead_s" not in ju.prewarm_fields(good, {}))
check("record: no prewarm file adds nothing", ju.prewarm_fields("", {"started": "1"}) == {})
check("record: junk adds nothing", ju.prewarm_fields('{"seconds": "x"}', {}) == {}
      and ju.prewarm_fields("[1]", {}) == {})

print("all passed" if not failed else f"{failed} failed")
sys.exit(1 if failed else 0)
