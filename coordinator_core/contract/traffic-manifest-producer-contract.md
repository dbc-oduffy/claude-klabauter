# coordinator → invocation-traffic manifest contract (DRAFT)

> **What this is.** The shape and counting rules of `traffic-manifest.json`, the per-machine
> rollup that makes KR4 of `goal-claude-klabauter-engine-of-record` computable: `Σ(traffic of ops served by
> coordinator_core) / Σ(traffic of all ~/.claude-resident entry-points)`. The manifest carries
> two legs so both slot into one shape. Machine-checkable shape:
> `coordinator_core/contract/traffic-manifest.schema.json`.
>
> **Who consumes this.** `coordinator_core/telemetry/traffic_manifest.py` (engine-leg producer),
> `coordinator_core/telemetry/traffic_coverage.py` (reader), the future resident-leg producer
> (B-β, owned by coordinator-claude), and `docs/plans/2026-07-20-kr-baselining-package.md` C4.
>
> **Spec backlink.** `docs/plans/2026-07-20-invocation-traffic-manifest-spike.md` § Manifest
> contract is the pinned source; change the plan's section first, then this file and the schema.

---

## 1. Shape

One JSON object, `additionalProperties: false` at every level above the per-op and
per-entry-point maps.

| Field | Type | Meaning |
|-------|------|---------|
| `schema_version` | `"1"` | Contract version. |
| `generated_at` | ISO-8601 UTC string | When the rollup ran. |
| `machine` | string | Hostname. The sink is per-machine. |
| `source` | object | `{sink: "op-latency.jsonl", generations, rows_scanned, rows_truncated, window: {t_first, t_last}}`. `window` bounds are epoch floats or `null` when no row was scanned. |
| `excluded` | object | `{non_complete_kinds, non_production_origins}`, counts of skipped rows. |
| `legs.engine_served` | object | `{status: "measured", total, ops: {<op key>: {count, by_origin, by_caller, errors}}}`. |
| `legs.resident_bash` | object | One of the two branches in § 3. |

## 2. Counting rules (engine leg)

1. **Counts are complete rows from `op-latency.jsonl`.** An invocation is one row whose
   `kind` is `complete`. A row with no `kind` key is a `complete` row: `op_latency.py` requires
   readers to treat an absent `kind` as `complete`, because legacy rows predate the field. A
   `kind == "complete"` equality test silently drops that traffic. Other kinds (`started`,
   `process_time`, `composition`) are tallied into `excluded.non_complete_kinds`.
2. **Non-production origins are excluded.** Rows whose `origin` is in
   `op_latency._NON_PRODUCTION_ORIGINS` (`test`, `benchmark`) are tallied into
   `excluded.non_production_origins`. A row with no `origin` is counted under `"unknown"`, not
   dropped.
3. **`elapsed_ms` is not carried.** The manifest measures traffic, not latency.
4. **A count is per-machine.** Sink rows carry no cross-machine key; manifests from different
   machines are separate readings, and `machine` names which.
5. **`errors`** counts rows whose `outcome` is not `ok`. **`by_caller`** keys a null caller as
   `"null"`.
6. **`rows_truncated: true`** means the reader hit its row cap and every count is a lower bound.

## 3. The `resident_bash` leg

A `oneOf` of two branches, so the leg fills in with no schema bump:

- **pending:** `{status: "pending", blocked_on: <string>, total: null, entry_points: null}`.
  The producer today always emits this, with `blocked_on: "B-β"`.
- **measured:** `{status: "measured", total: <int>, entry_points: {<resident entry-point
  path>: {count: <int>}}}`. A `measured` leg with a `null` total is invalid.

**Join key.** The resident leg is keyed by resident entry-point path. The sink's `caller` field
identifies the invoking surface on the engine side; how a resident entry-point path lines up
with a `caller` value is **recorded here, not solved**: no mapping between the two is defined,
and neither side may assume one. B-β owns establishing it.

## 4. Consumer rule

A reader computes a KR4 ratio only when `legs.resident_bash.status == "measured"`. While the
leg is `pending` the engine-leg total is a numerator, never KR4, and a reader returns no ratio
with the B-β reason. Emitting a ratio over half a denominator is a contract violation.

---

<!-- producer-contract: invocation-traffic manifest. DRAFT (2026-09-30). Discharges
     docs/plans/2026-07-20-invocation-traffic-manifest-spike.md T0 / R-AC1. -->
