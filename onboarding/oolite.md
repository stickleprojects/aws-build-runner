# Oolite: onboarding assessment (pilot)

Status: **draft, 2026-11-25.** Facts are from the oolite session's survey (2026-11-25), which Kieron hasn't reviewed. Nothing
is decided or built. Prices are US$ estimates for Sydney, from [PRICING.md](../PRICING.md). **M** = measured, **D** = documented,
**G** = guess, as oolite marked them.

## Summary

- **Recommendation: not now (option D).** No job fits well: nearly all need root, Docker or a privileged container, which
  our runners don't give, or Windows or macOS. The one that plausibly fits is a 20-second format check. Oolite's own view is
  that none is worth moving.
- **Revisit if** Kieron wants Docker-capable runners for other projects too, or the UI tests come back into use and become
  a bottleneck.
- **Image and cost:** no image needed while the answer is D. If the UI tests ever moved: at most about US$0.19 a run on spot, below.

## The project and its requirements

A space game in Objective-C, built with meson; GNUstep is built from source in the Linux jobs (D). Repo:
`stickleprojects/oolite`, a public fork with one collaborator (M). Upstream's CI isn't ours to move. No submodules (M).

Durations are from one cold run on 2026-11-18, all GitHub-hosted (M); about 8 minutes end to end.

| Job | Runs in | Time | Fits our runners today? |
|---|---|---|---|
| clang-format-check | ubuntu-latest | 20 s | Plausibly; too short to matter |
| build-github-pages | ubuntu-latest, sudo apt | 2m55s | No: sudo |
| build-appimage | archlinux container | 3m36s | No: container, root |
| build-ubuntu, with tests | ubuntu:26.04 container | 5m41s | No: container, root |
| build-fedora | fedora container | 3m01s | No: container, root |
| build-flatpak | privileged container | skipped, unknown | No: privileged |
| build-windows | Windows | 7m41s | No: not offered |
| build-macos | macOS | 3m32s | No: not offered |
| ui-tests, 5 jobs (disabled by hand at present) | Docker desktop images, software-rendered | 6m40s–19m30s, in failing runs; passing time unknown | No: Docker. Best candidate if that changed |
| framework-tests | sudo apt | 2m08s | No: sudo |

- **Volume:** bursty. 100 runs from 29 October to 18 November (M): 2 to 32 on active days, none for days between. Busiest hour unknown.
- **Curfew:** probably fine (G). Urgency low (G).
- **Needs network:** distro mirrors and GitHub (D). **Secrets:** only `GITHUB_TOKEN` (D).
- **Trust:** a public repo whose fork pull requests can run CI, so outside code must be shut out first (see the [security gate](README.md#security-gate)).
- **Caches:** one Actions cache, 7.5 MB (M). Artifact sizes unknown.
- **Unknown:** peak memory, CPU scaling, disk per job, incremental build time. The Wayland test image is about 4 GB (D); 30 GiB may be tight for all of them (G).
- **What AWS could add:** nothing GitHub-hosted runners can't do, except a GPU or real desktop, which we don't offer.

## Image

| Option | For | Against | Extra fixed cost a month |
|---|---|---|---|
| **A. Shared image, plus oolite's packages** | One bake, smoke test and weekly rebuild | Every project's tools reach every job; a bad package line can block everyone's bake | ≈ US$0.17 per extra GB of image (3 kept) |
| **B. Own image** | Isolated; can fail without affecting others | Second bake pipeline; needs `max_kept_amis` (3) or pruning reworked | ≈ US$1.75 for 3 images of ≈ 10.5 GB, plus ≈ US$0.20 for weekly bakes |
| **C. Generic base, installed per job** | No image work | Adds minutes to each job, and jobs have no sudo | US$0 fixed; the extra minutes cost as below |
| **D. No AWS runner** | No work; existing runners stay | None of the benefits | US$0 |

**D.** A and B don't solve it: oolite's builds need other distros (Arch, Fedora, Ubuntu 26.04) or Docker, not more
packages on Ubuntu 24.04. C fails because jobs have no root. Supporting them means Docker on the runners, a guardrail change
that needs its own review.

## Cost

Per instance-hour, including the public IPv4 address (US$0.005) and the 30 GiB root volume (≈ US$0.004):

| Runner | Spot | On-demand fallback |
|---|---|---|
| 2 vCPU (c7i.large) | ≈ US$0.057 (2026-11-16 price) | not priced in PRICING.md; ≈ 2.5× spot |
| 4 vCPU (c7i.xlarge) | ≈ US$0.10–0.11 | ≈ US$0.24 |

If Docker were allowed, what oolite's jobs would cost on 4 vCPU. **Upper-end estimates:** hosted times carry over, one minute
of boot per job, one machine per job.

| Work | Machine time per run | Spot | On demand | Runs, 29 Oct–18 Nov | Those runs, spot / on demand |
|---|---|---|---|---|---|
| UI tests (5 jobs at 19m30s) | ≈ 1.7 h | ≈ US$0.19 | ≈ US$0.41 | 26 | ≈ US$5 / US$11 |
| Linux jobs (pages, AppImage, Ubuntu, Fedora, format) | ≈ 0.34 h | ≈ US$0.04 | ≈ US$0.08 | 41 | ≈ US$1.50 / US$3.40 |

Flatpak isn't included: its time is unknown. Those runs are about three weeks, and the UI tests are off now, so the current cost would be lower.

**Headroom under the US$29 stop:** about US$12 at November's recent rate (≈ US$17 a month, 19–23 November), but only
about US$4 in the busiest month modelled (US$24–25). Credits hide usage from the stop while they last.
GitHub-hosted standard runners are free for public repos (general knowledge, to be confirmed), so AWS would need a
reason other than price.

## What must change on the platform

- `config.json` names one repo, and the pools reader, the bake and the smoke test are spellbound3d's. Serving a second
  consumer needs the registry in [#54](https://github.com/stickleprojects/aws-build-runner/issues/54), phase 4.
- The actor and repo allowlists are baked into the image (`ami/files/`), so a new repo means a rebake.
- The GitHub App must be able to read the project's repo, and register runners for it.
- Runners are shared: the 4-runner cap needs a fair share or a queue order, or one project can starve another.
- Usage reports need cost per project.

- Docker for jobs, the blocker for every job worth moving: a guardrail change, not proposed here.

Effort isn't estimated, as the recommendation is not to proceed.

## Decisions for Kieron

1. Accept "not now"? If not, is Docker on the runners worth a separate design and security review?
2. Keep the onboarding process for the next project, with the lessons below?

## What the pilot taught

- The survey decided it before any platform work, so step 1 is worth keeping cheap and first.
- Our limits should be in the request, up front: no Docker and no sudo ruled out nearly every job at once.
- A project with free hosted runners needs a reason other than price.
