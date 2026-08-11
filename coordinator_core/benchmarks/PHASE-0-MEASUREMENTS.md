# Phase-0 Measurements — per-op end-to-end latency baseline (qsub-01)

> Bootstrap measurement pass (AC6) that sets the honest per-op budgets in `budget-manifest.json`
> from real distributions. This is the evidence-of-record for the wave-1 COMPUTE_ONLY budget slate.
> Spec: `docs/plans/2026-07-10-qsub-01-latency-benchmark-harness.md`.

## Run identity

| field | value |
|---|---|
| measured_at | 2026-07-10 |
| code_sha | `1e17a6b85b27cc4e3d237102e540b5e6cebd0e85` |
| run_id | `e918170a-909a-4291-90ed-4e668a4d6e40` |
| sample_count (N) | 40 per op (warm-up discarded) |
| gating statistic | `min` (the Data Science Reviewer C2 — a p50 over small N is a coin-flip; `min`-vs-RAW-target is the resolved gate) |
| runner_isolation_mode | shared |
| machine | darwin (dev, machine-b) |

## Cold-start floor

- **cold_start_floor_ms = 57.11** (ping, bare, N=40)
- **floor_cov = 0.0155** — the floor is highly stable (1.55% coefficient of variation), which is why
  `min` is a trustworthy gate statistic here and why `floor_delta_ms` is advisory-only (subtracting
  two ~57ms draws with ~1.5% CoV amplifies variance and flips sign on sub-2ms contributions — never
  gate on it).

## Measured distribution (20 COMPUTE_ONLY ops, sorted by `min`)

[2 ops retired since this 2026-07-10 measurement pass — table below now lists 18 rows, not
20: `hooks.nudge_windows_subprocess_popup` (retired `1ba6dc77`) and
`hooks.nudge_improvement_queue_write` (retired DR-077-terms twin cleanup). The "20"/"18 of 20"
figures throughout this file are the measured-at-the-time record and are left as-is —
this is a historical measurement, not a live count.]

| op | min | p50 | p95 | p99 | floor_delta (min−floor) |
|---|---|---|---|---|---|
| hooks.postuse_advisory_dispatch | 57.31 | 59.13 | 66.08 | 70.60 | +0.19 |
| ping | 57.60 | 70.88 | 104.58 | 119.29 | +0.49 |
| hooks.nudge_unauthorized_handoff | 57.61 | 63.31 | 71.73 | 83.56 | +0.50 |
| hooks.suggest_sonnet_research | 59.31 | 61.21 | 65.63 | 68.66 | +2.20 |
| handoff.lineage_ancestry | 62.19 | 64.35 | 69.61 | 75.42 | +5.08 |
| goal.match_candidates | 62.31 | 65.66 | 80.54 | 84.28 | +5.20 |
| hooks.nudge_foreground_agent_dispatch | 63.53 | 70.63 | 110.14 | 116.17 | +6.42 |
| plan.match_candidates | 63.83 | 67.81 | 79.74 | 88.69 | +6.72 |
| roadmap.serve | 64.27 | 66.41 | 70.24 | 72.60 | +7.16 |
| records.query | 64.28 | 68.41 | 87.69 | 94.75 | +7.17 |
| handoff.match_candidates | 64.41 | 66.19 | 77.74 | 85.59 | +7.30 |
| handoff.has_live_children | 64.63 | 66.91 | 90.46 | 106.58 | +7.52 |
| hooks.nudge_em_code_dispatch | 65.34 | 70.76 | 117.76 | 156.80 | +8.23 |
| initiative.serve_set | 65.41 | 70.51 | 83.74 | 84.54 | +8.30 |
| commit.anchors | 67.26 | 71.62 | 80.24 | 80.99 | +10.15 |
| coverage.gate | 74.47 | 80.32 | 93.16 | 101.88 | +17.36 |
| ceremony.session_instructions | 85.76 | 88.94 | 97.42 | 98.51 | +28.65 |
| deliverable.rollup | 97.41 | 100.21 | 115.96 | 119.74 | +40.30 |

Observations:
- The cold-start floor (57.11ms) dominates: 16 of 20 ops sit within +10ms of the floor — their
  compute cost is near-zero and the budget is essentially the spawn cost.
- Three ops do materially more work on top of the floor: `coverage.gate` (+17ms), and the two
  state-rollup ops `ceremony.session_instructions` (+29ms) and `deliverable.rollup` (+40ms).
