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

## Red-set triage state (as of 2026-07-28, ~57 red)

The red set's baton was CONSUMED and ARCHIVED (`28cb6129`) — it lives at
`archive/handoffs/2026-07/2026-07-25-triage-red-tests-after-bin-collectability.md`, NOT under
`state/handoffs/`. The successor triage is
`state/audits/2026-07-28-coordinator-core-red-test-triage.md` (coordinator_core half). Check both
before cutting new triage scope.

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
