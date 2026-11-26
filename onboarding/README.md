# Onboarding a project to the AWS build runners

How a project gets a build agent here. Pilot: [oolite](oolite.md). Status: **draft**, to be corrected by what the pilot teaches.

## Who does what

| Who | Does |
|---|---|
| The project (its owner or its Claude session) | Surveys itself and answers the request below; owns its machine definitions |
| This repo (the platform) | Assesses fit, image, cost and security; builds and tests the platform side |
| Kieron | Decides whether to onboard, and approves every cost, limit and policy change |

The platform doesn't survey the project's repos: it describes what it needs, and the project answers when it can.

## Steps

1. **Request.** The project answers the questions below. "Don't know" is an answer; facts are marked measured, documented or guess.
2. **Assessment.** The platform writes `onboarding/<project>.md`: fit, image options, every option's cost, security, what must change here.
3. **Check.** The project confirms its facts in that file.
4. **Decision.** Kieron says yes, no or not yet, and picks the image option. Nothing is built before this.
5. **Platform change.** Registry entry, image, pools, and any policy change as `approved: pending`, through the normal PR process.
6. **Trial.** A few named jobs, with the project's current runner as the fallback. Measure time, cost and reclaims.
7. **Sign-off.** Kieron accepts the measured cost and the trial's result. The project switches its runner variable.
8. **Leaving.** Switch the variable back, then remove the pools, registry entry and image.

## What the project tells us

- **Jobs:** which repos and jobs would move, why, and where they run today. "None" is fine.
- **Per job:** OS or container; tools and versions; whether it needs root, Docker, privileged mode, a display, a GPU or network access; time, CPU, peak memory, disk.
- **Volume:** runs a day, the busiest hour, how urgent, and whether no runners from 22:00 to 07:00 Sydney is a problem.
- **Trust:** repo visibility, who can trigger jobs, whether fork PRs run, secrets needed.
- **Caches and artifacts:** sizes, and where they go.

## What every project gets, and its limits

- One fresh x86_64 Ubuntu 24.04 machine per job, spot first, up to 4 vCPU and 30 GiB of disk, for at most 90 minutes.
- At most 4 runners at once, **shared by all projects**. No runners 22:00–07:00 Sydney.
- Jobs get no sudo and no Docker, and upload at about 20 Mbps.
- No Windows or macOS.
- All projects share the US$35 ceiling and the US$29 stop: one project's spend can stop the others' builds.

The limits are `cost_boundary` in [policy.yaml](../part1-godmode/policy.yaml). Raising one, or adding an always-on cost, is Kieron's decision.

## Security gate

A public repo is the main risk: a fork's pull request must never reach a runner. Before any project is added, the
actor and repo allowlists must cover it, and jobs from forks must be refused, with a test that shows it.
