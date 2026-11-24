# Oolite: onboarding assessment (pilot)

Status: **draft, 2026-11-25. Waiting for the oolite session's answers** to the [request](README.md#what-the-project-tells-us).
Nothing is decided or built. Prices are US$ estimates for Sydney, from [PRICING.md](../PRICING.md).

## Summary

- **Recommendation:** none until oolite answers. The answers most likely to rule it out: jobs that need root, Docker or
  privileged containers, Windows or macOS, or that run for untrusted pull requests.
- **Image:** decide by the tool list; see [Image](#image). Leading option if the jobs fit: a shared image.
- **Cost:** about US$0.06 (2 vCPU) or US$0.11 (4 vCPU) per instance-hour on spot, plus US$0–2 a month for the image.

## The project and its requirements

Pending, from the oolite session:

| Needed | Answer |
|---|---|
| Repos and jobs to move, and why | pending |
| Tools, OS, root, Docker, display, GPU | pending |
| Time, CPU, memory, disk per job | pending |
| Runs a day, busiest hour, curfew | pending |
| Visibility, fork PRs, secrets | pending |
| Caches and artifacts | pending |

## Image

| Option | For | Against | Extra fixed cost a month |
|---|---|---|---|
| **A. Shared image, plus oolite's packages** | One bake, smoke test and weekly rebuild | Every project's tools reach every job; a bad package line can block everyone's bake | ≈ US$0.17 per extra GB of image (3 kept) |
| **B. Own image** | Isolated; can fail without affecting others | Second bake pipeline; needs `max_kept_amis` (3) or pruning reworked | ≈ US$1.75 for 3 images of ≈ 10.5 GB, plus ≈ US$0.20 for weekly bakes |
| **C. Generic base, installed per job** | No image work | Adds minutes to each job, and jobs have no sudo | US$0 fixed; the extra minutes cost as below |
| **D. No AWS runner** | No work; existing runners stay | None of the benefits | US$0 |

Lean towards A while the packages are few and trusted, B if the OS or toolchain differs. C works only for tools that
install without root.

## Cost

Per instance-hour, including the public IPv4 address (US$0.005) and the 30 GiB root volume (≈ US$0.004):

| Runner | Spot | On-demand fallback |
|---|---|---|
| 2 vCPU (c7i.large) | ≈ US$0.057 (2026-11-16 price) | not priced in PRICING.md; ≈ 2.5× spot |
| 4 vCPU (c7i.xlarge) | ≈ US$0.10–0.11 | ≈ US$0.24 |

Illustrative months on 4 vCPU (volumes are **not** oolite's; its answer replaces them):

| Instance-hours a month | Spot | All on demand |
|---|---|---|
| 5 | ≈ US$0.55 | ≈ US$1.20 |
| 20 | ≈ US$2.20 | ≈ US$4.80 |
| 50 | ≈ US$5.50 | ≈ US$12 |

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

Effort isn't estimated until the requirements are known.

## Decisions for Kieron

1. After oolite answers: onboard, not yet, or no?
2. Image option A, B, C or D?
3. If onboarding: a monthly amount for oolite within the US$35 ceiling, and whether it shares the 4-runner cap.
