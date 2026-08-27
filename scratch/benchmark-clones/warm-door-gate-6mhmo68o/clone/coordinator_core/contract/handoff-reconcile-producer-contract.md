# `handoff.reconcile_open` producer contract (TURNABLE-ON, op name RATIFIED)

> **What this is.** The producer-side wire contract for claude-klabauter's `handoff.reconcile_open` op —
> the auto-reconcile orchestrator that enumerates every currently-open handoff and evaluates each
> against the unified gate evaluator (C3), driving a structured gate-cascade-clear transition or
> surfacing the handoff for EM judgment. This doc pins the op's params/return shape, the C3
> clear-predicate spec, and the recommended invocation cadence.
>
> **REBUILT 2026-08-26 (C4, this plan) after DR-344's kill-bar deleted the prior 2,721-line
> implementation at 5,546.9 ms process time — 11x the 500 ms brightline.** The rebuild's REDUCED
> BUILD TARGET is gate-evaluation only: **C10 killed the DEC-1 commit-reality shipped-ness matcher
> (`commit_reality.py::evaluate_commit_reality`) outright, permanently (`state/kill-ledger.md`) —
> not deferred, not narrowed.** `auto-ship`, the `reconciled[]` array, and every DEC-1/commit-reality
> routing path documented below in the pre-rebuild sections are **GONE**, not reduced — there is no
> shipping-verdict half left to call. Measured (AC5, `benchmarks/process_time.py`,
> `k=20`/`k=7`): **431.25 ms cold CLI, 125.0 ms warm median (k=7, min 109.4), 0 spawns, `procs_per_call` 1.0** —
> under the 500 ms brightline on both instruments, warm being the serving path for DoE-claude, the
> only live consumer.
>
> **Status: WIRED AND FIRING, observation-only.** The op, the `reconcile/` compute package, and
> the DoE-owned policy-YAML grammar pin are all shipped and green.
>
> **CORRECTED 2026-08-25 — the previous status line was stale and said the opposite.** It read
> "not yet wired to a live caller… there is no `workday-start` (or other) call site invoking this
> op today". There is: DoE's `coordinator/commands/workday-start.md` § 1.10.6 makes it a Step -0.9
> judgment point (`### Auto-Reconcile`, after `### Addon Health`), routing through this repo's
> `coordinator/bin/check-auto-reconcile.py`. Confirmed by `doe-claude-em` 2026-08-25, and by the
> op-latency sink: **65 fires in 24 h, all `outcome=ok`, across 5 sessions**.
>
> **The call site and the arming flip were never coupled** — observation-only was *designed* to
> run and surface, so "wired" does not mean "armed". `dry_run` still gates every mutation exactly
> as § 6 and the grammar describe, and DoE has not flipped it. `auto_ship_enabled` is a DEAD ROUTE
> as of the rebuild above: the auto-ship path it gated no longer exists in this op, so the key has
> nothing left to arm — see § 1.1/§ 1.3 below.
>
> **Two open facts a reader should not mistake for settled.** (i) DoE accounts for exactly ONE fire
> per `/workday-start`, and § 1.10.6 is the only `reconcile_open` call site in their whole plugin
> tree — measured usage is ~13 per session, so ~12 fires per session are unattributed and are
> presumed claude-klabauter-side. The sink cannot currently name them: `origin` is null on 63 of those 65
> rows, so it records that an op fired and not who asked. (ii) **Pre-rebuild**, each fire cost
> **5,546 ms of process time** (0 spawns), 11x this repo's 500 ms brightline —
> `state/audits/2026-08-25-the-steady-state-residual-evaporated-and-the-cpu-blind-spot-behind-it.md`.
> At ~13 fires that was ~70 s of CPU per session — the cost the rebuild's 125.0 ms warm figure
> (above) buys down. This 5,546 ms figure describes the deleted implementation, kept for the
> before/after record, not a current measurement.
>
> The op name `handoff.reconcile_open` is
> **RATIFIED** by DoE (2026-07-13, `cross-repo/archive/2026-07-13-claude-central-em-doe-auto-reconcile-ratifications.md`),
> same path the cartography op names walked from provisional to ratified.
>
> **Boundary authority.** `/Users/example-operator/X/DoE-claude/docs/decisions/DR-047-doe-claude-klabauter-boundary-redraw-contract-vs-e.md`
> is the operative DoE↔claude-klabauter boundary authority underlying this op's design: **claude-klabauter owns the
> engine** (the compute — matcher, evaluator, orchestration, the fail-closed reader), **DoE owns
> the policy** (the threshold/data — the `auto-reconcile-policy.yaml` file DoE authors against
> claude-klabauter's grammar pin, `coordinator_core/contract/auto-reconcile-policy.grammar.md`). The
> superseded 2026-07-03 tri-plane-ownership-boundary doc is NOT the citation for this split — DR-047
> is. Concretely: `dry_run` resolution is **DoE-owned data pinned by the C9 grammar**, not a
> claude-klabauter-hardcoded named constant — a DoE-side policy edit changes the op's mutation-gating
> behavior with zero claude-klabauter code change. **The DEC-1 conservative auto-ship policy this paragraph
> used to cite as the concrete instance is dead** — C10 killed the matcher it gated (§ 1.3, § 3
> below) — so `mechanical_commit_denylist`/`cross_handoff_attribution` are now DoE-owned keys
> describing a route this op no longer serves, not a live example of the boundary.
>
> **Spec backlinks.**
> - Plan (source of truth): `docs/plans/2026-07-13-claude-klabauter-auto-reconcile-open-handoffs.md` § C4/C5
> - Op module: `coordinator_core/ops/handoff_reconcile.py`
> - Compute engines: `coordinator_core/reconcile/commit_reality.py` (C2, DEC-1),
>   `coordinator_core/reconcile/gate_eval.py` (C3), `coordinator_core/reconcile/policy_loader.py` (C9)
> - Policy grammar pin: `coordinator_core/contract/auto-reconcile-policy.grammar.md`
> - Boundary authority: `/Users/example-operator/X/DoE-claude/docs/decisions/DR-047-doe-claude-klabauter-boundary-redraw-contract-vs-e.md`
> - Batch-orchestration compliance precedent:
>   `docs/decisions/DR-212-handoff-lifecycle-inplace-frontmatter-mutation-carveout.md` (D2(ii)/Invariant-3)
> - Cadence rationale: `docs/decisions/DR-215-coordinator-core-command-type-execution-model.md` § 6

---

## 1. Op wire shape

### 1.1 Params

| Param | Type | Required | Default | Meaning |
|---|---|---|---|---|
| `dry_run` | bool | no | **`true`** | When true (the default), the op computes every verdict but performs ZERO transitions — no `gate-cascade-clear` call. (Pre-rebuild this also gated a `ship_and_archive` call; that call site is gone with the auto-ship path — § 1.2/§ 1.3.) Caller must pass `dry_run=false` explicitly to transition anything. Non-bool values are coerced to `true` (fail-conservative on malformed input). |
| `policy_path` | str | no | — | Override path forwarded to `reconcile.policy_loader.load_policy`. Test/CLI injection seam; production callers omit this and let the loader resolve via its own env-var/default-path chain (see the grammar pin doc § "Fail-closed contract"). |

`dry_run` **defaults to `true`** — this is the resolved outcome of the Staff Engineer review finding index 4:
conservatism parity with DEC-1's surface-never-guess invariant. Auto-mutation of work-state
warrants opt-in until DoE ratifies flipping the default via the C8 proposal memo (§ 7 below).

### 1.2 Return shape

**REBUILT 2026-08-26 — the `reconciled[]` array is GONE, not reduced.** C10 killed the DEC-1
commit-reality shipped-ness matcher permanently (`state/kill-ledger.md`); there is no shipping
verdict left to populate `reconciled[]` with, so the rebuilt op returns two arrays where the
pre-rebuild op returned three.

```json
{
  "gates_cleared": [ { "handoff_id": "...", "handoff_path": "...", "action": "gate-cascade-clear",
                        "verdict": "clear"|"narrow", "blocker_ids": ["..."], "dry_run": true,
                        "applied": false } ],
  "surfaced": [ { "handoff_id": "...", "reason": "...", "evidence": ["..."] } ],
  "exit_code": 0
}
```

| Field | Grain | Meaning |
|---|---|---|
| `gates_cleared` | one entry per handoff routed by a C3 verdict in `{clear, narrow}` | `action` is always `"gate-cascade-clear"`. `verdict` echoes C3's structured verdict. `blocker_ids` is the set of `blocked_by` members resolved as newly-shipped (the removal candidates C8 re-verifies at act-time — see § 3). `applied` is `false` on dry-run or when `blocker_ids` is empty. |
| `surfaced` | one entry per handoff that needed EM judgment | Populated for: C3 `verdict=="surface"`; and the C3 **narrow+surface composite** (a `narrow` verdict whose `remaining_blockers` includes an `abandoned` id — see § 4). (Pre-rebuild, this array was also populated for a C2 verdict falling below the auto-ship bar on a non-`awaiting_gate` handoff — that source is gone with the matcher, § 3.) Each entry carries `reason` (a short machine string) and `evidence` (the underlying evaluator evidence list). |
| `exit_code` | op-level | Always `0` — this op never fails loud for an individual handoff's verdict. A per-handoff mutation failure (e.g. a live `gate-cascade-clear` call error) is captured inside that handoff's `gates_cleared` entry (`exit_code`/`message` fields), not surfaced as an op-level failure. The only op-level non-zero exit is the `repo_root is None` guard (no socket-authoritative common_dir), which returns `exit_code: 1` with both arrays empty. |

**Not surfaced, not acted on:** a handoff whose C3 verdict is `not-cleared` (every `blocked_by`
member legitimately still open/awaiting_gate) receives NO entry in any of the three arrays — the
benign steady state. This is deliberate: flooding `surfaced[]` every invocation with handoffs that
are simply, correctly, still waiting would drown the real signal.

### 1.3 Verdict routing table

**REBUILT 2026-08-26 — the `C2 auto-ship` row below is GONE, not reduced.** C10 killed the DEC-1
commit-reality matcher permanently; there is no C2 verdict left to route. The table now carries
only C3's four gate-evaluator verdict values.

| C3 verdict | Handoff state | Route |
|---|---|---|
| C3 `clear` | `awaiting_gate` | `gates_cleared[]`; live call invokes C8's `gate-cascade-clear` verb; full flip to `ready_to_fire` |
| C3 `narrow` | `awaiting_gate` | `gates_cleared[]`; live call invokes C8's `gate-cascade-clear` verb; `blocked_by` narrows, stays `awaiting_gate`; ALSO appended to `surfaced[]` when `also_surface` is true (narrow+surface composite, § 4) |
| C3 `surface` | `awaiting_gate` | `surfaced[]` only, no transition |
| C3 `not-cleared` | `awaiting_gate` | no action, NOT surfaced (benign steady state) |

A **prose** `gate_dependency` gate never auto-transitions regardless of its clear/surface verdict
— `gate_eval`'s structured-vs-prose split already enforces this upstream (§ 4); this op simply
routes whatever verdict comes back, and the prose path's `clear` verdict is EM judgment per the
DoE alignment reply's item 3.

---

## 2. DR-212 batch-orchestration compliance

`handoff.reconcile_open` loops over every open handoff and, per handoff, invokes
`handoff.transition gate-cascade-clear` — an orchestrating op calling a per-file mutator in a
loop. (Pre-rebuild, this also invoked `handoff.ship_and_archive` on the now-deleted auto-ship
path; that call site is gone with C10, § 1.3.) This is **DR-212-compliant**, and is **NOT** the
batch-mutation pattern DR-212 reserves solely for `handoff.normalize` (D2(ii)/Invariant-3:
*"Future batch-mutation ops with different semantics or different target nouns would require
their own DR and cannot inherit this carve-out"*):

`reconcile_open` never itself batch-writes multiple `state/handoffs/*.md` files in one call — each
`gate-cascade-clear` invocation remains its own independent, already-DR-212-compliant single-file
`handoff.transition` call, per D2(ii)'s **"N independent per-file idempotent writes... not a
compound transaction"** language — the same distinction DR-212 already validates for
`handoff.normalize`'s internal loop, applied here to an *orchestrating* op rather than a single
*mutating* op. The `surfaced[]`/`gates_cleared[]` accumulation this handler builds is **read-side
response bookkeeping** (assembling a return list in local memory), not a cross-file write
transaction — the thing D2(ii)/Invariant-3 actually guards against. No new DR is needed for this
op on this basis.

---

## 3. DEC-1 — the commit-reality matcher (C2) — **KILLED 2026-08-26, removed permanently**

**This section describes a deleted code path, kept for historical/before-after record.** C10
killed `coordinator_core/reconcile/commit_reality.py::evaluate_commit_reality` outright and
permanently (`state/kill-ledger.md`) — `handoff.reconcile_open` never calls it, and the module no
longer exposes it (only unrelated helper residue two other modules import directly survives; see
the module's own docstring). The `auto-ship` verdict, `reconciled[]` array, and the routing row
this section fed all left the contract with it (§ 1.2, § 1.3 above). What follows is the design
this repo shipped and then deleted before it ever fired an auto-ship in production:

`coordinator_core/reconcile/commit_reality.py::evaluate_commit_reality` (DELETED) decided
`verdict: "auto-ship"` iff ALL THREE signals held, per handoff:

1. **SUBJECT MATCH** — `git log --pretty=format:"%H %s" --since=<created> -- <scope-paths>` yields
   a commit whose subject contains a noun token derived from the handoff's scope basenames/title,
   **excluding** denylisted mechanical-commit-subject prefixes (from the DoE-owned policy's
   `mechanical_commit_denylist`) — a `pickup:`/`memo:`/session-init/`handoff.transition`-family
   commit is never treated as completion evidence on its own.
2. **DELIVERABLE PRESENT** — the handoff's named deliverable path(s) (its scope pathspecs) exist
   on disk.
3. **SHA REACHABLE** — the candidate commit is git-reachable on HEAD (`git cat-file -e` +
   `git branch --contains`).

**Cross-handoff attribution guard** (the Staff Engineer review, finding index 2): even when all three signals
clear, if MORE THAN ONE other open handoff's scope pathspecs overlap the candidate commit's
touched paths, the verdict is **demoted to `surface`** — attribution is ambiguous, closing the
vector where a real commit + deliverable satisfies the three signals for handoff X but the work
actually belongs to a different open handoff Y with overlapping scope. Governed by the policy's
`cross_handoff_attribution` boolean (default `true`).

**The matcher encodes no threshold constant itself** — it reads `mechanical_commit_denylist` and
`cross_handoff_attribution` from the caller-supplied **loaded policy dict** (C9). This is the
concrete instance of the DR-047 boundary language above: a DoE-authored denylist edit changes
matcher behavior with zero claude-klabauter code change.

---

## 4. C3 — the unified gate evaluator's clear-predicate spec

`coordinator_core/reconcile/gate_eval.py::evaluate_gate` is COMPUTE_ONLY (pure read+compute, no
writes) and covers both the STRUCTURED path (`blocked_by:[stub-id,...]` graph edges on
`kind: spinoff-roadmap` handoffs) and the PROSE fallback path (free-text `gate_dependency`
one-liner on other handoff kinds). Load-bearing rules, converging with
`DoE-claude/archive/specs/2026-06/2026-06-27-status-propagation-primitive.md` §68-70:

- **All-shipped → `clear`.** ALL `blocked_by` members must be `shipped` SPECIFICALLY — `abandoned`
  is terminal (stops re-evaluation) but never counts toward clearing.
- **Abandoned-blocker → surface.** A `blocked_by` member that is `abandoned` routes to `surface`
  when it's the *only* unresolved state (no shipped, no still-open) — the dependent's premise is
  now likely-false/moot and needs EM judgment, not a silent auto-flip. **This rule (abandoned→
  surface) is a claude-klabauter extension flagged to DoE as a proposed spec addition** — the canonical
  status-propagation-primitive spec (§68-73) covers the shipped/asymmetry/gate_cleared_by rules but
  is silent on the abandoned-blocker case (see the C8 memo, § 7).
- **Partial-satisfaction → narrow, never fire-on-first-edge.** AND-reduce over EVERY member: when
  some-but-not-all are shipped, the reported `remaining_blockers` drops the shipped edges and
  `verdict=narrow` (caller stays `awaiting_gate`, `blocked_by` mutates down via C8). This is the
  tc-4 regression guard (`blocked_by:[tc-1, tc-5]` must NOT flip to `ready_to_fire` when only
  `tc-1` shipped), sourced from the tc-4 regression lesson
  `DoE-claude/archive/lessons/2026-07/2026-06-23-a-gate-reconcile-hook-that-flips-a-depen.yaml`.
- **`gate_cleared_by` provenance.** Shipped blockers' SHAs (`shipped_in`) are collected as
  `cleared_by_shas` and handed to C8, which appends them to the handoff's `gate_cleared_by:` array
  as the audit trail for which commits cleared which edges.
- **Fail-loud on `blocks`/`blocked_by` asymmetry.** When a blocker resolves in the live+archived
  index but its own `blocks:[...]` list does NOT name the dependent handoff back, `verdict=surface`
  — a data defect is surfaced, never auto-repaired.
- **NARROW+SURFACE composite** (the Staff Engineer review, finding index 1 — major): a `narrow` verdict whose
  `remaining_blockers` includes ANY `abandoned` id carries `also_surface=True` in the returned
  dict — the handoff must not silently rot gated on a dead blocker forever; § 1.2/1.3 above
  describe C4's routing of this composite into `surfaced[]` in addition to the narrow-mutation.
- **One-level DAG walk.** Only the handoff's direct `blocked_by` edges are resolved per invocation,
  with a visited-set cycle guard, even though the schema intends an acyclic graph.

**PROSE path** (fallback, non-roadmap handoffs): conservative resolution against
caller-supplied `witness_candidates` — zero candidates or >1 candidate both surface (never guess
among/without concrete pointers); exactly one candidate resolves to `clear` iff its
`deployment_state == shipped`, else `surface`. A prose-path `clear` verdict is still never
auto-transitioned by `handoff.reconcile_open` (§ 1.3) — EM judgment is retained for prose gates
per the DoE alignment reply's item 3.

Return shape per handoff: `{handoff_id, verdict: "clear"|"narrow"|"surface"|"not-cleared",
cleared_by_shas: [...], remaining_blockers: [...], evidence: [...], also_surface: bool}`.

**`contradiction` (C1, prose-gate-outliving-structured-blockers)**: precedent `also_surface` above,
this dict gains one more field the same way — a closed-shape pin covers the four VERDICT VALUES,
not the field set. `evaluate_gate`'s prose-dominance branch (rule 1: non-empty `blocked_by` AND
non-empty `gate_dependency`) already verdicts `surface` unconditionally, even when every
`blocked_by` member has since shipped; that unconditional `surface` is unchanged. What C1 adds:
when (and only when) every `blocked_by` member independently resolves `shipped`, the returned dict
additionally carries `"contradiction": {"kind": "prose-gate-outlived-structured-blockers",
"discharge_verb": "handoff.transition gate-recheck --cleared", "shipped_blocker_ids": [<blocked_by
ids, in blocked_by order>]}`. The key is ABSENT (not present-and-`None`) in every other case,
including the general (not-all-shipped) prose-dominance case and the empty-`blocked_by` case. No
new verdict value is introduced and no auto-transition follows — the discharge verb names the
existing `handoff.transition gate-recheck --cleared` path (`_gate_recheck` in
`coordinator_core/ops/handoff_transition.py`) an EM would invoke by hand.

`shipped_blocker_ids` preserves `blocked_by` order verbatim, DUPLICATES INCLUDED (Review:
code-reviewer, Finding 3, nit) — it is a literal copy of `blocked_by` (str-normalized), not a
deduplicated set, since a caller treating it as a set-shaped field would be reading a different
field than the one this doc describes.

---

## 5. Cadence — recommended `workday-start`-gated invocation, not per-`session.boot_sweep`

**DEC-2 recommendation:** invoke `handoff.reconcile_open` at `workday-start` cadence, NOT on every
`session.boot_sweep`. Rationale, cited from
`docs/decisions/DR-215-coordinator-core-command-type-execution-model.md` § 6 Consequences:
*"Per-invocation ~59ms cold start replaces a warm path. Invisible at ceremony/commit/session
cadence."* The reconcile op's `git log` scans across every open handoff's scope pathspecs are
genuinely heavier than the per-tool-call consumer DR-215 guards the cold-start budget against —
gating to once-daily `workday-start` keeps the op invisible at the cadence DR-215 protects, rather
than paying a heavier-than-59ms scan on every session boot.

**Already live** — DoE's `workday-start.md` § 1.10.6 wires this cadence today (see the header's
CORRECTED status). This section's DEC-2 rationale for *why* `workday-start`-gating rather than
`session.boot_sweep` is the right cadence stands unchanged by the rebuild.

---

## 6. `/pickup` Step 3 partial-retirement — verbatim source passage

Structured `blocked_by` edges evaluated by C3/routed by C8 are intended to retire the equivalent
manual check in DoE's `/pickup` skill Step 3d — but only the **structured-edge** portion; prose/
cross-repo-memo gates keep EM judgment, and the `awaiting_gate` aging check (14d/7d) is preserved
regardless (this op computes verdicts, it does not track aging). Quoted **verbatim** (not
paraphrased) from `coordinator/skills/pickup/SKILL.md` (DoE-claude repo, ~lines 180-186), sourced
from `state/lessons.md` commit `a8b2aba0` 2026-06-27 (DoE-claude repo):

> **Deliverable scope paths (REQUIRED — plan doc untouched ≠ deliverable unshipped):** A plan or
> stub doc can be untouched while its actual output artifacts have already shipped (or vice-versa).
> For any pending item backed by a plan/stub, ALSO glob the plan's/handoff's `scope:` frontmatter
> pathspecs and `ls` any named output artifacts. Extract paths from the `scope:` block (same
> `extract-scope-paths.sh` script as Step 1 preflight), then for each path: `ls -la <path>` (or
> `Glob <pattern>` for wildcard pathspecs). A deliverable file present on disk AND reachable via
> `git log --oneline -- <path>` since the handoff date is a strong shipped signal — treat the item
> as closed unless the plan Dispatch Ledger contradicts. Absence on disk does NOT mean shipped;
> presence without a commit reference is weak evidence only. *(Source: `state/lessons.md` commit
> a8b2aba0 2026-06-27 — "Pickup reconcile must glob the plan's DELIVERABLE scope paths, not just
> check the plan/handoff doc path for commits.")* Apply this check alongside the existing
> closure-signal sources above, not instead of them.

**DEC-1's three-signal matcher (§ 3 above) was the op-shaped generalization of this exact
heuristic prose** — same deliverable-scope-glob + commit-reachability logic, machine-executed and
guarded by the mechanical-commit denylist + cross-handoff attribution demotion this hand-rolled
`/pickup` check does not itself carry. **That generalization is dead with the matcher (§ 3, C10
kill):** the structured `blocked_by`-edge retirement this section leads with is unaffected (C3/C8
never depended on DEC-1), but the deliverable-scope-glob shortcut DEC-1 offered on top of it no
longer exists — `/pickup` Step 3d's manual deliverable-scope check has no machine-executed
replacement to retire it against.

---

## 7. What DoE receives via the C8 proposal memo

**AMENDED 2026-08-26 — this was the C6 memo; C6 is superseded and the memo ships from C8 instead,**
carrying a different lead. Sent via `coordinator/bin/cross-repo-memo` (never hand-written into
DoE's tree), it leads with the kill — the C2 shipped-ness verdict, the `auto-ship` routing value,
and the `reconciled[]` array this contract described above are gone, not reduced — then carries,
in order: (1) AC5's measured process-time figure for the rebuilt op (431.25 ms cold CLI / 125.0 ms
warm median, 0 spawns); (2) AC10's `gate_eval` false-positive rate (1 of 14 surviving rows); (3)
the `gate_eval` clear-predicate spec including the abandoned-surfaces extension flagged as a
proposed spec addition (§ 4).

**CORRECTED 2026-08-27 (EM) — an earlier draft of this section said the memo "does not ask DoE to
author an arming overlay" because "there is nothing left to arm." That conflated two independent
keys and was wrong in the direction that would have retired live DoE work.** The overlay arms
BOTH keys, and only one of them died:

| key | gates | status |
|---|---|---|
| `auto_ship_enabled` | the auto-ship route (shipped-ness verdict → `ship_and_archive`) | **DEAD** — C10 deleted the verdict; nothing computes `auto-ship`, so the key has nothing to trigger |
| `dry_run` | **every mutation**, i.e. whether `_gate_cascade_clear` is actually invoked | **LIVE** — the rebuilt op still calls it (`handoff_reconcile.py :: _resolve_dry_run` → `_gate_cascade_clear`), policy-authoritative, fail-closed default `true` |

So arming is still a real, open decision — it is just a NARROWER one than before the kill:
gate-cascade-clear only, never auto-ship. The memo must say that, and must not tell DoE their
arming work is moot.

**Measured on this repo, 2026-08-27**, because "armed" and "firing" are different claims and this
workstream has confused them before: `load_policy()` resolves `dry_run: False` /
`auto_ship_enabled: True` from the rung-3 repo overlay, so the gate-cascade path is **armed here
right now** and the rebuilt op honours it. A live fire returned `gates_cleared: 0` against
`surfaced: 17` — nothing has transitioned, not because the switch is off but because no handoff on
the current corpus meets the `clear` predicate. Armed, honoured, and idle are three separate
facts.

`auto-reconcile-policy.grammar.md`'s `auto_ship_enabled` key is DoE-owned data now describing a
dead route; the memo states that and proposes nothing about DoE's grammar file. Sending it is
external-facing and requires PM clearance before it goes out. This contract doc is cited by, not a
substitute for, that memo.

---

## 8. Out of scope

- **Wiring the `workday-start` call site.** DoE's to land on receipt of the C8 memo — not part of
  this op's ship.
- **Flipping the `dry_run` default to `false`.** Requires DoE ratification; this contract pins the
  current (conservative) default only.
- **Authoring or requesting a DoE-side arming overlay for auto-ship.** There is nothing left to
  arm — C10 killed the auto-ship path this overlay would have gated (§ 1.3, § 3, § 7).
- **`handoff.transition`'s `gate-recheck`/`repark`/`gate-cascade-clear` verb internals.** Owned by
  `coordinator_core/ops/handoff_transition.py` (C1/C8); this doc describes only how
  `handoff.reconcile_open` calls into them.
- **The `auto-reconcile-policy.yaml` full grammar.** Pinned in
  `coordinator_core/contract/auto-reconcile-policy.grammar.md` (C9), not restated here beyond the
  boundary-split framing in the header.

---

<!-- producer-contract: claude-klabauter handoff.reconcile_open op — turnable-on, op name RATIFIED (DoE 2026-07-13),
     rebuilt 2026-08-26 (C4, gate-eval only, auto-ship/reconciled[] deleted with DEC-1/C10), live
     caller is DoE's workday-start.md § 1.10.6. Spec backlinks: pln-claude-klabauter-auto-reconcile-pass-off-425848
     § C5; docs/plans/2026-08-25-reconcile-open-comes-back-under-the-bar.md § C4/C8/C10/C12. -->
