# Backlog

This file is documentation, not a work item.

Work items live here, one file per item, with the priority (`P0` correctness
to `P3` low) at the start of the filename, for example
`P1-health-check-ignores-expected-unavailable.md`. Two subdirectories:

- `blocked/`: waiting on an owner decision or on an environment (a device, a
  credential, a live Home Assistant).
- `deferred/`: deliberately out of scope.

An item is done when the commit that lands the work deletes its file. Every
item states what is wrong, the evidence (`file:line`, read, not inferred),
the consequence, testable acceptance criteria naming the tests that must
exist, the exact files to change and a suggested approach.

The specification is `docs/spec/`. A behaviour change edits the spec in the
same commit; where the spec and the code disagree, the code is wrong (see
`docs/spec/README.md`).
