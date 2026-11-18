#!/usr/bin/env python3
"""Read spellbound3d's tools/runner-smoke/machine-requirements.txt for the bake (format: ../MACHINE-REQUIREMENTS.md).

    machine_requirements.py FILE     print what setup.sh installs, as shell assignments it can eval:
                                       APT_PACKAGES='python3 git ...'   every distro: package
                                       NODE_VERSION=22.23.5 NODE_FILE=node-v22.23.5-linux-x64.tar.xz NODE_SHA256=...
                                       GH_VERSION=2.103.3 GH_FILE=gh_2.103.3_linux_amd64.tar.gz GH_SHA256=...
                                         (the GH_ lines only when the file pins gh; node is required)

Fails (exit 1, the reason on stderr) on anything the agreed format doesn't allow, so the bake never
guesses: a missing file, no node line, an unknown kind, an exact: tool other than node, an exact
version below its minimum or outside its range, or a missing or malformed sha256 line.
Every printed value is checked against a strict pattern first, so eval-ing it is safe.
"""
import re
import shlex
import sys

VERSION = re.compile(r"\d+(\.\d+)*")
PACKAGE = re.compile(r"[a-z0-9][a-z0-9.+-]+")
HASH = re.compile(r"[0-9a-f]{64}")
# The exact: tools the bake knows how to install: each one's official release file name (node agreed 2026-11-18;
# gh added 2026-11-18 because claude-sdlc's scripts need it and Ubuntu 24.04's is 2.45). A new one is a bake change first.
EXACT_TOOLS = {"node": "node-v{v}-linux-x64.tar.xz", "gh": "gh_{v}_linux_amd64.tar.gz"}


class Bad(Exception):
    pass


def numbers(v: str) -> tuple:
    return tuple(int(x) for x in v.split("."))


def at_least(v: str, minimum: str) -> bool:
    a, b = numbers(v), numbers(minimum)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) >= b + (0,) * (n - len(b))


def in_range(v: str, spec: str) -> bool:
    """Caret ranges joined by || (^22.13.0||^24.11.0): same major, at least that version."""
    for part in spec.split("||"):
        m = re.fullmatch(r"\^(\d+)\.(\d+)\.(\d+)", part)
        if not m or m.group(1) == "0":
            raise Bad(f"unsupported range part {part!r} in {spec!r}")
        lo = ".".join(m.groups())
        if numbers(v)[0] == int(m.group(1)) and at_least(v, lo):
            return True
    return False


def parse(text: str) -> dict:
    tools, modules, hashes, ranges = {}, [], {}, {}
    for n, raw in enumerate(text.splitlines(), 1):
        f = raw.split("#", 1)[0].split()
        if not f:
            continue
        kind, args = f[0], f[1:]
        where = f"line {n}"
        if kind == "tool" and len(args) == 3:
            name, minimum, install = args
            if minimum != "-" and not VERSION.fullmatch(minimum):
                raise Bad(f"{where}: minimum {minimum!r} isn't a dotted version or -")
            how, _, what = install.partition(":")
            if how not in ("exact", "distro", "with") or not what:
                raise Bad(f"{where}: install {install!r} isn't exact:, distro: or with:")
            if how == "exact" and (name not in EXACT_TOOLS or not VERSION.fullmatch(what)):
                raise Bad(f"{where}: exact: is only supported for {', '.join(EXACT_TOOLS)}, with a dotted version")
            if how == "distro" and not PACKAGE.fullmatch(what):
                raise Bad(f"{where}: {what!r} isn't an apt package name")
            tools[name] = {"minimum": minimum, "how": how, "what": what}
        elif kind == "module" and len(args) == 2:
            how, _, pkg = args[1].partition(":")
            if how != "distro" or not PACKAGE.fullmatch(pkg):
                raise Bad(f"{where}: module needs distro:<apt package>, not {args[1]!r}")
            modules.append(pkg)
        elif kind == "sha256" and len(args) == 3:
            if not HASH.fullmatch(args[2]):
                raise Bad(f"{where}: {args[2]!r} isn't 64 lowercase hex digits")
            hashes[args[0]] = (args[1], args[2])
        elif kind == "range" and len(args) == 3:
            ranges[args[0]] = args[1]
        else:
            raise Bad(f"{where}: unknown or malformed line {raw.strip()!r}")
    node = tools.get("node")
    if not node or node["how"] != "exact":
        raise Bad("no 'tool node <minimum> exact:<version>' line")
    for name, t in tools.items():
        if t["how"] == "with" and t["what"] not in tools:
            raise Bad(f"{name} comes with:{t['what']}, which isn't listed")
        if t["how"] != "exact":
            continue
        v = t["what"]
        if t["minimum"] != "-" and not at_least(v, t["minimum"]):
            raise Bad(f"{name} exact:{v} is below its minimum {t['minimum']}")
        if name in ranges and not in_range(v, ranges[name]):
            raise Bad(f"{name} exact:{v} is outside its range {ranges[name]}")
        want = EXACT_TOOLS[name].format(v=v)
        if hashes.get(name, ("",))[0] != want:
            raise Bad(f"{name} needs 'sha256 {name} {want} <hash>'")
    packages = [t["what"] for t in tools.values() if t["how"] == "distro"] + modules
    out = {"APT_PACKAGES": " ".join(dict.fromkeys(packages))}
    for name in EXACT_TOOLS:
        t = tools.get(name)
        if t and t["how"] == "exact":
            key = name.upper()
            out.update({f"{key}_VERSION": t["what"], f"{key}_FILE": hashes[name][0], f"{key}_SHA256": hashes[name][1]})
    return out


def main(argv) -> int:
    try:
        with open(argv[1], encoding="utf-8") as fh:
            out = parse(fh.read())
    except (OSError, IndexError) as e:
        print(f"machine_requirements.py: can't read the requirements file: {e}", file=sys.stderr)
        return 1
    except Bad as e:
        print(f"machine_requirements.py: {argv[1]}: {e}", file=sys.stderr)
        return 1
    for k, v in out.items():
        print(f"{k}={shlex.quote(v)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
