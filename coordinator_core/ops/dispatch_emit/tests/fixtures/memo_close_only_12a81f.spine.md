---
plan_id: pln-2026-10-22-c2c8fc-23-refire-st-08917d
run_id: 2026-10-22-c2c8fc-23-refire-stage-receipt
derived_from: sizing object
sizing_object: state/sizings/2026-10-22-c2c8fc-23-refire-stage-receipt.yaml
source_memo: state/cross-repo/inbox/2026-10-22-coordinator-content-repo-em-refire-stage-receipt-missing.md
deliverable_id: dlv-action-doe-memo-state-cross-repo-inbox-ce9e94
prime_exit_criterion:
  statement: The memo is answered with the landing shas dff8d697ef and 901de7c65f
    naming the refire fix already at HEAD, and tests covering touched files pass.
---

# Minted dispatch spine — 2026-10-22-c2c8fc-23-refire-stage-receipt

Derived from `state/sizings/2026-10-22-c2c8fc-23-refire-stage-receipt.yaml` by `dispatch.emit --sizing`.
**Do not hand-edit** — the next mint overwrites this file in place.

## Tasks

```yaml plan-tasks
- id: X1
  title: 'Action DoE memo(s) state/cross-repo/inbox/2026-10-22-coordinator-content-repo-em-refire-stage-receipt-missing.md:
    read each in full and do what it asks. The prescribed refire after a ledger-check
    halt halts with stage-receipt-missing; confirm against dff8d697ef or fix. If already
    fixed at HEAD, name the sha. A reply with landing shas is owed to coordinator-content-repo-em.'
  change_kind: doc-edit
  surface: state/cross-repo/inbox/2026-10-22-coordinator-content-repo-em-refire-stage-receipt-missing.md
  body: |
    Spec: state/sizings/2026-10-22-c2c8fc-23-refire-stage-receipt.yaml (X1)
    Intent:
    Action DoE memo(s) state/cross-repo/inbox/2026-10-22-coordinator-content-repo-em-refire-stage-receipt-missing.md: read each in full and do what it asks. The prescribed refire after a ledger-check halt halts with stage-receipt-missing; confirm against dff8d697ef or fix. If already fixed at HEAD, name the sha. A reply with landing shas is owed to coordinator-content-repo-em.
    Verification (this row is DONE only when this holds): The memo is answered with the landing shas dff8d697ef and 901de7c65f naming the refire fix already at HEAD, and tests covering touched files pass.
  writes:
  - state/cross-repo/inbox/2026-10-22-coordinator-content-repo-em-refire-stage-receipt-missing.md
```
