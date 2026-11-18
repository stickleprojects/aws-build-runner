#!/usr/bin/env python3
"""The bake's bundle (setup.sh plus files/, ONE deterministic xz tar) and its cloud-init user data.

    make-user-data.py --bundle OUT            write the bundle to OUT; print its SHA-256
    make-user-data.py BUCKET KEY SHA256       the user data: fetch.py, which gets the bundle from S3 (the bake
                                              role may read bake/* only), checks its SHA-256, then unpacks it
                                              to /opt/bake and runs setup.sh once at first boot
    make-user-data.py --files                 the bundle's files and sizes (what's inside, for checks and reviews)

EC2 user data is limited to 16 KB. The bundle used to travel inside it and reached 16,364 bytes (2026-11-18),
so it now goes through S3 (#48) and the user data stays a few KB whatever setup.sh grows to. The tar is
deterministic (sorted, mtime 0, root-owned), so an unchanged bundle has an unchanged hash and key.
"""
import base64
import hashlib
import io
import lzma
import re
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


def user_data(bucket: str, key: str, sha256: str) -> str:
    fetch = base64.b64encode(lzma.compress((HERE / "fetch.py").read_bytes(), preset=9)).decode()
    # A failed fetch reports FAILED (fetch.py) and powers off, so bake.sh stops at once.
    return ("#cloud-config\n"
            "# Bakes the build-runner AMI; see part2-runner/ami/setup.sh and make-user-data.py.\n"
            "write_files:\n"
            f"  - path: /opt/bake/fetch.py.xz\n    permissions: '0600'\n    encoding: b64\n    content: {fetch}\n"
            "runcmd:\n"
            "  - [sh, -c, 'xz -d /opt/bake/fetch.py.xz && python3 /opt/bake/fetch.py \"$0\" \"$1\" \"$2\" "
            "/opt/bake/bundle.txz && tar -xJf /opt/bake/bundle.txz -C /opt/bake --no-same-owner "
            "&& exec bash /opt/bake/setup.sh; poweroff', "
            f"{bucket}, {key}, {sha256}]\n")


def main() -> int:
    args = sys.argv[1:]
    if args[:1] == ["--files"]:
        for name, src, _ in members():
            print(f"{src.stat().st_size:7d}  {name}")
        return 0
    if args[:1] == ["--bundle"] and len(args) == 2:
        data = bundle()
        Path(args[1]).write_bytes(data)
        print(hashlib.sha256(data).hexdigest())
        return 0
    if len(args) != 3:
        raise SystemExit(__doc__)
    bucket, key, sha256 = args
    if not re.fullmatch(r"[a-z0-9.-]{3,63}", bucket) or not re.fullmatch(r"bake/[0-9a-f]{64}\.txz", key) \
            or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise SystemExit(f"make-user-data.py: unexpected bucket, key or SHA-256: {args}")
    text = user_data(bucket, key, sha256)
    if len(text.encode()) > LIMIT:
        raise SystemExit(f"user data is {len(text.encode())} bytes; EC2 allows {LIMIT}")
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