- p95/p99 tails are noisy (concurrent-session scheduling jitter on a shared dev machine) — this is
  exactly why the gate uses `min`, not a tail statistic.

## Fixture-realism spot-check (AC / prior-art §69)

Benchmarked `records.query` (worktree-scoped) against **claude-klabauter's real `state/` tree** in addition to
the pinned synthetic fixture (N=40 each):

| target | min | p50 |
|---|---|---|
| synthetic fixture | 61.94 | 64.17 |
| real claude-klabauter `state/` | 64.63 | 68.26 |

Real-state `min` is **+2.69ms (+4.3%)** above the synthetic fixture — well within noise (floor CoV is
1.55%) and far inside the 20% tolerance band. **The synthetic fixture does not shape-pin an
unrepresentative latency floor**; it is a faithful stand-in for a real repo's read cost on these ops.

## Honest budgets set (→ `budget-manifest.json`)

Gate model: verdict is `min ≤ target_ms × (1 + tolerance)`; tolerance is `relative 0.2` throughout.

- **COMPUTE_ONLY default: `target_ms = 70`** (band 84ms). Covers 18/20 ops (the fast cluster + the
  `coverage.gate` shoulder at 74.47ms). Set from the aggregate distribution — tight enough that a real
  regression trips it, not the loose provisional 150ms.
- **Per-op overrides** for the two ops whose real cost exceeds the tier band:
  - `ceremony.session_instructions`: `target_ms = 86` (band 103ms; measured min 85.76ms)
  - `deliverable.rollup`: `target_ms = 98` (band 118ms; measured min 97.41ms)
- **MUTATING default remains provisional (`_provisional: true`, 300ms)** — MUTATING ops are not
  benchmarked in wave-1 (AC7 is define-only; see `MUTATING-DESCRIPTOR-SLICE.md`).

**PM-gate note (C9):** the override targets diverge 1.23× and 1.40× from the COMPUTE_ONLY default —
neither reaches the ~2× material-divergence threshold that triggers a pre-commit PM gate. All 20 ops
`verdict: pass` under the honest budgets (recomputed deterministically from the measured `min`s via
the pure gate function). The slate is nonetheless SLA policy that qsub-03 will enforce as a merge
gate, so it is surfaced to the PM in the execution report for ratification.

## AC8 cartography overrides (2026-07-12 validation gate)

