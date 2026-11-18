# A consumer's runner definitions (`tools/runner-image/` in spellbound3d)

[#54](https://github.com/stickleprojects/aws-build-runner/issues/54): anything specific to spellbound3d lives in
spellbound3d. This repo is the platform. It owns the cost boundary, guardrails, labelling and tagging standards,
the runner service and the base image. spellbound3d owns its own machine definitions, in `tools/runner-image/`,
with the contract in its `README.md`. **Keep this doc and that README the same.** A change to the format is agreed
with spellbound3d first, then made in both places, as with [MACHINE-REQUIREMENTS.md](MACHINE-REQUIREMENTS.md).

| File (spellbound3d) | Phase | Read by |
|---|---|---|
| `tools/runner-image/pools.json` (+ `pools.schema.json`) | 1 | the launcher ([lambda/pools.py](../stack/lambda/pools.py)) |
| `tools/runner-smoke/machine-requirements.txt` | done | the bake ([MACHINE-REQUIREMENTS.md](MACHINE-REQUIREMENTS.md)) |
| `tools/runner-image/install.sh` | 2 | the bake, as ghrunner (to agree) |
| `tools/runner-image/allowed-actors.txt` | 3 | the job-started hook (to agree) |

## `pools.json`

The runner pools spellbound3d asks the platform for. Agreed with spellbound3d on 2026-11-18.

**How it's read:**
- The launcher reads it from `main` (never a branch), at most every 10 minutes (`POOLS_TTL`), with its GitHub App token (`contents: read`).
- Each read is checked against the cost boundary. A copy that fails is **refused as a whole**:
  - the launcher keeps the last good copy, in SSM `/build/runner/pools`;
  - it records the refusal and the reasons in `/build/runner/pools-status`;
  - the daily check alerts Kieron.
- So a merged change takes effect within about 10 minutes. The PR's approval in spellbound3d is the only review, and it's the approval of the spend change: no deploy step follows.
- Until the file exists (status `missing`) or has been read once, `config.json`'s `Pools` are the starting copy.
- If GitHub can't be reached (status `unreachable`), the last good copy stays.
- The webhook reads the last good copy, to know which labels are ours.

| Key | Meaning |
|---|---|
| `version` | Must be `1`. Any other version is refused. |
| `pools.<label>` | One pool per `runs-on` label. Labels match `aws-[a-z][a-z0-9-]*`. **The platform owns the prefix rule** (labelling standard). |
| `class` | `light` or `heavy`. **The platform owns the classes:** the usage report groups hours and cost by class and sizes heavy jobs. A new class needs the report changed first. |
| `spot` | Spot instance types, tried in order. At least one. |
| `on_demand` | Types used only when no spot type is available. `[]` means spot only. |
| `limits` (optional) | `max_runners` (instances at once, all pools together), `max_instance_minutes` (an instance is stopped after this, job or not), `idle_minutes` (an instance with no job is stopped after this). Each may only be stricter than the boundary. Leave one out to take the platform's value. |

An unknown key at any level is refused, on both sides. spellbound3d's `pools.schema.json` checks the format, as a
fail-closed mirror. Where the two differ, this side wins.

**What the platform's boundary check adds** ([lambda/pools.py](../stack/lambda/pools.py), boundary from
`policy.yaml`'s `cost_boundary` and EC2 instance types, passed by `deploy.sh`):
- **Approved types:** instance types match `t3.*`, `t3a.*`, or an `m*` or `c*` family at `large` or `xlarge`.
- **x86_64 only,** because the image is amd64. Graviton families (`t4g`, `c7g`, `c7gn`, `m6gd`, …) are refused, even though Claude's policy approves `t4g.*`.
- **At most 4 vCPU:** nothing above `xlarge`.
- **Limits:** `max_runners` ≤ 4, `max_instance_minutes` ≤ 90, `idle_minutes` ≤ 20.
- CI's `cost_boundary_holds` rule runs the same check on `config.json`'s starting copy, and proves it refuses an 8-vCPU, a Graviton and an unapproved type.

**Not in the file:** the curfew (platform policy, Kieron's), the repo and allowed repos (the platform's registry,
phase 4).
