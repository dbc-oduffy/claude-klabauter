# Test tiers — markers, worker count, selection

> Detail behind `CLAUDE.md` § Build & Test. Read this and `coordinator.local.md`'s Test-commands
> note before changing either tier.

## Roots and config

**Read the root list off `pyproject.toml` `[tool.pytest.ini_options]` `testpaths`, not off any
prose — including this doc.** It has gone stale twice; see § Why this doc exists. `test_*.py`
convention. No CI.

As a dated observation (2026-07-30, `--collect-only`): six roots — `coordinator_core/`,
`coordinator/tests/` (adopted from example-doctrine-repo, wired 2026-07-22), `coordinator/bin/` (wired 2026-07-25),
`coordinator/lib/`, `bin/`, `scripts/` — selecting **22,873 tests** (23,645 collected, 772
deselected).

## The fast/full split is marker-scoped, not path-scoped — and the tiers are NOT equal

- `fast_test_cmd`: `-m 'not cadence and not pending_fix and not designed_red' -n auto --maxprocesses=12`
- `full_test_cmd`: drops the `cadence` exclusion and runs serial.

Three markers govern it:

| Marker | Meaning |
|---|---|
| `cadence` | Heavy suites, cadence-gate tier only |
| `pending_fix` | Known-broken subject, excluded from BOTH tiers. Itemized worklist: `state/audits/2026-07-22-coordinator-tests-tier-classification.md` |
| `designed_red` | Red by design — its failure output IS the worklist, never a gate |

## Run the whole tier at most once

~7 minutes and ~26k tests against a standing set of pre-existing failures unrelated to any given
change — so a re-run to re-confirm a baseline buys no new information. Targeted suites plus one
consumer-side suite are sufficient evidence to commit. When failure attribution is genuinely in
doubt, stash the changed files and re-run only the failing set.

## Worker count is a derivation, not a constant

Parallelism is load-bearing, not an optimization: serial could not complete the
`/workday-complete` Step 1 gate. **But bare `-n auto` is unbounded and has killed a session.**

`-n auto` takes the host's PHYSICAL core count with no ceiling. Cap it at:

```
min(physical_cores / 2, usable_RAM_GB * 1024 / 150MB)
```

The halving is not a fudge factor: this tier averages ~4.3 live processes per xdist worker (it
spawns subprocesses), so one worker per core oversubscribes the CPU several-fold — the suite's own
behaviour, and therefore true on every platform. `--maxprocesses` is a `min()`, so `=12` binds only
above 12 physical cores and is inert on smaller machines.

**Recompute it for your box rather than inheriting 12, and if the tier is unstable, lower the cap
before reaching for serial.** Supporting measurements are Windows-only (24-core/95GB); the macOS
figures in `coordinator.local.md` are derived, not measured.

## Red-set triage state (as of 2026-08-07, `docs/plans/2026-08-07-guard-suite-back-to-a-gate.md`)

**Do not hand-edit the numbers below a third time — derive them.** Run
`python coordinator/bin/red-set-report.py coordinator_core/bash_guards/tests/` (C1, serial or
capped workers, never bare `-n auto`) for the live census, and read
`state/bash-guards/known-red.json` (C8) for the current classified set. This section records a
DATED observation from that plan's C8 close-out, not a fact to trust going forward.

**The three-pass history this plan's C8 was built to stop repeating.** Point 1 (2026-07-25/28,
read-only): the baton below, ~57 red, nothing fixed. Point 2 (2026-08-01→03, executed): 160 red
node ids adjudicated, 67 cleared, residual 93 — and no regrowth mechanism. Point 3
(2026-08-07): back up to ~169 red before this plan's fix chunks landed. Each pass cleared real
cells; none stopped the set regrowing.

**C8's derived measurement (2026-08-07, HEAD `94e675021`, `--workers 8`):** unfiltered 4621
collected / 4571 passed / 21 failed / 0 errored / 16 skipped / 13 xfailed. Of the 21 failed: 19
were unmarked (zero registry coverage) before C8's adjudication pass; all 19 now carry a
`pending_fix` marker with a `state/bash-guards/known-red.json` entry (owner, reason, `review_by`).
One (`test_guard_message_size.py::test_leg1_ceiling_per_band`, G7) was already marked and owned by
`state/handoffs/2026-08-03-guard-message-cap-remaining-16.md`, cited not re-owned. **Marked vs
fixed this chunk: 19 marked, 0 fixed** — C8's own remit is classification and the ratchet, not the
underlying guard bugs; every marked cell carries a plan owner in the registry, so this is not the
2026-07-28 outcome with better paperwork, per that outcome's own standard (a plan that marks cells
and declares victory with no owner attached).

