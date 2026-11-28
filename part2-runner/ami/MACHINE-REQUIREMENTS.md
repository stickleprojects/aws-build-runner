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

Status (2026-11-18): agreed. setup.sh reads the file with [files/machine_requirements.py](files/machine_requirements.py)
(tests: [test_machine_requirements.py](test_machine_requirements.py)). It installs every `distro:` package and
`node` at its `exact:` version from nodejs.org, checked against the file's `sha256`. spellbound3d's `prereqs` check,
the first check of their `godot-smoke.sh`, which `smoke-test.sh` runs on each new image, verifies the result.

**What's deliberately not on the list:** Godot, gitleaks, uv and the Python packages. spellbound3d's own scripts
install their pinned versions in every job (`tools/godot/version.env`, `tools/gitleaks/version.env`,
setup-stack's `UV_VERSION`, `uv.lock`). The image's copies of those are caches.

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
| `driver` | `<lavapipe or llvmpipe> distro:<apt package>` | A software graphics driver the render tests need. Added 2026-11-25. |
| `sha256` | `<tool> <file> <hash>` | The official release file for that tool's exact version. |

**`tool`:**
- `minimum` is a dotted version (`22.13`) or `-` for "present only". It's compared numerically, field by field.
  The prereqs check reads a tool's version from the first dotted number that `<name> --version` prints.
- `install` is one of:
  - `exact:<version>`: the bake installs exactly this version from the tool's official release. **Supported: `node` (required) and `gh` (optional; added 2026-11-18 for claude-sdlc's scripts).** Each has its official release file: `node-v<version>-linux-x64.tar.xz` from nodejs.org, `gh_<version>_linux_amd64.tar.gz` from cli/cli's releases.
  - `distro:<apt package>`: the bake installs it from Ubuntu 24.04's packages, unpinned (apt pins break on Ubuntu updates).
  - `with:<tool>`: it comes with another tool line, e.g. `npx` with `node`.

**`range`:**
- `range` is caret ranges joined by `||` with no spaces. `^22.13.0||^24.11.0` means 22.13.0 or later within 22.x, or 24.11.0 or later within 24.x.
- `source` is what requires it, e.g. `renovate@42.99.0`. spellbound3d's test
  (`tools/standards/tests/test_machine_requirements.py`) requires it to match the Makefile's `RENOVATE_VERSION`,
  so a renovate bump fails there until its `engines.node` is checked again.
- The prereqs check needs the version it finds to be inside the range, and the `exact:` version must be inside it too.
- It exists because a minimum alone isn't enough. Node 23.x and 24.0–24.10 pass "≥ 22.13" but crash renovate 42.
- **For the bake, it installs nothing.** setup.sh checks that the `exact:` version is inside the range, and fails the bake if not.

**`driver`:** (spellbound3d's change 0069)
- `name` is `lavapipe` (Vulkan) or `llvmpipe` (OpenGL). Any other name fails the bake.
- One package a line. A name may repeat, for a driver that needs more than one package.
- The bake installs the packages, with `--no-install-recommends`. spellbound3d's prereqs check starts each driver and names the one that fails.
- The display is a `tool`: `tool Xvfb - distro:xvfb`.
- The manifest lists the image's drivers (`drivers`), and the smoke test checks each one works before the image goes live (2026-11-26). `llvmpipe`: Chromium, headed under `xvfb-run -a -s "-screen 0 1600x2000x24"` with `--ignore-gpu-blocklist --use-angle=gl` (as spellbound3d's render jobs run it), must report a WebGL renderer containing `llvmpipe` and not SwiftShader. `lavapipe`: Mesa's Vulkan ICD is registered. A driver with no check fails the image, so a new driver name needs its check first.
- `libgl1-mesa-dri` as a `browser` extra in the web file (e.g. `browser chromium libgl1-mesa-dri xvfb`) also counts as the `llvmpipe` driver, so the same Chromium check runs (spellbound3d 0082, agreed 2026-11-26, until their 0069 adds driver lines here).
- Checked in a plain `ubuntu:24.04` container on 2026-11-25: with `xvfb`, `mesa-vulkan-drivers` and `libgl1-mesa-dri` installed, the loaders (`libvulkan1`, `libgl1`) came as dependencies. Under Xvfb, `vulkaninfo` and `glxinfo` both reported llvmpipe's renderer (lavapipe is Mesa's Vulkan driver on it). Godot wasn't run. `/usr` grew by 329 MB there, less on the image if its browser libraries already include some.

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

## Adding a tool

A new `tool` line can't go straight into the file: spellbound3d's CI checks every machine against it, so the line
fails every job until an image has the tool (spellbound3d PR #333, 2026-11-28). So:

1. The consumer asks us. We add its package to `AHEAD_PACKAGES` in [setup.sh](setup.sh), and a check that it runs to
   [smoke-test.sh](smoke-test.sh).
2. We bake. Once the image passes and is current, we tell the consumer.
3. The consumer adds the line; its prereqs check guards it from then on.
4. We remove the package from `AHEAD_PACKAGES` (a package in both is harmless meanwhile).

Ahead now: `ffmpeg` (ffmpeg, ffprobe) and `xvfb` (xvfb-run), for spellbound3d 0089's `movie.yml`.

## Web browsers: `tools/web/machine-requirements.txt`

A second file, owned by spellbound3d (their change 0030), for the system libraries of the browsers the web checks
use. Agreed with spellbound3d on 2026-11-24; their README states the same format. Read by
[files/web_deps.py](files/web_deps.py) (tests: [test_web_deps.py](test_web_deps.py)).

- One record per line: `browser <name> <packages>`. `name` is `chromium`, `firefox` or `webkit`, each at most once.
- `packages` is extra apt packages, separated by spaces, or `-` alone for none.
- A comment is a whole line starting with `#`; there are no end-of-line comments. Blank lines are ignored.
- **The bake fails** on an unknown kind, a malformed line, a missing file or a file with no browser line.

The bake installs, for those browsers, the packages `playwright install-deps --dry-run` names, at the Playwright
version in spellbound3d's `uv.lock`, plus the extras. Playwright runs as ghrunner and only names them; root only runs
apt, on names checked against a strict pattern. The browsers, Playwright and the Web templates aren't baked: jobs
install them. `smoke-test.sh` downloads each browser and needs it to start with WebGL 2 before the image becomes
current (Firefox under `xvfb-run`: it has no WebGL 2 without a display, even headless).
