## Change

<!-- One line: what this lets Claude do, or stop doing. -->

## For a permission change

- **Needed for**:
- **Smallest permission that does it** (an action rather than a whole service, if enough):
- **Cost if misused** (worst case, US$/month):
- **Risk** (data exposure, access to other things):
- [ ] Drafted in `part1-godmode/policy.yaml` as `approved: pending`, with a `changes:` entry
- [ ] Unblocking a costly action: also removed from `MUST_STAY_DENIED` in `part1-godmode/verify.py`
- [ ] Read the POLICY.md diff (the `policy` check's summary shows it)

The `approved` check stays red until Kieron approves (`approved: YYYY-MM-DD Kieron`).
After merging, a godmode session runs `part1-godmode/apply.sh`; CI never touches AWS.
