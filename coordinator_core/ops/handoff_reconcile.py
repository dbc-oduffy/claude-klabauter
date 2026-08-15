"""
coordinator_core.ops.handoff_reconcile — "handoff.reconcile_open" op (name RATIFIED by
DoE 2026-07-13, cross-repo/archive/2026-07-13-claude-central-em-doe-auto-reconcile-ratifications.md).

Purpose: the auto-reconcile orchestrator. Enumerates every currently-open handoff,
runs the C2 commit_reality matcher and the C3 gate_eval evaluator per handoff, and
either drives a transition (auto-ship / structured gate-cascade-clear) or appends the
handoff to `surfaced[]` for EM judgment. `dry_run` defaults to `true` — the caller
must pass `dry_run=false` explicitly to transition anything (the Staff Engineer review, finding
index 4 — resolved in-plan: conservatism parity with DEC-1's surface-never-guess
invariant; auto-mutation of work-state warrants opt-in until DoE ratifies flip-to-false
via the C6 co-memo).

Chain-walk reaper (C6, reconcile-open-consumed-in-flight-dead-zone plan): when a
node reaches verdict auto-ship, `_reconcile_ancestor_chain` walks its
`predecessor`/`origin_handoff` edges upward (`_CHAIN_EDGE_KINDS`) to catch the
abandoned-`ready_to_fire`-successor class (handoff/SKILL.md:324) — a pinned
predecessor whose continuation was carried forward by the shipped successor but
whose own scope wasn't independently matched by per-node C2. Chain topology is
NEVER itself evidence: each ancestor is archived only after clearing BOTH an
evidence gate (`_ancestor_evidence` — successor's touched paths subsume the
ancestor's own scope, OR the ancestor independently clears C2 on its own
evidence) AND a liveness gate (`_ancestor_liveness_blocked` — reverse_membership
+ live-claim/consumed_by, mirroring fleet/archive_handoffs.py's Checks 3/4, which
`handoff.ship_and_archive`'s own call path does not supply).

Open-set enumeration predicate (widened 2026-07-17, reconcile-open-consumed-in-flight-
dead-zone plan; status tokens updated for DR-084 dual-vocab reads, C5): the open set is
the EXACT complement of `coordinator_core/ops/fleet/archive_handoffs.py`'s `_is_terminal`
Branch A (`status in {claimed, consumed} AND deployment_state != "in_flight"` is
terminal/closed). Complementing Branch A gives two admitted shapes:
  (status in {open, active} AND deployment_state NOT IN _CLOSED_DEPLOYMENT_STATES)
  OR (status in {claimed, consumed} AND deployment_state == "in_flight")
Claude-klabauter's `status` axis is `{open, claimed}` under DR-084 (reads stay dual-tolerant on
the pre-migration `{active, consumed}` spellings) — "open"/"in_flight" as used above for
the OTHER (deployment_state) axis are `deployment_state` values, not `status` values, so
mixing the two axes in one predicate is loosely specified without this explicit
complement relationship. `claimed`/`consumed` handoffs in every OTHER deployment_state
(`awaiting_gate`, `ready_to_fire`, unset, ...) stay EXCLUDED from the open set — only
`claimed`/`consumed` + `in_flight` is admitted, matching Branch A's sole non-terminal
carve-out.

Per-verdict routing (the Staff Engineer review, finding index 3 — minor: route the C3 structured
verdict's full four-value enum explicitly, never collapse clear/narrow vs everything
else into a two-way branch):
    auto-ship (C2)      -> invoke handoff.ship_and_archive (reuse the shipped op;
                           never hand-stamp).
    clear     (C3)      -> invoke C8 gate-cascade-clear (all blockers resolved, flips
                           awaiting_gate -> ready_to_fire).
    narrow    (C3)      -> invoke C8 gate-cascade-clear (narrow-mutation, blocked_by
                           shrinks, stays awaiting_gate) AND append to surfaced[] when
                           `also_surface` is True (remaining_blockers includes an
                           abandoned id — narrow+surface composite, C3 finding 1).
    surface   (C3)      -> append to surfaced[] only, no transition.
    not-cleared (C3)    -> NO action, NOT surfaced — the benign steady state (every
                           blocker legitimately still awaiting_gate/in_flight); this
                           deliberately does not flood the EM signal every
                           workday-start with handoffs that are simply, correctly,
                           still waiting.
Everything else below the auto-ship/clear/narrow bar (C2 verdict in {surface,
no-match}) -> appended to surfaced[]. PROSE `gate_dependency` gates never
auto-transition regardless of clear/surface — gate_eval's structured-vs-prose split
already enforces this upstream (see reconcile/gate_eval.py); this op simply routes
whatever verdict comes back.

PM ruling 2026-08-01 (surface-only `gate_evidence` sweep): the `_AWAITING_GATE_STATE`
branch now reads each handoff's on-disk `gate_evidence:` block
(`handoff_transition._read_gate_evidence_resolved`, live-resolving every leg) and
threads it into `evaluate_gate` — previously this call always passed `gate_evidence=
None`, leaving the sweep blind to a `covers_prose: true` block that a human could
only reach via the manual `gate-recheck` verb. Consuming that evidence is explicitly
NOT the same as acting on it: whenever a handoff carries ANY `gate_evidence` block at
all, a would-be-transitioning `clear`/`narrow` verdict is intercepted before the
clear/narrow -> `_handle_gate_cascade` routing above and forced onto the
surfaced[]-only path (see the call site's own comment) — the PROSE-never-auto-
transitions invariant this docstring already claimed therefore still holds, now for a
REASON (an explicit guard) rather than an emergent property of `witness_candidates`
always being empty.

This guard is keyed on `gate_evidence` PRESENCE, not on whether a prose
`gate_dependency` also happens to be present (a first landing of this feature keyed on
`has_prose and covers_prose` — the ONLY combination `evaluate_gate`'s rule 0 actually
consumes gate_evidence for — and that guard decays to a no-op exactly as the corpus
migrates off `gate_dependency`, which `handoff.schema.json` marks DEPRECATED: an
evidence-only gate with no prose never reaches rule 0, so it falls through to the
pre-existing structured/vacuous path, which does NOT consult `gate_evidence` at all;
a prose-keyed guard would let that class auto-flip unverified). The surfaced[] entry
marks `gate_evidence_resolved: true` ONLY when the verdict was genuinely
evidence-derived (rule 0 fired); a handoff whose `gate_evidence` block exists but
wasn't actually consulted by this verdict (structured/vacuous path, no covering
prose) still gets intercepted defensively but with `gate_evidence_resolved: false` and
a reason naming the distinction — a human reading the orient surface can tell a
machine-checked "all legs satisfied" apart from "evidence present but not what decided
this" apart from a plain merely-surfaced gate. See docs/plans/2026-07-13-claude-klabauter-auto-
reconcile-open-handoffs.md § D5 ("evidence never auto-clears a gate").

STEADY-STATE COST (Review: code-reviewer Finding 5): every `awaiting_gate` handoff
carrying ANY `gate_evidence:` block pays LIVE sibling-repo I/O on every automatic
`reconcile_open` sweep, one `resolve_leg` call per I/O-kind leg
(`_read_gate_evidence_resolved` -> `_reresolve_gate_evidence_leg`) — previously this
live I/O only happened on-demand, once per manual `gate-recheck` invocation. Cost
scales linearly with (open awaiting_gate handoffs carrying gate_evidence) x (legs per
handoff) x (sibling-I/O latency per leg), with no cap or memoization; at the
15-live-record scale this feature was integrated against this is a non-issue, but it
is silent and unbounded as the corpus grows. `_read_gate_evidence_resolved` already
short-circuits the ONE free case (no live I/O at all when `deployment_state` isn't
`awaiting_gate` or no `gate_evidence:` block is present — see its own docstring). A
further `covers_prose`-keyed short-circuit (skip leg resolution when `covers_prose` is
falsy) was considered and rejected: `_read_gate_evidence_resolved` is a SHARED reader
also consumed by `handoff_gate_aging.classify_gate`, whose own no-prose evaluation
path consults these same legs regardless of `covers_prose` (see
`_read_gate_evidence_resolved`'s own docstring for the full reasoning) — this sweep
cannot narrow the shared reader's contract on its own initiative without starving
that other consumer.

DR-212 batch-orchestration compliance argument (must land here, not just the plan):
`handoff.reconcile_open` loops over every open handoff and, per handoff, invokes
`handoff.ship_and_archive` or `handoff.transition gate-cascade-clear` — an
orchestrating op calling per-file mutators in a loop. This is DR-212-compliant, NOT
the batch-mutation pattern DR-212 reserves solely for `handoff.normalize`
(D2(ii)/Invariant-3: "Future batch-mutation ops with different semantics or different
target nouns would require their own DR and cannot inherit this carve-out"):
`reconcile_open` never itself batch-writes multiple `state/handoffs/*.md` files in one
call — each `ship_and_archive`/`gate-cascade-clear` invocation remains its own
independent, already-DR-212-compliant single-file `handoff.ship_and_archive`/
`handoff.transition` call (per D2(ii)'s "N independent per-file idempotent writes...
not a compound transaction" language, which DR-212 already validates for
`handoff.normalize`'s internal loop). The `surfaced[]`/`reconciled[]`/`gates_cleared[]`
accumulation this handler builds is read-side response bookkeeping (assembling a
return list in local memory), not a cross-file write transaction — the thing
D2(ii)/Invariant-3 actually guards against. No new DR is needed for this op on this
basis.

Self-registration: importing this module fires register_op("handoff.reconcile_open").
Add to coordinator_core/ops/__init__.py and register its scope ("common_dir") in
ipc.py::_OP_KEY_SCOPE.

Spec backlink: pln-claude-klabauter-auto-reconcile-pass-off-425848 § C4

Negative-spec:
  - Does NOT auto-ship when the loaded policy's `auto_ship_enabled` is False
    (absent or malformed `auto-reconcile-policy.yaml`, C9's fail-closed
    conservative default) — this is the AC10 fail-closed gate, enforced here
    (not merely produced as a signal by C9's policy_loader): a C2
    verdict=='auto-ship' handoff still records a `reconciled[]` entry with
    `applied: False` and a `"auto_ship_enabled=false (fail-closed policy)"`
    reason, but ship_and_archive is never invoked, regardless of `dry_run`.
  - Does NOT hand-stamp deployment_state:shipped — always reuses
    handoff.ship_and_archive for the auto-ship path.
  - Does NOT hand-mutate blocked_by/gate_cleared_by on an `awaiting_gate`
    handoff — always reuses handoff.transition's gate-cascade-clear verb (C8),
    which independently re-verifies each blocker's live deployment_state at
    act-time before writing.
  - (jgate-clearance-recording-seam) DOES hand-mutate blocked_by/
    no_longer_blocked_by directly, in-module, for a pickup-claimed
    (`deployment_state: in_flight`) handoff only — a residue-retirement-only
    write that never flips deployment_state or touches gate_dependency/
    gate_evidence prose, reusing (never widening) `_gate_cascade_clear`,
    which is unreachable there by design (MutateAbort outside awaiting_gate).
    See `_handle_in_flight_blocked_by_retirement`'s own docstring. Review:
    staff-eng Finding 3 — this makes handoff_reconcile.py the codebase's
    SECOND writer of the schema's `blocked_by`/`no_longer_blocked_by` MOVE
    invariant, and the two writers currently DISAGREE: `_gate_cascade_clear`
    (handoff_transition.py, untouched by this pass — widening its own write
    scope is correctly out of THIS pass's write-scope) DROPS a retired id
    from both arrays instead of moving it, violating handoff.schema.json's
    declared union-invariant; `_handle_in_flight_blocked_by_retirement` is
    the correct reference implementation of that invariant. Recorded, not
    fixed here — see state/bug-backlog/2026-08-14-gate-cascade-clear-drops-
    blocked-by-entries-instead-of-moving.md.
  - Does NOT auto-transition a PROSE gate_dependency verdict — surfaces only,
    regardless of the C3 verdict value, per DoE alignment reply #3 (EM judgment
    retained for prose gates).
  - (PM ruling 2026-08-01) Does NOT let a `clear`/`narrow` verdict reach
    `_handle_gate_cascade` for ANY handoff carrying a `gate_evidence` block —
    keyed on gate_evidence PRESENCE, not on whether a prose `gate_dependency`
    also happens to be present (that narrower keying would decay to a no-op
    as the corpus migrates off the DEPRECATED `gate_dependency` field — an
    evidence-only gate never reaches `evaluate_gate`'s rule 0, so its verdict
    would come from the structured/vacuous path, which does not consult
    `gate_evidence` at all, and could reach `clear` with the evidence never
    actually checked). The resulting verdict is always forced onto
    surfaced[] only, marked `gate_evidence_resolved: True` only when the
    verdict was genuinely evidence-derived (rule 0 fired) and `False` when
    the evidence was merely present-but-unconsumed — never applied either
    way. Consuming `gate_evidence` to compute a BETTER answer is explicitly
    not the same as acting on that answer.
  - Does NOT transition anything when dry_run is true (the default, and the
    fail-closed value on any absent/malformed policy) — computes and returns
    verdicts only.
  - (D2(a)) Does NOT let a caller-supplied `dry_run` param silently override the
    loaded policy's own `dry_run` value — the policy is the SOLE source of
    truth; a disagreeing caller param is refused unless paired with a
    non-empty `dry_run_override_reason`, and even an applied override is
    logged at WARNING and surfaced in the response's `dry_run_override` field.
    See `_resolve_dry_run`.
  - Does NOT batch-write multiple state/handoffs/*.md files in a single call — see
    the DR-212 compliance argument above.
  - Does NOT (C6) archive a pinned-predecessor ancestor on chain topology alone —
    an ancestor whose scope is not subsumed by the successor's shipping commit
    AND does not independently clear C2, or that fails the liveness gate, is
    left untouched even when a descendant shipped.
  - Does NOT (C6) walk the predecessor chain from a node whose OWN verdict is
    anything other than auto-ship — a `ready_to_fire` successor with no ship
    evidence, and its pinned predecessor, are both left untouched.
  - (D1) Does NOT add a threshold or a policy key to detect unactioned
    candidates — see the D1 docstring section above for why both were
    rejected. The conservation assertion is unconditional, not tunable.
  - (D1) Does NOT report a conservation violation via a raised exception or
    any `.error`-shaped key — that is precisely the channel
    `check-auto-reconcile.py`'s dual silent-skip rule swallows. Always a
    distinct `conservation_violations` list plus a non-zero `exit_code`.
  - (D1) Does NOT treat "not present in THIS run's surfaced[]" as sufficient
    evidence a previously-surfaced candidate was acted on — only genuine
    terminality (no longer in the open set) or an explicit, non-empty
    `_DISPOSITION_FIELD` + `_DISPOSITION_REASON_FIELD` pair clears it. A
    candidate that quietly stopped being surfaced while still open (e.g. its
    verdict flipped to `not-cleared`'s benign-steady-state branch) is still a
    violation.
  - (D1) Does NOT persist conservation history under `state/` or
    `archive/` — `<common_dir>/coordinator-sessions/reconcile-history/` only,
    matching the existing session-bookkeeping convention. Never git-tracked,
    never doctrine content.
  - (C12a) Does NOT write a dry-run report unless `report_path` is explicitly
    supplied — the pre-C12a callers (check-auto-reconcile.py, production
    ceremony callers) omit it and get the identical pre-C12a return shape
    plus zero extra I/O. Does NOT embed a wall-clock timestamp in the report
    BODY (only the optional `report_run_label` header line) — see
    `_build_dry_run_report`'s docstring for why that purity is load-bearing
    for the AC13 idempotence property. Does NOT hand-roll file locking for
    the report write — routes through the same `locked_rmw` primitive as
    the D1 history file.

C3 acceptance-oracle fixes (2026-07-17, reconcile-open-consumed-in-flight-dead-zone
plan, C3 chunk — verified empirically against the AC8 fixtures, not merely inferred):
  1. Cross-handoff attribution guard vs. chain-walk conflict: `evaluate_commit_reality`'s
     attribution guard demotes to `surface` when >1 open handoff's scope overlaps the
     candidate commit's touched paths — but a C6 gate-(a) chain-walk scenario
     STRUCTURALLY requires the successor's shipping commit to touch its own pinned
     ancestor's scope (that IS the subsumption evidence), so the ancestor (an open
     handoff) always overlapped, always tripping the guard on the successor's OWN
     per-node evaluation and preventing `_handle_auto_ship`/`_reconcile_ancestor_chain`
     from ever firing. Fixed by `_chain_ancestor_norm_paths`: a handoff's own
     pinned-lineage ancestors (walked via `_CHAIN_EDGE_KINDS`) are excluded from the
     `other_open_handoffs` set passed to its own `evaluate_commit_reality` call — a
     predecessor/successor pair sharing scope via a continuation commit is NOT
     ambiguous cross-handoff attribution (that's the true-AC4 shape: two UNRELATED
     handoffs, no declared lineage edge between them).
  2. Stale-start-path gap: `_reconcile_ancestor_chain` used to call `walk_forward`
     starting from `handoff["_path"]` — but by the time it runs, `ship_and_archive`
     has ALREADY git-mv'd that file out of state/handoffs/, so walk_forward's own
     re-read of the (now-gone) start path returned `{}`, `node_gate=_is_open` rejected
     the start node, and the walk never reached `handoff`'s own predecessor/
     origin_handoff edges — every ancestor was silently missed. Fixed by resolving
     `handoff`'s OWN immediate chain edges from the IN-MEMORY frontmatter dict
     (captured pre-move, still accurate) and running `walk_forward` from each
     resolved immediate-ancestor path instead (still live on disk — only `handoff`
     itself moved).

D1 — the severed-observer gate (docs/plans/2026-07-26-push-side-write-discipline.md
§ D1, "Severed-observer gate — unactioned reconciler candidates fail loud"):

THE ROOT FAILURE this addresses: this op computed the correct `surfaced[]` answer
every day for weeks. Its output was a judgment list an operator was expected to
act on. Over 91 opportunities the operator acted 15 times, and nothing ever
noticed that the other 76 were going unactioned — the artifact existed, produced
the right output, and discharged nothing. Surfacing a candidate is not the same
as anyone having looked at it; a judgment list nobody is answerable to is a
severed observer. THIS GENERALIZES: any reconciler whose output is a judgment
list should be answerable to "did anything act on this?" — an emitter with no
paired conservation check is the anti-pattern this detector exists to catch,
here and anywhere else the same shape recurs.

Deliberately NOT a threshold ("fail if N candidates recur for M days") and NOT a
policy key. Both were considered and rejected: (1) a threshold needs baseline
data nobody has, which is the same "someone must remember to set this later"
class of debt this plan exists to end; (2) `check-auto-reconcile.py` (the
op's one documented consumer) has a DUAL SILENT-SKIP rule — it exits 0 with no
output both when the envelope carries `.error` and when the op is not
importable, so a fail-loud detector whose signal could land in `.error` is not
loud, it is exactly as silent as every other failure that script already
swallows; (3) `policy_loader.load_policy` returns `dry_run: True` /
`auto_ship_enabled: False` on an absent or malformed policy — a threshold held
as a policy key would mean a malformed policy silently disarms the detector
AND the transitioner (D2) together, the same failure mode in a second place.

Built instead as a CONSERVATION ASSERTION: every candidate present in run N's
`surfaced[]` must, by run N+1, be either (a) terminal — no longer in the open
set at all, i.e. archived/closed by the normal lifecycle — or (b) carrying a
RECORDED disposition on disk (`_DISPOSITION_FIELD` +
`_DISPOSITION_REASON_FIELD`, both non-empty — see `_check_conservation`).
Anything else is a violation: a candidate that recurs untouched is exactly the
76-of-91 pattern that went unnoticed. Precedent for mandatory justification
already lives in this codebase — `_repair_archived_shipped_in_handler`
(coordinator_core/ops/handoff_stamp.py) requires a non-empty `reason` on every
call, on the stated grounds that "an archived-record mutation with no recorded
justification is strictly worse than no repair verb at all." Same tooth,
applied to dispositions here: a bare "someone looked at it" flag would recreate
the operator-remembers gap this plan exists to close, so a disposition without
its own reason does not count as recorded.

Run-to-run comparison needs somewhere to read run N's surfaced set from when
computing run N+1's assertion. This op previously persisted no state of its
own — there is no established "this op's own state lives at X" precedent to
follow, so the choice is made explicit here: `_history_path` writes to
`<common_dir>/coordinator-sessions/reconcile-history/surfaced-history.json`,
the SAME location family (`<common_dir>/coordinator-sessions/...`) that
`fleet/_common.py`'s `_sessions_dir`/`handoff_claim_dir` already use for
per-run, non-doctrine session bookkeeping — inside the git common dir, never
the worktree, so it is ephemeral operational state, not tracked content, and
never collides with `state/` (doctrine substrate) or `archive/` (closed
records). The literal `"coordinator-sessions"` segment is duplicated here
rather than importing `fleet/_common.py`'s leading-underscore
`_sessions_dir` across a module boundary — that leading underscore is this
codebase's existing convention for "module-private", and this op already
respects it (imports only the non-underscore `handoff_claim_dir`,
`main_worktree_root`, `collect_live_handoff_paths` from that module).

Routed to fail loud WITHOUT inheriting `check-auto-reconcile.py`'s silent-skip
rule: a violation is never raised as an exception (an exception becomes a
JSON-RPC top-level `.error`, which is precisely the field that dual
silent-skip rule swallows — the loudest-looking channel available is actually
the quietest one this op could pick) and never nested under this response's
own `.error`-shaped keys. Instead the response carries a distinct
`conservation_violations` list (empty in the normal case) and `exit_code`
departs from this op's otherwise-always-0 contract (see `_handler`'s
docstring) — a NEW, deliberately-differently-shaped signal a caller must
explicitly choose to ignore, rather than a value already sitting inside a
field an existing consumer already discards by design. `check-auto-reconcile.py`
itself is a read-only consumer this op's write-scope excludes touching (routes
via a real caller/wiring change, out of scope here) — this op's job is to
make the fact loud at the source; wiring a specific consumer to look at it is
separate follow-on work.

HONEST SCOPE NOTE: after C1 (landed — the mint path now stamps + archives its
own predecessor in-transaction) plus an armed reconciler (D2), this failure
class is largely gone by construction for NEW work going forward — D1 monitors
RESIDUE, not the primary mechanism. It is worth building, and it is
deliberately NOT billed as "the" discharge-test artifact for this plan; that
distinction belongs to D1b (`provisional_until`/`revisit_by` field), which
covers the sibling failure class of an expired STAGED decision that was never
a reconciler candidate in the first place (a decision sitting in a spec/plan
file, not something `surfaced[]` ever computed) — D1's conservation assertion
structurally cannot see that class, by design, since it only ever compares two
runs' `surfaced[]` sets.

D2(a) — the policy `dry_run` key made load-bearing (2026-07-27,
docs/plans/2026-07-26-push-side-write-discipline.md § D2): `_resolve_dry_run`
makes the loaded policy's `dry_run` the SOLE source of truth, replacing the
prior `params.get("dry_run", True)`-only resolution that left the key
grammar-required (`policy_loader._REQUIRED_KEYS`) but consumed nowhere. Fail-
closed survives structurally — `policy_loader._conservative_policy()` hard-
codes `dry_run: True` on both its absent AND malformed branches, so this
resolution only ever sees `True` there regardless of caller params. A caller
param may still diverge from the policy value, but ONLY as a named, logged
escape (`dry_run_override_reason`, non-empty) — see `_resolve_dry_run`'s own
docstring.

D1 disposition-clearing mechanism — Review: code-reviewer (Finding 3): neither a
schema declaration nor a writer verb exists yet for `_DISPOSITION_FIELD`/
`_DISPOSITION_REASON_FIELD`, though `_has_recorded_disposition`/
`_check_conservation` above are already the read side that treats a recorded
pair as clearing a violation. Both gaps are DELIBERATE scope cuts for THIS
integration pass, not oversights:
  - Schema declaration: `handoff.schema.json`'s grammar SSOT is
    `coordinator/schemas/handoff.schema.json` in DoE-claude, one-way vendored
    into this repo's `coordinator_core/frontmatter/schemas/` copy (see that
    vendored file's own tamper-check test,
    `test_handoff_schema_matches_doe_head_after_dr084_revendor` in
    `coordinator_core/frontmatter/tests/test_schema_validate.py`) — editing
    the vendored copy in place without a matching DoE-side SSOT edit +
    re-vendor makes the vendored copy diverge from DoE HEAD and fails that
    tamper-check outright (confirmed: attempted in this integration pass,
    reverted once the drift test failed). Declaring the fields is therefore a
    cross-repo change this claude-klabauter-scoped integration pass cannot land alone.
  - Writer verb: a dedicated op (e.g. `handoff.record_disposition` mirroring
    `_repair_archived_shipped_in_handler`'s mandatory-reason shape) is real
    new-op-surface work — registration, handler, tests — genuinely out of a
    single-file review-integration pass.
Both are tracked as debt-backlog entries (see `state/debt-backlog/`) rather
than landed inline here. This is NOT fail-open: an unrecorded disposition
still correctly counts as a violation (`_check_conservation`) — the gap is
discoverability/ergonomics (schema-invisible, hand-edit-only), not
correctness.

D1 fail-loud read hazard — Review: code-reviewer (Finding 2), APPLY per EM
adjudication 2026-07-27: `_load_surfaced_history`/`_save_surfaced_history` below
now (a) route the write through `coordinator_core.locked_write.locked_rmw` for
cross-process flock + atomic mkstemp+os.replace (the SAME primitive
`handoff_stamp.py` already uses for concurrent frontmatter mutation), closing
the "two concurrent non-atomic write_text calls interleave and corrupt the
shared history file" hazard on the exact file ~8 concurrent sessions touch; and
(b) distinguish a genuinely-absent history file (first-ever run — degrades to
`{}` silently, unchanged, still correct) from a PRESENT-but-corrupt/malformed
one (JSON parse failure, unreadable, or wrong shape) — the latter now threads a
synthetic entry into `conservation_violations` (and therefore `exit_code: 2`)
instead of degrading to `{}` on a WARNING-level log line nobody is watching. A
silently-reset baseline made the conservation assertion pass vacuously — worse
than no assertion, because it looks like coverage; this makes that specific
failure shape fail through the SAME loud channel D1 already established for a
real violation, rather than inventing a second, quieter one.

D1 `conservation_violations`/`exit_code:2` has no live consumer yet — Review:
code-reviewer (Finding 4), documenting per that finding's second branch (a
consumer-wiring change is out of THIS file's write-scope; see below for why).
`check-auto-reconcile.py` (`coordinator/bin/check-auto-reconcile.py`) is this
op's one documented consumer and, as of this pass, still renders ONLY
`result.surfaced[]` — it does not read `conservation_violations` or branch on
`exit_code == 2` (see that script's own module docstring's dual silent-skip
rule: `.error` present or the op unimportable both degrade to a fully silent
exit 0; a non-`.error`, non-zero `exit_code` with a populated
`conservation_violations` list is currently rendered as if it were a clean
pass — nothing prints). Precisely where the signal is currently NOT surfaced:
no cron/ceremony/hook in this tree branches on this op's `exit_code` or
`conservation_violations` fields anywhere — a caller must explicitly invoke
`handoff.reconcile_open` in-process and read those two fields itself to see
this signal at all. Wiring `check-auto-reconcile.py` to render it is real,
separate follow-on work (that script lives outside this file's declared
write-scope for this integration pass); tracked as a debt-backlog entry (see
`state/debt-backlog/`) rather than landed inline here.

C12a — the durable dry-run report (docs/plans/2026-07-26-gate-resolution-widen-and-migrate.md
§ C12a): every prior dry-run pass computed the right `reconciled`/`gates_cleared`/`surfaced`
answer and then DISCARDED it — the branch-reconcile reader renders it into judgment-points/
directives prose for one morning briefing and nothing persists it. `report_path` (optional
handler param) closes that gap: when supplied, `_build_dry_run_report` renders one row per
open handoff — current status/deployment_state, the verdict this pass computed, and WHY
(which rule fired: auto-ship gate, gate-cascade-clear blocker_ids, surface reason, or the
benign not-cleared steady state) — and the handler writes it via `locked_rmw` (the SAME
cross-process flock + atomic mkstemp+os.replace primitive already used for
`surfaced-history.json` above; never a bare `write_text`, since a report path can be a shared
file multiple concurrent sessions dry-run against). The report body is a PURE function of
`open_handoffs`/`reconciled`/`gates_cleared`/`surfaced` — deliberately no wall-clock timestamp
in its content — so re-running against an unchanged corpus reproduces byte-identical text and
`locked_rmw`'s own skip-if-identical (`new_text == old_text`) makes the second write a true
no-op, not merely "another write that happens to match." `report_run_label` (optional,
caller-supplied) is the only place a human-meaningful label may appear, kept out of the
idempotence-bearing body precisely so supplying it cannot itself defeat the no-op property.

D2(b) — DELIBERATELY NOT DONE HERE: this chunk does NOT flip
`coordinator/auto-reconcile-policy.yaml`'s `dry_run` to `false` in DoE-claude.
`docs/plans/2026-07-26-push-side-write-discipline.md`'s third amendment (and
the sibling `2026-07-26-gate-resolution-widen-and-migrate.md` AC26/AC26b)
ratifies a property gate on arming that is independent of D2(a) landing:
"no live write path may produce a `shipped_in` value that the gate resolver
would read as CLEAR evidence without a `shipped_in_kind` tag that the schema
declares and the resolver discriminates on" — satisfied by that plan's AC22
(schema/enum conformance) + C22 (retiring `archive_stamp._resolve_scope_sha`
as a write-time strategy) + C24 (live-surface tagging + resolver
discrimination). As of this commit `_resolve_scope_sha` is still live
(`coordinator_core/archive_stamp.py`) and no schema declares
`shipped_in_kind` anywhere in this tree — AC22/AC23 are still `pending` in
both plans' own tracking tables. D2(a) makes the policy's `dry_run`
load-bearing so that THIS precondition is enforceable by a plain DoE-side
YAML edit once it clears; it does not itself clear the precondition.
"""

