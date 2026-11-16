#!/usr/bin/env python3
"""Print the cloud-init user data for the bake instance: setup.sh plus files/, as ONE xz-compressed tar,
base64'd into a #cloud-config, unpacked to /opt/bake and setup.sh run once at first boot.

One archive, not a gzip per file: EC2 user data is limited to 16 KB, and per-file gzip had reached 15 KB of it
(2026-11-16). One xz archive is ≈ 25% smaller. The tar is deterministic (sorted, mtime 0, root-owned).
If this still outgrows 16 KB, fetch the bundle from S3 instead (issue: bake bundle via a presigned URL).

    make-user-data.py           the user data
    make-user-data.py --files   the bundle's files and sizes (what's inside, for checks and reviews)
"""
import base64
import io
import lzma
import sys
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIMIT = 16384


def members() -> list[tuple[str, Path, int]]:
    out = [("setup.sh", HERE / "setup.sh", 0o755)]
    for f in sorted((HERE / "files").iterdir()):
        if not f.is_file() or f.name.startswith("."):
            continue  # skip __pycache__ and the like
        out.append((f"files/{f.name}", f, 0o755 if f.suffix in (".sh", ".py") else 0o644))
    return out


def bundle() -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, src, mode in members():
            data = src.read_bytes()
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime, info.uid, info.gid = len(data), mode, 0, 0, 0
            tar.addfile(info, io.BytesIO(data))
    return lzma.compress(raw.getvalue(), preset=9 | lzma.PRESET_EXTREME)


def main() -> int:
    if "--files" in sys.argv[1:]:
        for name, src, _ in members():
            print(f"{src.stat().st_size:7d}  {name}")
        return 0
    data = base64.b64encode(bundle()).decode()
    text = ("#cloud-config\n"
            "# Bakes the build-runner AMI; see part2-runner/ami/setup.sh.\n"
            "write_files:\n"
            f"  - path: /opt/bake/bundle.txz\n    permissions: '0600'\n    encoding: b64\n    content: {data}\n"
            "runcmd:\n"
            "  - [tar, -xJf, /opt/bake/bundle.txz, -C, /opt/bake, --no-same-owner]\n"
            "  - [bash, /opt/bake/setup.sh]\n")
    if len(text.encode()) > LIMIT:
        raise SystemExit(f"user data is {len(text.encode())} bytes; EC2 allows {LIMIT}")
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
