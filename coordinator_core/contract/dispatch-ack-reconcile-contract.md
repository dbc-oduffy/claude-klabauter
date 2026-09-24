# Dispatch-ack reconcile contract (key, four states, outcomes, retention, tombstone, wire shape)

> **What this is.** The producer-side interface for `dispatch_ack.AckStore` and the
> `warm.request_status` poll op it serves — what a caller may rely on when a MUTATING dispatch
> comes back `WARM_DISPATCH_INDETERMINATE` (-32004). Cites the 2026-09-09 staff-eng ruling
> (DoE-claude `state/rulings/2026-09-09-staff-eng-rulings-run-c0d7111e.md` § Row M11, read @
> `2860147bc`), which prescribes recording that the engine was asked before it dispatches, and
> stamping the outcome on completion, so the caller's reconcile read cannot false-negative.
>
> **Two corrections to the 2026-09-09 ruling, recorded here because this contract implements it
> against the tree as it actually is, not as the ruling assumed:**
> 1. The JSON-RPC request `id` is **not** unique per dispatch — every caller sends `"id": 1`
>    (`invoke/__main__.py :: _dispatch_argv_body`; both door legs hard-code it too). The key
>    cannot be the request id; it is minted separately (§ 1).
> 2. The frame-read seam the record is admitted at is `coordinator_core/warm/server.py ::
>    _serve_line`, not `coordinator_core/warm/door/`. The door mints a key and carries it into
>    its own -32004, but the admit/stamp/status record lives only in the accept process that
>    `_serve_line` runs in.
>
> Spec backlink: `docs/plans/2026-09-23-warm-dispatch-reconcile.md` (D1-D5, C2).
> Sibling contract: `coordinator_core/contract/completion-evidence-producer-contract.md` — what a
> `finished` state's `outcome` proves about the mutation is that contract's `evidence_class`
> answer, not this one's. This contract makes no claim of its own about mutation correctness.

---

## 1. The key (D1)

The client mints one dispatch key per request, `f"{os.getpid()}-{<clock>_ns}"`, and sends it as
the `_dispatch_key` side-channel field (the same channel already carries `_caller`, `_env` and
`_settings_home`). `_serve_line` pops it before dispatch. The clock is
`time.monotonic_ns()` where the executing OS's spike verdict showed it ordered across
independently-started interpreters, else `time.time_ns()` — see
`docs/research/spike-verdicts/2026-09-23-dispatch-ack-cost-and-ordering.md` (viable; Linux picked
`monotonic_ns()`; Windows/Darwin readings recorded as owed). The key is never the JSON-RPC `id`,
and the `id` field's current value is unchanged by this contract.

An unkeyed frame (an old door build, or an old client caught in a rollout skew window) is
dispatched exactly as before this contract existed, and is recorded nowhere.

## 2. Where the record lives (D2)

`dispatch_ack.AckStore` is an in-memory store in the accept process, guarded by one
`threading.Lock`. It is never disk-backed and never survives process exit by design (§ 5). It
holds three operations a caller-facing reader must understand:

- **`admit(key, method) -> bool`** — "may this frame be dispatched, and is it now recorded as
  asked?" Called by `_serve_line` after every refusal that proves a frame was never dispatched
  (untrusted caller, skew), and before `dispatch(...)`. Refuses (returns falsy / raises) for:
  a key already present in the store in any state — admitted, stamped, or tombstoned (this is
  what makes the `not-dispatched` re-run instruction in § 3 sound); and a key at or below
  `low_water_ns`, or minted before `boot_ns`, even when no tombstone record for it survived
  eviction. Only non-COMPUTE_ONLY methods are admitted, matching `_op_may_mutate`'s fail-closed
  rule. If `admit` raises, the frame is refused as provably undispatched and is never dispatched
  unacknowledged — there is no swallowed-failure path here, unlike `telemetry/op_latency.py`.
- **`stamp(key, outcome)`** — "did the handler return, and how?" Called by whichever leg
  actually sees the handler return (the pool done-callback, or the in-process synchronous
  return).