from __future__ import annotations
import sys

import asyncio
import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, TypeVar

import yaml

from coordinator_core.archival import reverse_membership
from coordinator_core.coverage import _get_handoff_consumed_by
from coordinator_core.dag import _read_meta, handoff_edges, resolve_target, walk_forward
from coordinator_core.frontmatter.primitives import (
    read_fm_field,
    read_fm_field_unquoted,
    rebuild,
    split_frontmatter,
)
from coordinator_core.frontmatter.schema_validate import format_validation_errors
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT
from coordinator_core.liveness import cs_claim_holder_live, resolve_live_session_ids
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops.fleet._common import (
    main_worktree_root,
    collect_live_handoff_paths,
    handoff_claim_dir,
)
from coordinator_core.ops.handoff_ship_archive import _handler as _ship_and_archive_handler
from coordinator_core.ops.handoff_transition import (
    _handler as _handoff_transition_handler,
    _blocker_clears_gate,
    _insert_fm_array_field,
    _read_gate_evidence_resolved,
    _replace_fm_array_field,
    _validate_fm,
)
from coordinator_core.reconcile.commit_reality import (
    evaluate_commit_reality,
    _pathspec_overlaps,
    _touched_paths,
)
from coordinator_core.reconcile.gate_eval import (
    consumes_gate_evidence,
    evaluate_gate,
)
from coordinator_core.reconcile.policy_loader import load_policy, policy_report_fields
from coordinator_core.wire_paths import rel_id

_LOG = logging.getLogger(__name__)

#: C6 chain-walk edge kinds — predecessor (lineage) + origin_handoff (provenance),
#: walked upward from a positively-shipped node to reconcile the pinned predecessor
#: chain (docs/plans/2026-07-17-reconcile-open-consumed-in-flight-dead-zone.md § C6).
#: Deliberately excludes additional_predecessors/forked_from — not named in the C6
#: spec, and origin_handoff is namespace-isolated elsewhere (dag.py) precisely so it
#: is walked only via an explicit opt-in edge_kinds set, which this is.
_CHAIN_EDGE_KINDS = frozenset({"predecessor", "origin_handoff"})

