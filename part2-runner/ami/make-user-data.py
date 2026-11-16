#!/usr/bin/env python3
"""Print the cloud-init user data for the bake instance: setup.sh plus files/, gzipped and base64'd
into a #cloud-config (EC2 user data is limited to 16 KB), with setup.sh run once at first boot."""
import base64
import gzip
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIMIT = 16384


def entry(src: Path, dest: str, mode: str) -> str:
    data = base64.b64encode(gzip.compress(src.read_bytes(), mtime=0)).decode()
    return (f"  - path: {dest}\n    permissions: '{mode}'\n    encoding: gz+b64\n"
            f"    content: {data}\n")


def main() -> int:
    files = [entry(HERE / "setup.sh", "/opt/bake/setup.sh", "0755")]
    for f in sorted((HERE / "files").iterdir()):
        if not f.is_file() or f.name.startswith("."):
            continue  # skip __pycache__ and the like
        files.append(entry(f, f"/opt/bake/files/{f.name}", "0755" if f.suffix in (".sh", ".py") else "0644"))
    text = ("#cloud-config\n"
            "# Bakes the build-runner AMI; see part2-runner/ami/setup.sh.\n"
            "write_files:\n" + "".join(files) +
            "runcmd:\n  - [bash, /opt/bake/setup.sh]\n")
    if len(text.encode()) > LIMIT:
        raise SystemExit(f"user data is {len(text.encode())} bytes; EC2 allows {LIMIT}")
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