- **`status(key) -> dict`** — "what does the engine know of this key?", returning one of the
  four poll states in § 4. Its tombstone insert for an absent, non-evicted key happens in the
  same critical section as its lookup (D3, § 5) — this is what closes the pipe-buffer race.

`boot_ns` answers "could this engine have been the one asked?" `low_water_ns`, the mint time of
the oldest evicted key, answers "could a record for this key have been evicted?" No other value
is carried by the store.

## 3. Stamped outcomes (D2)

Five outcomes are stamped, never more:

| Outcome | Stamped when |
|---|---|
| `result` | The handler returned a JSON-RPC result. |
| `error` | The handler returned a JSON-RPC error (the code travels with the stamp). |
| `abandoned` | `_pool_dispatch`'s timeout branch returned -32004 to the connection before the handler's own return was seen. |
| `worker-lost` | `BrokenProcessPool` — the pool worker died mid-op. |
| `not-dispatched` | Every pre-handler refusal inside `_run_dispatch` or `_pool_dispatch_worker` (including `_settings_home_refusal`), and a successful pre-dispatch `future.cancel()` in `_pool_dispatch`. The handler never ran. |

A -32004 that `_pool_dispatch`'s timeout branch returns to the connection is never itself a
terminal stamp — `abandoned` is stamped later, by the done-callback, once the future actually
resolves. Likewise a -32004 from `ipc._dispatch_message_impl`'s op-timeout stamps `abandoned`,
which reads as unknowable-final (§ 4), never as finished.

`_pool_dispatch_worker` runs in a different process and cannot reach the accept process's store
directly; a refusal there marks the worker's own returned envelope with a private side-channel
field (the same rides-the-returned-dict shape `_stderr` already uses). The accept-process
done-callback pops that field and stamps `not-dispatched` from it. **The field never reaches the
wire** — it is consumed before the response is written to the connection.

## 4. Poll states and the outcome → state mapping

The poll answers exactly one of four states, always:

| Poll state | Reached from | What it licenses |
|---|---|---|
| `not_received` | Outcome `not-dispatched`, OR an absent key tombstoned by `status` itself (§ 5) | The **only** state that licenses a re-run: "this key has not run and never will." |
| `executing` | Key admitted, not yet stamped, engine still the one that admitted it | No re-run. Wait or poll again. |
| `finished` | Outcome `result` or `error` | No re-run. The outcome and its evidence class (sibling contract, § "What this is" above) describe what happened. |
| `unknowable` | Outcome `abandoned` or `worker-lost`, OR a detectable record-loss case (§ 5) | No re-run. The engine cannot say. |

Both routes to `not_received` — the tombstoned-absent-key route and the stamped
`not-dispatched` route — exist because `admit` refuses any key already in the store (§ 2); that
refusal is what makes "has not run and never will" a sound claim for either route, not just a
convenient default. A successful pre-dispatch `future.cancel()` therefore stamps
`not-dispatched` and must never surface as `executing`.

## 5. Race safety and the tombstone (D3)

When `status(key)` finds no record for a key that is newer than both `boot_ns` and
`low_water_ns`, it answers `not_received` **and writes a tombstone for that key in the same
critical section as the lookup**. From that point, `admit` refuses any frame later carrying that
key, dispatched or not. This is what turns "the engine never read this frame (yet)" into "the
engine never read this frame and never will" — the property the 2026-09-01 double-execution
incident (row `077d1a9a38b1`) shows is required before `not_received` can license a re-run: a
frame answered `not_received` without a tombstone can still sit unread in a pipe buffer and be
read after the poll, reopening exactly that false negative one layer down.

**Eviction never reopens a tombstoned key.** Eviction is FIFO over the one store and raises
`low_water_ns` to the evicted key's mint time; `admit` refuses any key at or below `low_water_ns`
regardless of whether the tombstone record for it personally survived eviction. A tombstoned key
that later gets evicted is still refused, because `low_water_ns` alone is the refusal condition —
not the tombstone record's continued presence.