#: deployment_state values that remove a handoff from the open set even when
#: status is still "open" (read-tolerant fallback: "active") — mirrors
#: archive_handoffs.py's terminal-set values, applied here on the open-set side
#: of the two-axis predicate (see module docstring). Value now lives in
#: coordinator_core.lifecycle_constants (SSOT, DR-084 C3).
_CLOSED_DEPLOYMENT_STATES = HANDOFF_TERMINAL_DEPLOYMENT

#: the single deployment_state that keeps a status in {claimed, consumed} handoff
#: OPEN — the archive-complement of archive_handoffs.py's _is_terminal Branch A
#: (status in {claimed, consumed} AND deployment_state != in_flight is terminal/
#: closed), so deployment_state==in_flight is the sole non-terminal carve-out.
_CONSUMED_OPEN_DEPLOYMENT_STATE = "in_flight"

_AWAITING_GATE_STATE = "awaiting_gate"

#: D2(a) — the caller param that carries a NAMED, LOGGED escape from the policy's
#: declared `dry_run` posture (mirrors handoff_stamp.py's
#: _repair_archived_shipped_in_handler mandatory-`reason` pattern). Without a
#: non-empty string here, a caller-supplied `dry_run` that disagrees with the
#: loaded policy's `dry_run` is REFUSED — the policy value wins silently-to-the-
#: caller but loudly-to-the-log, never the other way round. See _resolve_dry_run.
_DRY_RUN_OVERRIDE_REASON_PARAM = "dry_run_override_reason"

#: D1 severed-observer gate — the two frontmatter fields that together count as
#: a "recorded disposition" for a previously-surfaced candidate. Both must be
#: non-empty strings (mirrors handoff_stamp.py's _repair_archived_shipped_in_handler
#: mandatory-`reason` precedent, cited in the D1 module-docstring section above) —
#: a bare acknowledgement flag with no reason would recreate the exact
#: operator-remembers gap D1 exists to close.
_DISPOSITION_FIELD = "reconcile_disposition"
_DISPOSITION_REASON_FIELD = "reconcile_disposition_reason"

#: D1 — ephemeral run-history location, <common_dir>/coordinator-sessions/...,
#: same location family as fleet/_common.py's _sessions_dir (see docstring).
_RECONCILE_HISTORY_RELPATH = ("coordinator-sessions", "reconcile-history", "surfaced-history.json")


def _history_path(common_dir: Path) -> Path:
    """D1 — absolute path to the persisted previous-run surfaced-id map.

    Lives under the git common dir, never the worktree — ephemeral
    per-run bookkeeping, not doctrine content, not git-tracked. See the D1
    module-docstring section for why this location was chosen.
    """
    path = common_dir
    for segment in _RECONCILE_HISTORY_RELPATH:
        path = path / segment
    return path


def _load_surfaced_history(history_path: Path) -> "tuple[Dict[str, str], Optional[str]]":
    """D1 — return (previous_surfaced_map, load_error).

    `load_error` is None on a clean read AND on the legitimate first-ever-run
    case (history file genuinely absent — see Negative-spec). It is a
    non-empty diagnostic string whenever the file EXISTS but is unreadable,
    not valid JSON, or has an unexpected shape — Review: code-reviewer
    (Finding 2): degrading those cases to `{}` on a log-only WARNING makes the
    conservation assertion pass vacuously for THIS run (it never actually got
    to compare against the real previous-run set), which looks like coverage
    but isn't. The caller threads a non-None `load_error` into
    `conservation_violations` so it fails LOUD through the same channel as a
    real violation, instead of a WARNING nobody is watching. Now that
    `_save_surfaced_history` writes atomically via `locked_rmw`, a reader can
    no longer observe a partially-written file mid-write — a present-but-
    corrupt file at this point means genuine corruption/tampering, not a
    write race, so treating it as loud rather than "no prior run" is correct
    either way.

    Negative-spec: an ABSENT file is not an error — genuinely first-ever-run
    is expected and must not be conflated with real corruption.
    """
    try:
        raw = history_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, None
    except OSError as exc:
        return {}, f"unreadable ({exc})"
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return {}, f"not valid JSON ({exc})"
    surfaced = data.get("surfaced") if isinstance(data, dict) else None
    if not isinstance(surfaced, dict):
        return {}, "malformed 'surfaced' key (missing or not an object)"
    return {
        str(handoff_id): str(rel_path)
        for handoff_id, rel_path in surfaced.items()
        if handoff_id
    }, None


async def _save_surfaced_history(
    history_path: Path, surfaced_map: Dict[str, str], repo_root: Path,
) -> None:
    """D1 — persist THIS run's surfaced-id map for the NEXT run's comparison.

    Review: code-reviewer (Finding 2): routed through
    `coordinator_core.locked_write.locked_rmw` (the same cross-process
    flock + atomic mkstemp+os.replace primitive `handoff_stamp.py` already
    uses for concurrent frontmatter mutation) instead of a bare
    `write_text` — this file lives under the git common dir and is written
    by every session reconciling this repo (~8 concurrent sessions is the
    plausible load), so an unlocked, non-atomic write can interleave with
    another session's write and corrupt the shared file. `mutate` ignores
    the pre-lock on-disk content and always returns THIS run's payload —
    there is no genuine merge to perform (each run's surfaced-id map is a
    fresh, independent snapshot, not derived from the previous one), so
    last-writer-wins-under-lock is the correct semantics; only atomicity and
    serialization matter here, not conflict resolution.

    Best-effort at the OUTER layer only: a lock-timeout or OS-level write
    failure here degrades the NEXT run's conservation assertion (it will
    see a stale or empty prior set) but must not fail THIS run, which has
    already computed and returned its own verdicts.
    """
    payload = json.dumps({"surfaced": surfaced_map}, indent=2, sort_keys=True) + "\n"
    try:
        history_path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(
            locked_rmw,
            history_path,
            lambda _old_text: payload,
            repo_root=repo_root,
            missing_ok=True,
        )
    except (OSError, LockTimeout) as exc:
        _LOG.warning(
            "handoff.reconcile_open: D1 could not persist surfaced-history to %s — "
            "the conservation assertion on the NEXT run will see a stale or empty "
            "prior set: %s",
            history_path, exc,
        )


def _has_recorded_disposition(meta: Dict[str, Any]) -> bool:
    """D1 — True iff `meta` carries BOTH `_DISPOSITION_FIELD` and
    `_DISPOSITION_REASON_FIELD` as non-empty strings. See the module-level
    comment on those two constants for why both are required."""
    disposition = meta.get(_DISPOSITION_FIELD)
    reason = meta.get(_DISPOSITION_REASON_FIELD)
    return bool(isinstance(disposition, str) and disposition.strip()) and bool(
        isinstance(reason, str) and reason.strip()
    )