The 5 `cartography.*` ops (strand-a plan) were run through the same harness primitives
(`budget.resolve_budget` + `gate.evaluate`, N=8, 2 warm-up) as a standalone gate check, not yet
folded into `op_fixtures.COMPUTE_ONLY_FIXTURES`'s 20-op wave-1 set (cartography ops are `scope:
"none"` with an explicit `target_root`/`files` param, not `--repo`-keyed). Measured against this
repo (`claude-klabauter`, ~600+ tracked files for `tree`, a 40-file sample for `symbols`/`edges`):

| op | min (ms) | verdict under 70ms default | action |
|---|---|---|---|
| `cartography.tree` | 105.25 | fail (band 84ms) | override → `target_ms = 105` |
| `cartography.file_index` | 63.58 | pass | none (default) |
| `cartography.churn` | 58.28 | pass | none (default) |
| `cartography.symbols` | 77.50 | pass (band 84ms) | none (default; AST-heavy but under band) |
| `cartography.edges` | 89.00 | fail (band 84ms) | override → `target_ms = 89` |

`tree` walks + reads every git-tracked file in the repo for its `loc` count — genuinely heavier
than the flat-read/small-fixture ops the 70ms default was calibrated against. `edges` is the
plan-anticipated AST-heavy member (per-file `ast.parse` import/call-graph extraction). Both
overrides are set from measured `min`, per the same honest-budget convention as
`ceremony.session_instructions`/`deliverable.rollup` above — not a silent breach.

A real bug surfaced during this gate run: `cartography.symbols` crashed with "Handler returned
non-serializable result: Object of type set is not JSON serializable" on `coordinator_core/
archival.py`'s `_TERMINAL_STATUSES: Set[str] = {...}` module constant — `ast.literal_eval` on a
set-literal RHS legitimately returns a Python `set`, which `symbols.py`'s `_literal_value` passed
through unnormalized. Fixed in `coordinator_core/cartography/symbols.py` (`_json_safe` recursively
sorts sets/frozensets to lists, `repr()`s bytes) with regression coverage in
`coordinator_core/cartography/tests/test_symbols.py`.

## C10 — projection cold-start replay cost, uncompacted and compacted (2026-08-11)

sat-01's AC3 (see `state/roadmap/sovereign-tracker-2026-07-17/MEASUREMENT-2026-07-28-ac3-read-events-requantified.md`)
measured `tracker_store.read_events`'s cold-start cost only. `tracker_projection.render_status`
(the fold C5's compaction snapshot claims to bound) sits on top of that read and had never been
measured. `coordinator_core/benchmarks/measure_render_status.py` (`run_c10_measurement`, N=10,
warmup=2 — two independent runs shown below, taken minutes apart on a live, ~50-70-concurrent-
session machine, not an idle baseline) produces the AC16/AC17 numbers.

Fixture: 500 events/shard × 3 shards background ("noise", no `axis` field, skipped in one
dict-membership check) + 300 `manual_close` transition events for one target item
(`bench-item-0001`) written raw (uncompacted) or folded into one `kind: "snapshot"` event plus a
5-event unsnapshotted tail (compacted). **Stated record count: 1,800 uncompacted / 1,506
compacted, at 3 shards** (AC16).

| run | point | events | shards | min ms | mean ms | stdev |
|---|---|---|---|---|---|---|
| 1 | uncompacted baseline | 1 800 | 3 | 301.4 | 313.9 | 7.8 |
| 1 | uncompacted growth | 15 300 | 3 | 359.6 | 391.0 | 18.8 |
| 1 | compacted | 1 506 | 3 | 293.0 | 335.7 | 26.5 |
| 2 | uncompacted baseline | 1 800 | 3 | ~301–345 | — | — |
| 2 | uncompacted growth | 15 300 | 3 | 391.9 | 460.2 | 52.6 |
| 2 | compacted | 1 506 | 3 | 311.0 | 335.4 | 19.4 |

Budget band: **84.0 ms** (`tracker.render_status` has no manifest override — resolves via the
COMPUTE_ONLY tier default, `target_ms: 70`, matching `tracker.read_events`'s own unlisted status).

**AC16 — uncompacted vs budget, and the breach point.** `301.4ms` (run 1) / `~301-345ms` (run 2)
is well over the 84ms band — **`extrapolated_breach_total_events_at_shard_count.total_events:
"already breached at baseline"` both runs**, exactly as sat-01's AC3 re-measurement found for
`read_events` alone: the binding constraint is cold-start/import cost, not event count. This
module's baseline is ~2.4x `read_events`' own re-measured ~125ms floor
(`MEASUREMENT-2026-07-28-ac3-read-events-requantified.md`) — isolated by timing a bare
`import coordinator_core.tracker_projection` subprocess against a bare
`import coordinator_core.tracker_store` one: **~333ms vs ~79ms**, so most of the extra ~180ms
`render_status` pays over `read_events` alone is `tracker_projection`'s own import chain
(`tracker_entities` and what it pulls in), not the fold loop. This is a real, reportable
observation about `coordinator_core/tracker_projection.py`'s import cost — flagged here, not
fixed here (out of scope for C10 per its brief).

**AC17 — compacted delta is near-zero, as predicted, not a tuning failure.** `compaction_delta_ms:
-8.4` (run 1) / `-8.9` (run 2) — compacted measured marginally FASTER in both runs, but
`compaction_delta_noise_dominated: True` both times (noise floor 68–93ms, an order of magnitude
larger than the delta). The honest read: compaction has **no measurable effect** on cold-start
`render_status` latency at this scale, exactly as predicted — `read_events` still parses every
line of every shard regardless of the snapshot, and that parse/IO plus the import-chain cost above
dominates the cold start completely. Compaction's real value (bounding per-item fold arithmetic as
event count grows per-item, and keeping shard files append-only/mergeable) is not visible at this
measurement's timescale and was never claimed to be a latency lever by C5 correctly read. Sat-04
should look at the import chain, not compaction, if `tracker.render_status`-shaped latency ever
needs to move.

Reproduce: `python -m coordinator_core.benchmarks.measure_render_status`.

## Reproduce

```bash
python -m coordinator_core.benchmarks --n 40 --out /tmp/qsub_phase0_summary.json
```

Each run appends one `ConformanceRecord` per op per `code_sha` to the append-only baseline store
(`baselines/baseline.jsonl`) — never overwrite-on-green, so a regression is visible as a new row
against the same op at a later `code_sha`.
