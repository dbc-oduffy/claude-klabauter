# chunk_shaper replay fixtures

Five plan files vendored for `coordinator_core/benchmarks/chunk_shaper_replay.py`. Each keeps its
frontmatter `title`/`status`/`plan_id` and its `plan-tasks` block byte-for-byte; the rest of the
plan prose is dropped because `read_spine` consumes only the spine.

Source: experiments repo commit `ed5e3fa`, read-only, new-file hunks of
`naked-vs-coordinator/runs/<run>/work.diff` with the leading `+` stripped:

- `r52/` from `ladder-xl-dispatch-ontology__coordinator__r52` (XL): `docs/plans/2026-10-24-dispatch-point-inferred-edges.md`, `docs/plans/2026-10-24-2026-10-24-dispatch-point-inferred-edges-finish.md`, `docs/plans/2026-10-24-2026-10-24-dispatch-point-land-and-verify.md`.
- `r51/` from `ladder-xxl-perf-audit__coordinator__r51` (XXL): `docs/plans/2026-10-24-perf-audit-bundle-pin-delivery-bc8d06.md`, `docs/plans/2026-10-24-perf-audit-spawn-bundle-dataload.md`.

Tests and the harness never read the experiments repo; these files are the whole input.
