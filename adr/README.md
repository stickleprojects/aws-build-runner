# Architecture decision records

One file per decision: what was decided, why, what else was considered, and what would make us revisit it. Research
behind a decision goes in [support/](support/), named after the ADR's number.

| ADR | Title | Status |
| --- | --- | --- |
| [0001](0001-spellbound3d-pipeline-split.md) | How spellbound3d's pipelines use the laptop and AWS | Proposed |

## How it works

- **Numbering:** `NNNN-short-title.md`, numbered in order, never reused.
- **Status:** Proposed → Accepted (or Rejected). Later it can become Superseded by ADR-NNNN, or Deprecated.
- **Who decides:** only Kieron accepts an ADR, or changes one's status. Claude sessions propose. A decision that touches
  the AWS policy or the cost boundary still follows [the change process](../part1-godmode/README.md#change-process).
- **Accepted ADRs aren't rewritten.** To change a decision, write a new ADR that supersedes it, and link the two.
- **Each ADR** lists its options with a monthly price, its trade-offs and consequences, and the measured triggers for
  revisiting it.