**A poll for `warm.request_status` is never itself subject to `admit`'s tombstone or skew
refusal.** `_serve_line` intercepts `warm.request_status` and answers it directly from
`AckStore.status`, before any `admit` call and outside the refusal path that new mutating frames
go through. A poll therefore always resolves to one of the four states in § 4, including during a
skew-driven eviction race — it never itself returns -32004.

## 6. Survival across process exit (D4)

The record deliberately does not survive an engine process exit. Every detectable form of that
loss answers fail-closed, never `not_received`:

- A key minted before this engine's `boot_ns` → `unknowable(engine-restarted)`.
- A key at or below `low_water_ns` (the highest mint time ever evicted) → `unknowable(expired)`.
- The poll reaching the cold handler (no resident engine) → `unknowable(no-resident-engine)`.

Routine engine exits (idle demotion, drain) cannot abandon a pool op in flight — `_serve_line`'s
drain path binds `drain_outstanding() == in_flight() + _pool_outstanding()` before either
shutdown trigger fires — so this path covers crash-only exits. A clock stepping backwards only
ever moves an answer towards `unknowable`, never towards `not_received`.

## 7. Retention (C1 spike figures)

Capacity is a fixed, generous figure — **4000 entries** — rather than derived from the
op-latency sink's dispatch rate: the store is in-memory, each record is on the order of 150-250
bytes, and eviction already fails closed via `low_water_ns` (§ 5). 4000 entries covers at least
10x `ipc.mutation_read_deadline_for`'s longest deadline (`DISPATCH_TIMEOUT_SECS = 30.0s`, so
300s) at a conservatively estimated 10 mutating dispatches/second sustained on one accept
process (`10 * 300 = 3000 <= 4000`). Memory ceiling: ~1MB, negligible against the fleet-floor
24GB host. Full derivation: `docs/research/spike-verdicts/2026-09-23-dispatch-ack-cost-and-ordering.md`.

## 8. New refusal code — tombstoned key

A key `admit` refuses because it is already in the store, or is at/below `low_water_ns`/`boot_ns`
(§ 2, § 5), is reported via a **new** JSON-RPC error code constant distinct from any existing
-32004 shape. This code is intentionally **absent** from `door_core.c ::
is_provably_undispatched` — the door mints a fresh key on every call (§ 1), so a door-originated
frame never meets a tombstoned key, and adding the code there would license a cold re-run the
door has no basis for.

## 9. `warm.request_status` wire shape (D5)

`{"key": str}` in, COMPUTE_ONLY, scope `none`. `_serve_line` intercepts and answers it from
`AckStore` in the accept process — it is never submitted to the pool (a pool worker cannot see
the accept process's memory and would answer from an empty store). The registered handler itself
runs only on the cold or pool path, where no store is visible, and always returns
`unknowable(no-resident-engine)`.

Every answer carries:

| Field | Present when | Meaning |
|---|---|---|
| `state` | always | One of `not_received`, `executing`, `finished`, `unknowable` (§ 4). |
| `reason` | `state == unknowable` | `engine-restarted`, `expired`, or `no-resident-engine` (§ 6). |
| `outcome` | `state == finished` | `result` or `error`, plus the error code for `error` (§ 3). Names the sibling contract's evidence class for the method; makes no claim of its own about the mutation. |
| `engine_boot_ns` | always | This engine's `boot_ns`, letting a caller detect a restart it hasn't otherwise noticed. |
| `engine_pid` | always | This engine's pid. |

## 10. What this contract does not cover

- **What a `finished` outcome proves about the mutation.** That is
  `completion-evidence-producer-contract.md`'s `evidence_class(op)` answer, cited by name, never
  re-derived here.
- **The AckStore implementation, its server wiring, or its tests.** Built in C3 of the spec plan.
- **The `warm.request_status` op's registration, module, or classification entries.** Built in
  C4.
- **Client-side minting, envelope shape, or the CLI poll-command ladder.** Built in C5.
- **The reproduction/acceptance oracle.** Built in C6.
- **The door legs' key-minting and rebuild.** Built in C7; this contract's key format (§ 1) and
  refusal code (§ 8) are what C7 must match.
- **Reading side effects, or naming a settle interval.** The 2026-09-09 ruling rejects both
  outright, and this contract prescribes neither.