def _check_conservation(
    previous_surfaced: Dict[str, str],
    open_by_id: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """D1 conservation assertion: every id in `previous_surfaced` (run N's
    surfaced[]) must, in `open_by_id` (run N+1's freshly-enumerated open set),
    be EITHER absent (terminal — closed/archived by the normal lifecycle since
    run N) OR present with a recorded disposition (`_has_recorded_disposition`).
    Anything else is a violation: still open, still no recorded disposition.

    Returns an empty list when nothing violates (including the trivial case
    where `previous_surfaced` is empty — first-ever run, or a prior run whose
    surfaced[] was itself empty).
    """
    violations: List[Dict[str, Any]] = []
    for handoff_id, rel_path in previous_surfaced.items():
        meta = open_by_id.get(handoff_id)
        if meta is None:
            # Terminal — no longer in the open set. Passes.
            continue
        if _has_recorded_disposition(meta):
            continue
        violations.append({
            "handoff_id": handoff_id,
            "handoff_path": rel_path,
            "reason": (
                f"surfaced in the previous reconcile pass and is STILL open with "
                f"no recorded {_DISPOSITION_FIELD!r}/{_DISPOSITION_REASON_FIELD!r} "
                "disposition — D1 conservation assertion violated (severed-observer "
                "gate)"
            ),
        })
    return violations


#: C12a — proposed-action keys that represent an actual transition candidate
#: (the resolver decided this handoff SHOULD move), as opposed to a mere
#: surface or the benign not-cleared steady state. Used only to compute the
#: report's "flip count" summary line — a row counts as a flip candidate
#: whenever the resolver's OWN verdict called for a transition, independent
#: of whether `dry_run` actually suppressed the write (a dry-run flip count
#: answers "how many would move if armed", not "how many moved this pass").
_TRANSITION_PROPOSED_ACTIONS = frozenset({
    "ship_and_archive", "ship_and_archive_chain_ancestor", "gate-cascade-clear",
})


def _build_dry_run_report(
    worktree_root: Path,
    open_handoffs: List[Dict[str, Any]],
    reconciled: List[Dict[str, Any]],
    gates_cleared: List[Dict[str, Any]],
    surfaced: List[Dict[str, Any]],
    dry_run: bool,
    policy_source: str,
    policy_path: Optional[str],
    report_run_label: Optional[str] = None,
) -> str:
    """C12a durable dry-run report body — one Markdown table row per open
    handoff: current status/deployment_state, this pass's proposed verdict,
    and WHY (which rule fired).

    `policy_source`/`policy_path` (§ C10 / AC16) are the flattened
    `policy_loader.policy_report_fields(policy_result)` pair — surfaced in
    the header alongside `dry_run` so a reader can tell "absent" / "malformed"
    / "loaded" apart, and see which path was resolved (under
    `CLAUDE_PLUGIN_ROOT`, the SHARED PLUGIN TREE — not this repo's own tree),
    without a cross-repo round-trip
    (`cross-repo/inbox/2026-07-28-doe-claude-em-handoff-terminal-starvation-
    answers.md`).

    Deliberately a PURE function of its list arguments — no wall-clock
    read, no I/O — so identical inputs always render identical text. That
    purity is load-bearing: `locked_rmw`'s own skip-if-identical write path
    (see `locked_write.py`) is what makes two consecutive passes over an
    UNCHANGED corpus collapse to "one write, then a no-op" (AC13) — if this
    function embedded `datetime.now()` in the body, every pass would differ
    and the no-op property would never hold. `report_run_label`, when
    supplied, is the one human-meaningful annotation permitted, and lives in
    the header only for exactly that reason.

    Precedence per handoff_id mirrors the handler's own routing order (C2
    auto-ship bar checked before the C3 awaiting_gate branch, which is
    checked before the residual surface case) — see `_handler`'s per-handoff
    loop, which this rendering intentionally re-derives from the SAME three
    accumulator lists it already produced rather than re-computing verdicts.

    The `proposed` cell is rendered prose, not a stable enum — it may gain
    parenthetical suffixes (e.g. `"surface (contradiction)"`) across changes.
    `action` is the machine-readable field (used for `flip_count` /
    `_TRANSITION_PROPOSED_ACTIONS` membership); do not parse `proposed`.
    """
    reconciled_by_id: Dict[str, Dict[str, Any]] = {}
    for entry in reconciled:
        hid = entry.get("handoff_id")
        if hid and hid not in reconciled_by_id:
            reconciled_by_id[str(hid)] = entry
    gate_by_id: Dict[str, Dict[str, Any]] = {
        str(entry["handoff_id"]): entry for entry in gates_cleared if entry.get("handoff_id")
    }
    surface_by_id: Dict[str, Dict[str, Any]] = {
        str(entry["handoff_id"]): entry for entry in surfaced if entry.get("handoff_id")
    }

    rows: List[str] = []
    verdict_counts: Dict[str, int] = {}
    flip_count = 0

    for handoff in sorted(open_handoffs, key=lambda h: str(h.get("id") or "")):
        hid = str(handoff.get("id") or "(no id)")
        abs_path = handoff.get("_path")
        row_path = _rel_path(worktree_root, abs_path) if abs_path else ""
        current = (
            f"status={handoff.get('status')!r} "
            f"deployment_state={handoff.get('deployment_state')!r}"
        )

        if hid in reconciled_by_id:
            entry = reconciled_by_id[hid]
            action = str(entry.get("action") or "ship_and_archive")
            gate = entry.get("gate")
            proposed = f"{action} (gate={gate})" if gate else action
            why = entry.get("message") or "C2 commit_reality verdict=auto-ship"
        elif hid in gate_by_id:
            entry = gate_by_id[hid]
            # Review: staff-eng Finding 1/2 — this bucket now holds TWO
            # distinct writers: `_handle_gate_cascade`'s C3 gate-cascade-clear
            # entries (action="gate-cascade-clear", carries a `verdict`) and
            # `_handle_in_flight_blocked_by_retirement`'s in-flight residue
            # retirement entries (action="blocked-by-retire-in-flight", never
            # carries a `verdict` — no C3 gate_eval verdict ever ran for this
            # path). Hard-coding action="gate-cascade-clear" here rendered an
            # in-flight retirement as a fabricated `gate-cascade-clear
            # (verdict=None)` row with a "C3 gate_eval cleared" justification
            # that never fired, AND inflated `flip_count` (below) with a
            # non-transition, since that literal is a
            # `_TRANSITION_PROPOSED_ACTIONS` member. Deriving `action` from
            # the entry itself fixes both.
            action = str(entry.get("action") or "gate-cascade-clear")
            if action == "blocked-by-retire-in-flight":
                proposed = action
                why = (
                    f"in-flight blocked_by residue retirement blocker_ids="
                    f"{entry.get('blocker_ids')} (deployment_state untouched, "
                    "not a C3 gate_eval verdict)"
                )
            else:
                proposed = f"{action} (verdict={entry.get('verdict')})"
                why = f"C3 gate_eval cleared blocker_ids={entry.get('blocker_ids')}"
            # Review: coordinator:code-reviewer — a handoff can land in BOTH
            # gate_by_id and surface_by_id (the narrow+surface composite —
            # also_surface set on an abandoned/dangling-ref remainder, see
            # gate_eval.py's _evaluate_structured_gate). Without this, the
            # elif chain silently drops the surfaced half of a composite
            # verdict, rendering it as a plain clear.
            if hid in surface_by_id:
                surface_reason = surface_by_id[hid].get("reason") or "surfaced for EM judgment"
                why = f"{why}; also surfaced: {surface_reason}"
        elif hid in surface_by_id:
            entry = surface_by_id[hid]
            action = "surface"
            proposed = "surface (contradiction)" if entry.get("contradiction") else action
            why = entry.get("reason") or "surfaced for EM judgment"
        else:
            action = "not-cleared"
            proposed = "not-cleared (benign steady state)"
            why = "no rule fired this pass — every declared blocker is still legitimately unresolved"

        verdict_counts[proposed] = verdict_counts.get(proposed, 0) + 1
        if action in _TRANSITION_PROPOSED_ACTIONS:
            flip_count += 1

        why_cell = str(why).replace("|", "\\|").replace("\n", " ")
        rows.append(f"| {hid} | {row_path} | {current} | {proposed} | {why_cell} |")

    lines: List[str] = ["# Gate resolver dry-run report", ""]
    if report_run_label:
        lines.append(f"- run label: {report_run_label}")
    lines.extend([
        f"- dry_run: {dry_run}",
        f"- policy_source: {policy_source}",
        f"- policy_path: {policy_path}",
        f"- open handoffs surveyed: {len(open_handoffs)}",
        f"- flip candidates (resolver verdict calls for a transition): {flip_count}",
        "",
        "## Per-verdict breakdown",
        "",
    ])
    for verdict in sorted(verdict_counts):
        lines.append(f"- {verdict}: {verdict_counts[verdict]}")
    lines.extend([
        "",
        "## Per-baton detail",
        "",
        "| handoff_id | path | current | proposed verdict | why |",
        "|---|---|---|---|---|",
        *rows,
        "",
    ])
    return "\n".join(lines) + "\n"


def _resolve_dry_run(policy: Dict[str, Any], params: Dict[str, Any]) -> "tuple[bool, Optional[Dict[str, Any]]]":
    """D2(a) — the loaded policy is the SOLE source of truth for `dry_run`.

    Returns `(effective_dry_run, override_info)`. `override_info` is `None` in
    the normal (no-override) case; a dict describing what happened whenever a
    caller-supplied `params["dry_run"]` disagreed with the policy value.

    `policy.get("dry_run", True)` already carries the fail-closed guarantee —
    `policy_loader._conservative_policy()` hard-codes `dry_run: True` on both
    the absent AND malformed branches (never both False), so an absent/
    malformed policy yields `dry_run=True` here with ZERO caller involvement.
    Arming is therefore only ever an explicit, present, valid, DoE-authored
    `dry_run: false` in the policy file — never an accident of a missing one.

    A caller-supplied `params["dry_run"]` that AGREES with the policy value is
    not an override (nothing is being bypassed) and is silently accepted. A
    caller-supplied `params["dry_run"]` that DISAGREES with the policy value
    is an override attempt, which requires a non-empty
    `params[_DRY_RUN_OVERRIDE_REASON_PARAM]` string:
      - reason present and non-empty -> override APPLIED, logged at WARNING
        (this is an exceptional escape from a DoE-declared posture — it
        should be visible in logs, mirroring handoff_stamp.py's
        `_repair_archived_shipped_in_handler` mandatory-`reason` precedent).
      - reason absent/empty -> override REFUSED, logged at WARNING, policy
        value wins. This is the "unnamed caller override is refused" case —
        `check-auto-reconcile.py` never supplies `dry_run` at all, so this
        branch only fires for a hypothetical future caller that tries to
        silently flip posture without naming why.
    """
    policy_dry_run = policy.get("dry_run", True)
    if not isinstance(policy_dry_run, bool):
        policy_dry_run = True

    caller_dry_run = params.get("dry_run")
    if not isinstance(caller_dry_run, bool) or caller_dry_run == policy_dry_run:
        return policy_dry_run, None

    reason = params.get(_DRY_RUN_OVERRIDE_REASON_PARAM)
    if isinstance(reason, str) and reason.strip():
        _LOG.warning(
            "handoff.reconcile_open: dry_run OVERRIDE applied — policy declares "
            "dry_run=%s, caller requested dry_run=%s with reason %r",
            policy_dry_run, caller_dry_run, reason,
        )
        return caller_dry_run, {
            "applied": True,
            "policy_dry_run": policy_dry_run,
            "requested_dry_run": caller_dry_run,
            "reason": reason,
        }

    _LOG.warning(
        "handoff.reconcile_open: dry_run override REFUSED — caller requested "
        "dry_run=%s against policy's dry_run=%s with no non-empty %r reason; "
        "deferring to policy (DoE owns rules, not a bypassable precedence "
        "convention)",
        caller_dry_run, policy_dry_run, _DRY_RUN_OVERRIDE_REASON_PARAM,
    )
    return policy_dry_run, {
        "applied": False,
        "policy_dry_run": policy_dry_run,
        "requested_dry_run": caller_dry_run,
        "reason": None,
    }


def _is_open(meta: Dict[str, Any]) -> bool:
    """Widened open predicate — the archive-complement of _is_terminal Branch A.

    Admits two shapes:
      (status in {open, active} AND deployment_state NOT IN _CLOSED_DEPLOYMENT_STATES)
      OR (status in {claimed, consumed} AND deployment_state == "in_flight")

    Reads are dual-tolerant per DR-084 (new vocabulary preferred, old accepted as
    fallback — writers elsewhere in the fleet emit new vocabulary only).

    This is the EXACT complement of coordinator_core/ops/fleet/archive_handoffs.py's
    `_is_terminal` Branch A (`status in {claimed, consumed} AND deployment_state !=
    "in_flight"` is terminal/closed) — see module docstring. `claimed`/`consumed`
    handoffs in any OTHER deployment_state (`awaiting_gate`, `ready_to_fire`, unset,
    ...) stay EXCLUDED here; only deployment_state==in_flight is admitted.

    # DoE lvv-04/C3 forward-compat — lockstep-update when consumed->claimed lands:
    # if DoE's lifecycle-vocab roadmap (lvv-04/C3) renames consumed->claimed or adds
    # new non-terminal deployment_state values that can co-occur with the renamed
    # status, this predicate (and archive_handoffs.py's Branch A it complements) must
    # be extended in lockstep — otherwise the two predicates silently drift apart and
    # a handoff could land in neither the open set nor the terminal set (or both).
    """
    status = (meta.get("status") or "").strip().lower()
    deployment_state = (meta.get("deployment_state") or "").strip().lower()
    if status in ("open", "active"):
        return deployment_state not in _CLOSED_DEPLOYMENT_STATES
    if status in ("claimed", "consumed"):
        return deployment_state == _CONSUMED_OPEN_DEPLOYMENT_STATE
    return False


def _collect_open_handoffs(worktree_root: Path) -> List[Dict[str, Any]]:
    """Enumerate state/handoffs/*.md and return parsed frontmatter dicts for the open set."""
    open_handoffs: List[Dict[str, Any]] = []
    for path in collect_live_handoff_paths(worktree_root):
        meta = _read_meta(str(path))
        if not meta:
            continue
        if not _is_open(meta):
            continue
        meta = dict(meta)
        meta["_path"] = str(path)
        meta.setdefault("id", meta.get("id") or path.stem)
        open_handoffs.append(meta)
    return open_handoffs


_WalkT = TypeVar("_WalkT")


def _walk_archive_md_files(
    archive_dir: Path,
    on_file: Callable[[Path], Optional[_WalkT]],
    on_scan_error: Callable[[OSError], None],
) -> "tuple[List[_WalkT], List[str]]":
    """Shared `archive/handoffs/` walker: os.walk(onerror=...) + `.md` filter +
    scan_errors bookkeeping, parameterized by a per-file callback.

    Review: code-reviewer (Finding 3) — factored out of
    `_collect_all_handoffs_for_gate_index` and `_collect_all_handoff_paths`,
    which were near-duplicate `os.walk(archive_dir, onerror=...)` implementations
    differing only in what they do with a successfully-read entry and their
    warning message wording. Both now delegate here; `on_scan_error` still lets
    each caller log its own subsystem-specific scan-gap rationale.

    NOTE: uses os.walk(onerror=...), NOT rglob("*.md") — Path.glob()'s selector
    silently swallows PermissionError while walking (verified: unreadable dir ->
    glob() yields an empty iterator, no exception), which made the previous
    `except OSError` here dead code for the exact permission-denied case it was
    meant to guard (mirrors roadmap_dag.py's `_collect_stub_paths` fix).
    """
    results: List[_WalkT] = []
    scan_errors: List[str] = []
    if not archive_dir.is_dir():
        return results, scan_errors
    walk_errors: List[OSError] = []
    for dirpath, _dirnames, filenames in os.walk(archive_dir, onerror=walk_errors.append):
        for fn in filenames:
            if not fn.endswith(".md"):
                continue
            path = Path(dirpath) / fn
            if not path.is_file():
                continue
            item = on_file(path)
            if item is not None:
                results.append(item)
    for exc in walk_errors:
        on_scan_error(exc)
        scan_errors.append(f"{getattr(exc, 'filename', archive_dir)}: {exc}")
    return results, scan_errors


def _collect_all_handoffs_for_gate_index(
    worktree_root: Path,
) -> "tuple[List[Dict[str, Any]], List[str]]":
    """Return (all_handoffs, scan_errors) — parsed frontmatter dicts, each
    carrying its own `_path` (mirroring `_collect_open_handoffs`'s convention),
    for the live+archived union. Consumed by gate_eval's `blocked_by` stub-id
    resolution (durable-id survives the archive move) and by the
    session-ownership index built over the same corpus
    (`ownership_index.build_ownership_index`, which resolves claim-store
    basenames against THIS function's output rather than re-walking the
    corpus itself — this is the one shared walker for the live+archived
    handoff corpus; nothing else should re-implement it).

    Walks `state/handoffs/` (live) + `archive/handoffs/` + `archive/completed/`
    (both archive roots via the shared `_walk_archive_md_files` os.walk
    helper — never `rglob`, which silently swallows PermissionError; see that
    function's own docstring). `scan_errors` is non-empty whenever EITHER
    archive subtree could not be fully scanned — an unreadable subtree here
    means gate_eval's `blocked_by` stub-id lookup (or the ownership index's
    basename resolution) may be missing an archived record, which must not be
    indistinguishable from "that record genuinely does not exist".

    Review: code-reviewer — Finding 6 (nit): this function is now called by
    THREE independent consumers per invocation cycle (gate_eval's blocked_by
    resolution, ownership_index.build_ownership_index, and
    ac27_differential_oracle.py), each re-walking + re-copying the full
    live+archived corpus rather than sharing one materialized result within
    a single ceremony run. Not flagged as a performance P-anything (no
    evidence this is hot-path/latency-sensitive here) — worth a look for
    whoever eventually profiles `/workstream-complete`.
    """
    all_handoffs: List[Dict[str, Any]] = []
    for path in collect_live_handoff_paths(worktree_root):
        meta = _read_meta(str(path))
        if meta:
            meta = dict(meta)
            meta["_path"] = str(path)
            all_handoffs.append(meta)
    scan_errors: List[str] = []

    for archive_subdir in ("handoffs", "completed"):
        archive_dir = worktree_root / "archive" / archive_subdir

        def _on_scan_error(exc: OSError, archive_dir: Path = archive_dir) -> None:
            _LOG.warning(
                "handoff.reconcile_open: cannot scan archived handoff subtree %s for "
                "the C3 gate index — %s; an awaiting_gate handoff whose blocked_by "
                "names an archived blocker under this subtree may fail to resolve "
                "(indistinguishable from 'that blocker id does not exist' without "
                "this signal)",
                getattr(exc, "filename", archive_dir), exc,
            )

        def _on_file(p: Path) -> "Optional[Dict[str, Any]]":
            meta = _read_meta(str(p))
            if not meta:
                return None
            meta = dict(meta)
            meta["_path"] = str(p)
            return meta

        archived_handoffs, archive_scan_errors = _walk_archive_md_files(
            archive_dir, _on_file, _on_scan_error,
        )
        all_handoffs.extend(archived_handoffs)
        scan_errors.extend(archive_scan_errors)

    return all_handoffs, scan_errors


def _rel_path(worktree_root: Path, abs_path: str) -> str:
    """Best-effort worktree-relative path string for a handler param (falls back to abs)."""
    try:
        return rel_id(Path(abs_path).resolve(), worktree_root.resolve())
    except ValueError:
        return abs_path


def _norm_path(path: str) -> str:
    """Resolve a path string for stable cross-source identity comparison (walk_forward's
    os.path.abspath keys vs collect_live_handoff_paths'/_read_meta's Path.resolve() keys)."""
    try:
        return str(Path(path).resolve())
    except OSError:
        return path


def _collect_all_handoff_paths(worktree_root: Path) -> "tuple[List[str], List[str]]":
    """Return (paths, scan_errors) — absolute path strings for all handoffs (live +
    archived), the dag_index reverse_membership needs for C6's per-ancestor
    liveness gate (R3). Mirrors fleet/archive_handoffs.py's own
    `_collect_all_handoff_paths` builder (not imported directly — that name is
    that module's private helper).

    `scan_errors` non-empty means dag_index may be missing an archived path, so
    the C6 per-ancestor liveness gate (`_ancestor_liveness_blocked`'s
    `reverse_membership` call) could silently miss a live child living under
    that unreadable subtree — a fail-OPEN risk (undercounting live children),
    the opposite direction of the gate's own fail-closed intent, so this must
    be surfaced rather than dropped.
    """
    paths: List[str] = [str(p) for p in collect_live_handoff_paths(worktree_root)]
    archive_dir = worktree_root / "archive" / "handoffs"

    def _on_scan_error(exc: OSError) -> None:
        _LOG.warning(
            "handoff.reconcile_open: cannot scan archived handoff subtree %s for "
            "the C6 dag_index — %s; the per-ancestor liveness gate may undercount "
            "live children under this subtree (fail-open risk)",
            getattr(exc, "filename", archive_dir), exc,
        )

    archived_paths, scan_errors = _walk_archive_md_files(
        archive_dir, lambda p: str(p.resolve()), _on_scan_error,
    )
    paths.extend(archived_paths)
    return paths, scan_errors


def _chain_ancestor_norm_paths(handoff: Dict[str, Any]) -> Set[str]:
    """Return normalized ancestor path strings (excluding `handoff` itself)
    reachable upward from `handoff` via `_CHAIN_EDGE_KINDS` (predecessor /
    origin_handoff), gated to the open set via `_is_open` (same walk shape as
    `_reconcile_ancestor_chain`).

    Purpose: exclude a handoff's OWN pinned-lineage ancestors from C2's
    cross-handoff attribution guard (`evaluate_commit_reality`'s
    `other_open_handoffs` set). A pinned predecessor's scope legitimately
    overlapping a successor's continuation-shipping commit's touched paths is
    NOT ambiguous cross-handoff attribution (the real AC4 shape: two genuinely
    UNRELATED handoffs happening to share scope) — it is the exact C6
    chain-walk continuation shape the successor's own shipping commit is
    supposed to establish. Without this exclusion, ANY gate-(a) chain-walk
    scenario (successor's commit touches its own pinned ancestor's scope, by
    construction) would ALSO trip the attribution guard on the successor's OWN
    per-node C2 evaluation — demoting it to `surface` and preventing
    `_handle_auto_ship`/`_reconcile_ancestor_chain` from ever firing at all
    (verified empirically: the AC8 positive/partial-scope fixtures both failed
    with 'ambiguous attribution' before this exclusion was added).

    Returns an empty set (never raises) when `handoff` carries no `_path` or
    the walk itself fails — falls back to the pre-existing (stricter)
    behavior, never silently widens exclusion beyond what is provably a
    pinned-lineage ancestor.

    TRUST-BOUNDARY NOTE (code-review Finding 3, P2 — accepted, reviewed
    assumption, not a corroborated invariant): this walk trusts the
    `predecessor`/`origin_handoff` frontmatter fields as author-accurate, with
    NO corroborating check (e.g. that the excluded ancestor's own successor
    field, if any, points back to `handoff`). This is the SAME trust level
    every other lineage-consuming op in this codebase already applies to
    these fields — not a new or weaker standard. The accepted risk: a
    mis-declared or stale lineage edge on a handoff that ALSO has a genuinely
    unrelated, currently-open co-claimant sharing scope with the shipping
    commit would have that ambiguity silently excluded from C2's
    cross-handoff attribution guard for this pairing. No fixture exercises a
    mis-declared lineage edge (all chain fixtures use accurate `predecessor:`
    pointers) — this trust boundary is intentionally unverified against its
    own adversarial case, named here for the EM/C5 confirmation-memo record.
    """
    start_path = handoff.get("_path")
    if not start_path:
        return set()
    try:
        walk = walk_forward(start_path, edge_kinds=set(_CHAIN_EDGE_KINDS), node_gate=_is_open)
    except Exception:
        return set()
    abs_start = _norm_path(start_path)
    return {
        _norm_path(p) for p in (walk.get("orderedPaths") or []) if _norm_path(p) != abs_start
    }


def _scope_subsumed(ancestor_scope: Sequence[str], touched: Set[str]) -> bool:
    """True iff EVERY ancestor scope pathspec overlaps at least one touched path —
    full subsumption, not mere any-overlap (C6 gate (a) evidence bar). An ancestor
    with no scope at all has nothing provably covered, so this returns False (fail
    closed to "left untouched") rather than vacuously True."""
    if not ancestor_scope:
        return False
    return all(_pathspec_overlaps([spec], touched) for spec in ancestor_scope)


def _ancestor_evidence(
    ancestor_meta: Dict[str, Any],
    successor_touched: Set[str],
    worktree_root: Path,
    policy: Dict[str, Any],
    open_handoffs: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """C6 per-ancestor EVIDENCE GATE (the Staff Engineer F2, MAJOR) — never infer shipped-ness
    from chain topology alone.

    Returns a dict `{"gate": "a"}` or `{"gate": "b", "candidate_sha": <sha>}` when
    the ancestor is verified-shippable, or None when NEITHER gate clears (ancestor
    is left untouched / surfaced by the caller).

    Gate (a): the successor's shipping commit's touched paths SUBSUME the
    ancestor's own scope (every ancestor scope pathspec overlaps a touched path).
    Gate (b): the ancestor independently clears C2 auto-ship on its own evidence
    (its own candidate_sha, verified against its own scope — NOT the successor's).
    """
    ancestor_scope = list(ancestor_meta.get("scope") or [])
    if _scope_subsumed(ancestor_scope, successor_touched):
        return {"gate": "a"}

    ancestor_path_norm = _norm_path(str(ancestor_meta.get("_path") or ""))
    other_open = [
        h for h in open_handoffs
        if _norm_path(str(h.get("_path") or "")) != ancestor_path_norm
    ]
    ancestor_verdict = evaluate_commit_reality(
        ancestor_meta, worktree_root, policy, other_open
    )
    if ancestor_verdict.get("verdict") == "auto-ship":
        return {"gate": "b", "candidate_sha": ancestor_verdict.get("candidate_sha")}

    return None


async def _ancestor_liveness_blocked(
    ancestor_path: Path,
    ancestor_meta: Dict[str, Any],
    dag_index: List[str],
    common_dir: Path,
    worktree_root: Optional[Path] = None,
    open_handoffs: Optional[List[Dict[str, Any]]] = None,
    exclude_norm_path: Optional[str] = None,
) -> Optional[str]:
    """C6 PER-ANCESTOR LIVENESS GATE (R3, load-bearing).

    `handoff.ship_and_archive` routes to `fleet/archive_shipped_handoffs.py`'s
    `_is_shipped_terminal`, confirmed carrying NO reverse_membership (Check 3) gate
    at all — that check lives only in `fleet/archive_handoffs.py`'s `_is_terminal`
    and is NOT in the call path this chain-walk uses to archive an ancestor. So the
    scope-subsumption gate above is NOT sufficient on its own: this reuses the SAME
    `reverse_membership`, `cs_claim_holder_live`, and `handoff_claim_dir` helpers
    archive_handoffs.py's Checks 3/4 use (mirrors archive_handoffs.py:307-358's
    exact usage pattern), applied explicitly here since ship_and_archive's own call
    path supplies no reverse_membership gate.

    (2026-08-13 update: `_is_shipped_terminal` GAINED its own live-claim-dir Check 3
    — see that function's docstring. That does NOT make this gate's live-claim half
    redundant, and the reverse reading is the dangerous one: `handoff.ship_and_archive`
    passes `holder_initiated=True`, which skips that new Check 3 unconditionally, so
    the ancestor-archival path this chain-walk uses still reaches archival with NO
    live-claim gate of its own. This gate remains the only live-claim check on that
    path — and it is the one that matters most here, because the session driving a
    chain-walk holds the SUCCESSOR's claim, not necessarily the ancestor's: the claim
    `holder_initiated` waves through may well be a peer's. Load-bearing for all three
    of live-claim, reverse_membership, and the open-child scan below.)

    Review: code-reviewer (F2, P2) — `reverse_membership`'s `_TERMINAL_STATUSES`
    exclusion (archival.py) has no reference to `deployment_state`, so a
    `status==consumed AND deployment_state==in_flight` child (still OPEN under
    THIS module's own C1 `_is_open` widening) is unconditionally excluded from
    archival.py's live-children set. Rather than widen the shared archival.py
    helper (fleet-wide blast radius on the existing sweep, routed separately per
    EM disposition), this gate independently re-derives liveness from this
    module's own open set: when `open_handoffs`/`worktree_root` are supplied, any
    handoff in the open set (already gated by this module's `_is_open`, which
    DOES admit consumed+in_flight) that names `ancestor_path` as its own
    `predecessor`/`origin_handoff` makes the ancestor liveness-blocked, closing
    the grandparent dead-zone gap `reverse_membership` alone would miss.

    `exclude_norm_path`, when supplied, is the chain-walk's OWN originating
    successor (the `handoff` argument to `_reconcile_ancestor_chain` — the node
    whose auto-ship triggered this walk), excluded from the scan. That
    successor's own predecessor edge pointing at an ancestor is the exact
    continuation-baton shape this chain-walk exists to reconcile, NOT evidence
    of a separate live child — without this exclusion every gate-(a) ancestor
    would trivially self-block on its own triggering successor (verified
    empirically: the AC8 positive fixture regressed to "liveness gate blocked"
    on its own successor before this exclusion was added).

    Returns a non-empty reason string when the ancestor is live (archival BLOCKED),
    or None when both checks clear.
    """
    if not dag_index:
        return "dag_index empty — cannot determine children (fail-closed)"
    try:
        children = await asyncio.to_thread(
            reverse_membership, str(ancestor_path), dag_index
        )
    except ValueError as exc:
        return f"reverse_membership error: {exc}"
    if children:
        return f"has live children: {len(children)}"

    claim_dir = handoff_claim_dir(common_dir, ancestor_path)
    if claim_dir.is_dir():
        try:
            holder_live = await asyncio.to_thread(cs_claim_holder_live, str(claim_dir))
        except Exception as exc:
            # Fail-closed-to-keep (mirrors archive_handoffs.py's own degrade-on-
            # exception discipline) — an unreadable claim dir must degrade to the
            # consumed_by fallback below, never assume-terminal/archivable.
            _LOG.warning(
                "handoff.reconcile_open: cs_claim_holder_live raised for %s — "
                "degrading to consumed_by fallback (fail-closed-to-keep): %s",
                claim_dir, exc,
            )
            holder_live = None
        if holder_live:
            return "live claim (claim-dir holder live)"

    consumed_by_sid = _get_handoff_consumed_by(str(ancestor_path))
    if consumed_by_sid:
        live_sids: frozenset = await asyncio.to_thread(resolve_live_session_ids)
        if consumed_by_sid in live_sids:
            return f"consumed_by session {consumed_by_sid!r} is live"

    # Review: code-reviewer (F2, P2) — independent open-child liveness scan.
    # archival.py's reverse_membership above cannot see a consumed+in_flight
    # child as live (see docstring); this closes that gap using THIS module's
    # own open set instead of touching the shared archival.py helper.
    if open_handoffs and worktree_root is not None:
        ancestor_abs = _norm_path(str(ancestor_path))
        for candidate in open_handoffs:
            candidate_path = candidate.get("_path")
            if not candidate_path:
                continue
            candidate_norm = _norm_path(str(candidate_path))
            if candidate_norm == ancestor_abs:
                continue
            if exclude_norm_path and candidate_norm == exclude_norm_path:
                continue
            candidate_dir = str(Path(candidate_path).resolve().parent)
            for raw_ref in handoff_edges(candidate, _CHAIN_EDGE_KINDS):
                try:
                    target_abs = resolve_target(raw_ref, candidate_dir, str(worktree_root))
                except Exception:
                    # Review: code-reviewer (Finding 2) -- route through the
                    # module logger instead of a raw print, matching the
                    # discipline used by cs_claim_holder_live's except-branch
                    # immediately above.
                    _LOG.warning(
                        "handoff.reconcile_open: _ancestor_liveness_blocked: "
                        "resolve_target failed for ref %r in candidate %s — "
                        "skipping this edge: %s",
                        raw_ref, candidate_path, sys.exc_info()[1],
                    )
                    continue
                if not target_abs or target_abs == "git-history":
                    continue
                if _norm_path(target_abs) == ancestor_abs:
                    return (
                        "live child by declared lineage: "
                        f"{candidate.get('id') or candidate_path!r} names this "
                        "ancestor as predecessor/origin_handoff"
                    )

    return None


async def _reconcile_ancestor_chain(
    handoff: Dict[str, Any],
    successor_sha: Optional[str],
    worktree_root: Path,
    repo_root: Path,
    policy: Dict[str, Any],
    open_handoffs: List[Dict[str, Any]],
    dag_index: List[str],
    dry_run: bool,
    auto_ship_enabled: bool,
    reconciled: List[Dict[str, Any]],
) -> None:
    """C6 chain-walk reaper — reconcile the pinned predecessor chain when a node
    reaches verdict auto-ship (closes the abandoned-ready_to_fire-successor gap,
    handoff/SKILL.md:324).

    Walks `predecessor`/`origin_handoff` upward from `handoff`'s own path
    (`_CHAIN_EDGE_KINDS`), gated to the open set via `_is_open` as `walk_forward`'s
    `node_gate` — a closed/archived ancestor halts that branch of the walk rather
    than being visited (satisfies the "reuse _is_open" liveness-gate-(i)
    requirement structurally, since walk_forward never adds a gate-rejected node to
    its result). `walk_forward` also supplies bounded-walk cycle guarding
    (gray/black DFS sets) — no separate cycle guard is rolled here.

    For EACH ancestor reached: verify (never infer from topology) via
    `_ancestor_evidence` (gate a/b) AND `_ancestor_liveness_blocked` (gate ii)
    before archiving. Either gate failing leaves the ancestor untouched (an
    entry is still recorded in `reconciled` for visibility, `applied: False`).
    The SHA stamped into an archived ancestor's `shipped_in` is the SUCCESSOR's
    sha under gate (a), or the ancestor's OWN candidate_sha under gate (b) — never
    conflated (see `_ancestor_evidence` docstring / plan body laundering-risk note).
    DR-096 (DoE-claude 2026-07-26 ruling) makes that distinction visible on disk,
    not just in this code path's control flow: gate (a) stamps
    `shipped_in_kind: successor`, gate (b) stamps `shipped_in_kind: ship-commit`.

    STALE-START-PATH GAP (F5, load-bearing — verified empirically this session):
    `_handle_auto_ship` invokes this chain-walk AFTER `handoff.ship_and_archive`
    has already git-mv'd `handoff`'s own file out of state/handoffs/ into
    archive/handoffs/ — so `handoff["_path"]` (captured before the move) no
    longer exists on disk by the time this function runs. `walk_forward` would
    re-read that stale path as its very first DFS step (`_read_meta(abs_path)`),
    get `{}` (file gone), and have `node_gate=_is_open` reject the START node
    itself (status/deployment_state both empty) — which means walk_forward
    NEVER reaches the edge-collection step for `handoff`'s own predecessor/
    origin_handoff fields, so `orderedPaths` comes back EMPTY and every
    ancestor is silently missed, even a directly-pinned one. This does NOT
    self-heal via walk_forward's own re-read cache, because the cache is keyed
    on successful reads only. The fix: resolve `handoff`'s OWN immediate
    predecessor/origin_handoff edges from the IN-MEMORY `handoff` dict (captured
    pre-move, so still accurate) via `handoff_edges`/`resolve_target`, then run
    `walk_forward` from EACH resolved immediate-ancestor path (which are still
    live on disk — only `handoff` itself moved) to continue the walk upward for
    deeper (grandparent+) chains, merging results and de-duplicating by
    normalized path.
    """
    start_path = handoff.get("_path")
    if not start_path:
        return

    handoff_dir = str(Path(start_path).resolve().parent)
    raw_edges = handoff_edges(handoff, _CHAIN_EDGE_KINDS)
    immediate_targets: List[str] = []
    for raw_ref in raw_edges:
        try:
            target_abs = resolve_target(raw_ref, handoff_dir, str(worktree_root))
        except Exception as exc:
            reconciled.append({
                "handoff_id": handoff.get("id"),
                "action": "chain_walk",
                "applied": False,
                "error": f"resolve_target raised for {raw_ref!r}: {exc}",
            })
            continue
        if target_abs and target_abs != "git-history":
            immediate_targets.append(target_abs)

    if not immediate_targets:
        return

    abs_start = _norm_path(start_path)
    seen_norm: Set[str] = set()
    ancestor_paths: List[str] = []
    walked_nodes: Dict[str, Any] = {}

    for target in immediate_targets:
        try:
            walk = walk_forward(target, edge_kinds=set(_CHAIN_EDGE_KINDS), node_gate=_is_open)
        except Exception as exc:
            reconciled.append({
                "handoff_id": handoff.get("id"),
                "action": "chain_walk",
                "applied": False,
                "error": f"walk_forward raised for {target!r}: {exc}",
            })
            continue
        walked_nodes.update(walk.get("nodes") or {})
        for p in walk.get("orderedPaths") or []:
            norm = _norm_path(p)
            if norm == abs_start or norm in seen_norm:
                continue
            seen_norm.add(norm)
            ancestor_paths.append(p)

    if not ancestor_paths:
        return

    successor_touched: Set[str] = (
        _touched_paths(worktree_root, successor_sha) if successor_sha else set()
    )

    for ancestor_abs in ancestor_paths:
        ancestor_path = Path(ancestor_abs)
        entry: Dict[str, Any] = {
            "handoff_id": None,
            "handoff_path": _rel_path(worktree_root, ancestor_abs),
            "action": "ship_and_archive_chain_ancestor",
            "successor_id": handoff.get("id"),
            "dry_run": dry_run,
        }
        try:
            raw_meta = walked_nodes.get(ancestor_abs) or _read_meta(ancestor_abs)
            if not raw_meta:
                entry["applied"] = False
                entry["message"] = "ancestor frontmatter unreadable"
                reconciled.append(entry)
                continue

            ancestor_meta = dict(raw_meta)
            ancestor_meta.setdefault("id", ancestor_meta.get("id") or ancestor_path.stem)
            ancestor_meta["_path"] = ancestor_abs
            entry["handoff_id"] = ancestor_meta.get("id")

            # Idempotent (AC9): reuse _is_open (C1's own predicate) — an ancestor
            # already archived (by independent per-node C2 earlier in this same
            # pass, or otherwise already closed) is a no-op, not a double-ship.
            # walk_forward's node_gate already excludes non-open nodes from the
            # walk result, so this is a defensive re-check, not the sole gate.
            if not _is_open(ancestor_meta):
                entry["applied"] = False
                entry["message"] = "ancestor already closed (idempotent no-op)"
                reconciled.append(entry)
                continue

            evidence = _ancestor_evidence(
                ancestor_meta, successor_touched, worktree_root, policy, open_handoffs,
            )
            if evidence is None:
                entry["applied"] = False
                entry["message"] = (
                    "not scope-subsumed by successor and does not independently "
                    "clear C2 — left untouched (surface)"
                )
                reconciled.append(entry)
                continue

            block_reason = await _ancestor_liveness_blocked(
                ancestor_path, ancestor_meta, dag_index, repo_root,
                worktree_root=worktree_root, open_handoffs=open_handoffs,
                exclude_norm_path=abs_start,
            )
            if block_reason:
                entry["applied"] = False
                entry["message"] = f"liveness gate blocked: {block_reason}"
                reconciled.append(entry)
                continue

            gate = evidence["gate"]
            # DR-096 (DoE-claude 2026-07-26 ruling): gate (a) stamps the SUCCESSOR's
            # sha onto this ancestor -> kind="successor"; gate (b) stamps the
            # ancestor's OWN candidate_sha -> kind="ship-commit". Never conflated
            # (see this function's own docstring, "laundering-risk note") — the two
            # gates now write a discriminated kind, not just an untagged sha, so a
            # reader of the stamped frontmatter can tell which without re-deriving it.
            stamp_sha = successor_sha if gate == "a" else evidence.get("candidate_sha")
            stamp_kind = "successor" if gate == "a" else "ship-commit"
            entry["gate"] = gate
            entry["candidate_sha"] = stamp_sha

            # Review: code-reviewer (F4, nit) — the ship_and_archive handler only
            # stamps shipped_in when the field is currently ABSENT (idempotent-if-
            # absent, by design). If the ancestor already carries a pre-existing
            # shipped_in that differs from this chunk's correctly-gated stamp_sha,
            # that stamp handler no-op would silently swallow the mismatch. Surface
            # it here so it's visible in the reconciled[] entry rather than lost.
            pre_existing_shipped_in = ancestor_meta.get("shipped_in")
            if pre_existing_shipped_in and pre_existing_shipped_in != stamp_sha:
                entry["stale_shipped_in_mismatch"] = {
                    "pre_existing": pre_existing_shipped_in,
                    "computed": stamp_sha,
                }

            if not auto_ship_enabled:
                entry["applied"] = False
                entry["message"] = "auto_ship_enabled=false (fail-closed policy)"
                reconciled.append(entry)
                continue
            if dry_run:
                entry["applied"] = False
                reconciled.append(entry)
                continue

            result = await _ship_and_archive_handler(
                {
                    "handoff_path": entry["handoff_path"],
                    "sha": stamp_sha or "",
                    "kind": stamp_kind,
                },
                repo_root,
            )
            entry["applied"] = bool(result.get("shipped")) or bool(result.get("archived"))
            entry["exit_code"] = result.get("exit_code")
            entry["message"] = result.get("message") or result.get("error")
            reconciled.append(entry)
        except Exception as exc:
            # F4 error isolation — a mid-chain raise (e.g. a concurrent-session git
            # conflict in this shared worktree) surfaces as this ancestor's error
            # entry; the walk CONTINUES to remaining ancestors / the enumeration
            # continues to remaining top-level nodes (op contract: exit_code 0
            # always, per-handoff failure captured in its own entry).
            entry["applied"] = False
            entry["error"] = f"chain-walk ancestor raised: {exc}"
            reconciled.append(entry)
            continue


async def _handle_auto_ship(
    handoff: Dict[str, Any],
    commit_verdict: Dict[str, Any],
    worktree_root: Path,
    repo_root: Path,
    dry_run: bool,
    auto_ship_enabled: bool,
    reconciled: List[Dict[str, Any]],
    policy: Dict[str, Any],
    open_handoffs: List[Dict[str, Any]],
    dag_index: List[str],
) -> None:
    """Route a C2 verdict=='auto-ship' handoff to handoff.ship_and_archive, then
    (C6) reconcile its pinned predecessor chain — see `_reconcile_ancestor_chain`.

    Fail-closed gate (AC10, EM-verified P0 upgrade of Slice-A Finding 2): when
    the loaded policy's `auto_ship_enabled` is False (absent-policy or
    malformed-policy conservative default), NO ship_and_archive invocation
    happens regardless of `dry_run` — treated as a dry_run-style short-circuit
    with an explicit reason recorded, so an absent/malformed
    `auto-reconcile-policy.yaml` can never silently auto-ship.
    """
    handoff_id = handoff.get("id")
    rel_path = _rel_path(worktree_root, handoff["_path"])
    candidate_sha = commit_verdict.get("candidate_sha")

    entry: Dict[str, Any] = {
        "handoff_id": handoff_id,
        "handoff_path": rel_path,
        "action": "ship_and_archive",
        "candidate_sha": candidate_sha,
        "dry_run": dry_run,
    }
    if not auto_ship_enabled:
        entry["applied"] = False
        entry["message"] = "auto_ship_enabled=false (fail-closed policy)"
        reconciled.append(entry)
    elif dry_run:
        entry["applied"] = False
        reconciled.append(entry)
    else:
        try:
            result = await _ship_and_archive_handler(
                {"handoff_path": rel_path, "sha": candidate_sha or ""}, repo_root
            )
        except Exception as exc:
            # F4 error isolation (the Staff Engineer review) — a mid-loop raise (e.g. a
            # concurrent-session git conflict in this shared worktree) surfaces
            # as this node's error entry; the enumeration CONTINUES to the
            # remaining open handoffs rather than aborting the whole reconcile
            # pass (op contract: exit_code 0 always, per-handoff failure
            # captured in that handoff's entry, :311-314). The pinned
            # predecessor chain is skipped for this node this pass — it will
            # be retried on the next invocation once the node's own ship
            # clears.
            entry["applied"] = False
            entry["error"] = f"ship_and_archive raised: {exc}"
            reconciled.append(entry)
            return
        # Slice-B review Finding 6 (nit) — confirmed against handoff_ship_archive.py:
        # ship_and_archive's result shape has no single top-level `applied`/success
        # field analogous to handoff.transition's; it exposes `shipped` (bool) and
        # `archived` (bool) as its two success signals (see _err()/_handler()'s
        # return shapes). `shipped OR archived` is therefore the correct — not
        # merely inconsistent-looking — derivation here; _handle_gate_cascade's
        # direct `result.get("applied")` read works only because gate-cascade-clear
        # genuinely returns that single field.
        entry["applied"] = bool(result.get("shipped")) or bool(result.get("archived"))
        entry["exit_code"] = result.get("exit_code")
        entry["message"] = result.get("message") or result.get("error")
        reconciled.append(entry)

    # C6: on a node reaching verdict auto-ship, reconcile its pinned predecessor
    # chain (ready_to_fire-successor class) — follow ONLY from here, never from
    # an unshipped node (DEC-5).
    await _reconcile_ancestor_chain(
        handoff, candidate_sha, worktree_root, repo_root, policy, open_handoffs,
        dag_index, dry_run, auto_ship_enabled, reconciled,
    )


async def _handle_gate_cascade(
    handoff: Dict[str, Any],
    gate_verdict: Dict[str, Any],
    worktree_root: Path,
    repo_root: Path,
    dry_run: bool,
    gates_cleared: List[Dict[str, Any]],
    surfaced: List[Dict[str, Any]],
) -> None:
    """Route a C3 verdict in {clear, narrow} to C8's gate-cascade-clear verb.

    A `narrow` verdict whose `also_surface` is True (remaining_blockers includes an
    abandoned id, C3's narrow+surface composite) is ALSO appended to surfaced[] in
    addition to driving the narrow-mutation — the dead-blocker case must not silently
    rot forever gated.
    """
    handoff_id = handoff.get("id")
    rel_path = _rel_path(worktree_root, handoff["_path"])
    verdict = gate_verdict.get("verdict")
    cleared_by_shas = gate_verdict.get("cleared_by_shas") or []
    # Slice-B review Finding 5 (nit): consume gate_eval's explicit
    # cleared_blocker_ids field directly rather than re-deriving it via
    # blocked_by - remaining_blockers set difference against two
    # separately-sourced lists.
    blocker_ids = list(gate_verdict.get("cleared_blocker_ids") or [])

    entry: Dict[str, Any] = {
        "handoff_id": handoff_id,
        "handoff_path": rel_path,
        "action": "gate-cascade-clear",
        "verdict": verdict,
        "blocker_ids": blocker_ids,
        "dry_run": dry_run,
    }

    if dry_run or not blocker_ids:
        entry["applied"] = False
        gates_cleared.append(entry)
    else:
        result = await _handoff_transition_handler(
            {
                "verb": "gate-cascade-clear",
                "handoff_path": rel_path,
                "blocker_ids": blocker_ids,
                "blocker_shas": cleared_by_shas,
            },
            repo_root,
        )
        entry["applied"] = bool(result.get("applied"))
        entry["exit_code"] = result.get("exit_code")
        entry["message"] = result.get("message") or result.get("error")
        gates_cleared.append(entry)

    if verdict == "narrow" and gate_verdict.get("also_surface"):
        surfaced.append({
            "handoff_id": handoff_id,
            "reason": "narrow+surface composite: remaining_blockers includes an abandoned id",
            "evidence": gate_verdict.get("evidence") or [],
        })


async def _handle_in_flight_blocked_by_retirement(
    handoff: Dict[str, Any],
    worktree_root: Path,
    repo_root: Path,
    dry_run: bool,
    gates_cleared: List[Dict[str, Any]],
    surfaced: List[Dict[str, Any]],
    clears_cache: Optional[Dict[str, bool]] = None,
) -> bool:
    """C-in-flight — retire structured `blocked_by` residue on a pickup-claimed
    (`deployment_state: in_flight`) handoff that `_AWAITING_GATE_STATE`'s own
    gate-cascade branch never reaches (docs/research/2026-08-14-jgate-
    clearance-recording-seam.md: `_is_open` enumerates a claimed+in_flight
    handoff into `open_handoffs`, but the per-handoff gate-cascade branch is
    keyed on `deployment_state == awaiting_gate` only).

    Owned here, not by `pickup_assemble` (EM ruling, cross-repo/inbox/
    2026-08-04-example-market-data-repo-em-pickup-jgate-cleared-strands-gate-
    fields.md) — pickup must not become a second author of
    `blocked_by`/`no_longer_blocked_by` state.

    Deliberately separate from `handoff_transition._gate_cascade_clear`, not a
    widening of it: that verb MUTATE-ABORTs outside `awaiting_gate` by
    contract, an in_flight retirement must never flip `deployment_state` or
    touch `gate_dependency`/`gate_evidence` prose, and this pass's write-scope
    is this file alone. Reuses (never modifies) `handoff_transition.py`'s
    act-time evidence predicate `_blocker_clears_gate` and the raw-YAML
    array-field helpers (`_replace_fm_array_field`/`_insert_fm_array_field`,
    `_validate_fm`).

    Evidence rule (never age/prose/absence-of-evidence): identical to
    `_gate_cascade_clear`'s own — `_blocker_clears_gate` returns `clears=True`
    only for a `shipped` blocker, or a `continued` blocker whose
    `continued_into` chain resolves to `shipped`; `closed`/`abandoned`/
    unresolvable/ambiguous never clear. An id that doesn't clear is left
    untouched in `blocked_by` — no partial guessing, no age-based retirement.

    MOVE semantics (handoff.schema.json `no_longer_blocked_by`): a retiring
    `blocked_by` entry is relocated into `no_longer_blocked_by`, never merely
    dropped — the union of the two arrays is invariant across a resolution.
    This is the FIRST writer of `no_longer_blocked_by` anywhere in
    `coordinator_core`. NOTE: `handoff_transition._gate_cascade_clear` still
    DROPS a retired id from both arrays on its own awaiting_gate path instead
    of moving it — a known divergence, not fixed here (out of this pass's
    write-scope); see state/bug-backlog/2026-08-14-gate-cascade-clear-drops-
    blocked-by-entries-instead-of-moving.md.

    `blocking_notes` is untouched — advisory prose the resolver never reads.

    gate_evidence surface-only invariant: mirrors the `_AWAITING_GATE_STATE`
    branch's own guard — "evidence never auto-clears a gate" (module
    docstring § D5) — a handoff carrying ANY `gate_evidence` block is always
    forced onto `surfaced[]`, never auto-applied, dry_run or not. Checked by
    PRESENCE only, before any candidate enumeration: `_read_gate_evidence_
    resolved` short-circuits to `None` outside `awaiting_gate`, so it cannot
    be reused here — a lightweight presence check off the already-parsed
    frontmatter dict is the correct-shaped mirror.

    Claim-lock / act-time re-verification: `mutate` re-resolves each
    candidate id's LIVE state fresh, under the file lock, immediately before
    moving any edge — an enumeration-time verdict (optionally cache-backed
    via `clears_cache`) is never write-authoritative; `mutate` never reads or
    writes `clears_cache`.

    Returns True iff this handoff was intercepted for REPORTING purposes
    (something was retired, a retirement attempt was recorded — dry-run
    preview or a failed write — or the handoff was surfaced under the
    gate_evidence guard above); False when there was nothing to retire and no
    evidence to surface. The caller does NOT `continue` on a truthy return —
    the C9 ledger/mirror desync check below is independent of this branch's
    own admission logic and must still run every pass.
    """
    handoff_id = handoff.get("id")

    gate_evidence = handoff.get("gate_evidence")
    if isinstance(gate_evidence, dict) and gate_evidence:
        legs = gate_evidence.get("legs")
        surfaced.append({
            "handoff_id": handoff_id,
            "reason": (
                "in_flight handoff carries a gate_evidence block — evidence "
                "never auto-clears a gate (mirrors the awaiting_gate "
                "surface-only invariant, module docstring § D5); blocked_by "
                "retirement is NOT attempted while evidence is in play, "
                "regardless of dry_run"
            ),
            "evidence": legs if isinstance(legs, list) else [],
        })
        return True

    blocked_by = handoff.get("blocked_by") or []
    if not isinstance(blocked_by, list) or not blocked_by:
        return False

    # Enumeration-time candidate set — mirrors _handle_gate_cascade's own use
    # of a pre-lock snapshot (gate_verdict, itself derived from evaluate_gate's
    # read of the in-memory `handoff` dict) to decide WHETHER to attempt a
    # write at all; the write itself re-verifies fresh under lock below.
    #
    # Review: staff-eng Finding 7 (perf) — `clears_cache`, when supplied by
    # the caller, memoises this ENUMERATION-pass call only, keyed on blocker
    # id, for the lifetime of one `_handler` invocation. The act-time
    # re-verify inside `mutate` below deliberately never reads or writes this
    # cache — that call is the write-authoritative re-check and must always
    # see live state, cache or no cache.
    candidate_ids: List[str] = []
    for blocker_id in blocked_by:
        bid = str(blocker_id)
        if clears_cache is not None and bid in clears_cache:
            clears = clears_cache[bid]
        else:
            clears, _detail = _blocker_clears_gate(bid, worktree_root)
            if clears_cache is not None:
                clears_cache[bid] = clears
        if clears:
            candidate_ids.append(bid)

    if not candidate_ids:
        return False

    rel_path = _rel_path(worktree_root, handoff["_path"])
    entry: Dict[str, Any] = {
        "handoff_id": handoff_id,
        "handoff_path": rel_path,
        "action": "blocked-by-retire-in-flight",
        "blocker_ids": candidate_ids,
        "dry_run": dry_run,
    }

    if dry_run:
        entry["applied"] = False
        gates_cleared.append(entry)
        return True

    handoff_path = Path(handoff["_path"])
    _state: Dict[str, Any] = {"applied": False, "message": "", "retired": []}

    def mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(
                "blocked-by-retire-in-flight: no parseable YAML frontmatter in "
                f"{handoff_path}"
            )

        fm_dict = yaml.safe_load(split.fm_text) or {}
        current_blocked_by = fm_dict.get("blocked_by") or []
        if not isinstance(current_blocked_by, list):
            raise MutateAbort(
                f"blocked-by-retire-in-flight: blocked_by is not a list in {handoff_path}"
            )
        current_no_longer = fm_dict.get("no_longer_blocked_by") or []
        if not isinstance(current_no_longer, list):
            current_no_longer = []

        # Act-time re-verification (mirrors _gate_cascade_clear's own the Staff Engineer
        # F0 discipline): re-resolve EACH candidate id's LIVE state fresh,
        # immediately before moving any edge — a caller-supplied enumeration-
        # time verdict is never write-authoritative.
        live_retiring = [
            bid for bid in candidate_ids
            if bid in current_blocked_by and _blocker_clears_gate(bid, worktree_root)[0]
        ]
        if not live_retiring:
            # Already retired concurrently, or the live re-check no longer
            # clears — byte-identical no-op (locked_rmw skips the write).
            # Review: staff-eng Finding 6 — record a distinct message so this
            # outcome is not indistinguishable, downstream in the report, from
            # a dry-run preview (the `dry_run` key differs but nothing else
            # did before this fix); `_state["retired"]` stays `[]`, which the
            # caller below renders as `blocker_ids: []` rather than echoing
            # the (now stale) enumeration-time `candidate_ids`.
            _state["message"] = (
                f"blocked-by-retire-in-flight {handoff_path} — no-op: every "
                "enumeration-time candidate was already retired concurrently "
                "or no longer clears on live re-check"
            )
            return old_text

        new_blocked_by = [bid for bid in current_blocked_by if bid not in live_retiring]
        new_no_longer = list(current_no_longer)
        for bid in live_retiring:
            if bid not in new_no_longer:
                new_no_longer.append(bid)

        fm = split.fm_text
        fm = _replace_fm_array_field(fm, "blocked_by", new_blocked_by)
        if read_fm_field(fm, "no_longer_blocked_by") is not None:
            fm = _replace_fm_array_field(fm, "no_longer_blocked_by", new_no_longer)
        else:
            fm = _insert_fm_array_field(fm, "no_longer_blocked_by", new_no_longer, "blocked_by")

        # Post-mutation schema validation gate — raise MutateAbort to skip
        # the write, same discipline as every other mutating verb in
        # handoff_transition.py.
        errors = _validate_fm(fm)
        if errors:
            details = format_validation_errors(errors)
            raise MutateAbort(
                f"blocked-by-retire-in-flight: handoff frontmatter validation "
                f"failed: {details}"
            )

        _state["applied"] = True
        _state["retired"] = live_retiring
        _state["message"] = (
            f"blocked-by-retire-in-flight {handoff_path} — moved {live_retiring} "
            "blocked_by -> no_longer_blocked_by (deployment_state untouched)"
        )
        return rebuild(split, fm)

    try:
        await asyncio.to_thread(locked_rmw, handoff_path, mutate, repo_root=repo_root)
    except FileNotFoundError as exc:
        entry["applied"] = False
        entry["error"] = f"blocked-by-retire-in-flight: handoff not found: {exc}"
        gates_cleared.append(entry)
        return True
    except LockTimeout as exc:
        entry["applied"] = False
        entry["error"] = (
            f"blocked-by-retire-in-flight: timed out waiting for file lock on "
            f"{handoff_path}: {exc}"
        )
        gates_cleared.append(entry)
        return True
    except MutateAbort as exc:
        entry["applied"] = False
        entry["error"] = exc.args[0] if exc.args else "blocked-by-retire-in-flight: aborted"
        gates_cleared.append(entry)
        return True

    entry["applied"] = _state["applied"]
    # Review: staff-eng Finding 6 — `_state["retired"]` is now the sole
    # source: `[]` on the concurrent-no-op path (nothing left to do),
    # non-empty on a genuine apply. No longer falls back to the stale
    # enumeration-time `candidate_ids`, which made "nothing left to do" and
    # "applied" indistinguishable in the report except via the `dry_run` key.
    entry["blocker_ids"] = _state["retired"]
    entry["message"] = _state["message"]
    gates_cleared.append(entry)
    return True


def _read_mirror_desync_fields(handoff_path: Path) -> "Optional[Dict[str, Optional[str]]]":
    """C9 AC16 conjuncts 3+4 — live re-read of the mirror's `claimed_by` /
    `consumed_by` / `status` fields, independent of the (possibly stale)
    in-memory handoff dict this loop iterates over.

    Returns `None` on any read error (fail-closed-to-no-fire — AC16); returns
    a dict with `None` values for genuinely-absent fields on a clean read.
    Mirrors `claim_state._read_mirror_claim`'s dual-tolerant `claimed_by`-
    wins-over-`consumed_by` read shape and 4 KiB read cap, but exposes
    `status` too (which that helper does not, since C1's own accessor has no
    need of it) and is intentionally NOT reused from that module — C1's own
    import-discipline docstring restricts `coordinator_core.claim_state` to a
    narrow dependency set that this op's own import graph already satisfies
    independently, and this op needs one extra field that accessor does not
    expose.
    """
    try:
        with open(handoff_path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read(4096)
    except OSError:
        return None

    def _clean(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        return value if value and value.lower() not in ("null", "none") else None

    return {
        "claimed_by": _clean(read_fm_field_unquoted(content, "claimed_by")),
        "consumed_by": _clean(read_fm_field_unquoted(content, "consumed_by")),
        "status": _clean(read_fm_field_unquoted(content, "status")),
    }


async def _ledger_mirror_desync_check(
    handoff: Dict[str, Any],
    common_dir: Path,
) -> "tuple[str, str, Optional[str], Optional[str]]":
    """C9 admission gate — AC16: fires only when ALL of (1) the ledger claim
    dir exists, (2) `cs_claim_holder_live` returns True, (3) the mirror
    carries no `claimed_by` AND no `consumed_by`, (4) the mirror `status` is
    `open`. Reuses the ledger-probe pattern already established by this
    module's `_ancestor_liveness_blocked` (`handoff_claim_dir` +
    `asyncio.to_thread(cs_claim_holder_live, ...)`, fail-closed-to-keep
    degrade).

    Returns `(outcome, reason, ledger_holder, ledger_claimed_at)`:
      "not_admitted" — a conjunct cleanly failed (no read error); the caller
          falls through to the ordinary routing untouched — this is the
          benign, overwhelmingly common case (most open handoffs simply are
          not ledger-vs-mirror desynced).
      "read_error"   — a read error (or an unthreadable ledger `claimed_at` —
          see below) was hit while evaluating a conjunct; the caller
          surfaces (AC16 no-op-and-surface), never fires.
      "admitted"     — all four conjuncts hold and the ledger's `claimed_at`
          is readable; the caller may fire (or dry-run no-op) the delegated
          re-stamp.

    A ledger claim dir with a live holder but an unreadable/absent
    `claimed_at` file (the writer, `session/claims.py::_write_claim_meta`,
    always writes it immediately after `session_id` in the same claim, so
    this is a degenerate/corrupted-fixture case, not a reachable steady
    state) is folded into the "read_error" outcome rather than treated as a
    fifth admission conjunct: threading anything other than the ledger's own
    `claimed_at` as `at` would corrupt the exact field this leg exists to
    repair (see the C9 chunk brief's `at`-parameter hard constraint), so an
    unreadable `claimed_at` must degrade to no-op-and-surface exactly like
    any other conjunct read error, never fire with a substitute value.
    """
    handoff_path_raw = handoff.get("_path")
    if not handoff_path_raw:
        return "not_admitted", "no _path on handoff", None, None
    handoff_path = Path(handoff_path_raw)

    claim_dir = handoff_claim_dir(common_dir, handoff_path)
    try:
        dir_exists = claim_dir.is_dir()
    except OSError as exc:
        return "read_error", f"ledger claim dir stat failed: {exc}", None, None
    if not dir_exists:
        return "not_admitted", "no ledger claim dir", None, None

    try:
        holder_live = await asyncio.to_thread(cs_claim_holder_live, str(claim_dir))
    except Exception as exc:
        return "read_error", f"cs_claim_holder_live raised: {exc}", None, None
    if not holder_live:
        return "not_admitted", "ledger claim dir holder not live", None, None

    try:
        session_id = (claim_dir / "session_id").read_text(encoding="utf-8").strip()
    except OSError as exc:
        return "read_error", f"ledger session_id read failed: {exc}", None, None
    if not session_id:
        return "not_admitted", "ledger session_id empty", None, None

    try:
        ledger_claimed_at = (claim_dir / "claimed_at").read_text(encoding="utf-8").strip() or None
    except OSError as exc:
        return "read_error", f"ledger claimed_at read failed: {exc}", None, None
    if ledger_claimed_at is None:
        return (
            "read_error",
            "ledger claimed_at empty/absent — cannot thread as `at` without "
            "risking corruption of the field this leg exists to repair",
            None, None,
        )

    mirror_fields = _read_mirror_desync_fields(handoff_path)
    if mirror_fields is None:
        return "read_error", "mirror re-read failed", None, None
    if mirror_fields["claimed_by"] or mirror_fields["consumed_by"]:
        return "not_admitted", "mirror already carries a claim", None, None
    if mirror_fields["status"] != "open":
        return (
            "not_admitted",
            f"mirror status={mirror_fields['status']!r} != 'open'",
            None, None,
        )

    return "admitted", "ledger live-holder desync detected", session_id, ledger_claimed_at


async def _handle_ledger_mirror_desync(
    handoff: Dict[str, Any],
    ledger_holder: str,
    ledger_claimed_at: str,
    worktree_root: Path,
    repo_root: Path,
    dry_run: bool,
    reconciled: List[Dict[str, Any]],
) -> None:
    """C9 — repair a detected ledger-vs-mirror claim desync on a live holder
    (the branch-switch-revert incident this plan's Problem section
    documents) by DELEGATING the mirror re-stamp to `handoff.transition
    verb=claim` — see AC6/AC10. This function NEVER writes
    `state/handoffs/*.md` frontmatter itself (DR-212 sanctions exactly three
    ops for that: `handoff.transition`, `handoff.stamp`, `handoff.normalize`
    — this is not one of them) and NEVER writes the ledger (AC7 — the ledger
    is already correct; only the mirror lags).

    Passes the LEDGER's `claimed_at` through as `at` — never the current
    wall-clock time — so the re-stamped mirror's `claimed_at` matches the
    ledger's byte-for-byte; re-stamping with wall-clock time would itself
    corrupt the exact field this leg exists to repair.

    A raised exception from the delegated `handoff.transition` call is
    caught here (mirrors `_handle_auto_ship`'s own per-handoff error
    isolation) — this leg still writes nothing itself in that case (AC10),
    and the reconcile pass continues to the remaining open handoffs rather
    than aborting.
    """
    handoff_id = handoff.get("id")
    rel_path = _rel_path(worktree_root, handoff["_path"])

    entry: Dict[str, Any] = {
        "handoff_id": handoff_id,
        "handoff_path": rel_path,
        "action": "ledger_mirror_desync_reclaim",
        "ledger_holder": ledger_holder,
        "ledger_claimed_at": ledger_claimed_at,
        "dry_run": dry_run,
    }

    if dry_run:
        entry["applied"] = False
        reconciled.append(entry)
        return

    try:
        result = await _handoff_transition_handler(
            {
                "verb": "claim",
                "handoff_path": rel_path,
                "session_id": ledger_holder,
                "at": ledger_claimed_at,
            },
            repo_root,
        )
    except Exception as exc:
        entry["applied"] = False
        entry["exit_code"] = 1
        entry["message"] = f"handoff.transition raised: {exc}"
        reconciled.append(entry)
        return

    entry["applied"] = bool(result.get("applied"))
    entry["exit_code"] = result.get("exit_code")
    entry["message"] = result.get("message") or result.get("error")
    reconciled.append(entry)


@register_op("handoff.reconcile_open")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'handoff.reconcile_open' handler — enumerate open handoffs, decide,
    transition-or-surface.

    Params:
        dry_run (bool, optional) — D2(a): the loaded policy's OWN `dry_run` value
                is the SOLE source of truth (defaults to True on any absent/
                malformed policy — see policy_loader._conservative_policy). This
                param is NOT a default-overridable convenience any more; supplying
                it only matters when it DISAGREES with the policy value, and even
                then only takes effect paired with a non-empty
                `dry_run_override_reason` (see below) — an unnamed disagreement is
                refused and the policy value governs. When true (whether via
                policy or an applied override), computes every verdict but
                performs ZERO transitions (no ship_and_archive call, no
                gate-cascade-clear call).
        dry_run_override_reason (str, optional) — required, non-empty, ONLY when
                `dry_run` is supplied and disagrees with the loaded policy's own
                `dry_run` value; a named, logged escape from DoE's declared
                posture (mirrors handoff_stamp.py's
                `_repair_archived_shipped_in_handler` mandatory-`reason`
                pattern). See `_resolve_dry_run`.
        policy_path (str, optional) — override path forwarded to reconcile.policy_loader
                (test/CLI injection seam; production callers omit this).
        report_path (str, optional) — C12a: when supplied, absolute or repo-relative
                path this pass writes a durable Markdown dry-run report to (current
                verdict / proposed verdict / why, one row per open handoff — see
                `_build_dry_run_report`). Written via `locked_rmw` (cross-process
                flock + atomic replace); a lock-timeout or OS-level write failure
                degrades to a logged WARNING and does not fail this call — the
                computed `reconciled`/`gates_cleared`/`surfaced` result is still
                returned either way. Omitted entirely: no report is written (the
                pre-C12a behavior).
                Review: coordinator:code-reviewer — pass a DATED path (e.g.
                `state/audits/YYYY-MM-DD-gate-resolver-dry-run.md`) if this
                report's numbers will be cited later (a memo, an incident
                writeup); a fixed path is overwritten on every pass, by
                design, for the AC13 idempotence property above, so a fixed
                path is NOT a durable citation target across two passes whose
                underlying corpus has moved.
        report_run_label (str, optional) — human-meaningful label placed in the
                report's header only; never part of the body rows, so supplying
                it cannot itself defeat the report's idempotence property (see
                `_build_dry_run_report`'s docstring). Ignored when `report_path`
                is not supplied.

    repo_root handler arg is the git common dir (_OP_KEY_SCOPE="common_dir"); the
    worktree is derived via main_worktree_root(repo_root), same as
    handoff.ship_and_archive/handoff.transition.

    Returns:
        {reconciled: [...], gates_cleared: [...], surfaced: [...],
         policy_source, policy_path, dry_run_override: {...} | None,
         conservation_violations: [...], exit_code, scan_incomplete, scan_errors}
        policy_source/policy_path (§ C10 / AC16) are the flattened
        `policy_loader.policy_report_fields(policy_result)` pair — "absent" |
        "malformed" | "loaded", plus the resolved policy path (under
        `CLAUDE_PLUGIN_ROOT`, the shared plugin tree, not this repo's own
        tree; `None` only when resolution found no candidate at all). Also
        surfaced in the C12a Markdown report header when `report_path` is
        supplied — see `_build_dry_run_report`.
        dry_run_override is non-None only when a caller-supplied `dry_run` param
        disagreed with the loaded policy's own `dry_run` — see `_resolve_dry_run`
        and the `dry_run`/`dry_run_override_reason` Params entries above.
        exit_code is 0 for every per-handoff outcome computed this pass — this op
        never fails loud for an individual handoff's verdict; a per-handoff
        mutation failure is captured in that handoff's reconciled/gates_cleared
        entry (exit_code/message fields) rather than aborting the whole
        enumeration. THE ONE EXCEPTION (D1, severed-observer gate): exit_code is
        2, and conservation_violations is non-empty, whenever a handoff that was
        in the PREVIOUS run's surfaced[] is still open in THIS run with no
        recorded reconcile_disposition/reconcile_disposition_reason — see the D1
        module-docstring section. This is deliberately a NEW, distinctly-shaped
        signal (never a raised exception, never nested under an `.error` key) so
        a caller cannot mistake it for check-auto-reconcile.py's silently-skipped
        `.error` channel.
        scan_incomplete (bool) / scan_errors (list[str]) — True/non-empty when the
        archive/handoffs/ subtree behind the C3 gate index and/or the C6 dag_index
        could not be fully scanned (permission-denied or similar). Callers must
        treat a benign-looking result computed under scan_incomplete=True as
        "incomplete data", not as equivalent to a clean, fully-scanned read.
    """
    if repo_root is None:
        return {
            "reconciled": [],
            "gates_cleared": [],
            "surfaced": [],
            "conservation_violations": [],
            "exit_code": 1,
            "error": "handoff.reconcile_open: repo_root is required (no socket-authoritative common_dir)",
        }

    policy_path = params.get("policy_path")

    worktree_root = main_worktree_root(repo_root)

    policy_result = load_policy(policy_path)
    policy = policy_result.policy
    auto_ship_enabled = bool(policy.get("auto_ship_enabled", False))
    # § C10 / AC16 — flatten PolicyResult.source/.resolved_path into the two
    # report fields; merged into the returned dict and the C12a Markdown
    # report below so a reader can tell "absent"/"malformed"/"loaded" apart
    # without a cross-repo round-trip.
    policy_fields = policy_report_fields(policy_result)

    # D2(a) — the loaded policy is the SOLE source of truth for dry_run; a
    # caller param can only diverge via a named+logged override. See
    # _resolve_dry_run's own docstring for the fail-closed + override contract.
    dry_run, dry_run_override = _resolve_dry_run(policy, params)

    open_handoffs = _collect_open_handoffs(worktree_root)
    all_handoffs, gate_index_scan_errors = _collect_all_handoffs_for_gate_index(worktree_root)
    dag_index, dag_index_scan_errors = _collect_all_handoff_paths(worktree_root)
    scan_errors = gate_index_scan_errors + dag_index_scan_errors

    # D1 severed-observer gate: compare run N's persisted surfaced[] against
    # THIS run's freshly-enumerated open set before doing anything else this
    # pass touches — open_by_id is keyed off the same open_handoffs this
    # handler is about to route, so a candidate's absence here is genuine
    # terminality, not an artifact of a later mutation this same pass performs.
    open_by_id: Dict[str, Dict[str, Any]] = {
        h.get("id"): h for h in open_handoffs if h.get("id")
    }
    history_path = _history_path(repo_root)
    previous_surfaced, history_load_error = _load_surfaced_history(history_path)
    conservation_violations = _check_conservation(previous_surfaced, open_by_id)
    if history_load_error:
        # Review: code-reviewer (Finding 2) — a PRESENT-but-corrupt/malformed
        # history file must fail LOUD through the same channel as a real D1
        # violation, not degrade silently to "no prior candidates" (see
        # _load_surfaced_history's own docstring for why absent != corrupt).
        conservation_violations.append({
            "handoff_id": None,
            "handoff_path": str(history_path),
            "reason": (
                f"D1 surfaced-history at {history_path} could not be read "
                f"({history_load_error}) — the conservation assertion for "
                "THIS run could not compare against the previous run's "
                "surfaced set; treat as a coverage gap, not a clean pass"
            ),
        })

    reconciled: List[Dict[str, Any]] = []
    gates_cleared: List[Dict[str, Any]] = []
    surfaced: List[Dict[str, Any]] = []
    # Review: staff-eng Finding 7 (perf) — memoises
    # _handle_in_flight_blocked_by_retirement's ENUMERATION-pass
    # _blocker_clears_gate calls only, keyed on blocker id, for this single
    # _handler invocation. See that function's own docstring for why the
    # act-time re-verify inside its `mutate` closure never touches this cache.
    _in_flight_clears_cache: Dict[str, bool] = {}

    for handoff in open_handoffs:
        handoff_id = handoff.get("id")
        deployment_state = (handoff.get("deployment_state") or "").strip().lower()

        # Exclude this handoff's OWN pinned-lineage ancestors from the
        # cross-handoff attribution guard — see _chain_ancestor_norm_paths.
        ancestor_norm_paths = _chain_ancestor_norm_paths(handoff)
        other_open = [
            h for h in open_handoffs
            if h is not handoff and _norm_path(str(h.get("_path") or "")) not in ancestor_norm_paths
        ]
        commit_verdict = evaluate_commit_reality(handoff, worktree_root, policy, other_open)

        if commit_verdict.get("verdict") == "auto-ship":
            await _handle_auto_ship(
                handoff, commit_verdict, worktree_root, repo_root, dry_run,
                auto_ship_enabled, reconciled, policy, open_handoffs, dag_index,
            )
            continue

        if deployment_state == _AWAITING_GATE_STATE:
            # Review: code-reviewer (Finding 1) -- thread the C3 gate-index scan
            # gap into evaluate_gate so an unresolved blocked_by id under an
            # unscannable archive subtree reads as "can't confirm" rather than
            # "confirmed dangling ref". Uses gate_index_scan_errors specifically
            # (not the combined scan_errors below), since dag_index_scan_errors
            # covers the unrelated C6 liveness-gate subtree scan and would
            # misattribute an unrelated scan failure as the reason this
            # blocked_by id couldn't resolve.
            #
            # PM ruling 2026-08-01 (surface-only evidence), CORRECTED per EM
            # review 2026-08-01: the guard below is keyed on `gate_evidence`
            # being PRESENT on this handoff at all (`gate_evidence_present`),
            # NOT on whether a prose `gate_dependency` happens to be present
            # too. An earlier version of this guard keyed on
            # `has_prose_gate and covers_prose` (only reachable via
            # evaluate_gate's rule 0) -- but `gate_dependency` is DEPRECATED
            # (handoff.schema.json), and an evidence-only gate (no prose at
            # all) never reaches rule 0 (`has_prose` is False), so it falls
            # through to the pre-existing structured `blocked_by` / vacuous-
            # freed path -- which does NOT consult `gate_evidence` at all (see
            # gate_eval.py's own PRECEDENCE docstring: "no prose, gate_evidence
            # present ... evaluate_gate does NOT wire this branch ... falls
            # through to the pre-existing structured blocked_by / vacuous-
            # freed path, entirely unchanged"). That prose-keyed guard would
            # decay to a no-op exactly as the live corpus migrates off
            # `gate_dependency` -- an evidence-only gate could reach `clear`
            # (vacuously, if `blocked_by` is also empty, or via the structured
            # path) with its `gate_evidence` never actually checked, and that
            # verdict would flow straight into the clear/narrow ->
            # `_handle_gate_cascade` routing below, an auto-flip on a gate
            # nobody verified. Gating on mere PRESENCE closes that regardless
            # of whether this pass's verdict was genuinely evidence-derived
            # (`evidence_consumed`, tracked separately below for the surfaced
            # entry's own honesty) or merely evidence-adjacent (present on
            # disk but structurally unconsumed by this verdict) -- either way
            # a human reviews it, the sweep never does. See docs/plans/
            # 2026-07-13-claude-klabauter-auto-reconcile-open-handoffs.md § D5 ("evidence
            # never auto-clears a gate").
            # Review: code-reviewer Finding 6 -- `.get("_path")` with an explicit
            # None-check, matching this file's own convention (e.g. line 719),
            # instead of a direct `handoff["_path"]` KeyError risk.
            _handoff_path = handoff.get("_path")
            gate_evidence = (
                _read_gate_evidence_resolved(Path(_handoff_path), date.today())
                if _handoff_path is not None
                else None
            )
            gate_evidence_present = gate_evidence is not None
            # Review: code-reviewer Finding 2/3 -- reuse gate_eval's own
            # `consumes_gate_evidence`, the single source of truth for whether
            # `evaluate_gate` actually reaches rule 0 and consults
            # `gate_evidence` for this handoff, instead of a locally-
            # reimplemented copy of its precedence. A local mirror caused this
            # exact bug class once already (see gate_eval.py's own docstring
            # "C4 RECONCILIATION"): the prior inline expression here hardcoded
            # `not _has_blocking_notes(handoff)` as an unconditional veto,
            # which was correct back when blocking_notes dominance was
            # unconditional but silently went stale (both under- and
            # over-reporting `evidence_consumed`) the moment gate_eval.py's
            # own precedence moved out from under it (DR-259 demotion + the C2
            # scaffold-sentinel rule SC). Calling gate_eval's own predicate
            # means this can never drift from `evaluate_gate` again without
            # gate_eval's own test suite catching it first.
            evidence_consumed = consumes_gate_evidence(handoff, gate_evidence)
            gate_verdict = evaluate_gate(
                handoff, all_handoffs, witness_candidates=None,
                scan_incomplete=len(gate_index_scan_errors) > 0,
                scan_errors=gate_index_scan_errors,
                gate_evidence=gate_evidence,
            )
            verdict = gate_verdict.get("verdict")

            if gate_evidence_present and verdict in ("clear", "narrow"):
                # Surface-only invariant: a would-be-transitioning verdict on
                # a handoff carrying ANY gate_evidence is always forced onto
                # surfaced[] instead of `_handle_gate_cascade` -- never
                # auto-applied, regardless of dry_run.
                if evidence_consumed:
                    reason = (
                        "gate_eval verdict=clear (gate_evidence resolved -- all legs "
                        "satisfied; not auto-applied, review to confirm)"
                        if verdict == "clear"
                        else f"gate_eval verdict={verdict} (gate_evidence resolved -- "
                             "narrow-mutation candidate, not auto-applied)"
                    )
                else:
                    reason = (
                        f"gate_eval verdict={verdict} (gate_evidence present on this "
                        "handoff but not consumed by this verdict -- the structured/"
                        "vacuous path does not consult gate_evidence without covering "
                        "prose; surfaced defensively, never auto-applied while evidence "
                        "is in play)"
                    )
                surfaced.append({
                    "handoff_id": handoff_id,
                    "reason": reason,
                    "evidence": gate_verdict.get("evidence") or [],
                    "gate_evidence_resolved": evidence_consumed,
                })
                continue

            if verdict in ("clear", "narrow"):
                await _handle_gate_cascade(
                    handoff, gate_verdict, worktree_root, repo_root, dry_run,
                    gates_cleared, surfaced,
                )
                continue

            if verdict == "surface":
                entry = {
                    "handoff_id": handoff_id,
                    "reason": "gate_eval verdict=surface",
                    "evidence": gate_verdict.get("evidence") or [],
                }
                if gate_evidence_present:
                    entry["reason"] = (
                        "gate_eval verdict=surface (gate_evidence resolved -- still "
                        "blocked)"
                        if evidence_consumed
                        else "gate_eval verdict=surface (gate_evidence present on this "
                             "handoff, not consumed by this verdict)"
                    )
                    entry["gate_evidence_resolved"] = evidence_consumed
                # C3 -- a prose-dominance surface verdict may carry a
                # `contradiction` (prose gate outliving its own shipped
                # structured blockers). Composes with the gate_evidence
                # reason above rather than replacing it, so an operator
                # reading `surfaced[]` alone -- the artifact actually
                # consulted, not this evaluator's return dict -- sees both
                # facts and the concrete discharge verb without needing to
                # know it exists. Does NOT auto-transition anything -- see
                # this module's negative-spec on prose gate_dependency
                # verdicts (surfaces only, per DoE alignment reply #3).
                contradiction = gate_verdict.get("contradiction")
                if contradiction is not None:
                    entry["contradiction"] = contradiction
                    entry["reason"] = (
                        f"{entry['reason']}; CONTRADICTION: "
                        f"{contradiction.get('kind')} -- shipped blocked_by "
                        f"{contradiction.get('shipped_blocker_ids')} but the prose "
                        "gate still dominates; discharge via "
                        f"{contradiction.get('discharge_verb')}"
                    )
                surfaced.append(entry)
                continue

            if verdict == "not-cleared":
                # benign steady state, NO action, NOT surfaced (unaffected by
                # gate_evidence presence -- not-cleared never reaches
                # _handle_gate_cascade regardless, so the surface-only
                # invariant is not at risk here; changing this bucket's
                # surfacing behavior is a separate, un-asked-for design
                # question). One of exactly four contractual verdict strings
                # (see the terminal `raise` below, and
                # contract/handoff-reconcile-producer-contract.md § 4, which
                # pins the vocabulary this branch and that raise both rely on).
                continue

            # Review: coordinator:code-reviewer -- unreachable for the four
            # legitimate verdicts (clear / narrow / surface / not-cleared,
            # pinned by contract/handoff-reconcile-producer-contract.md § 4);
            # `gate_verdict` included for diagnosability if that contract is
            # ever violated upstream.
            raise ValueError(
                f"unrecognized gate_eval verdict {verdict!r} for handoff "
                f"{handoff_id!r} -- the verdict ladder (auto-ship / clear / "
                "narrow / surface / not-cleared) has no branch for this value "
                f"-- full gate_verdict={gate_verdict!r}"
            )

        in_flight_intercepted = False
        if deployment_state == _CONSUMED_OPEN_DEPLOYMENT_STATE:
            # Widened cleanup branch (jgate-clearance-recording-seam) — a
            # pickup-claimed handoff (_is_open admits status in {claimed,
            # consumed} AND deployment_state == in_flight) is enumerated into
            # open_handoffs but, before this branch existed, fell straight
            # through the loop unhandled: the gate-cascade cleanup above is
            # keyed on deployment_state == awaiting_gate only. This branch
            # closes that gap WITHOUT touching the awaiting_gate path above
            # (additive — see _handle_in_flight_blocked_by_retirement's own
            # docstring for why it is a separate write path, never a widened
            # _gate_cascade_clear call, and never a deployment_state flip).
            #
            # Review: staff-eng Finding 5 — deliberately does NOT `continue`
            # straight past the C9 ledger/mirror desync check on a truthy
            # return here (unlike the ORIGINAL landing, which did). A
            # retirement (or a gate_evidence surface) is already durably
            # recorded in gates_cleared/surfaced; skipping C9 for a claimed
            # in_flight handoff on the exact pass it retires residue would
            # hide a claim desync for one pass on precisely the handoffs C9
            # exists to check. C9's admission logic is independent of this
            # branch's outcome, so it always runs below. `in_flight_intercepted`
            # is still tracked so that, once C9 itself declines to admit,
            # this handoff is not ALSO appended to the terminal
            # commit_reality catch-all surface below — it was already fully
            # reported by this branch.
            in_flight_intercepted = await _handle_in_flight_blocked_by_retirement(
                handoff, worktree_root, repo_root, dry_run, gates_cleared,
                surfaced, clears_cache=_in_flight_clears_cache,
            )
            # Falls through unconditionally to the C9 desync check.

        # C9 — before the terminal surface fall-through, check for a ledger-
        # vs-mirror claim desync on a live holder (the branch-switch-revert
        # incident this plan's Problem section documents) and repair it BY
        # DELEGATION when admitted — see AC6/AC7/AC9/AC10/AC16.
        desync_outcome, desync_reason, desync_holder, desync_claimed_at = (
            await _ledger_mirror_desync_check(handoff, repo_root)
        )
        if desync_outcome == "admitted":
            await _handle_ledger_mirror_desync(
                handoff, desync_holder, desync_claimed_at, worktree_root,
                repo_root, dry_run, reconciled,
            )
            continue
        if desync_outcome == "read_error":
            surfaced.append({
                "handoff_id": handoff_id,
                "reason": (
                    "C9 ledger/mirror desync check hit a read error on an "
                    "admission conjunct — degraded to no-op-and-surface "
                    f"(AC16, never to fire): {desync_reason}"
                ),
                "evidence": [],
            })
            continue

        if in_flight_intercepted:
            # Already fully reported by _handle_in_flight_blocked_by_retirement
            # (a retirement entry in gates_cleared, or a gate_evidence surface
            # entry) and C9 declined to admit above — do not ALSO append the
            # generic commit_reality catch-all surface for this handoff.
            continue

        # Below the C2 auto-ship bar and not awaiting_gate -> surface.
        surfaced.append({
            "handoff_id": handoff_id,
            "reason": f"commit_reality verdict={commit_verdict.get('verdict')!r}",
            "evidence": commit_verdict.get("evidence") or [],
        })

    # D1: persist THIS run's surfaced-id map for the NEXT run's conservation
    # check. Best-effort (see _save_surfaced_history) — never blocks the return.
    current_surfaced_map: Dict[str, str] = {}
    for entry in surfaced:
        handoff_id = entry.get("handoff_id")
        if not handoff_id:
            continue
        meta = open_by_id.get(handoff_id)
        current_surfaced_map[str(handoff_id)] = (
            _rel_path(worktree_root, meta["_path"]) if meta else ""
        )
    await _save_surfaced_history(history_path, current_surfaced_map, repo_root)

    # C12a — durable dry-run report (opt-in via report_path). Best-effort at
    # the OUTER layer only, matching _save_surfaced_history's own contract
    # immediately above: a lock-timeout or OS-level write failure here must
    # not fail a call whose verdicts have already been computed and are
    # about to be returned.
    report_path = params.get("report_path")
    if report_path:
        report_text = _build_dry_run_report(
            worktree_root, open_handoffs, reconciled, gates_cleared, surfaced,
            dry_run, policy_fields["policy_source"], policy_fields["policy_path"],
            report_run_label=params.get("report_run_label"),
        )
        report_target = Path(report_path)
        try:
            await asyncio.to_thread(
                locked_rmw,
                report_target,
                lambda _old_text: report_text,
                repo_root=repo_root,
                missing_ok=True,
            )
        except (OSError, LockTimeout) as exc:
            _LOG.warning(
                "handoff.reconcile_open: C12a could not persist dry-run report to "
                "%s — the computed verdicts are still returned, but no durable "
                "artifact was written this pass: %s",
                report_target, exc,
            )

    return {
        "reconciled": reconciled,
        "gates_cleared": gates_cleared,
        "surfaced": surfaced,
        # § C10 / AC16 — PolicyResult.source ("absent"/"malformed"/"loaded")
        # and the resolved policy path (under CLAUDE_PLUGIN_ROOT, the shared
        # plugin tree — NOT this repo's own tree), so a caller can tell
        # which policy state this pass ran under without a cross-repo
        # round-trip. See policy_loader.policy_report_fields.
        "policy_source": policy_fields["policy_source"],
        "policy_path": policy_fields["policy_path"],
        # D2(a): non-None ONLY when a caller-supplied `dry_run` param
        # disagreed with the loaded policy's `dry_run` value — see
        # _resolve_dry_run. `applied: True` means the named+reasoned
        # override won; `applied: False` means it was refused and the
        # policy's own dry_run governed this pass instead.
        "dry_run_override": dry_run_override,
        # D1 severed-observer gate: non-empty ONLY when a previously-surfaced
        # candidate is still open with no recorded disposition (see module
        # docstring's D1 section + _check_conservation). Deliberately a
        # distinct field, never nested under an `.error`-shaped key — a
        # caller must explicitly choose to ignore this, rather than have it
        # land inside a field an existing consumer (check-auto-reconcile.py)
        # already discards by design.
        "conservation_violations": conservation_violations,
        # exit_code is 0 in the normal case — a per-handoff mutation failure
        # is captured in that handoff's reconciled/gates_cleared entry rather
        # than aborting the pass (unchanged from the pre-D1 contract). D1
        # is the ONE exception: exit_code is 2 whenever conservation_violations
        # is non-empty, regardless of dry_run — this is a NEW op-level failure
        # mode a caller must check for explicitly.
        "exit_code": 2 if conservation_violations else 0,
        # Tier 2: True when the archive scan behind the C3 gate index and/or
        # the C6 dag_index could not be fully enumerated. gate_index_scan_errors
        # is separately threaded into evaluate_gate() above (Finding 1 fix) so a
        # surfaced[] entry's evidence text names the scan gap instead of
        # asserting a confirmed dangling ref; this op-level field is the
        # combined C3+C6 total for the caller to treat a "not-cleared"/benign
        # steady-state verdict computed under scan_incomplete=True as "we
        # couldn't fully verify", not as equivalent to a genuinely clean read
        # of the same shape.
        "scan_incomplete": len(scan_errors) > 0,
        "scan_errors": scan_errors,
    }