**A regrowth ratchet now gates the tree** (`coordinator_core/tests/test_known_red_ratchet.py` — it
lives OUTSIDE `coordinator_core/bash_guards/tests/` deliberately: it shells out to a pass that
executes the measured tree, so hosting it inside that tree made it re-execute itself, recursing
without bound. Same principle `test_red_set_report.py` already follows,
carries no marker itself): the observed unmarked-red nodeid SET must be a subset of the registry
(catches regrowth and fix-one-break-one swaps by naming the offending nodeid), every marker in the
tree must have a registry entry with non-empty owner/reason (catches mark-to-satisfy), and no
registry entry may be past its `review_by` (catches decay into permanence). Regenerate the
registry via `coordinator/bin/regenerate-known-red-registry.py`, wired to `red-set-report.py` —
never hand-edit the JSON file's node-id set directly.

**Marker-USE vs marked-CELL reconciliation — and a worked example of this document's own rule.**

The numbers, all derived by `pytest --collect-only -m "cadence or pending_fix or designed_red"`
against `coordinator_core/bash_guards/tests/`, not asserted:

| | marker DECORATORS | marked NODE-IDS |
|---|---|---|
| before C8 | 2 | **40** |
| after C8 (+19 `pending_fix`, +1 `cadence` on the ratchet's own subset gate) | 22 | **60** |

The two are not the same quantity and differ by more than an order of magnitude, because ONE
decorator on a parametrised test covers many cells: `test_dispatch_latency_bound.py` carries a
single `cadence` use that expands to **39 node-ids**, and `test_guard_message_size.py` carries a
single `pending_fix` covering 1. 39 + 1 = the 40.

**The correction, recorded because getting it wrong is the failure this section exists to name.**
An earlier revision of this paragraph stated that only ~2 cells were genuinely marker-excluded and
that the ~40 figure was measurement noise. **That was wrong: 40 was exactly right.** The reasoning
error was reading a DECORATOR count as a CELL count — the very conflation this table now separates.

What *was* genuinely wrong is a different and narrower thing, and it is fixed: `red-set-report.py`
originally inferred marker-exclusion by diffing the failure sets of two separately-executed pytest
runs. On a shared tree where peers commit continuously, a transient failure present in one run and
absent from the other is silently attributed to marker exclusion. That inference is invalid
regardless of whether its answer happened to come out right, and the error direction is the
dangerous one — a transient counted as marker-excluded is REMOVED from the unmarked-red set, so the
ratchet's subset gate UNDER-fires and hides regressions. Since C1b the split is derived from two
`pytest --collect-only` passes (`marked = collected_all - collected_fast`), which execute nothing
and so cannot be perturbed by a concurrent edit; the script also reports `head_before`/`head_after`
and a `tree_moved_during_measurement` flag, so a run can say whether the tree held still under it.

Both facts are worth keeping: a right answer reached by an invalid method still needed the method
fixed, and a confident correction of a correct number is how this document's numbers went stale
twice before. **Derive them, or read them from source at the moment you need them** — including
when you are certain the existing number is wrong.

**Pre-C8 baton (archived).** The red set's original baton was CONSUMED and ARCHIVED (`28cb6129`)
— it lives at
`archive/handoffs/2026-07/2026-07-25-triage-red-tests-after-bin-collectability.md`, NOT under
`state/handoffs/`. The successor triage is
`state/audits/2026-07-28-coordinator-core-red-test-triage.md` (coordinator_core half). Both are
superseded by this section and `known-red.json` for current state; check them only for the
pre-C8 history.

## Why this doc exists — the bullet went stale twice

**First, 2026-07-28.** CLAUDE.md claimed `testpaths = ["coordinator_core"]`, both tiers
"deliberately equal" at a bare `pytest coordinator_core/`, a "~9k-test suite", and
`coordinator/tests/` as "not yet in the validate path" — four facts, all stale, in always-loaded
doctrine. A red-suite triage reasoned from that bullet and wrongly wrote off 45 failures as an
unwired corpus that was in fact gating.

**Second, 2026-07-30 — the fix did not hold, and that is the lesson worth more than either
correction.** Two days after the correction it was wrong again: `testpaths` had grown from three
roots to six (`coordinator/lib`, `bin`, `scripts` added) and selection had moved 19,694 → 22,873
(20,458 → 23,645 collected, 764 → 772 deselected), while the prose stayed put. Caught during
substrate verification for the diff-scoped-ceremony-gates plan, whose whole mechanism reads
`testpaths` — so a plan was briefly designed against a three-root world that had not existed for
days.

**The structural point.** These facts drift whenever anyone edits `pyproject.toml`, which is
often, and nothing couples prose to that file. Correcting the numbers a third time is not the fix.
Either derive them, or read them from source at the moment you need them. Treat any test-count or
testpaths figure in prose — here or in CLAUDE.md — as a dated observation, not a fact.
