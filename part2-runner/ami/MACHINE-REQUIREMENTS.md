# Machine requirements (spellbound3d's `machine-requirements.txt`)

spellbound3d's `make check` needs a few things from the machine. This file is how it says which ones. It states
**every version `make check` needs from the machine**, in one place. It is owned by spellbound3d and read by:

- **spellbound3d's prereqs check**, in `tools/runner-smoke/`, on the laptop runner and on every new AWS image (our
  `smoke-test.sh` runs it). It checks that each tool is present, at least its minimum, and inside any range.
- **our bake** (`setup.sh`). It installs each `exact:` tool, checked against its `sha256` line, and each `distro:`
  package.

The format was agreed with spellbound3d on 2026-11-18 (Kieron asked for it to be documented on both sides).
spellbound3d's copy is in `tools/runner-smoke/README.md`, section "Machine requirements". **Keep the two the same.**
A change to the format is agreed with spellbound3d first, then made in both places.

Status (2026-11-18): agreed. setup.sh doesn't read the file yet, because the bake's user data is at its 16 KB limit:
[#48](https://github.com/stickleprojects/aws-build-runner/issues/48) comes first. Until then, setup.sh pins
`NODE_MAJOR=22` and `smoke-test.sh` checks Node ≥ 22.13 itself ([#45](https://github.com/stickleprojects/aws-build-runner/issues/45)).

## The file

`tools/runner-smoke/machine-requirements.txt` on spellbound3d's `main`. The bake reads it from the checkout it bakes from.

## Syntax

- UTF-8 text, one record per line.
- Fields are separated by one or more spaces.
- Blank lines are ignored, and so is anything from `#` to the end of the line, including after a record.
- No quoting, and no spaces inside a field.
- It must parse with POSIX sh: `while read -r kind f1 f2 f3 rest`.
- **An unknown record kind fails both readers.** Nothing is skipped silently.

## Records

| Record | Fields | Meaning |
|---|---|---|
| `tool` | `<name> <minimum> <install>` | A command on the job's `PATH`, run as ghrunner. |
| `range` | `<tool> <range> <source>` | Versions of that tool that work, and what requires them. |
| `module` | `<python3 module> distro:<apt package>` | A module that must import with the system `python3`. |
| `sha256` | `<tool> <file> <hash>` | The official release file for that tool's exact version. |

**`tool`:**
- `minimum` is a dotted version (`22.13`) or `-` for "present only". It's compared numerically, field by field.
- `install` is one of:
  - `exact:<version>`: the bake installs exactly this version from the tool's official release. **Only `node` is supported today.**
  - `distro:<apt package>`: the bake installs it from Ubuntu 24.04's packages, unpinned (apt pins break on Ubuntu updates).
  - `with:<tool>`: it comes with another tool line, e.g. `npx` with `node`.

**`range`:**
- `range` is caret ranges joined by `||` with no spaces. `^22.13.0||^24.11.0` means 22.13.0 or later within 22.x, or 24.11.0 or later within 24.x.
- `source` is what requires it, e.g. `renovate@42.99.0`. spellbound3d's test requires it to match the Makefile's `RENOVATE_VERSION`.
- It exists because a minimum alone isn't enough. Node 23.x and 24.0–24.10 pass "≥ 22.13" but crash renovate 42.
- **For the bake, it installs nothing.** setup.sh checks that the `exact:` version is inside the range, and fails the bake if not.

**`sha256`:**
- `file` is the release file name exactly, e.g. `node-v22.23.5-linux-x64.tar.xz`.
- `hash` is 64 lowercase hex characters.
- There's exactly one line per `exact:` tool and architecture. Today that's linux-x64 only (all runner types are c7i); arm64 would be a second line.
- The bake refuses a download whose hash doesn't match.

## Rules

Both sides hold to these:
- Every `exact:` tool has a `sha256` line, and the file name in that line contains the exact version.
- `minimum` ≤ `exact`.
- If a tool has a `range` line, its `exact` version is inside the range. That's tested in spellbound3d, and the bake checks it too.
- The bake **fails** if the file is missing or unreadable, has no `node` line, or has an unknown kind. There's no fallback to a version of ours.
- A new `exact:` tool, or a new record kind, needs our side changed first. spellbound3d tells us before merging one.

## How a change flows

1. A PR in spellbound3d changes the file. For example, a renovate bump moves the `range`, maybe the `minimum`, and then `exact` and its `sha256`.
2. The next image bake picks it up: the weekly one, Monday 08:30 Sydney, or a manual Actions → **ami** run.
3. `smoke-test.sh` runs spellbound3d's prereqs check on the new image before it becomes current. If it fails, the current image stays in use.

## Example (spellbound3d's file on 2026-11-18)

```
tool node 22.13 exact:22.23.5
range node ^22.13.0||^24.11.0 renovate@42.99.0
sha256 node node-v22.23.5-linux-x64.tar.xz cdeb8670a0c81f51b4dc4ac61919f9b1ad917b304bf8221eb939570663d240f8
tool npx - with:node
tool python3 - distro:python3
module venv distro:python3-venv
module ensurepip distro:python3-venv
tool git - distro:git
tool make - distro:make
tool curl - distro:curl
tool unzip - distro:unzip
```

The `sha256` was checked against nodejs.org's `SHASUMS256.txt` for v22.23.5 by both sides on 2026-11-18.
