# `handoff.reconcile_open` producer contract (TURNABLE-ON, op name RATIFIED)

> **What this is.** The producer-side wire contract for claude-klabauter's `handoff.reconcile_open` op —
> the auto-reconcile orchestrator that enumerates every currently-open handoff, evaluates each
> against the DEC-1 commit-reality matcher (C2) and the unified gate evaluator (C3), and either
> drives a transition (auto-ship / structured gate-cascade-clear) or surfaces the handoff for EM
> judgment. This doc pins the op's params/return shape, the DEC-1 policy-as-example-doctrine-repo-owned-data
> boundary, the C3 clear-predicate spec, and the recommended invocation cadence.
>
> **Status: TURNABLE-ON, not yet wired to a live caller.** The op, the `reconcile/` compute
> package, and the example-doctrine-repo-owned policy-YAML grammar pin are all shipped and green. There is no
> `workday-start` (or other) call site invoking this op today — that wiring is example-doctrine-repo's to land on
> receipt of the C6 proposal memo (see § 6). The op name `handoff.reconcile_open` is
> **RATIFIED** by example-doctrine-repo (2026-07-13, `cross-repo/archive/2026-07-13-claude-central-em-doe-auto-reconcile-ratifications.md`),
> same path the cartography op names walked from provisional to ratified.
>
> **Boundary authority.** `/Users/example-operator/X/example-doctrine-repo/docs/decisions/DR-047-example-doctrine-repo-claude-klabauter-boundary-redraw-contract-vs-e.md`
> is the operative example-doctrine-repo↔claude-klabauter boundary authority underlying this op's design: **claude-klabauter owns the
> engine** (the compute — matcher, evaluator, orchestration, the fail-closed reader), **example-doctrine-repo owns
> the policy** (the threshold/data — the `auto-reconcile-policy.yaml` file example-doctrine-repo authors against
> claude-klabauter's grammar pin, `coordinator_core/contract/auto-reconcile-policy.grammar.md`). The
> superseded 2026-07-03 tri-plane-ownership-boundary doc is NOT the citation for this split — DR-047
> is. Concretely: the DEC-1 conservative auto-ship policy is **example-doctrine-repo-owned data pinned by the C9
> grammar**, not a claude-klabauter-hardcoded named constant — a example-doctrine-repo-side threshold edit (e.g. widening the
> mechanical-commit denylist) changes matcher behavior with zero claude-klabauter code change.
>
> **Spec backlinks.**
> - Plan (source of truth): `docs/plans/2026-07-13-claude-klabauter-auto-reconcile-open-handoffs.md` § C4/C5
> - Op module: `coordinator_core/ops/handoff_reconcile.py`
> - Compute engines: `coordinator_core/reconcile/commit_reality.py` (C2, DEC-1),
>   `coordinator_core/reconcile/gate_eval.py` (C3), `coordinator_core/reconcile/policy_loader.py` (C9)
> - Policy grammar pin: `coordinator_core/contract/auto-reconcile-policy.grammar.md`
> - Boundary authority: `/Users/example-operator/X/example-doctrine-repo/docs/decisions/DR-047-example-doctrine-repo-claude-klabauter-boundary-redraw-contract-vs-e.md`
> - Batch-orchestration compliance precedent:
>   `docs/decisions/DR-212-handoff-lifecycle-inplace-frontmatter-mutation-carveout.md` (D2(ii)/Invariant-3)
> - Cadence rationale: `docs/decisions/DR-215-coordinator-core-command-type-execution-model.md` § 6

---

## 1. Op wire shape

### 1.1 Params

| Param | Type | Required | Default | Meaning |
|---|---|---|---|---|
| `dry_run` | bool | no | **`true`** | When true (the default), the op computes every verdict but performs ZERO transitions — no `ship_and_archive` call, no `gate-cascade-clear` call. Caller must pass `dry_run=false` explicitly to transition anything. Non-bool values are coerced to `true` (fail-conservative on malformed input). |
| `policy_path` | str | no | — | Override path forwarded to `reconcile.policy_loader.load_policy`. Test/CLI injection seam; production callers omit this and let the loader resolve via its own env-var/default-path chain (see the grammar pin doc § "Fail-closed contract"). |

`dry_run` **defaults to `true`** — this is the resolved outcome of the Staff Engineer review finding index 4:
conservatism parity with DEC-1's surface-never-guess invariant. Auto-mutation of work-state
warrants opt-in until example-doctrine-repo ratifies flipping the default via the C6 co-memo (§ 6 below).

### 1.2 Return shape

```json
{
  "reconciled": [ { "handoff_id": "...", "handoff_path": "...", "action": "ship_and_archive",
                     "candidate_sha": "...", "dry_run": true, "applied": false } ],
  "gates_cleared": [ { "handoff_id": "...", "handoff_path": "...", "action": "gate-cascade-clear",
                        "verdict": "clear"|"narrow", "blocker_ids": ["..."], "dry_run": true,
                        "applied": false } ],
  "surfaced": [ { "handoff_id": "...", "reason": "...", "evidence": ["..."] } ],
  "exit_code": 0
}
```

| Field | Grain | Meaning |
|---|---|---|
| `reconciled` | one entry per handoff routed by a C2 `verdict=="auto-ship"` | `action` is always `"ship_and_archive"`. `applied` is `false` on the dry-run path (and whenever `dry_run` is true) — `true` only after a live `handoff.ship_and_archive` call reports success. On a live call, `exit_code`/`message` echo that op's own result. |
| `gates_cleared` | one entry per handoff routed by a C3 verdict in `{clear, narrow}` | `action` is always `"gate-cascade-clear"`. `verdict` echoes C3's structured verdict. `blocker_ids` is the set of `blocked_by` members resolved as newly-shipped (the removal candidates C8 re-verifies at act-time — see § 3). `applied` is `false` on dry-run or when `blocker_ids` is empty. |
| `surfaced` | one entry per handoff that needed EM judgment | Populated for: C3 `verdict=="surface"`; the C2 verdict falling below the auto-ship bar (`surface`/`no-match`) on a non-`awaiting_gate` handoff; and the C3 **narrow+surface composite** (a `narrow` verdict whose `remaining_blockers` includes an `abandoned` id — see § 4). Each entry carries `reason` (a short machine string) and `evidence` (the underlying matcher/evaluator evidence list). |
| `exit_code` | op-level | Always `0` — this op never fails loud for an individual handoff's verdict. A per-handoff mutation failure (e.g. a live `ship_and_archive`/`gate-cascade-clear` call error) is captured inside that handoff's `reconciled`/`gates_cleared` entry (`exit_code`/`message` fields), not surfaced as an op-level failure. The only op-level non-zero exit is the `repo_root is None` guard (no socket-authoritative common_dir), which returns `exit_code: 1` with all three arrays empty. |

**Not surfaced, not acted on:** a handoff whose C3 verdict is `not-cleared` (every `blocked_by`
member legitimately still open/awaiting_gate) receives NO entry in any of the three arrays — the
benign steady state. This is deliberate: flooding `surfaced[]` every invocation with handoffs that
are simply, correctly, still waiting would drown the real signal.

### 1.3 Verdict routing table

| C2/C3 verdict | Handoff state | Route |
|---|---|---|
| C2 `auto-ship` | any | `reconciled[]`; live call invokes `handoff.ship_and_archive` (never hand-stamped) |
| C3 `clear` | `awaiting_gate` | `gates_cleared[]`; live call invokes C8's `gate-cascade-clear` verb; full flip to `ready_to_fire` |
| C3 `narrow` | `awaiting_gate` | `gates_cleared[]`; live call invokes C8's `gate-cascade-clear` verb; `blocked_by` narrows, stays `awaiting_gate`; ALSO appended to `surfaced[]` when `also_surface` is true (narrow+surface composite, § 4) |
| C3 `surface` | `awaiting_gate` | `surfaced[]` only, no transition |
| C3 `not-cleared` | `awaiting_gate` | no action, NOT surfaced (benign steady state) |
| C2 `{surface, no-match}` | not `awaiting_gate` | `surfaced[]` only |

A **prose** `gate_dependency` gate never auto-transitions regardless of its clear/surface verdict
— `gate_eval`'s structured-vs-prose split already enforces this upstream (§ 4); this op simply
routes whatever verdict comes back, and the prose path's `clear` verdict is EM judgment per the
Example-doctrine-repo alignment reply's item 3.

---

## 2. DR-212 batch-orchestration compliance

`handoff.reconcile_open` loops over every open handoff and, per handoff, invokes
`handoff.ship_and_archive` or `handoff.transition gate-cascade-clear` — an orchestrating op calling
per-file mutators in a loop. This is **DR-212-compliant**, and is **NOT** the batch-mutation
pattern DR-212 reserves solely for `handoff.normalize` (D2(ii)/Invariant-3: *"Future batch-mutation
ops with different semantics or different target nouns would require their own DR and cannot
inherit this carve-out"*):

`reconcile_open` never itself batch-writes multiple `state/handoffs/*.md` files in one call — each
`ship_and_archive`/`gate-cascade-clear` invocation remains its own independent,
already-DR-212-compliant single-file `handoff.ship_and_archive`/`handoff.transition` call, per
D2(ii)'s **"N independent per-file idempotent writes... not a compound transaction"** language —
the same distinction DR-212 already validates for `handoff.normalize`'s internal loop, applied
here to an *orchestrating* op rather than a single *mutating* op. The `surfaced[]`/`reconciled[]`/
`gates_cleared[]` accumulation this handler builds is **read-side response bookkeeping** (assembling
a return list in local memory), not a cross-file write transaction — the thing D2(ii)/Invariant-3
actually guards against. No new DR is needed for this op on this basis.

---

## 3. DEC-1 — the commit-reality matcher (C2)

`coordinator_core/reconcile/commit_reality.py::evaluate_commit_reality` decides `verdict:
"auto-ship"` iff ALL THREE signals hold, per handoff:

1. **SUBJECT MATCH** — `git log --pretty=format:"%H %s" --since=<created> -- <scope-paths>` yields
   a commit whose subject contains a noun token derived from the handoff's scope basenames/title,
   **excluding** denylisted mechanical-commit-subject prefixes (from the example-doctrine-repo-owned policy's
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
concrete instance of the DR-047 boundary language above: a example-doctrine-repo-authored denylist edit changes
matcher behavior with zero claude-klabauter code change.

---

## 4. C3 — the unified gate evaluator's clear-predicate spec

`coordinator_core/reconcile/gate_eval.py::evaluate_gate` is COMPUTE_ONLY (pure read+compute, no
writes) and covers both the STRUCTURED path (`blocked_by:[stub-id,...]` graph edges on
`kind: spinoff-roadmap` handoffs) and the PROSE fallback path (free-text `gate_dependency`
one-liner on other handoff kinds). Load-bearing rules, converging with
`example-doctrine-repo/archive/specs/2026-06/2026-06-27-status-propagation-primitive.md` §68-70:

- **All-shipped → `clear`.** ALL `blocked_by` members must be `shipped` SPECIFICALLY — `abandoned`
  is terminal (stops re-evaluation) but never counts toward clearing.
- **Abandoned-blocker → surface.** A `blocked_by` member that is `abandoned` routes to `surface`
  when it's the *only* unresolved state (no shipped, no still-open) — the dependent's premise is
  now likely-false/moot and needs EM judgment, not a silent auto-flip. **This rule (abandoned→
  surface) is a claude-klabauter extension flagged to example-doctrine-repo as a proposed spec addition** — the canonical
  status-propagation-primitive spec (§68-73) covers the shipped/asymmetry/gate_cleared_by rules but
  is silent on the abandoned-blocker case (see the C6 memo, § 6).
- **Partial-satisfaction → narrow, never fire-on-first-edge.** AND-reduce over EVERY member: when
  some-but-not-all are shipped, the reported `remaining_blockers` drops the shipped edges and
  `verdict=narrow` (caller stays `awaiting_gate`, `blocked_by` mutates down via C8). This is the
  tc-4 regression guard (`blocked_by:[tc-1, tc-5]` must NOT flip to `ready_to_fire` when only
  `tc-1` shipped), sourced from the tc-4 regression lesson
  `example-doctrine-repo/archive/lessons/2026-07/2026-06-23-a-gate-reconcile-hook-that-flips-a-depen.yaml`.
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
per the example-doctrine-repo alignment reply's item 3.

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

There is **no live caller today** — this is a recommendation for the C6 co-memo (§ 6) to carry to
Example-doctrine-repo, who wires the call site on receipt.

---

## 6. `/pickup` Step 3 partial-retirement — verbatim source passage

Structured `blocked_by` edges evaluated by C3/routed by C8 are intended to retire the equivalent
manual check in example-doctrine-repo's `/pickup` skill Step 3d — but only the **structured-edge** portion; prose/
cross-repo-memo gates keep EM judgment, and the `awaiting_gate` aging check (14d/7d) is preserved
regardless (this op computes verdicts, it does not track aging). Quoted **verbatim** (not
paraphrased) from `coordinator/skills/pickup/SKILL.md` (example-doctrine-repo repo, ~lines 180-186), sourced
from `state/lessons.md` commit `a8b2aba0` 2026-06-27 (example-doctrine-repo repo):

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

DEC-1's three-signal matcher (§ 3 above) is the **op-shaped generalization** of this exact
heuristic prose — same deliverable-scope-glob + commit-reachability logic, machine-executed and
guarded by the mechanical-commit denylist + cross-handoff attribution demotion this hand-rolled
`/pickup` check does not itself carry.

---

## 7. What example-doctrine-repo receives via the C6 proposal memo

This contract doc is the artifact the C6 cross-repo memo (kind: `proposal`, to `claude-central-em`)
points at when it asks example-doctrine-repo to ratify: (1) this op's params/return shape; (2) the
`auto-reconcile-policy.yaml` grammar pin example-doctrine-repo authors against; (3) the `gate_eval` clear-predicate
spec including the abandoned-surfaces extension flagged as a proposed spec addition (§ 4); plus the
routing asks — `strangle_route` example-doctrine-repo's `handoff-transition.js gate-recheck`/`repark` to the new
Claude-klabauter verbs, and the partial `/pickup` Step 3d retirement described in § 6. See the plan's C6 task
body for the full memo composition spec; this contract doc is cited by, not a substitute for, that
memo.

---

## 8. Out of scope

- **Wiring the `workday-start` call site.** example-doctrine-repo's to land on receipt of the C6 memo — not part of
  this op's ship.
- **Flipping the `dry_run` default to `false`.** Requires example-doctrine-repo ratification via the C6 co-memo;
  this contract pins the current (conservative) default only.
- **`handoff.transition`'s `gate-recheck`/`repark`/`gate-cascade-clear` verb internals.** Owned by
  `coordinator_core/ops/handoff_transition.py` (C1/C8); this doc describes only how
  `handoff.reconcile_open` calls into them.
- **The `auto-reconcile-policy.yaml` full grammar.** Pinned in
  `coordinator_core/contract/auto-reconcile-policy.grammar.md` (C9), not restated here beyond the
  boundary-split framing in the header.

---

<!-- producer-contract: claude-klabauter handoff.reconcile_open op — turnable-on, op name RATIFIED (example-doctrine-repo 2026-07-13),
     no live caller yet. Spec backlink: docs/plans/2026-07-13-claude-klabauter-auto-reconcile-open-handoffs.md § C5. -->
