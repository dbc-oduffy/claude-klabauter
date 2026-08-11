# Test tiers — markers, worker count, selection

> Detail behind `CLAUDE.md` § Build & Test. Read this and `coordinator.local.md`'s Test-commands
> note before changing either tier.

## Roots and config

**Read the root list off `pyproject.toml` `[tool.pytest.ini_options]` `testpaths`, not off any
prose — including this doc.** `test_*.py` convention. No CI.

Any test-count or `testpaths` figure written in prose — here or in `CLAUDE.md` — is a dated
observation, not a fact: it drifts whenever anyone edits `pyproject.toml`, which is often, and
nothing couples prose to that file. Derive the current root list and selection count with
`pytest --collect-only` rather than trusting a number written down here.

## The fast/full split is marker-scoped, not path-scoped — and the tiers are NOT equal

- `fast_test_cmd`: `-m 'not cadence and not pending_fix and not designed_red' -n auto`, capped by
  `--maxprocesses` — read the current cap off `coordinator.local.md` frontmatter, not from here.
- `full_test_cmd`: drops the `cadence` exclusion and runs serial.

Three markers govern it:

| Marker | Meaning |
|---|---|
| `cadence` | Heavy suites, cadence-gate tier only |
| `pending_fix` | Known-broken subject, excluded from BOTH tiers. Itemized worklist lives under `state/audits/` |
| `designed_red` | Red by design — its failure output IS the worklist, never a gate |

A `designed_red` test's failure count is itself a live worklist, not a fact to cite in prose —
run it and read its own output for the current itemized set rather than trusting a number written
here.

## Run the whole tier at most once

A full-tier run carries a standing set of pre-existing failures unrelated to any given change, so
a re-run to re-confirm a baseline buys no new information. Targeted suites plus one consumer-side
suite are sufficient evidence to commit. When failure attribution is genuinely in doubt, stash the
changed files and re-run only the failing set. Derive current runtime and suite size with
`pytest --collect-only` rather than trusting a figure written down here.

## Worker count is a derivation, not a constant

Parallelism is load-bearing, not an optimization: serial cannot complete the fast-tier gate in
reasonable time. **But bare `-n auto` is unbounded and has killed a session.**

`-n auto` takes the host's PHYSICAL core count with no ceiling. Cap it at:

```
min(physical_cores / 2, usable_RAM_GB * 1024 / 150MB)
```

The halving is not a fudge factor: this tier spawns subprocesses per worker (several live
processes per xdist worker), so one worker per core oversubscribes the CPU several-fold — the
suite's own behaviour, and therefore true on every platform. `--maxprocesses` is a `min()`, so
`=12` binds only above 12 physical cores and is inert on smaller machines.

**Recompute it for your box rather than inheriting any cached figure, and if the tier is
unstable, lower the cap before reaching for serial.**

## Known-red set — derive, do not trust prose

The bash-guards known-red set regrows whenever an unmarked test starts failing and nobody
classifies it, so any count written in prose here goes stale immediately. Get the live census
with `python coordinator/bin/red-set-report.py coordinator_core/bash_guards/tests/` (serial or
capped workers, never bare `-n auto`), and read `state/bash-guards/known-red.json` for the
currently classified set (owner, reason, `review_by` per entry).

A regrowth ratchet gates the tree (`coordinator_core/tests/test_known_red_ratchet.py`, hosted
outside `coordinator_core/bash_guards/tests/` because it shells out to a pass that executes the
measured tree, so hosting it inside would recurse without bound): the observed unmarked-red
node-id set must be a subset of the registry (catches regrowth and fix-one-break-one swaps by
naming the offending node id), every marker in the tree must have a registry entry with
non-empty owner/reason (catches mark-to-satisfy), and no registry entry may be past its
`review_by` (catches decay into permanence). Regenerate the registry via
`coordinator/bin/regenerate-known-red-registry.py`, wired to `red-set-report.py` — never
hand-edit the JSON file's node-id set directly.

**Marker-DECORATOR counts are not the same quantity as marked-NODE-ID counts, and conflating them
is a standing failure mode.** One decorator on a parametrised test expands to many node ids — a
single `cadence` use can cover dozens of cells, a single `pending_fix` use can cover one. Derive
both counts separately with `pytest --collect-only -m "cadence or pending_fix or designed_red"`
rather than trusting either figure from prose. The split between marked and unmarked-red node ids
is derived from two `--collect-only` passes (`marked = collected_all - collected_fast`), which
execute nothing and so cannot be perturbed by a concurrently-committing peer — never inferred by
diffing the failure sets of two separately-executed runs, which is vulnerable to attributing a
transient failure to marker exclusion and under-firing the ratchet's subset gate.

**Derive these numbers, or read them from source at the moment you need them — including when you
are certain an existing number is wrong.** A confident correction of a number is exactly how this
document's figures go stale.
