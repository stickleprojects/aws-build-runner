#!/usr/bin/env python3
"""Reads the files a job touches first, at boot, so the job doesn't wait on them (build-runner-prewarm.service).

A new instance's disk is made from the AMI's snapshot, and EBS fetches each block from S3 the first time
it's read. A job on a fresh instance paid that on its first touch of each tool: the first `godot --version`
took up to 18 s and unzipping Godot up to 17 s. On an instance that had been up 85 s before its job, the
same steps took under a second (spellbound3d's setup breakdown, 2026-11-19). This reads those files in
parallel while the runner is still registering and waiting for its job, so the job finds them fetched and,
mostly, already in the page cache.

    prewarm.py             read the hot paths, write /run/build-runner-prewarm.json, print a summary
    prewarm.py --dry-run   list what would be read (count and size per path), read nothing

It never fails the boot: a missing path is skipped and unreadable files are counted. It runs beside the
runner, not before it, so it never delays a job's start. Standard library only.
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

R = "/opt/github-runners/spellbound3d"
H = f"{R}/home"
# In the order a job first needs them: the runner (it starts at once), then setup-stack's tools (Godot,
# uv, the caches it checks), then make check's (runner-smoke, gitleaks, gdtoolkit), then the rest.
HOT = [
    f"{R}/bin", f"{R}/externals",
    f"{H}/.local/opt",           # the Godot install (install-godot.sh's target)
    f"{H}/.cache/godot",         # the pinned Godot zip (setup-stack checks its SHA-512)
    "/opt/uv", f"{H}/.cache/pip",
    f"{H}/.cache/uv",            # spellbound3d's uv.lock packages
    "/opt/build-runner",         # spellbound3d's runner-smoke
    f"{H}/.local/bin", f"{H}/.cache",
    "/usr/lib/git-core", "/usr/local/bin", "/usr/local/lib/node_modules", "/usr/lib/python3",
    f"{H}/.npm",                 # Claude Code's cache: light jobs only, so last
]
# Root's, outside /run/build-runner (which the job user owns, and which may not exist yet at this point).
OUT = Path("/run/build-runner-prewarm.json")
WORKERS = 16                     # EBS fetches faster with several reads in flight
CHUNK = 1 << 20


def files(roots: list[str]) -> list[str]:
    """Regular files under each root, in root order, each file once (roots overlap, e.g. .cache)."""
    seen: set[tuple[int, int]] = set()
    out = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for name in sorted(filenames):
                p = os.path.join(dirpath, name)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                key = (st.st_dev, st.st_ino)
                if not (st.st_mode & 0o170000 == 0o100000) or key in seen:
                    continue  # symlinks, sockets and the like; hard links and repeats once
                seen.add(key)
                out.append(p)
    return out


def read_all(path: str) -> int:
    """Bytes read from path; -1 if it couldn't be read."""
    n = 0
    buf = bytearray(CHUNK)
    try:
        with open(path, "rb", buffering=0) as f:
            while (got := f.readinto(buf)):
                n += got
    except OSError:
        return -1
    return n


def prewarm(roots: list[str], workers: int = WORKERS) -> dict:
    start = time.monotonic()
    todo = files(roots)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        sizes = list(pool.map(read_all, todo, chunksize=8))
    ok = [s for s in sizes if s >= 0]
    return {"msg": "prewarm", "seconds": round(time.monotonic() - start, 1), "files": len(ok),
            "mb": round(sum(ok) / 2**20, 1), "unreadable": len(sizes) - len(ok), "ended": int(time.time()),
            "roots": [r for r in roots if os.path.isdir(r)]}


def main() -> int:
    if "--dry-run" in sys.argv[1:]:
        for root in HOT:
            fs = files([root])
            print(f"{root}: {len(fs)} files, {sum(os.lstat(f).st_size for f in fs) / 2**20:.1f} MB")
        return 0
    result = prewarm(HOT)
    print(json.dumps(result))
    try:
        OUT.write_text(json.dumps(result) + "\n")
    except OSError as e:
        print(f"prewarm: couldn't write {OUT}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
