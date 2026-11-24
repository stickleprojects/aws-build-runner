#!/usr/bin/env python3
"""The web browsers' system libraries, for the bake (format: ../MACHINE-REQUIREMENTS.md, "Web browsers").

    web_deps.py browsers FILE    read spellbound3d's tools/web/machine-requirements.txt; print shell assignments:
                                   WEB_BROWSERS='chromium firefox webkit'   WEB_EXTRA='libmanette-0.2-0'
    web_deps.py packages         read `playwright install-deps --dry-run` on stdin; print its package names

The first command's output is eval'd and the second's goes to apt as root, so every word is checked against a
strict pattern, and anything unexpected fails the bake (exit 1, the reason on stderr).
"""
import re
import shlex
import sys

BROWSERS = ("chromium", "firefox", "webkit")
PACKAGE = re.compile(r"[a-z0-9][a-z0-9.+-]+")
MISSING = re.compile(r"Missing system dependencies \((\d+)\):")
NONE_MISSING = "All system dependencies are installed."


class Bad(Exception):
    pass


def browsers(text: str) -> dict:
    """`browser <name> <packages, space-separated, or ->`; a comment is a whole line starting with #."""
    found, extra = [], []
    for n, raw in enumerate(text.splitlines(), 1):
        f = raw.split()
        if not f or f[0].startswith("#"):
            continue
        if f[0] != "browser" or len(f) < 3:
            raise Bad(f"line {n}: unknown or malformed line {raw.strip()!r}")
        name, packages = f[1], f[2:]
        if name not in BROWSERS:
            raise Bad(f"line {n}: {name!r} isn't one of {', '.join(BROWSERS)}")
        if name in found:
            raise Bad(f"line {n}: {name} is listed twice")
        found.append(name)
        if packages == ["-"]:
            continue
        for p in packages:
            if not PACKAGE.fullmatch(p):
                raise Bad(f"line {n}: {p!r} isn't an apt package name")
        extra += packages
    if not found:
        raise Bad("no browser line")
    return {"WEB_BROWSERS": " ".join(found), "WEB_EXTRA": " ".join(dict.fromkeys(extra))}


def packages(text: str) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines == [NONE_MISSING]:
        return []
    m = MISSING.fullmatch(lines[0]) if lines else None
    if not m:
        raise Bad(f"not a dry-run list: {' '.join(lines)[:200]!r}")
    names = lines[1:]
    for p in names:
        if not PACKAGE.fullmatch(p):
            raise Bad(f"{p!r} isn't an apt package name")
    if len(names) != int(m.group(1)):
        raise Bad(f"the list says {m.group(1)} packages and has {len(names)}")
    return names


def main(argv) -> int:
    try:
        if len(argv) == 3 and argv[1] == "browsers":
            with open(argv[2], encoding="utf-8") as fh:
                for k, v in browsers(fh.read()).items():
                    print(f"{k}={shlex.quote(v)}")
        elif argv[1:] == ["packages"]:
            print(" ".join(packages(sys.stdin.read())))
        else:
            raise Bad("usage: web_deps.py browsers FILE | packages")
    except (OSError, UnicodeDecodeError, Bad) as e:
        print(f"web_deps.py: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
