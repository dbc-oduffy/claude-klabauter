"""
coordinator_core.reconcile.gate_eval — unified gate evaluator (COMPUTE_ONLY).

Purpose: given one `awaiting_gate` handoff, decide whether its gate is now cleared,
partially cleared, or must surface for EM judgment. This is C3's "C7 folded in"
unified evaluator — a single module covering both the STRUCTURED path (spinoff-roadmap
`blocked_by:[stub-id,...]` graph edges) and the PROSE fallback path (the free-text
`gate_dependency` one-liner other handoff kinds carry). This module is COMPUTE_ONLY
(DR-208 classification): pure read + compute over caller-supplied handoff dicts, NO
writes, NO git mutation, NO frontmatter mutation. `handoff.reconcile_open` (C4) is the
only caller authorized to act on a `clear`/`narrow` verdict, via C8's
`gate-cascade-clear` verb.

STRUCTURED-PATH LOAD-BEARING RULES (converges with
`DoE-claude/archive/specs/2026-06/2026-06-27-status-propagation-primitive.md` §68-70's
gate-cascade design; rule 3 sourced from the tc-4 regression lesson
`DoE-claude/archive/lessons/2026-07/2026-06-23-a-gate-reconcile-hook-that-flips-a-depen.yaml`):
    (1) CLEAR predicate = ALL `blocked_by` members are `shipped` SPECIFICALLY, not
        merely terminal ({shipped, abandoned, continued, closed} is the terminal set
        for stopping re-evaluation, but abandoned/continued/closed never count toward
        clearing — DR-084 P1 dual-vocabulary window: `abandoned` is the retiring old
        term, `continued`/`closed` are its replacements, all three treated
        identically here). `shipped` alone is not sufficient either (C6): the
        member must ALSO carry a `shipped_in` commit sha — a terminus that reads
        `shipped` with no `shipped_in` has no clearing provenance to record and
        never enters the cleared set; it surfaces instead via
        `_classify_blocked_by`'s `unstamped_shipped_ids` bucket.
    (2) A `blocked_by` member that is `abandoned`/`continued`/`closed` -> SURFACE,
        never clear. The dependent's premise is now likely-false/moot and needs EM
        judgment, not a silent auto-flip. EXCEPTION — `continued` alone also carries
        a `continued_into` pointer (C5, docs/plans/2026-07-27-... successor-terminus
        window): `continued` is a REDIRECT, not an ending, so this module chases
        `continued_into` to its terminus (`_chase_continuation`) before applying
        this rule. Only when the terminus itself is genuinely `shipped` does the
        edge clear; a still-open terminus, an unresolvable/dangling redirect, a
        chain exceeding the depth cap, or a chase cycle all fall back to this
        rule's ordinary SURFACE treatment — `continued` is never trusted as
        discharge merely because it is terminal on the deployment axis (the
        lvv-05/lvv-06 corpus case this chase exists for: lvv-05 is `continued`,
        its successor was still open when the PM's manual gate-drain caught the
        earlier naive-clear defect this chase closes).
    (3) PARTIAL-SATISFACTION -> NARROW, never fire-on-first-edge. AND-reduce over
        EVERY member: when some-but-not-all are shipped, drop the shipped edges from
        the reported `remaining_blockers` and verdict=narrow (caller stays
        `awaiting_gate`, mutates `blocked_by` down). This is the tc-4 regression
        guard: `blocked_by:[tc-1, tc-5]` must NOT flip to ready_to_fire when only
        tc-1 shipped.
    (4) FAIL-LOUD on `blocks`/`blocked_by` asymmetry (A blocks B but B doesn't list A
        in blocked_by) -> verdict=surface. A data defect is surfaced, never
        auto-repaired.
    (5) One-level DAG walk (this handoff's direct `blocked_by` edges only) with a
        visited-set cycle guard, even though the schema intends an acyclic graph.

NARROW+SURFACE COMPOSITE (the Staff Engineer review, finding 1 — major): a `narrow` verdict whose
`remaining_blockers` includes any `abandoned`/`continued`/`closed` id must ALSO carry a surface signal
(`also_surface=True` in the returned dict) — the handoff must not silently rot gated on
a dead blocker forever. C4 appends such handoffs to `surfaced[]` in addition to driving
the narrow-mutation.

PROSE-PATH RULES (fallback, unchanged from the original C7 design): non-roadmap
`awaiting_gate` handoffs carry `gate_dependency` as a free-text one-liner (subsystem
name, not a path, per schema). Conservative: resolve to a witness ONLY when the body/
frontmatter gives a concrete, checkable pointer (a named plan/handoff `id` that the
caller confirms is shipped); otherwise verdict=surface. Ambiguous resolution (more than
one candidate witness) also surfaces — never guess between candidates.

Return shape per handoff:
    {handoff_id, verdict: "clear"|"narrow"|"surface"|"not-cleared",
     cleared_by_shas: [sha, ...], remaining_blockers: [id, ...],
     cleared_blocker_ids: [id, ...], evidence: [str, ...], also_surface: bool}

`contradiction` (C1, see "C3 STALENESS EVIDENCE ON DOMINANCE" below) is an
OPTIONAL additional key `evaluate_gate` appends only on the prose-dominance
(rule 1) all-shipped case — absent, not `None`-valued, everywhere else. Not
a fifth verdict value; `verdict` stays `"surface"` wherever it appears.

`cleared_blocker_ids` (Slice-B review Finding 5, nit) is the set of
`blocked_by` ids this verdict actually clears (parallel to `cleared_by_shas`,
one id per sha) — an explicit field so callers (C4's `_handle_gate_cascade`)
don't need to re-derive it via `blocked_by - remaining_blockers` set
difference against two separately-sourced lists.

Terminal-set for STOPPING re-eval = {shipped, abandoned, continued, closed} (mirrors
the live `deployment_state` enum, dual-vocabulary during DR-084 P1). CLEAR predicate =
shipped-with-a-sha-only, per rule (1).

TRIAGE PROJECTION (`evaluate_gate_triage`, added for the stale-`awaiting_gate`-batch-
audit use case — 20+ handoffs sitting `awaiting_gate` for weeks, most never
re-evaluated after their blockers shipped): a THIRD function alongside
`evaluate_gate`'s four-way clear/narrow/surface/not-cleared and
`_evaluate_structured_gate`/`_evaluate_prose_gate`'s split, reusing the SAME
`_classify_blocked_by`/`_has_asymmetry`/`_index_by_id` primitives rather than
re-walking `blocked_by` a second way (this module remains the ONE place that
reads handoff frontmatter and decides gate status — no parallel evaluator
anywhere else in the repo). Emits a four-way `status`:
`"freed"|"still-blocked"|"indeterminate"|"review-due"`, does NOT touch
`evaluate_gate`'s own four-way contract (pinned by the C5 producer-contract
doc — callers of `evaluate_gate` are unaffected), and is NEVER wired to
auto-mutation — see its own docstring for the full derivation, the
prose-gate-dominance precedence rule, the `gate_evidence` AND-reduce
projection (below), and the empty-`blocked_by`-is-vacuously-freed case.

C4 RECONCILIATION (`_is_structured_gate`, docs/plans/2026-07-13-claude-klabauter-auto-
reconcile-open-handoffs.md § C4): before C4, `evaluate_gate` (mutating) and
`evaluate_gate_triage` (reporting) each decided structured-vs-prose
eligibility independently — `evaluate_gate` gated on `kind ==
"spinoff-roadmap" and blocked_by`, `evaluate_gate_triage` never checked
`kind` at all. Two evaluators independently deciding the same routing
question is the sibling-evaluator shape this module exists to avoid (see
TRIAGE PROJECTION above). C4 extracts that routing question into one shared
predicate, `_is_structured_gate(handoff)` — True iff `blocked_by` is
non-empty, kind-independent — and reconciles `evaluate_gate` onto it,
widening its eligibility from roadmap-only to ANY kind.

Widening drags two things along with it that C4 also fixes:
  - PROSE-DOMINANCE PARITY: `evaluate_gate_triage` already refuses to let
    structured satisfaction free a gate that also carries a non-empty prose
    `gate_dependency` (see PRECEDENCE below) — `evaluate_gate` had no such
    guard, because roadmap-kind handoffs rarely carried both fields. Widened
    eligibility drags the wider both-fields population into the mutating
    path, so `evaluate_gate` now applies the SAME dominance: a handoff with
    both a non-empty `blocked_by` and a non-empty `gate_dependency` verdicts
    `surface`, never `clear`, regardless of structured shipped-state. Keyed
    on `gate_dependency` at C4 time — briefly widened post-C4 to also key on
    `blocking_notes` with the identical unconditional dominance, then
    RE-DEMOTED by a later chunk to a narrower vacuous-clear-prevention role
    (`blocking_notes` never dominates a non-empty `blocked_by` at all,
    satisfied or not); see "BLOCKING_NOTES DOMINANCE" below for the current
    semantics.
  - VACUOUS-EMPTY PARITY: `evaluate_gate_triage` already treats an empty
    `blocked_by` with no prose gate as vacuously `freed` (for-all over the
    empty set). `evaluate_gate` previously fell through to the PROSE
    fallback path in that case (conservative `surface`), because the old
    `kind == "spinoff-roadmap" and blocked_by` eligibility test was False for
    an empty `blocked_by` regardless of kind. `evaluate_gate` now matches:
    empty `blocked_by` + no prose -> `clear`. This is also the fix for the
    LINEAGE-IS-NOT-GATING regression a naive routing rewrite could otherwise
    reintroduce: a spinoff with `predecessor`/`origin_*` fields populated and
    an empty `blocked_by` must resolve `clear`/`ready_to_fire`, not surface
    merely because it fell through to the prose path with no witness.
  - ASYMMETRY-CHECK KIND GUARD: `_has_asymmetry` assumed every blocker
    authors a `blocks:` back-reference — a roadmap-kind convention. Widening
    eligibility to ANY kind means a blocker of some OTHER kind that has never
    adopted that convention (no `blocks:` field at all) would otherwise fire
    a false-positive asymmetry on every such edge. `_has_asymmetry` now skips
    the check entirely for a blocker whose own `kind != "spinoff-roadmap"` —
    only a roadmap-kind blocker is held to the back-reference convention.

BLOCKING_NOTES DOMINANCE — VACUOUS-CLEAR-PREVENTION ONLY (post-C4, then
DEMOTED by the C4 chunk of docs/plans/2026-08-03-gate-dependency-template-
emission-spec.md — the corpus's own migration exposed a gap C4 left in place,
and this module's `blocking_notes` handling has now been corrected twice):
the original C4 above keyed prose dominance on `gate_dependency` alone and
explicitly documented `blocking_notes` as inert. A live corpus migration
subsequently moved the operative human-authored gate text for some handoff
kinds OUT OF `gate_dependency` and INTO `blocking_notes` — a correct move
under the migration's own translation rules, and one that exposed exactly the
gap C4's inert-`blocking_notes` premise left open: an `awaiting_gate`
handoff with `blocked_by: []` and a non-empty `blocking_notes` (e.g. "Windows
machine required for AC7 verification — no baton, advisory only") is a real,
unmet, human-checkable gate, but the vacuous-clear/vacuous-freed branch (rule
3 above / the empty-`blocked_by`-no-prose branch below) checked nothing but
`gate_dependency` and cleared it. The fix at the time went further than that
gap required: `blocking_notes` was given the IDENTICAL unconditional dominance
`gate_dependency` has — refusing `clear`/`freed` even when a non-empty,
FULLY-SATISFIED `blocked_by` graph said otherwise. That over-corrected: the
`handoff.schema.json` `blocking_notes` property description declares it
"Advisory prose, NEVER read by the resolver — inert by construction. Home for
the 'needs a Windows box' class of real-but-not-a-dependency constraint" — a
resolver that lets it override a satisfied structured graph is not inert, it
is a second gate mechanism the schema never advertised. Live-corpus
measurement (2026-08-03, 216 handoffs, 4 with non-empty `blocking_notes`)
found exactly this: handoffs whose own `blocking_notes` text read "Nothing
blocking... was ratified" or "DISCHARGED 2026-07-28..." were parked `surface`
by dominance alone, contradicting their own prose.

RECONCILED SEMANTICS (this chunk): `blocking_notes` now confers dominance
ONLY over the VACUOUS-CLEAR/VACUOUS-FREED case — the same narrow gap the
Windows-box motivating defect actually needed closed, and nothing wider. A
non-empty `blocked_by` whose members all resolve `shipped` clears/frees
REGARDLESS of `blocking_notes` — notes never beat structure, and (unlike
`gate_dependency`) `blocking_notes` no longer participates in the
non-vacuous structured walk's outcome at all (no dominance branch, no
staleness addendum — the walk's own verdict is final). An EMPTY `blocked_by`
with a non-empty `blocking_notes` still verdicts `surface`/`indeterminate`,
unchanged from before and covering the Windows-box case this dominance
originally exists for. `gate_dependency` dominance (rule 1) is completely
UNTOUCHED by this reconciliation — only `blocking_notes` (rule 1a) was ever
over-broad, and rule 1a itself now applies solely to the empty-`blocked_by`
case. `blocking_notes`
remains a purely opaque signal wherever it does dominate, never
machine-resolvable: no witness lookup, no cascade, no attempt to parse a
baton out of the note text. `_has_blocking_notes` mirrors `_has_prose_gate`'s
whitespace-is-empty discipline, so a whitespace-only `blocking_notes` does
not park a baton forever.

C2 SCAFFOLD SENTINEL (docs/plans/2026-08-03-gate-dependency-template-
emission-spec.md § C2): `coordinator-doc-new`'s roadmap-baton/goal-seed/
roadmap-seed scaffolds default an unrequested `--gate-dependency` to the
literal string `PLACEHOLDER` (pre-C1) or, post-C1, to a `blocking_notes:
PLACEHOLDER — name the condition...` line — either way, a template line
nobody has edited, not a human naming a gate. `_has_prose_gate`/
`_has_blocking_notes` apply a whitespace-is-empty discipline so a
whitespace-only value does not park a baton forever; an unfilled scaffold
placeholder is the SAME failure wearing a different costume — a value that
IS non-empty text, so those two predicates alone would read it as ordinary
authored prose. Reading it as ordinary prose already happens to route to
`surface`/`indeterminate` via the pre-existing dominance branches (rules 1/
1a), which is *safe* but not *legible* — the evidence text those branches
emit ("prose gate_dependency=... present ... dominates") reads as if a human
stated a real gate, when in fact nobody has.

`_scaffold_sentinel_field` checks BOTH fields (`gate_dependency` first,
mirroring rule 1's precedence over rule 1a) via `_is_scaffold_sentinel`, a
PREFIX test on the stripped value — exactly `PLACEHOLDER`, or `PLACEHOLDER`
followed by a space or em/en-dash separator (the C1 scaffold's own authored
continuation) — NEVER a substring search: authored prose that happens to
CONTAIN the word "placeholder" in a real sentence (e.g. "blocked on the
placeholder registry landing") keeps ordinary dominance (AC2.3). When either
field matches, `evaluate_gate`/`evaluate_gate_triage` short-circuit BEFORE
every other rule (including rule 0's `covers_prose` witness — an unnamed
gate has no prose for a `gate_evidence` block to legitimately cover, and
including rule 1a's now-narrowed `blocking_notes` check) to `surface`/
`indeterminate` with a DISTINCT evidence line naming the unfilled field,
never the generic dominance/no-witness text. The sentinel must NEVER route
to `clear`/`freed` — the wrong-but-obvious reading ("no gate named yet" =>
vacuously clear) would let an unfilled stub with `blocked_by: []` fall
through the vacuous-clear branch (rule 3) and auto-clear a baton whose gate
nobody ever stated, strictly worse than the C1 defect this exists to fix.
Post-BLOCKING_NOTES-DOMINANCE-demotion, this guard is load-bearing for
CORRECTNESS, not merely legibility, whenever the scaffold placeholder lands
in `blocking_notes` alongside a non-empty, fully-satisfied `blocked_by`: were
the SC check absent, an unfilled `blocking_notes` placeholder would no
longer be caught by rule 1a at all in that shape (rule 1a is empty-`blocked_
by`-only now) and would fall through to the structured walk's own `clear`
verdict — auto-clearing a baton whose gate nobody ever named. The SC check
closes that path unconditionally, regardless of `blocked_by` state.

C3 STALENESS EVIDENCE ON DOMINANCE (docs/plans/2026-08-03-gate-dependency-
template-emission-spec.md § C3): dominance (rule 1, `gate_dependency`) can
fire on prose whose named structured co-blockers have since ALL shipped —
the awca-02 corpus case: prose read "no acceptance oracle exists until
success criteria ship", the named `blocked_by` member had already shipped,
and four readers in a row concluded the dominance mechanism itself was
broken rather than recognizing the prose had simply gone stale. The VERDICT
never changes — it stays `surface`; a reader must still be the one to decide
stale prose is safe to retire. `_all_blocked_by_shipped_evidence` reuses
`_classify_blocked_by`/`_index_by_id` — the SAME shipped-state predicate and
live+archived index the structured path itself uses, no new sibling I/O, no
clock read, no parsing a baton out of the prose text (AC3.4) — to check
whether EVERY `blocked_by` member independently resolves as shipped (or, for
a `continued` member, chases to a genuinely shipped terminus, exactly as
`_classify_blocked_by` already does for the structured path). Only when ALL
members clear this bar does rule 1's evidence gain an addendum naming each
blocker id and its shipping sha (AC3.1); a single unresolved, abandoned/
continued/closed-and-unchased, still-open, or disposed member means the
caller has NOT substantiated staleness and the addendum is omitted entirely
(AC3.2) — never a partial or hedged staleness claim. An empty `blocked_by`
never gains this addendum either (AC3.3) — there is nothing to have shipped.

C1 (docs/plans/2026-08-03-gate-dependency-template-emission-spec.md § C1):
`evaluate_gate`'s rule-1 branch previously computed this all-shipped addendum
and then discarded it into prose evidence only — no machine-legible signal
recorded that a fully-satisfied structured graph and an unconditionally-
dominating prose clause are now in contradiction. When (and only when)
`staleness_evidence is not None`, the returned dict now additionally carries
a `contradiction` key: `{"kind":
"prose-gate-outlived-structured-blockers", "discharge_verb":
"handoff.transition gate-recheck --cleared", "shipped_blocker_ids": [...]
}` (`shipped_blocker_ids` is `blocked_by`, in `blocked_by` order — every
member is, by construction of `staleness_evidence is not None`, resolved
shipped). The key is ABSENT, not present-and-`None`, in every other case —
this is additive, never a fifth verdict value; `verdict` stays the string
`"surface"` and the contradiction never auto-clears itself. Rule 1a
(`blocking_notes`) is untouched: per the note above, its own staleness
addendum is unreachable by construction (`blocked_by` empty whenever rule 1a
fires), so `contradiction` is likewise unreachable there.

Post-BLOCKING_NOTES-DOMINANCE-demotion (AC4.7 of the same spec's C4 chunk):
rule 1a (`blocking_notes`) no longer applies to a non-empty `blocked_by` at
all, so this staleness addendum can no longer appear on rule 1a's own
evidence — it is unreachable there by construction, since rule 1a now only
ever fires with `blocked_by` empty, and `_all_blocked_by_shipped_evidence`
always returns `None` on an empty list (AC3.3). The addendum remains fully
reachable via rule 1 (`gate_dependency`), which this reconciliation left
untouched — the awca-02 motivating shape (a prose `gate_dependency` alongside
an all-shipped `blocked_by`) still exercises it exactly as before; rule 1 is
now the ONLY place this addendum can appear.

GATE_EVIDENCE PROJECTION (C3, docs/plans/2026-07-26-structured-sibling-
evidence-gates.md § C3 — this module's D2/D3a implementation; widened in C6
to also feed `evaluate_gate`, see below): a handoff may additionally carry a
caller-assembled `gate_evidence` block — `{"covers_prose": bool, "legs":
[leg, ...]}` — whose legs this module resolves against ITS OWN predicate,
exactly the way `evaluate_gate`'s `witness_candidates` argument already
receives caller-resolved raw data and applies the shipped-state predicate at
line ~483: neither `evaluate_gate_triage` nor `evaluate_gate` ever performs
sibling I/O itself (DR-208 purity is load-bearing), so every I/O-kind leg
(`file-exists`/`frontmatter-field`/`commit-ancestor`/`test-node-id`/
`probe-op-key`/`commit-sha`/`sibling-commitment-ref` — the last four adopted
in C6 from `coordinator/schemas/cutover.schema.json`'s already-ratified
`verified_by.kind` union, see PER-LEG PREDICATE below) arrives PRE-RESOLVED:
the caller runs its own re-verification per leg (`sibling_fact.resolve_leg`
for the original three, a `cutover_gate.py`-style `_reverify_*` for the C6
four) and merges the `{read_ok, observed, error}` observation onto the leg's
own authored `{leg_id, kind, expected}` declaration before calling in. `kind:
human` and `kind: deadline` legs carry no I/O at all (D4/D3a) — a `deadline`
leg's `elapsed: bool` is likewise caller-computed (a system-clock read is
non-deterministic, impure input this module refuses to perform on its own),
never derived here.

C6 WIDENING — `evaluate_gate` (the MUTATING evaluator) now also consumes
`gate_evidence`, not only `evaluate_gate_triage` (the reporting projection):
before C6, a handoff whose real gate was an external, non-baton fact (a
sibling REPO's reply, a re-run test, a re-checked commit — never a
`blocked_by` slug) had no path to `clear` at all — the slug-or-advisory-prose
binary forced it to sit `indeterminate`/`surface` forever, EVEN once the
external fact became true, because `evaluate_gate_triage`'s own
`gate_evidence` consumption was wired only to the non-mutating triage
projection. `evaluate_gate` now applies the IDENTICAL D2/D2a precedence
(`covers_prose: True` -> evidence authoritative for the whole gate; anything
less -> falls through to the pre-C6 `witness_candidates` prose fallback,
unchanged) — see `_gate_evidence_status_to_verdict` for the freed->clear /
everything-else->surface projection onto `evaluate_gate`'s binary
clear/surface prose shape. This is a NEW WITNESS SOURCE feeding the existing
prose-resolution mechanism, not a third verdict class: `gate_evidence` legs
and `witness_candidates` handoff-dicts are two DIFFERENT shapes of the same
question ("is there a concrete, checkable witness for this prose gate"),
resolved by the SAME `evaluate_gate` entrypoint via the SAME `_has_prose_gate`
gate — never a parallel evaluator.

PRECEDENCE (D2 — the core of the C3 change, unchanged in C6 except that
`evaluate_gate` now applies it too; `_has_prose_gate` unchanged). Any caller
needing to know whether a GIVEN verdict actually consumed `gate_evidence`
(rather than re-deriving that from these bullets) MUST call
`consumes_gate_evidence(handoff, gate_evidence)` — the single source of truth
for this precedence, walking the SC sentinel / demoted rule-1a /
rule-0-`covers_prose` gates in the same order `evaluate_gate` itself checks
them, never a re-stated boolean expression (see its own docstring):
    - prose present AND gate_evidence present AND `covers_prose: True`
        -> evidence wins; prose is demoted to commentary evidence, the
           gate_evidence legs' AND-reduce is authoritative. In
           `evaluate_gate` this is checked FIRST, before even the structured-
           vs-vacuous routing (rule 0) — an exact mirror of
           `evaluate_gate_triage`'s own check order.
    - prose present WITHOUT gate_evidence, OR (prose present AND
      gate_evidence present but `covers_prose` is not `True`)
        -> today's unconditional indeterminate (`evaluate_gate_triage`) /
           legacy `witness_candidates` fallback (`evaluate_gate`), entirely
           unchanged. The `covers_prose` gate (D2a, eng-director F3) exists
           so a PARTIAL backfill — legs covering only some of what the prose
           sentence names — cannot silently free a gate merely because every
           authored leg happens to resolve; migration off prose must be an
           explicit human assertion (`covers_prose: true`), never an
           emergent property of "some legs exist".
    - no prose, gate_evidence present (regardless of `covers_prose`, which
      is moot with nothing to demote) -> evidence legs' AND-reduce is
      authoritative in `evaluate_gate_triage`. `evaluate_gate` does NOT wire
      this branch (C6 scope: only the prose-gate witness source, matching
      the oaxis-01 motivating shape — a no-prose gate_evidence-only witness
      for the mutating evaluator is not this chunk's fixture and is left to
      a future chunk should a real case arise).
    - no prose, no gate_evidence -> falls through to the pre-existing
      structured `blocked_by` / vacuous-freed path, entirely unchanged.

PER-LEG PREDICATE (`_evaluate_gate_evidence_leg`):
    - `file-exists` / `frontmatter-field`: `read_ok is False` -> indeterminate
      (could not ask); else `observed == expected` -> satisfied, else
      unsatisfied.
    - `commit-ancestor`: `read_ok is False` -> indeterminate; else
      `observed is True` -> satisfied, else unsatisfied (mirrors
      `sibling_fact.resolve_leg`'s own tri-state git-ancestry read).
    - `test-node-id` / `probe-op-key` / `commit-sha` / `sibling-commitment-ref`
      (C6, external-gate class — adopted verbatim from
      `coordinator/schemas/cutover.schema.json`'s `verified_by.kind` union,
      not re-invented): `read_ok is False` -> indeterminate; else `observed
      is True` -> satisfied, else unsatisfied — same boolean-observed
      treatment as `commit-ancestor`, since a caller's re-verification
      (rerun the pytest node, re-invoke the op, `git show` the SHA, read the
      `state/cross-repo-commitments/*.yaml` FK) reduces to pass/fail with no
      `expected` value to compare against. This is the class of REAL gate
      that can never be a `blocked_by` slug — e.g. the oaxis-01 shape,
      `blocked_by: []` with a prose gate "rag acl-principal axis co-design
      reply reconciled": example-retrieval-repo is a sibling repo, not a baton, so
      `sibling-commitment-ref` (an FK to a local `state/cross-repo-
      commitments/*.yaml` record that itself attests the sibling's
      confirmation) is the witness — surfaces (indeterminate/unsatisfied)
      while unwitnessed, clears (satisfied) once a `gate_evidence` leg
      carrying it resolves True.
    - `human`: always indeterminate — permanent, by construction (D4); never
      machine-resolvable, carries its own authored `reason`.
    - `deadline`: `elapsed: True` -> a DISTINCT `review-due` leg status,
      EXCLUDED from the AND-reduce toward `freed` (D3a) — an elapsed date
      asserts only that time passed, nothing about whether the deferred
      thing is now wanted, feasible, or decided; a single elapsed-deadline
      leg must never free a gate. `elapsed: False` -> unsatisfied (not yet
      due), same AND-reduce treatment as any other not-yet-satisfied leg.

AND-REDUCE (`reduce_gate_evidence`): any indeterminate leg (`human` included)
makes the WHOLE gate `indeterminate` — never freed on partial satisfaction,
mirroring the tc-4 narrow guard above. Absent any indeterminate leg, any
`review-due` leg makes the whole gate `review-due`. Absent both, any
unsatisfied leg makes it `still-blocked`. Only when every leg resolves
satisfied is the gate `freed`. An empty `legs` list is a malformed
`gate_evidence` block (declared but nothing to evaluate) and resolves
`indeterminate`, never vacuously freed — vacuous-freed is reserved for the
genuinely-ungated `blocked_by: []`-and-no-gate_evidence case below.

Spec backlink: pln-claude-klabauter-auto-reconcile-pass-off-425848 § C3,
docs/plans/2026-07-26-structured-sibling-evidence-gates.md § C3

LINEAGE IS NOT GATING (PM ruling 2026-07-26, settled — the resolver's input set
is `blocked_by` and NOTHING else): this module never reads, walks, or writes
`predecessor`, `additional_predecessors`, `forked_from`, or any `origin_*`
field (`origin_session`, `origin_handoff`, `origin_plan_id`, `origin_goal_id`,
...) to decide or infer a gate. Those fields answer "where did this baton come
from" — an authorship/provenance question — not "what must ship before this
baton may fire", which is `blocked_by`'s question alone. A spinoff is authored
`predecessor: none` BY INVARIANT (it is a fork, not a continuation of a
running chain), and it is routinely, correctly `ready_to_fire` while the
session that spawned it (its `origin_session`) is still running, or while its
`origin_handoff` sits open — the parent baton's own lifecycle is orthogonal to
whether the fork's OWN `blocked_by` edges have shipped. Doctrine already holds
that adjacency is not ancestry (a `blocks`/`blocked_by` edge is not implied by
being created "near" another baton); this rule is the ancestry-side twin:
ancestry (predecessor/origin lineage) is not gating either.

THE WRONG TURN THIS SPEC EXISTS TO PREVENT: a "helpful" resolver that walks a
handoff's `predecessor` chain (or inspects its `origin_*` fields) to infer an
implied dependency — reasoning "this baton was forked from X, so it can't be
ready until X is done" — would gate every spinoff on its own origin baton,
exactly backwards from the roadmap-fork design: spinoffs exist precisely so
their work can proceed independently of the parent session's continued
progress, not chained behind it. Live-corpus verification (2026-07-27): 30 of
33 gated (`awaiting_gate`) batons under `DoE-claude/state/handoffs/` carry a
non-`none` `predecessor` and/or a non-null `origin_*` field — 91% of the live
corpus would trip a naive implementation that conflated lineage with gating.

Negative-spec:
  - Does NOT write any file, git object, frontmatter, or repo state — pure compute.
  - Does NOT invoke `handoff.ship_and_archive`, `gate-cascade-clear`, or any mutating
    op/verb — that is C4's/C8's job.
  - Does NOT auto-repair a `blocks`/`blocked_by` asymmetry — always surfaces it.
  - Does NOT flip on partial `blocked_by` satisfaction — narrows instead (tc-4 guard).
  - Does NOT treat an `abandoned`/`continued`/`closed` blocked_by member as clearing
    evidence — EXCEPT a `continued` member whose `continued_into` chain, chased to
    its terminus, resolves that terminus as genuinely `shipped` (C5) — see rule (2)'s
    exception clause above; `abandoned`/`closed` are never chased, only `continued`.
  - Does NOT walk more than one level of the dependency graph per invocation
    (this bound is on the `blocked_by` SIBLING-edge walk — rule 5 — and is
    distinct from the `continued_into` CHAIN chase's own separate depth cap,
    `_MAX_CONTINUATION_CHASE_DEPTH`; the two traversals walk different edge
    kinds and are bounded independently).
  - Does NOT chase a `continued_into` chain past its depth cap, or around a
    cycle — both SURFACE rather than looping (C5).
  - Does NOT resolve a path-shaped `continued_into` value by reading the
    filesystem — this module performs no I/O; a path fallback resolves ONLY
    against whatever path-shaped field the caller's collector already
    attached to its in-memory handoff dicts (`get_by_path`, basename-matched),
    and an unmatched path is `unresolved` -> SURFACE, never treated as a clear.
  - Does NOT stamp a chased clear's terminus id into `cleared_blocker_ids`/
    `cleared_by_shas` — those arrays stay 1:1-paired against the ORIGINAL
    `blocked_by` id (`handoff_transition.py`'s `gate-cascade-clear` verb
    requires this pairing and validates blocker-id membership against the
    handoff's own `blocked_by`); the terminus is named only in `evidence` text.
  - Does NOT guess a prose `gate_dependency` witness when more than one candidate
    matches, or when no concrete pointer is given — surfaces instead.
  - Does NOT perform sibling I/O for `gate_evidence` legs — every I/O-kind leg
    arrives caller-pre-resolved via `coordinator_core.sibling_fact.resolve_leg`;
    this module applies only the verdict predicate over the resolved observation.
  - Does NOT read the system clock for `kind: deadline` legs — `elapsed` is
    caller-computed and handed in, never derived from `datetime.now()` here.
  - Does NOT let a partial `gate_evidence` backfill silently free a prose gate
    — `covers_prose: True` must be explicitly asserted by the leg author.
  - Does NOT invent a third verdict class for the external-gate case (C6) —
    `test-node-id`/`probe-op-key`/`commit-sha`/`sibling-commitment-ref` legs
    are a NEW WITNESS SOURCE feeding the existing prose-resolution mechanism
    (`evaluate_gate`'s `covers_prose` branch), never a parallel evaluator or
    a new field on the return dict.
  - Does NOT re-verify a `test-node-id`/`probe-op-key`/`commit-sha`/
    `sibling-commitment-ref` leg itself, and does NOT validate that the leg
    carries a `repo:` qualifier — both are the caller's job (mirroring
    `sibling_fact.resolve_leg`'s existing required `repo` field for the
    other three I/O kinds, and `cutover_gate.py`'s `_reverify_*` family for
    re-verification semantics); this module only applies the boolean-observed
    predicate over what the caller already resolved.
  - Does NOT wire a no-prose, `gate_evidence`-only witness into `evaluate_gate`
    (C6 scope is the prose-gate witness source only, matching the oaxis-01
    motivating shape) — `evaluate_gate_triage` already supports that
    combination; `evaluate_gate` does not, pending a real driving fixture.
  - Does NOT read or walk `predecessor`, `additional_predecessors`, `forked_from`,
    or any `origin_*` field to infer, narrow, or clear a gate — the resolver's
    input set is `blocked_by` and nothing else (see "LINEAGE IS NOT GATING"
    above). Never writes any of those fields either.
  - Does NOT let `evaluate_gate` clear a handoff carrying a non-empty prose
    `gate_dependency` alongside a non-empty `blocked_by`, even when every
    structured member has shipped — prose dominance (C4 RECONCILIATION
    above) mirrors `evaluate_gate_triage`'s own precedence rule; the mutating
    path narrows/surfaces instead, never silently clears.
  - Does NOT let either evaluator clear/free a handoff whose `blocked_by` is
    EMPTY and whose `blocking_notes` is non-empty (see "BLOCKING_NOTES
    DOMINANCE" above) — the vacuous-clear/vacuous-freed reading is refused in
    that case only. Does NOT let a non-empty `blocking_notes` override a
    SATISFIED, non-empty `blocked_by` graph — unlike `gate_dependency`,
    `blocking_notes` is not consulted at all once `blocked_by` is non-empty;
    the structured walk's own verdict (clear/narrow/surface, freed/
    still-blocked/indeterminate) stands regardless of `blocking_notes`. Does
    NOT machine-resolve `blocking_notes` in either case — no witness lookup,
    no cascade, no attempt to parse a baton out of it; it is an opaque human
    signal, dominance (where it applies) without resolvability.
  - Does NOT gate `evaluate_gate`'s structured-path eligibility on `kind`
    (C4) — any handoff kind with a non-empty `blocked_by` is eligible; only
    `_has_asymmetry`'s back-reference check remains kind-scoped (roadmap-kind
    blockers only), to avoid a false-positive fire on a blocker kind that
    never adopted the `blocks:` convention.
  - Does NOT treat an unfilled `coordinator-doc-new` scaffold placeholder in
    `gate_dependency`/`blocking_notes` as "no gate" — a `PLACEHOLDER`-
    sentinel value never routes to `clear`/`freed`; it gets its own
    `surface`/`indeterminate` branch with a distinct evidence line, checked
    ahead of every other rule (C2, see "C2 SCAFFOLD SENTINEL" above).
  - Does NOT match `PLACEHOLDER` as a substring — `_is_scaffold_sentinel` is
    a PREFIX test on the value's stripped form; authored prose that merely
    contains the word "placeholder" in a real sentence keeps ordinary
    dominance (AC2.3).
  - Does NOT assert staleness (C3) it cannot substantiate — the "every
    structured blocker has since shipped" evidence addendum on rules 1/1a
    fires ONLY when `_all_blocked_by_shipped_evidence` positively confirms
    every `blocked_by` member via the existing shipped-state predicate/
    index; it never changes the verdict, and a single unshipped/unresolved/
    dead/disposed member yields no addendum at all (see "C3 STALENESS
    EVIDENCE ON DOMINANCE" above).

C7 AC8 — `resolved_without_baton` DISPOSITION (docs/plans/2026-07-13-claude-klabauter-
auto-reconcile-open-handoffs.md § C7; live-corpus re-census 2026-07-27 pinned
this to 4 unique dangling ids across 742 files, all four verified — via git
history, not this module — as shipped-then-pruned by the keep-10 archive
window, not genuinely-never-existed): a dangling `blocked_by` id is, absent
any disposition, a loud finding on EVERY pass over the same gated handoff —
correct the first time, noise on the fiftieth. This module now recognizes an
OPERATOR-AUTHORED disposition, `handoff.blocked_by_dispositions[blocker_id]
== {"disposition": "resolved_without_baton", "reason": str}`, read off the
GATED handoff's own frontmatter (parallel to `gate_dependency`/
`blocking_notes` already living there) — never inferred, never derived, and
never stamped by this module itself (COMPUTE_ONLY holds; the stamp is an
operator hand-edit, exactly as the asymmetry/dangling-ref findings above are
themselves never auto-repaired).

Semantics (`_resolved_without_baton_reason`, threaded into `_classify_blocked_
by` as a SIXTH bucket, `disposed_ids`, split out of `unresolved_ids`):
  - A disposed id's evidence line is quiet (names the disposition and its
    reason) instead of `_unresolved_reason`'s loud "dangling blocked_by
    ref(s)" line, and is excluded from `also_surface`/`indeterminate`
    triggers — the operator already looked at this edge; re-flagging it
    every pass is exactly the noise this AC exists to stop.
  - A disposed id is STILL folded into `remaining_blockers`
    (`evaluate_gate`) / still counts toward a non-`freed` status
    (`evaluate_gate_triage`) — critical: the disposition records WHY the ref
    will never resolve, it does NOT assert the blocked-on work shipped, so it
    must never by itself flip a gate to `clear`/`freed`. `evaluate_gate`
    reaches this by construction (disposed ids only ever ADD to
    `remaining_blockers`, never subtract from it, so the pre-existing
    "`if not remaining_blockers: clear`" branch cannot fire on a
    disposition-only remainder); `evaluate_gate_triage` gets an explicit
    disposed-only branch -> `still-blocked` (never `freed`) for the same
    reason.
  - Only the EXACT literal `"resolved_without_baton"` is honored — a missing,
    misspelled, or different `disposition` value is treated as NO
    disposition, so a genuinely dangling AND undispositioned slug still
    fails loud exactly as before (the AC8 requirement: "still FAILS LOUD on
    a slug that is dangling AND has no disposition"). This is a widening of
    what a per-blocker-id record can quiet, not a weakening of the default
    dangling-ref treatment.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

from coordinator_core.frontmatter.baton_class import canonical_kind
from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT

#: deployment_state values that stop re-evaluation of a blocker edge (mirrors the
#: live `deployment_state` enum's terminal subset). `abandoned`/`continued`/`closed`
#: are terminal but do NOT clear a gate — see CLEAR predicate (rule 1).
#: SSOT: coordinator_core.lifecycle_constants.HANDOFF_TERMINAL_DEPLOYMENT.
_TERMINAL_STATES: frozenset = HANDOFF_TERMINAL_DEPLOYMENT

_SHIPPED_STATE = "shipped"
_ABANDONED_STATE = "abandoned"
#: DR-084 dual-vocabulary, intentionally permanent: `abandoned` (old) sits
#: alongside `continued`/`closed` (new). All three are terminal-but-not-shipped:
#: rule (2) treats any of them identically (never clears, always surfaces) —
#: dual-tolerant read, no write path in this module. See
#: coordinator_core/lifecycle_constants.py module docstring for the exit
#: condition (9d00b459 is the incident of record).
_NON_SHIPPED_TERMINAL_STATES: frozenset = frozenset(
    {_ABANDONED_STATE, "continued", "closed"}
)


#: Durable `handoff_id` shape (see `handoff_transition.py::_resolve_blocker_
#: deployment_state`, the act-time mutating resolver this compute-time index must
#: agree with): `hnd-<slug>-<6-hex>`. Pattern-fenced, so a `blocked_by` entry in
#: this shape is discriminable WITHOUT a lookup — it can only ever be a durable
#: `handoff_id`, never a `stub_id`/path-stem `id` (those never start with `hnd-`
#: followed by a 6-hex suffix; verified against today's `sat`/`qsub`/`strang`
#: roadmap families, none of which use this shape).
#: Review: code-reviewer (Finding 1, P1) — negative lookahead excludes
#: placeholder-derived ids. A scaffold-minted id (`hnd-placeholder-replace-with-...-<6-hex>`)
#: is otherwise well-formed and would resolve against a `blocked_by` entry,
#: silently clearing it instead of leaving it dangling — the false-clear class
#: `handoff.schema.json`'s narrow closes only at schema-validation time, not at
#: this compute-time resolver. See
#: cross-repo/inbox/2026-08-05-doe-claude-em-placeholder-id-minting-fix-unfiled.md.
_HANDOFF_ID_PATTERN = re.compile(r"^hnd-(?!placeholder-replace-with)[a-z0-9-]+-[0-9a-f]{6}$")


def _path_basename(path: Any) -> Optional[str]:
    """Last path segment (POSIX or Windows separators), or None for a non-string.

    Deliberately not `os.path.basename`/`pathlib` — this module performs no
    filesystem access whatsoever (COMPUTE_ONLY, see module docstring) and a
    plain string split is sufficient for a path-shaped frontmatter VALUE (never
    an actual filesystem path this module opens).
    """
    if not isinstance(path, str) or not path:
        return None
    return path.replace("\\", "/").rsplit("/", 1)[-1]


class _TypedHandoffIndex:
    """Prefix-discriminated two-index resolver: durable `handoff_id` vs `stub_id`/`id`.

    Mirrors `handoff_transition.py::_resolve_blocker_deployment_state`'s act-time
    match (`blocker_id in (fm_dict.get("stub_id"), fm_dict.get("handoff_id"))`) at
    compute time — the two resolvers must agree, or the gate index reads a baton
    as permanently dangling while the mutating path resolves it fine (the defect
    this class fixes). Deliberately NOT a single widened flat dict: a `stub_id`
    namespace (unprefixed per-roadmap-family, e.g. `"01"`) and a `handoff_id`
    namespace (globally-unique, `hnd-`-prefixed) are two different key spaces: the
    former is already a documented collision hazard (`_index_by_id`, two families
    sharing an unprefixed stub_id → last-write-wins), and merging a THIRD key
    source (`handoff_id`) into that hazard would let a durable id collide with an
    unrelated stub_id and silently resolve the wrong handoff's `deployment_state`
    (a false clear or false block, no error, no surface). Keeping the two
    namespaces in separate dicts and routing lookups by the query key's own shape
    avoids that collision entirely — a `hnd-...` key can never mean a stub_id, so
    it never competes for the same slot.
    """

    __slots__ = ("_by_handoff_id", "_by_stub_id", "_by_path_basename")

    def __init__(
        self,
        by_handoff_id: Dict[str, Dict[str, Any]],
        by_stub_id: Dict[str, Dict[str, Any]],
        by_path_basename: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        self._by_handoff_id = by_handoff_id
        self._by_stub_id = by_stub_id
        self._by_path_basename = by_path_basename or {}

    def get(
        self, key: str, default: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        if isinstance(key, str) and _HANDOFF_ID_PATTERN.match(key):
            return self._by_handoff_id.get(key, default)
        return self._by_stub_id.get(key, default)

    def get_by_path(
        self, path: str, default: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """Resolve a `state/handoffs/...`-shaped path reference (C5's
        `continued_into` path fallback — pre-existing successors authored before
        `handoff_id` backfill carry no durable id, only this path shape).

        Matched by BASENAME, not full-string equality: this module is
        COMPUTE_ONLY (no filesystem access, no worktree-root resolution — see
        module docstring negative-spec), so it never re-reads the path off
        disk. The caller-supplied index only carries whatever path-shaped
        field a collector happened to attach. Both collectors now do:
        `_collect_open_handoffs` sets `_path` for the LIVE set, and
        `_collect_all_handoffs_for_gate_index` sets it for the live AND
        archived entries it returns, so an archived basename resolves here
        rather than falling through unindexed as it once did.
        A `continued_into` value is authored worktree-relative
        (`state/handoffs/<file>.md`); comparing it byte-for-byte against an
        absolute `_path` would never match. Handoff filenames are
        timestamp+slug unique, so a basename match is a safe, conservative
        proxy — never a guess between multiple candidates (ambiguous
        basenames are simply not indexed, see `_index_by_id`).
        """
        return self._by_path_basename.get(_path_basename(path), default)


def _index_by_id(handoffs: Sequence[Dict[str, Any]]) -> "_TypedHandoffIndex":
    """Build a durable-id -> handoff-dict index over the live+archived union.

    Purpose: the structured path resolves `blocked_by` ids against this index
    rather than against file paths, so a blocker that has already archived (shipped)
    is still resolvable (durable-id survives the state/handoffs/ -> archive/handoffs/
    move). Handoffs missing both an indexable `handoff_id` and a `stub_id`/`id` are
    simply not indexed (unresolvable as a blocker target — surfaced by the caller
    when referenced).

    Two typed sub-indices, not one flat dict (see `_TypedHandoffIndex`): a
    `handoff_id` field matching `_HANDOFF_ID_PATTERN` is indexed into
    `by_handoff_id`; `id` (falling back to the collector's path-stem synthesis) or
    `stub_id` is indexed into `by_stub_id`. A handoff carrying both is indexed into
    both — this mirrors the act-time resolver's `stub_id, handoff_id` OR-match
    rather than picking one field to trust.

    Last-write-wins on a duplicate key WITHIN a sub-index: `by_stub_id` is still a
    flat dict keyed on `id or stub_id`, with no cross-roadmap-family namespace
    guard. Two roadmap families that both happen to use an unprefixed `stub_id`
    (e.g. two families both naming a stub `"01"`) would collide, and whichever
    handoff is iterated last silently wins the slot (2026-07-20 claude-central-em
    false-positive memo, Defect 1 edge case). Today's `sat`/`qsub`/`strang`
    families are already prefixed and do not collide in practice — this is a
    documented risk, not an observed defect. `by_handoff_id` carries the same
    last-write-wins shape in theory, but `handoff_id` is asserted durable-unique
    elsewhere (see `handoff_transition.py`'s duplicate-id ambiguity guard) —
    a real collision there is a data-integrity defect this index does not detect.
    """
    by_handoff_id: Dict[str, Dict[str, Any]] = {}
    by_stub_id: Dict[str, Dict[str, Any]] = {}
    by_path_basename: Dict[str, Dict[str, Any]] = {}
    for h in handoffs:
        handoff_id = h.get("handoff_id")
        if isinstance(handoff_id, str) and _HANDOFF_ID_PATTERN.match(handoff_id):
            by_handoff_id[handoff_id] = h
        hid = h.get("id") or h.get("stub_id")
        if isinstance(hid, str) and hid:
            by_stub_id[hid] = h
        # C5 continued_into path-fallback support (`get_by_path`): index
        # whatever path-shaped field the caller's collector attached
        # (`_path` for the live set; a generic `path` fallback for any other
        # caller shape). Ambiguous basenames intentionally collide
        # last-write-wins here, same posture as `by_stub_id` above — ambiguity
        # is a caller data-collision, not this index's job to disambiguate.
        basename = _path_basename(h.get("_path")) or _path_basename(h.get("path"))
        if basename:
            by_path_basename[basename] = h
    return _TypedHandoffIndex(by_handoff_id, by_stub_id, by_path_basename)


def _blocker_deployment_state(blocker: Optional[Dict[str, Any]]) -> Optional[str]:
    """Return a resolved blocker's `deployment_state`, or None when unresolved."""
    if blocker is None:
        return None
    state = blocker.get("deployment_state")
    return state if isinstance(state, str) else None


def _has_asymmetry(
    handoff: Dict[str, Any],
    blocked_by_ids: Sequence[str],
    all_index: "_TypedHandoffIndex",
) -> bool:
    """Rule (4): detect `blocks`/`blocked_by` asymmetry against each resolved blocker.

    For every blocker that resolves in `all_index`, its own `blocks:[...]` list must
    name this handoff's id back — otherwise the graph is inconsistent (a data defect)
    and the caller must fail loud (surface), never auto-repair.

    Identity is tested against a CANDIDATE SET, not a single key (2026-07-20
    claude-central-em false-positive memo, Defect 1): `_index_by_id` keys the index
    on `id or stub_id` (:101), but a caller like `_collect_open_handoffs` may inject
    a path-stem `id` for stubs that carry no `id:` frontmatter of their own — while
    a `blocks:[...]` list is authored against the durable `stub_id`. Comparing the
    handoff's single `id` field against `blocker_blocks` (a `stub_id` namespace) can
    never match in that case, firing false asymmetry on every symmetric edge. Testing
    `{stub_id, id} & blocker_blocks` mirrors the index's own dual-key fallback rather
    than inventing a third convention. Does NOT normalise `id` at ingestion — that
    would change the `handoff_id` DoE's renderer keys on in `surfaced[]` (riskier).
    """
    handoff_ids = {v for v in (handoff.get("stub_id"), handoff.get("id")) if isinstance(v, str) and v}
    if not handoff_ids:
        return False
    for blocker_id in blocked_by_ids:
        blocker = all_index.get(blocker_id)
        if blocker is None:
            continue
        if canonical_kind(blocker.get("kind")) != "roadmap-baton":
            # C4 widened-eligibility guard: the blocks:/blocked_by symmetry
            # check is a ROADMAP-kind authoring convention (a roadmap blocker
            # is expected to list every handoff it blocks). A blocker of any
            # OTHER kind was never expected to maintain that back-reference
            # at all — widening `evaluate_gate`'s eligibility to ANY kind
            # would otherwise fire a false-positive asymmetry on every such
            # edge (a blocker simply not authoring `blocks:` reads
            # identically to one that authored it wrong). Only a
            # roadmap-kind blocker is held to the convention.
            continue
        blocker_blocks = blocker.get("blocks") or []
        if not isinstance(blocker_blocks, list):
            continue
        if not (handoff_ids & set(blocker_blocks)):
            return True
    return False


def _unresolved_reason(
    unresolved_ids: List[str], scan_incomplete: bool, scan_errors: Sequence[str]
) -> str:
    """Evidence text for a `blocked_by` id absent from the live+archived index.

    # --- Tier 2 (behaviour change -- PM sign-off required) ---
    Absence from the index is ambiguous: either the id is a genuine dangling ref
    (data defect), or the archive/handoffs/ subtree behind the index could not be
    fully scanned (`handoff_reconcile.py`'s `scan_incomplete`/`scan_errors`, added
    94d8251f) and the id's handoff simply wasn't seen. Asserting "dangling ref —
    data defect" when the true cause is a scan gap is itself misleading to the EM
    reading `evidence` — the caller ask (2026-07-22) is that this case must read
    as "can't confirm", not as a confirmed defect, whenever `scan_incomplete` is
    True. Verdict/clearing behaviour is unchanged either way (still surface/
    narrow+surface, never clear) — only the reason text differs.
    """
    if scan_incomplete:
        return (
            f"cannot confirm blocked_by id(s) {unresolved_ids} resolve — archive "
            f"scan incomplete, unscannable subtree(s): {list(scan_errors)}; NOT "
            f"treated as a confirmed data defect while the scan is incomplete"
        )
    return (
        f"dangling blocked_by ref(s) — blocker id(s) unresolvable in "
        f"live+archived index: {unresolved_ids}"
    )
    # --- end Tier 2 ---


#: C7 AC8 — the only disposition value this module honors. See module
#: docstring "C7 AC8" for the full contract.
_RESOLVED_WITHOUT_BATON = "resolved_without_baton"


def _resolved_without_baton_reason(
    blocker_id: str, dispositions: Optional[Dict[str, Any]]
) -> Optional[str]:
    """Operator-authored reason for `blocker_id`'s `resolved_without_baton`
    disposition, or `None` if none is recorded (or the record doesn't match
    the exact recognized shape — see module docstring "C7 AC8").

    `dispositions` is the GATED handoff's own `blocked_by_dispositions` dict
    (never derived, never auto-populated by this module — an operator hand-
    edit), threaded in by `_evaluate_structured_gate`/`evaluate_gate_triage`
    from `handoff.get("blocked_by_dispositions")`. Deliberately strict: only
    `entry["disposition"] == "resolved_without_baton"` exactly is honored —
    anything else (missing key, typo, a future disposition vocabulary this
    module doesn't yet recognize, a non-dict entry) returns `None`, so the
    caller falls back to treating `blocker_id` as an ordinary unresolved
    (loud) dangling ref. A missing `reason` string still honors the
    disposition (the marker itself is what stops re-surfacing) but is called
    out in the returned text so a reason-less disposition is visibly weaker
    evidence than an authored one.
    """
    if not isinstance(dispositions, dict):
        return None
    entry = dispositions.get(blocker_id)
    if not isinstance(entry, dict):
        return None
    if entry.get("disposition") != _RESOLVED_WITHOUT_BATON:
        return None
    reason = entry.get("reason")
    return reason if isinstance(reason, str) and reason.strip() else "no reason authored"


_CONTINUED_STATE = "continued"

#: C5 continued_into chase depth cap. `continued` is authored as a one-hop
#: redirect in every corpus instance observed (lvv-05 -> its dr084 successor,
#: one hop) — this cap exists purely as a defensive bound against a pathological
#: multi-hop chain (each hop itself re-continued), not because multi-hop chains
#: are expected or supported by any authoring convention. Exceeding it surfaces
#: (rule: chain exceeds depth cap -> SURFACE, do not loop) rather than raising,
#: matching this module's conservative-never-guess posture everywhere else.
_MAX_CONTINUATION_CHASE_DEPTH = 8


def _resolve_continuation_target(
    continued_into: Any, all_index: "_TypedHandoffIndex"
) -> Optional[Dict[str, Any]]:
    """Resolve one `continued_into` hop against the caller-supplied index.

    `continued_into` is `handoff_id`-shaped (`hnd-<slug>-<6hex>`) preferred —
    resolved via `_TypedHandoffIndex.get`, the SAME lookup C2c's typed index
    already gives every other `blocked_by`/`blocks` id in this module. A
    non-`hnd-...`-shaped value is treated as the documented PATH fallback
    (pre-existing successors authored before `handoff_id` backfill carry no
    durable id, only a `state/handoffs/...` path) and resolved EXACTLY ONCE via
    `get_by_path` — never re-read as a live filesystem path (this module is
    COMPUTE_ONLY and performs no I/O; see module docstring negative-spec). A
    path-shaped value that does not resolve in the index returns None, which
    the caller (`_chase_continuation`) treats as `unresolved` — SURFACE, never
    a clear; a dangling `continued_into` is worse than useless to trust.
    """
    if not isinstance(continued_into, str) or not continued_into:
        return None
    if _HANDOFF_ID_PATTERN.match(continued_into):
        return all_index.get(continued_into)
    return all_index.get_by_path(continued_into)


def _chase_continuation(
    blocker_id: str,
    blocker: Dict[str, Any],
    all_index: "_TypedHandoffIndex",
) -> Dict[str, Any]:
    """Follow a `continued` blocker's `continued_into` chain to its terminus.

    `continued` is terminal-on-the-deployment-axis but is NOT discharge (module
    docstring rule 2) — this function is the "what actually happened at the far
    end of the redirect" lookup that lets a chased-through blocker still clear
    a gate when, and only when, its terminus genuinely shipped.

    Returns a dict: `{"outcome": "shipped"|"open"|"cycle"|"depth_cap"|
    "unresolved", "sha": Optional[str], "chain_ids": [blocker_id, ...],
    "evidence": [str, ...]}`.

    - terminus `shipped` -> outcome="shipped", `sha` is the TERMINUS's own
      `shipped_in` (the blocker itself, being `continued` not `shipped`, has no
      `shipped_in` of its own to report) — the edge clears.
    - terminus reached but not shipped (any other deployment_state, including
      a DIFFERENT terminal state like `abandoned`/`closed`) -> outcome="open".
      This is the lvv-05 case: SURFACE, never clear.
    - chain exceeds `_MAX_CONTINUATION_CHASE_DEPTH` hops -> outcome="depth_cap"
      -> SURFACE, does not loop further.
    - chain revisits a `continued_into` target already seen in THIS chase ->
      outcome="cycle" -> SURFACE, reusing the same visited-set discipline rule
      (5)'s one-level walk already applies to `blocked_by` (a distinct
      visited-set scoped to this chase, not `_evaluate_structured_gate`'s
      per-handoff walk guard — chasing a chain is a different traversal than
      walking sibling `blocked_by` edges, and the two guards must not be
      conflated or a legitimate diamond-shaped `blocked_by` graph would
      false-fire this chase's cycle guard).
    - `continued_into` missing, or resolves nowhere in the index (handoff_id
      unresolvable, or path fallback basename unmatched) -> outcome="unresolved"
      -> SURFACE. A dangling redirect is never trusted as a clear.

    Provenance (Failure mode 1, C5 brief): `evidence` names BOTH hops
    explicitly (`"{blocker_id} continued -> chased to terminus {terminus_id}
    shipped (shipped_in=...)"`) even though `sha` carries only the terminus's
    own SHA — the caller's `cleared_by_shas`/`cleared_blocker_ids` arrays stay
    1:1-paired against the ORIGINAL `blocked_by` id (`handoff_transition.py`'s
    `gate-cascade-clear` verb requires `len(blocker_ids) == len(blocker_shas)`
    and validates every `blocker_id` is a member of the handoff's OWN
    `blocked_by` — substituting the terminus id into that array would break
    both invariants). Recording which id actually discharged the gate is
    therefore an evidence-text responsibility, not an array-shape one; do NOT
    "fix" this by inserting the terminus id into `cleared_blocker_ids`. A
    terminus that resolves `outcome="shipped"` with `sha=None` (C6) does not
    itself keep those two arrays 1:1 — `_classify_blocked_by` is what
    preserves the pairing, by routing a `sha=None` shipped outcome into its
    own `unstamped_shipped_ids` bucket instead of appending it to either
    array at all.
    """
    chain_ids: List[str] = [blocker_id]
    visited = {blocker_id}
    evidence: List[str] = []
    current = blocker
    current_id = blocker_id
    depth = 0

    while True:
        state = _blocker_deployment_state(current)
        if state == _SHIPPED_STATE:
            sha = current.get("shipped_in")
            sha = sha if isinstance(sha, str) and sha else None
            evidence.append(
                f"{blocker_id} continued -> chased to terminus {current_id} "
                f"shipped (shipped_in={sha!r})"
            )
            return {
                "outcome": "shipped",
                "sha": sha,
                "chain_ids": chain_ids,
                "evidence": evidence,
            }
        if state != _CONTINUED_STATE:
            evidence.append(
                f"{blocker_id} continued -> chased to terminus {current_id}, "
                f"still {state!r} (not shipped) — never clears the gate (rule 2)"
            )
            return {
                "outcome": "open",
                "sha": None,
                "chain_ids": chain_ids,
                "evidence": evidence,
            }

        depth += 1
        if depth > _MAX_CONTINUATION_CHASE_DEPTH:
            evidence.append(
                f"{blocker_id} continued_into chain exceeds depth cap "
                f"({_MAX_CONTINUATION_CHASE_DEPTH} hops) — surfaced, not chased "
                f"further"
            )
            return {
                "outcome": "depth_cap",
                "sha": None,
                "chain_ids": chain_ids,
                "evidence": evidence,
            }

        continued_into = current.get("continued_into")
        next_handoff = _resolve_continuation_target(continued_into, all_index)
        if next_handoff is None:
            evidence.append(
                f"{blocker_id} continued_into={continued_into!r} does not "
                f"resolve in the live+archived index — surfaced, never cleared"
            )
            return {
                "outcome": "unresolved",
                "sha": None,
                "chain_ids": chain_ids,
                "evidence": evidence,
            }

        next_id = (
            next_handoff.get("handoff_id")
            or next_handoff.get("id")
            or next_handoff.get("stub_id")
            or continued_into
        )
        if next_id in visited:
            evidence.append(
                f"cycle detected chasing {blocker_id}'s continued_into chain — "
                f"{next_id!r} already visited in this chase — surfaced, not "
                f"chased further"
            )
            return {
                "outcome": "cycle",
                "sha": None,
                "chain_ids": chain_ids,
                "evidence": evidence,
            }
        visited.add(next_id)
        chain_ids.append(next_id)
        current = next_handoff
        current_id = next_id


def _classify_blocked_by(
    blocked_by_ids: Sequence[str],
    all_index: "_TypedHandoffIndex",
    dispositions: Optional[Dict[str, Any]] = None,
) -> "tuple[List[str], List[str], List[str], List[str], List[str], List[str], List[str], List[str]]":
    """Classify every `blocked_by` id against the live+archived index into
    seven buckets, shared by `_evaluate_structured_gate` (C3's clear/narrow/
    surface four-way) and `evaluate_gate_triage` (the freed/still-blocked/
    indeterminate three-way) — ONE classification pass, two different verdict
    projections over it, per the "exactly one gate evaluator" constraint this
    module is extended under.

    Returns (shipped_ids, shipped_shas, abandoned_ids, unresolved_ids,
    still_open_ids, disposed_ids, unstamped_shipped_ids, evidence).
    `abandoned_ids` here means "resolved to a `_NON_SHIPPED_TERMINAL_STATES`
    member" (abandoned/continued/closed — DR-084 dual-vocabulary), not
    literally the old `abandoned` token alone — and, since C6, never a
    `shipped` id regardless of whether it carries a `shipped_in`: a `shipped`
    terminus (direct or chased) is either paired into `shipped_ids`/
    `shipped_shas` (has a sha) or routed into `unstamped_shipped_ids` (does
    not); it is never a member of `abandoned_ids`.

    `dispositions` (C7 AC8, module docstring "C7 AC8"): the gated handoff's
    own `blocked_by_dispositions` dict, or `None`. `disposed_ids` is a SIXTH
    bucket split out of what would otherwise be `unresolved_ids` — an id
    absent from `all_index` (dangling) whose disposition resolves via
    `_resolved_without_baton_reason`. Disposed ids get a quiet evidence line
    (not `_unresolved_reason`'s loud one) and are never counted toward
    `also_surface`/`indeterminate`; they are still returned as their own
    bucket so callers fold them into `remaining_blockers`/a non-freed status
    — a disposition explains why a ref won't resolve, it never asserts the
    blocked-on work shipped, so it must never by itself clear/narrow/free a
    gate.

    `unstamped_shipped_ids` (C6) is a SEVENTH bucket, split out for the
    identical reason `disposed_ids` was split out of `unresolved_ids`: a
    differently-caused non-clearing id needs its own evidence line, not
    inherited wording from a bucket whose documented meaning it does not
    share. A `blocked_by` member (direct, or via a `continued` chase to its
    terminus) whose `deployment_state` reads `shipped` but carries no
    `shipped_in` has no clearing provenance to record in `shipped_ids`/
    `shipped_shas` — the 1:1-paired arrays `_gate_cascade_clear` consumes —
    so it is excluded from those two arrays entirely and returned here
    instead. Every caller folds `unstamped_shipped_ids` into its own
    non-clearing/non-freed result (mirroring `disposed_ids`'s treatment);
    never into `abandoned_ids`/`dead_ids`, whose documented meaning
    ("resolved to a non-shipped terminal state") it does not share.
    """
    shipped_ids: List[str] = []
    shipped_shas: List[str] = []
    abandoned_ids: List[str] = []
    unresolved_ids: List[str] = []
    still_open_ids: List[str] = []
    disposed_ids: List[str] = []
    unstamped_shipped_ids: List[str] = []
    evidence: List[str] = []

    for blocker_id in blocked_by_ids:
        blocker = all_index.get(blocker_id)
        if blocker is None:
            reason = _resolved_without_baton_reason(blocker_id, dispositions)
            if reason is not None:
                disposed_ids.append(blocker_id)
                evidence.append(
                    f"{blocker_id} unresolvable in live+archived index but carries a "
                    f"recorded resolved_without_baton disposition ({reason!r}) — "
                    "operator-asserted, does not clear the gate, and is excluded "
                    "from the unresolvable-ref finding on every subsequent pass"
                )
            else:
                unresolved_ids.append(blocker_id)
            continue
        state = _blocker_deployment_state(blocker)
        if state == _SHIPPED_STATE:
            sha = blocker.get("shipped_in")
            if isinstance(sha, str) and sha:
                shipped_ids.append(blocker_id)
                shipped_shas.append(sha)
                evidence.append(f"{blocker_id} shipped (shipped_in={sha!r})")
            else:
                # C6: a `shipped` terminus with no `shipped_in` has no
                # clearing provenance to record in the paired arrays — it
                # surfaces via the seventh bucket instead of entering
                # `shipped_ids` unconditionally (the original defect).
                unstamped_shipped_ids.append(blocker_id)
                evidence.append(
                    f"{blocker_id} shipped but carries no shipped_in — never "
                    "clears the gate; stamp shipped_in via handoff.stamp "
                    "kind: no-commit with the sanctioned "
                    "substantively-shipped-no-commit:<YYYY-MM-DD> token when "
                    "there genuinely is no ship commit"
                )
        elif state == _CONTINUED_STATE:
            # C5: `continued` is terminal-but-not-discharge (rule 2) — but
            # unlike `abandoned`/`closed`, it carries a `continued_into`
            # pointer to where the work actually went. Chase it to its
            # terminus before deciding: a `shipped` terminus DOES clear this
            # edge (with both hops named in evidence, never in the
            # id/sha arrays — see `_chase_continuation`'s docstring); any
            # other outcome (still open, depth cap, cycle, unresolved
            # redirect) falls back to the exact same non-clearing treatment
            # `abandoned`/`closed` already get. Act-time re-verification is
            # unaffected — `handoff_transition.py`'s
            # `_resolve_blocker_deployment_state` re-checks the blocker (and,
            # per this same rule, must independently re-chase) at mutation
            # time regardless of what this compute-time pass concluded.
            chase = _chase_continuation(blocker_id, blocker, all_index)
            evidence.extend(chase["evidence"])
            if chase["outcome"] == "shipped":
                sha = chase["sha"]
                if isinstance(sha, str) and sha:
                    shipped_ids.append(blocker_id)
                    shipped_shas.append(sha)
                else:
                    # C6: same rule as the plain-shipped branch above, for a
                    # `continued` blocker whose chased terminus is shipped
                    # with no `shipped_in` of its own.
                    unstamped_shipped_ids.append(blocker_id)
                    evidence.append(
                        f"{blocker_id} continued -> chased terminus shipped "
                        "but carries no shipped_in — never clears the gate; "
                        "stamp shipped_in via handoff.stamp kind: no-commit "
                        "with the sanctioned "
                        "substantively-shipped-no-commit:<YYYY-MM-DD> token "
                        "when there genuinely is no ship commit"
                    )
            else:
                abandoned_ids.append(blocker_id)
        elif state in _NON_SHIPPED_TERMINAL_STATES:
            abandoned_ids.append(blocker_id)
            evidence.append(
                f"{blocker_id} {state} — never clears the gate (rule 2)"
            )
        else:
            still_open_ids.append(blocker_id)
            evidence.append(f"{blocker_id} not yet terminal (deployment_state={state!r})")

    return (
        shipped_ids,
        shipped_shas,
        abandoned_ids,
        unresolved_ids,
        still_open_ids,
        disposed_ids,
        unstamped_shipped_ids,
        evidence,
    )


def _is_structured_gate(handoff: Dict[str, Any]) -> bool:
    """Shared routing/eligibility predicate for the STRUCTURED `blocked_by`
    path — used identically by BOTH `evaluate_gate` (mutating four-way path)
    and `evaluate_gate_triage` (reporting three-way projection), per the
    "exactly one gate evaluator" constraint (module docstring TRIAGE
    PROJECTION / C4 RECONCILIATION): two evaluators independently deciding
    structured-vs-prose eligibility is the sibling-evaluator error this
    module exists to avoid.

    True iff `handoff` carries a non-empty `blocked_by` list. Kind-
    independent (widened from the pre-C4 `kind == "spinoff-roadmap"`
    restriction) — ANY handoff kind with a real `blocked_by` graph edge is
    eligible for the structured walk, not only `spinoff-roadmap`.

    Purely an ELIGIBILITY test over `blocked_by` — it does NOT itself decide
    prose-dominance. Eligibility and precedence are different questions: a
    handoff can be structured-eligible (non-empty `blocked_by`) and still
    carry a live prose `gate_dependency` that must dominate the structured
    outcome — see `evaluate_gate`'s and `evaluate_gate_triage`'s own
    `_has_prose_gate` precedence checks, applied by the caller around this
    predicate, never folded into it.
    """
    return bool(handoff.get("blocked_by") or [])


def _evaluate_structured_gate(
    handoff: Dict[str, Any],
    all_index: "_TypedHandoffIndex",
    visited: set,
    scan_incomplete: bool = False,
    scan_errors: Sequence[str] = (),
) -> Dict[str, Any]:
    """Evaluate the STRUCTURED `blocked_by` path for one spinoff-roadmap handoff.

    One-level walk (rule 5): only this handoff's direct `blocked_by` edges are
    resolved and evaluated; a blocker that itself has unshipped dependents is not
    recursively walked in this invocation (bounded to keep each event auditable to
    one gate-eval pass, mirroring the cascade-gates one-level-per-ship-event design).

    `scan_incomplete`/`scan_errors` (threaded from `handoff_reconcile.py`'s C3 gate
    index collector, 94d8251f): when True, this call's `all_index` may be missing
    entries under an unreadable archive/handoffs/ subtree. A blocker that resolves
    positively (found in `all_index`, terminal state) is unaffected — finding a
    thing proves it exists regardless of what else the scan missed. A blocker that
    does NOT resolve is where the ambiguity lives — see `_unresolved_reason`.
    """
    handoff_id = handoff.get("id", "<unknown>")
    blocked_by_ids: List[str] = [b for b in (handoff.get("blocked_by") or []) if isinstance(b, str)]

    evidence: List[str] = []

    if handoff_id in visited:
        return {
            "handoff_id": handoff_id,
            "verdict": "surface",
            "cleared_by_shas": [],
            "remaining_blockers": list(blocked_by_ids),
            "cleared_blocker_ids": [],
            "evidence": [f"cycle detected: {handoff_id} already visited in this walk"],
            "also_surface": False,
        }
    visited = visited | {handoff_id}

    if not blocked_by_ids:
        return {
            "handoff_id": handoff_id,
            "verdict": "not-cleared",
            "cleared_by_shas": [],
            "remaining_blockers": [],
            "cleared_blocker_ids": [],
            "evidence": ["blocked_by is empty — nothing to evaluate"],
            "also_surface": False,
        }

    if _has_asymmetry(handoff, blocked_by_ids, all_index):
        return {
            "handoff_id": handoff_id,
            "verdict": "surface",
            "cleared_by_shas": [],
            "remaining_blockers": list(blocked_by_ids),
            "cleared_blocker_ids": [],
            "evidence": [
                "blocks/blocked_by asymmetry detected — data defect, not auto-repaired"
            ],
            "also_surface": False,
        }

    dispositions = handoff.get("blocked_by_dispositions")
    (
        shipped_ids,
        shipped_shas,
        abandoned_ids,
        unresolved_ids,
        still_open_ids,
        disposed_ids,
        unstamped_shipped_ids,
        classify_evidence,
    ) = _classify_blocked_by(blocked_by_ids, all_index, dispositions)
    evidence.extend(classify_evidence)

    if unresolved_ids:
        evidence.append(_unresolved_reason(unresolved_ids, scan_incomplete, scan_errors))

    # C7 AC8: disposed_ids ALWAYS fold into remaining_blockers alongside
    # abandoned/still-open/unresolved — a disposition never clears/narrows a
    # gate by itself (module docstring "C7 AC8"). Because disposed_ids can
    # only ever ADD to remaining_blockers, never subtract from it, the
    # "if not remaining_blockers: clear" branch below cannot fire on a
    # disposition-only remainder — this is structural, not a separate guard.
    # C6: unstamped_shipped_ids folds in on the same principle — a shipped
    # terminus with no shipped_in has no clearing provenance to record, so
    # it can only ever ADD to remaining_blockers, never subtract from it.
    remaining_blockers = (
        abandoned_ids + still_open_ids + unresolved_ids + disposed_ids + unstamped_shipped_ids
    )

    if abandoned_ids and not shipped_ids:
        # Slice-A review Finding 3 (P2): abandoned blocker(s) exist but NOTHING
        # is actually shipped — there is no edge to narrow (cleared_by_shas
        # would be empty). A "narrow" verdict that narrows nothing is
        # misleading to the C8 consumer's verdict taxonomy; this is pure
        # surface (still_open_ids may be non-empty here — that's fine, they
        # remain legitimately gated, only the abandoned id needs surfacing).
        return {
            "handoff_id": handoff_id,
            "verdict": "surface",
            "cleared_by_shas": [],
            "remaining_blockers": remaining_blockers,
            "cleared_blocker_ids": [],
            "evidence": evidence,
            "also_surface": False,
        }

    if abandoned_ids:
        return {
            "handoff_id": handoff_id,
            "verdict": "narrow",
            "cleared_by_shas": shipped_shas,
            "remaining_blockers": remaining_blockers,
            "cleared_blocker_ids": list(shipped_ids),
            "evidence": evidence
            + ["narrow+surface composite: remaining_blockers includes an abandoned id"],
            "also_surface": True,
        }

    if unstamped_shipped_ids and not shipped_ids:
        # C6: mirrors the abandoned-and-not-shipped branch above — a shipped-
        # but-unstamped blocker exists but NOTHING is actually clearable
        # (cleared_by_shas would be empty), so there is no edge to narrow.
        # Pure surface (still_open_ids may be non-empty here — that's fine).
        return {
            "handoff_id": handoff_id,
            "verdict": "surface",
            "cleared_by_shas": [],
            "remaining_blockers": remaining_blockers,
            "cleared_blocker_ids": [],
            "evidence": evidence,
            "also_surface": False,
        }

    if not remaining_blockers:
        return {
            "handoff_id": handoff_id,
            "verdict": "clear",
            "cleared_by_shas": shipped_shas,
            "remaining_blockers": [],
            "cleared_blocker_ids": list(shipped_ids),
            "evidence": evidence,
            "also_surface": False,
        }

    if shipped_ids:
        return {
            "handoff_id": handoff_id,
            "verdict": "narrow",
            "cleared_by_shas": shipped_shas,
            "remaining_blockers": remaining_blockers,
            "cleared_blocker_ids": list(shipped_ids),
            "evidence": evidence,
            # 2026-07-20 claude-central-em false-positive memo, Defect 1
            # recommendation: parity with the abandoned-id composite above — a
            # narrow verdict whose remaining_blockers includes a dangling
            # (unresolvable) ref must not silently rot un-surfaced either.
            # C6: same parity for a co-blocker that shipped with no
            # shipped_in — the MIXED-CASE RESIDUAL named in the chunk spec:
            # this narrow verdict applies (narrowing on the with-sha
            # blocker) but must not leave the no-sha id rotting un-surfaced.
            "also_surface": bool(unresolved_ids or unstamped_shipped_ids),
        }

    if unresolved_ids:
        # Defect 1 recommendation: a `blocked_by` id that resolves nowhere in the
        # live+archived index is (absent a scan gap) a genuine data defect
        # (dangling ref), not a benign steady state. Falling through to
        # `not-cleared` here would silently swallow it — `not-cleared` is
        # deliberately NOT surfaced by `handoff_reconcile.py` (benign "still
        # gated, no action" path). Return `surface` instead — `evidence` already
        # carries the distinct reason line from `_unresolved_reason` (dangling-ref
        # framing, or scan-gap framing when `scan_incomplete`) — so C4 appends
        # this to `surfaced[]` for EM judgment either way.
        return {
            "handoff_id": handoff_id,
            "verdict": "surface",
            "cleared_by_shas": [],
            "remaining_blockers": remaining_blockers,
            "cleared_blocker_ids": [],
            "evidence": evidence,
            "also_surface": False,
        }

    return {
        "handoff_id": handoff_id,
        "verdict": "not-cleared",
        "cleared_by_shas": [],
        "remaining_blockers": remaining_blockers,
        "cleared_blocker_ids": [],
        "evidence": evidence,
        "also_surface": False,
    }


def _evaluate_prose_gate(
    handoff: Dict[str, Any],
    witness_candidates: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Evaluate the PROSE `gate_dependency` fallback path for a non-roadmap handoff.

    Conservative resolution: `witness_candidates` is the caller-supplied set of
    concrete, checkable pointers the handoff body/frontmatter names (e.g. a sibling
    handoff/plan `id` the body cites plus its resolved `deployment_state`/`shipped_in`).
    - Zero candidates -> surface (no concrete pointer given, do not guess).
    - Exactly one candidate whose deployment_state == shipped -> clear.
    - Exactly one candidate not yet shipped -> surface (still-gated is EM judgment
      for prose gates per DoE alignment reply #3 — the op never auto-transitions a
      prose-path verdict regardless of clear/surface).
    - More than one candidate -> surface (ambiguous resolution, never guess).
    """
    handoff_id = handoff.get("id", "<unknown>")
    gate_dependency = handoff.get("gate_dependency", "")

    if not witness_candidates:
        return {
            "handoff_id": handoff_id,
            "verdict": "surface",
            "cleared_by_shas": [],
            "remaining_blockers": [],
            "cleared_blocker_ids": [],
            "evidence": [
                f"prose gate_dependency={gate_dependency!r} has no concrete checkable witness"
            ],
            "also_surface": False,
        }

    if len(witness_candidates) > 1:
        ids = [c.get("id", "<unknown>") for c in witness_candidates]
        return {
            "handoff_id": handoff_id,
            "verdict": "surface",
            "cleared_by_shas": [],
            "remaining_blockers": [],
            "cleared_blocker_ids": [],
            "evidence": [
                f"prose gate_dependency={gate_dependency!r} resolves to >1 candidate "
                f"witness ({ids}) — ambiguous, not auto-resolved"
            ],
            "also_surface": False,
        }

    witness = witness_candidates[0]
    witness_state = _blocker_deployment_state(witness)
    witness_id = witness.get("id", "<unknown>")

    if witness_state == _SHIPPED_STATE:
        sha = witness.get("shipped_in")
        return {
            "handoff_id": handoff_id,
            "verdict": "clear",
            "cleared_by_shas": [sha] if isinstance(sha, str) and sha else [],
            "remaining_blockers": [],
            "cleared_blocker_ids": [witness_id],
            "evidence": [f"prose witness {witness_id} shipped (shipped_in={sha!r})"],
            "also_surface": False,
        }

    return {
        "handoff_id": handoff_id,
        "verdict": "surface",
        "cleared_by_shas": [],
        "remaining_blockers": [witness_id],
        "cleared_blocker_ids": [],
        "evidence": [
            f"prose witness {witness_id} not yet shipped "
            f"(deployment_state={witness_state!r})"
        ],
        "also_surface": False,
    }


def _gate_evidence_status_to_verdict(status: str) -> str:
    """Project `reduce_gate_evidence`'s four-way `status` onto `evaluate_gate`'s
    own clear/surface vocabulary (C6). The prose path has always been binary
    (`_evaluate_prose_gate` returns only `clear`/`surface`, never `narrow` —
    there is no partial-list-of-witnesses concept to narrow against, unlike
    the structured `blocked_by` path) — a `gate_evidence`-driven prose
    override preserves that same binary shape rather than inventing a new
    partial-clear state: only `freed` maps to `clear`; `still-blocked`,
    `indeterminate`, and `review-due` all map to `surface` (never guess which
    of those three "almost cleared" states is safe to treat as narrow)."""
    return "clear" if status == "freed" else "surface"


def evaluate_gate(
    handoff: Dict[str, Any],
    live_and_archived_handoffs: Sequence[Dict[str, Any]],
    witness_candidates: Optional[Sequence[Dict[str, Any]]] = None,
    scan_incomplete: bool = False,
    scan_errors: Optional[Sequence[str]] = None,
    gate_evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compute the unified gate verdict for one `awaiting_gate` handoff.

    Routing (C4 — see module docstring "C4 RECONCILIATION", reconciled onto
    the same `_is_structured_gate` predicate `evaluate_gate_triage` uses):
      SC. (C2, module docstring "C2 SCAFFOLD SENTINEL") `gate_dependency` or
         `blocking_notes` carries an unfilled `coordinator-doc-new` scaffold
         placeholder (`_scaffold_sentinel_field`) — checked BEFORE every
         other rule, including rule 0's `covers_prose` witness (an unnamed
         gate has no prose for a `gate_evidence` block to legitimately
         cover): `surface`, unconditionally, with a distinct evidence line
         naming the unfilled field. NEVER `clear` — treating an unfilled
         scaffold as "no gate" would let a stub with `blocked_by: []` fall
         through to rule 3's vacuous-clear branch and auto-clear a baton
         whose gate nobody ever named.
      0. `gate_evidence.covers_prose is True` (C6 — checked FIRST, mirroring
         `evaluate_gate_triage`'s own D2 precedence order, which also checks
         this before touching structured classification at all): the prose
         gate is demoted to commentary and the `gate_evidence` legs' AND-
         reduce is authoritative for the WHOLE gate, `blocked_by` included —
         `freed` -> `clear` (with `cleared_blocker_ids`/`remaining_blockers`
         covering the full `blocked_by` list, since the evidence witnesses
         the gate itself, not one individual edge), anything else ->
         `surface` (see `_gate_evidence_status_to_verdict`). This is the
         EXTERNAL-GATE witness source: a real gate that can never be a
         `blocked_by` slug (a sibling REPO is not a baton — the oaxis-01
         motivating case) is authored as a `gate_evidence` leg
         (`test-node-id`/`probe-op-key`/`commit-sha`/`sibling-commitment-ref`,
         see module docstring PER-LEG PREDICATE) instead of forcing the
         slug-or-advisory-prose binary to hold a shape it has no home for.
      1. Non-empty `blocked_by` AND a non-empty prose `gate_dependency` AND
         NOT covered by evidence (rule 0) -> PROSE DOMINATES: `surface`,
         unconditionally, regardless of whether every structured member has
         shipped. (C3) When every `blocked_by` member independently resolves
         as shipped (module docstring "C3 STALENESS EVIDENCE ON DOMINANCE"),
         the evidence gains an addendum naming each blocker id and its
         shipping sha — the verdict itself never changes.
      1a. A non-empty `blocking_notes` -> BLOCKING_NOTES DOMINATES: `surface`,
         unconditionally, with the SAME dominance `gate_dependency` confers in
         rule 1 — checked regardless of whether `blocked_by` is empty,
         populated-and-satisfied, or `gate_dependency` is absent entirely (see
         module docstring "BLOCKING_NOTES DOMINANCE"). `blocking_notes` is
         never machine-resolved (no witness lookup, no cascade) — it is an
         opaque "a human said something still gates this" signal. (C3) Same
         staleness-evidence addendum as rule 1 above when every `blocked_by`
         member has shipped.
      2. Non-empty `blocked_by`, no prose `gate_dependency`, no
         `blocking_notes` -> STRUCTURED path, ANY handoff `kind` (widened
         from the old `kind == "spinoff-roadmap"` restriction).
      3. Empty `blocked_by`, no prose `gate_dependency`, no `blocking_notes`
         -> vacuously `clear` (mirrors `evaluate_gate_triage`'s own
         vacuous-`freed` case; also the fix for LINEAGE IS NOT GATING — a
         spinoff with `predecessor`/`origin_*` fields and nothing else gating
         it must resolve `clear`).
      4. Empty `blocked_by`, non-empty prose `gate_dependency`, no `blocking_
         notes`, no covering `gate_evidence` -> legacy PROSE fallback using
         `witness_candidates` (caller-resolved concrete pointers named by the
         handoff's `gate_dependency` prose; empty/None when the caller found
         none) — unaffected by this reconciliation. A `gate_evidence` block
         present WITHOUT `covers_prose: True` is a partial backfill and falls
         through to this same legacy path unchanged (D2a — migration off
         prose must be an explicit human assertion, never an emergent
         property of "some legs happen to exist").

    Args:
        handoff: parsed frontmatter dict for the handoff under evaluation. Recognized
            keys: "id", "kind", "blocked_by" (list[str]), "blocks" (list[str]),
            "gate_dependency" (str).
        live_and_archived_handoffs: parsed frontmatter dicts for the full live+archived
            union (state/handoffs/ + archive/handoffs/), used to resolve `blocked_by`
            stub-ids by durable `id`. Caller's responsibility to assemble this union.
        witness_candidates: caller-resolved concrete witness handoff dicts for the
            PROSE path (ignored on the structured path). Defaults to empty.
        scan_incomplete: True when `live_and_archived_handoffs` was assembled under
            an archive/handoffs/ subtree that could not be fully scanned
            (`handoff_reconcile.py`'s `scan_incomplete` output, 94d8251f) — meaning
            a `blocked_by` id absent from the index may simply be unscanned, not
            genuinely nonexistent. STRUCTURED-path only (ignored on the PROSE
            path); see `_unresolved_reason`.
        scan_errors: the caller's `scan_errors` list (unscannable subtree
            descriptions) — folded into the evidence reason when
            `scan_incomplete` is True. Ignored when `scan_incomplete` is False.
        gate_evidence: optional caller-assembled `{"covers_prose": bool,
            "legs": [leg, ...]}` block (C6, same shape `evaluate_gate_triage`
            already accepts — see module docstring "GATE_EVIDENCE
            PROJECTION"). `None` (the default) reproduces every pre-C6 code
            path byte-for-byte; this parameter is purely additive and only
            takes effect when `covers_prose` is explicitly `True` (rule 0).

    Returns:
        {handoff_id, verdict: "clear"|"narrow"|"surface"|"not-cleared",
         cleared_by_shas: [...], remaining_blockers: [...], evidence: [...],
         also_surface: bool}
    """
    handoff_id = handoff.get("id", "<unknown>")
    blocked_by = handoff.get("blocked_by") or []
    structured_eligible = _is_structured_gate(handoff)
    has_prose = _has_prose_gate(handoff)
    has_blocking_notes = _has_blocking_notes(handoff)
    has_evidence = gate_evidence is not None
    covers_prose = has_evidence and bool(gate_evidence.get("covers_prose"))

    sentinel_field = _scaffold_sentinel_field(handoff)
    if sentinel_field is not None:
        # Rule SC (C2, module docstring "C2 SCAFFOLD SENTINEL"): checked
        # BEFORE every other rule, including rule 0's covers_prose witness —
        # an unfilled scaffold placeholder is not a human-named gate, so
        # there is nothing for a gate_evidence block to legitimately cover.
        # Never `clear` — see module docstring for why that would be strictly
        # worse than the C1 defect this exists to fix.
        return {
            "handoff_id": handoff_id,
            "verdict": "surface",
            "cleared_by_shas": [],
            "remaining_blockers": list(blocked_by),
            "cleared_blocker_ids": [],
            "evidence": [
                f"{sentinel_field}={handoff.get(sentinel_field)!r} is an unfilled "
                "coordinator-doc-new scaffold placeholder, not authored prose — "
                "nobody has ever stated what gates this baton; never clears, and "
                "does not get the ordinary prose/blocking_notes-dominance evidence "
                "line"
            ],
            "also_surface": False,
        }

    if has_blocking_notes and not structured_eligible:
        # BLOCKING_NOTES DOMINANCE — DEMOTED (C4 gate-dependency-template-
        # emission-spec chunk; module docstring "BLOCKING_NOTES DOMINANCE"):
        # `blocking_notes` no longer overrides a SATISFIED STRUCTURED GRAPH —
        # it now only prevents an EMPTY `blocked_by` from reading as
        # "nothing gates this" (the vacuous-clear case rule 3 would otherwise
        # take, and the original Windows-box motivating defect this dominance
        # exists to fix). `structured_eligible` is False here, so
        # `blocked_by` is empty by construction — there is no structured
        # graph for this branch to defer to, and `_all_blocked_by_shipped_
        # evidence` would return None unconditionally on an empty list (C3
        # AC3.3), so no staleness addendum is possible or attempted here.
        return {
            "handoff_id": handoff_id,
            "verdict": "surface",
            "cleared_by_shas": [],
            "remaining_blockers": list(blocked_by),
            "cleared_blocker_ids": [],
            "evidence": [
                f"blocking_notes={handoff.get('blocking_notes')!r} present and "
                "blocked_by is empty — nothing structural to defer to, so "
                "blocking_notes prevents the vacuous-clear reading; never "
                "cleared, and never machine-resolved"
            ],
            "also_surface": False,
        }

    if has_prose and covers_prose:
        # Rule 0 (C6): mirrors evaluate_gate_triage's own D2 precedence,
        # checked first and before structured classification, exactly as
        # that function checks it — a second, independently-drifting
        # precedence decision is the sibling-evaluator shape this module
        # exists to avoid (module docstring "C4 RECONCILIATION").
        status, evidence, _leg_results = reduce_gate_evidence(gate_evidence)
        verdict = _gate_evidence_status_to_verdict(status)
        cleared = verdict == "clear"
        return {
            "handoff_id": handoff_id,
            "verdict": verdict,
            "cleared_by_shas": [],
            "remaining_blockers": [] if cleared else list(blocked_by),
            "cleared_blocker_ids": list(blocked_by) if cleared else [],
            "evidence": [
                f"prose gate_dependency={handoff.get('gate_dependency')!r} demoted to "
                "commentary — gate_evidence.covers_prose is True"
            ]
            + evidence,
            "also_surface": False,
        }

    if has_prose and structured_eligible:
        # PROSE-DOMINANCE (C4): reconciled with evaluate_gate_triage's own
        # precedence rule. Widening eligibility to ANY kind drags the
        # both-fields population into this mutating evaluator — without this
        # guard it would silently `clear` a handoff whose real precondition
        # is the untested prose clause. Keys on gate_dependency here — UNTOUCHED
        # by the C4 (gate-dependency-template-emission-spec) blocking_notes
        # demotion above: `gate_dependency` dominance still fires unconditionally
        # whenever `blocked_by` is non-empty, regardless of shipped-state
        # (module docstring "BLOCKING_NOTES DOMINANCE" — only `blocking_notes`
        # was demoted, never this branch). The verdict itself is STILL always
        # `surface` here, unconditionally — what C1 (below) adds is that the
        # all-shipped case now names its own contradiction instead of only
        # narrating it in prose evidence.
        evidence_lines = [
            f"prose gate_dependency={handoff.get('gate_dependency')!r} present "
            f"alongside blocked_by={list(blocked_by)} — prose gate dominates per "
            "precedence rule; never cleared by structured satisfaction alone"
        ]
        staleness_evidence = _all_blocked_by_shipped_evidence(
            handoff, list(blocked_by), live_and_archived_handoffs
        )
        # `shipped_blocker_ids` below must mirror the SAME normalized
        # (str-only) list `_all_blocked_by_shipped_evidence` actually
        # confirmed all-shipped, not the raw `blocked_by` — otherwise a
        # non-str member (filtered out inside the helper, Finding 1) would
        # land in `shipped_blocker_ids` unresolved, breaking the "every
        # member is, by construction, resolved shipped" invariant the module
        # docstring documents for this field.
        str_blocked_by = [b for b in blocked_by if isinstance(b, str)]
        result = {
            "handoff_id": handoff_id,
            "verdict": "surface",
            "cleared_by_shas": [],
            "remaining_blockers": list(blocked_by),
            "cleared_blocker_ids": [],
            "evidence": evidence_lines,
            "also_surface": False,
        }
        if staleness_evidence is not None:
            evidence_lines.append(
                "every structured blocked_by member has since shipped, though "
                "the prose was never re-checked against that: "
                + "; ".join(staleness_evidence)
            )
            # C1 (docs/plans/2026-08-03-gate-dependency-template-emission-
            # spec.md): the all-shipped case previously appended this
            # evidence line and then discarded it — no machine-legible
            # signal recorded that a fully-satisfied structured graph and an
            # unconditionally-dominating prose clause are now in tension.
            # `contradiction` names that tension explicitly so a caller (or
            # an EM scanning `evaluate_gate` output) can find it without
            # re-parsing evidence prose. It never changes the verdict — a
            # human, via `handoff.transition gate-recheck --cleared`
            # (`_gate_recheck` in handoff_transition.py, which retires the
            # prose non-destructively via `_retire_gate_dependency`), is
            # still the one who decides stale prose is safe to retire.
            result["contradiction"] = {
                "kind": "prose-gate-outlived-structured-blockers",
                "discharge_verb": "handoff.transition gate-recheck --cleared",
                # Duplicates in `blocked_by` are preserved verbatim here
                # (Review: code-reviewer, Finding 3, nit) — this mirrors
                # `blocked_by` itself rather than deduping to a set, since
                # `shipped_blocker_ids` is documented as "blocked_by, in
                # blocked_by order" and a caller expecting set semantics
                # would be reading a different field than the one the
                # contract doc and DR-266 describe.
                "shipped_blocker_ids": str_blocked_by,
            }
        return result

    if structured_eligible:
        all_index = _index_by_id(live_and_archived_handoffs)
        return _evaluate_structured_gate(
            handoff,
            all_index,
            visited=set(),
            scan_incomplete=scan_incomplete,
            scan_errors=scan_errors or (),
        )

    if not has_prose:
        # Vacuous case (mirrors evaluate_gate_triage's own vacuous-`freed`
        # branch): blocked_by is empty and there is no prose gate_dependency
        # either — nothing structurally gates this handoff. LINEAGE IS NOT
        # GATING (module docstring): predecessor/origin_* fields are never
        # consulted here — an empty blocked_by is the whole story.
        return {
            "handoff_id": handoff_id,
            "verdict": "clear",
            "cleared_by_shas": [],
            "remaining_blockers": [],
            "cleared_blocker_ids": [],
            "evidence": [
                "blocked_by is empty and no prose gate_dependency present — "
                "nothing structurally gates this handoff"
            ],
            "also_surface": False,
        }

    return _evaluate_prose_gate(handoff, witness_candidates or [])


def consumes_gate_evidence(
    handoff: Dict[str, Any], gate_evidence: Optional[Dict[str, Any]]
) -> bool:
    """True iff `evaluate_gate(handoff, ..., gate_evidence=gate_evidence)` would
    actually reach rule 0 and consult `gate_evidence` for this handoff.

    SINGLE SOURCE OF TRUTH for "was gate_evidence actually consumed by this
    verdict" (`handoff_reconcile.py`'s `evidence_consumed` computation calls
    this directly rather than re-deriving it) — the same defect class this
    module's own docstring already records once (a locally-reimplemented
    mirror of `evaluate_gate`'s precedence drifting out from under it when the
    precedence itself changed): `evaluate_gate` checks rule SC (the C2
    scaffold sentinel), THEN the demoted rule 1a (`blocking_notes` dominance,
    now vacuous-`blocked_by`-only per "BLOCKING_NOTES DOMINANCE"), and ONLY
    THEN rule 0's `has_prose and covers_prose` test — so this predicate walks
    the IDENTICAL three gates in the IDENTICAL order, calling the exact same
    helper predicates `evaluate_gate` itself calls (`_scaffold_sentinel_field`,
    `_is_structured_gate`, `_has_blocking_notes`, `_has_prose_gate`), never a
    restated boolean expression. A future precedence-order edit to
    `evaluate_gate` that does not also update this function is caught by
    `evaluate_gate`'s own test suite via AC5.5's cross-check (this module's
    tests assert this predicate and `evaluate_gate`'s actual behaviour agree
    across a field-combination matrix) — the drift cannot land silently.

    Returns False whenever `evaluate_gate` would short-circuit before rule 0
    (the SC sentinel, or the demoted rule 1a `blocking_notes`-on-empty-
    `blocked_by` case) — in both of those cases `evaluate_gate` never reads
    `gate_evidence` at all, regardless of what it contains. Returns False when
    `gate_evidence` is falsy/`None` or lacks `covers_prose: True` (rule 0's own
    gate). Returns True only when `evaluate_gate` reaches rule 0 AND
    `covers_prose` is `True` — the sole combination where the evidence legs'
    AND-reduce is actually authoritative for the verdict.
    """
    if _scaffold_sentinel_field(handoff) is not None:
        return False
    structured_eligible = _is_structured_gate(handoff)
    if _has_blocking_notes(handoff) and not structured_eligible:
        return False
    has_prose = _has_prose_gate(handoff)
    covers_prose = gate_evidence is not None and bool(gate_evidence.get("covers_prose"))
    return has_prose and covers_prose


def _has_prose_gate(handoff: Dict[str, Any]) -> bool:
    """True iff `handoff` carries a non-empty `gate_dependency` one-liner."""
    gate_dependency = handoff.get("gate_dependency")
    return isinstance(gate_dependency, str) and gate_dependency.strip() != ""


def _has_blocking_notes(handoff: Dict[str, Any]) -> bool:
    """True iff `handoff` carries a non-empty (non-whitespace) `blocking_notes`.

    Dominance twin of `_has_prose_gate` (see module docstring "BLOCKING_NOTES
    DOMINANCE"): a corpus migration deposits the operative human-authored gate
    text into `blocking_notes` rather than `gate_dependency` for some handoff
    kinds, so this predicate must apply the SAME strip-and-check discipline —
    a whitespace-only value is empty, never a gate, exactly as `gate_dependency`
    already treats it."""
    blocking_notes = handoff.get("blocking_notes")
    return isinstance(blocking_notes, str) and blocking_notes.strip() != ""


#: C2 — `coordinator-doc-new`'s unfilled scaffold default (module docstring
#: "C2 SCAFFOLD SENTINEL"). The prefix tuple covers the C1 scaffold's own
#: authored continuation (`PLACEHOLDER — name the condition...`) plus a bare
#: space separator, without matching a real sentence that merely contains
#: the word "placeholder" (AC2.3 — see `_is_scaffold_sentinel`, a PREFIX
#: test, never a substring search).
_SCAFFOLD_SENTINEL = "PLACEHOLDER"
_SCAFFOLD_SENTINEL_PREFIXES = ("PLACEHOLDER ", "PLACEHOLDER—", "PLACEHOLDER —")


def _is_scaffold_sentinel(value: Any) -> bool:
    """True iff `value`'s STRIPPED form is exactly the scaffold placeholder,
    or begins with one of its authored continuations — a PREFIX test, never
    a substring search (AC2.3): authored prose that merely CONTAINS the word
    "placeholder" in a real sentence (e.g. "blocked on the placeholder
    registry landing") is NOT a sentinel and keeps ordinary dominance.
    A non-string value is never a sentinel (AC2.4 — whitespace-is-empty
    behaviour is unaffected: `_has_prose_gate`/`_has_blocking_notes` already
    treat a whitespace-only value as no gate at all, and a whitespace-only
    value's stripped form is `""`, which matches neither test here)."""
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    if stripped == _SCAFFOLD_SENTINEL:
        return True
    return stripped.startswith(_SCAFFOLD_SENTINEL_PREFIXES)


def _scaffold_sentinel_field(handoff: Dict[str, Any]) -> Optional[str]:
    """Which of `gate_dependency`/`blocking_notes` (if either) carries an
    unfilled C1 scaffold placeholder — `gate_dependency` checked first,
    mirroring rule 1's precedence over rule 1a's `blocking_notes`. Returns
    `None` when neither field is a sentinel (including when a field is
    empty/whitespace-only, or is authored prose that merely contains the
    word "placeholder" — see `_is_scaffold_sentinel`)."""
    if _is_scaffold_sentinel(handoff.get("gate_dependency")):
        return "gate_dependency"
    if _is_scaffold_sentinel(handoff.get("blocking_notes")):
        return "blocking_notes"
    return None


def _all_blocked_by_shipped_evidence(
    handoff: Dict[str, Any],
    blocked_by_ids: Sequence[str],
    live_and_archived_handoffs: Sequence[Dict[str, Any]],
) -> Optional[List[str]]:
    """C3 (module docstring "C3 STALENESS EVIDENCE ON DOMINANCE"): when EVERY
    member of `blocked_by_ids` resolves as `shipped` — directly, or via a
    `continued` chase to a genuinely shipped terminus, exactly the SAME
    classification `_evaluate_structured_gate` itself applies — return the
    classification's own per-blocker evidence lines (each already names the
    blocker id and its/the terminus's shipping sha, AC3.1); `None` otherwise
    (an unresolved, abandoned/continued/closed-and-unchased, still-open, or
    disposed member present, or `blocked_by_ids` is empty) — the caller must
    never assert staleness it cannot substantiate (AC3.2/AC3.3).

    Reuses `_classify_blocked_by`/`_index_by_id` (AC3.4) — the SAME shipped-
    state predicate and live+archived index every other classification in
    this module already uses. Performs no sibling I/O, no clock read, and
    parses nothing out of prose; it only asks whether the STRUCTURED graph
    this handoff already carries has, since the prose was authored, shipped
    out from under it in its entirety.

    `blocked_by_ids` is normalized to `str` members HERE, not by the caller
    (Review: code-reviewer, Finding 1, P1) — `evaluate_gate` previously passed
    its raw, unfiltered `blocked_by` list while `evaluate_gate_triage` filtered
    to `isinstance(b, str)` before calling in, so a `blocked_by` entry shaped
    `["<shipped-id>", None]` could resolve staleness differently across the
    two evaluators (the None member fails to resolve in one path's raw list
    but was already stripped in the other's). Normalizing inside this shared
    helper means both callers key on the SAME precondition regardless of
    caller-side discipline — the exact invariant the module docstring's
    "SAME precondition" language requires. `evaluate_gate_triage`'s own
    caller-side filter is left in place (it feeds more than this one call),
    but this helper no longer depends on it.
    """
    blocked_by_ids = [b for b in blocked_by_ids if isinstance(b, str)]
    if not blocked_by_ids:
        return None
    all_index = _index_by_id(live_and_archived_handoffs)
    (
        shipped_ids,
        _shipped_shas,
        abandoned_ids,
        unresolved_ids,
        still_open_ids,
        disposed_ids,
        unstamped_shipped_ids,
        evidence,
    ) = _classify_blocked_by(
        blocked_by_ids, all_index, handoff.get("blocked_by_dispositions")
    )
    # C6: a shipped-but-unstamped member (unstamped_shipped_ids) is not
    # staleness evidence this function can assert either — it has no
    # clearing provenance, same as an abandoned/unresolved/still-open/
    # disposed member.
    if abandoned_ids or unresolved_ids or still_open_ids or disposed_ids or unstamped_shipped_ids:
        return None
    if set(shipped_ids) != set(blocked_by_ids):
        return None
    return evidence


def _chain_tokens(value: Any) -> List[str]:
    """Split a completion-log `chain` field into hyphen-delimited tokens.

    Returns `[]` for a non-string/blank value — callers treat that as "no
    usable chain identity to match against", not a wildcard match.
    """
    if not isinstance(value, str) or not value.strip():
        return []
    return [t for t in value.strip().split("-") if t]


def _id_is_contiguous_subsequence(id_tokens: Sequence[str], chain_tokens: Sequence[str]) -> bool:
    """True iff `id_tokens` appears as a CONTIGUOUS run inside `chain_tokens`.

    E.g. id_tokens=["strang", "01"] matches chain_tokens=[..., "strang",
    "01", "tc3", "emission", ...] (the `2026-07-04-strang-01-tc3-emission-
    port-facade-respin` completion-log chain slug) but does NOT match a
    chain where "strang" and "01" are not adjacent, or where either token
    appears alone without its pair.
    """
    n = len(id_tokens)
    if n == 0 or n > len(chain_tokens):
        return False
    for start in range(len(chain_tokens) - n + 1):
        if list(chain_tokens[start:start + n]) == list(id_tokens):
            return True
    return False


def _completion_chain_match_kind(blocker_id: str, chain: Any) -> Optional[str]:
    """Return `"exact"`, `"fuzzy"`, or `None` for whether a completion-log
    entry's `chain` field identifies `blocker_id`.

    `"exact"`: `chain == blocker_id` verbatim (e.g. `chain: "strang-02"` for
    `blocked_by: [strang-02]`) — as solid an identity match as `_index_by_id`'s
    own exact-key lookup over the handoff corpus; no guessing involved.

    `"fuzzy"`: `blocker_id`'s own hyphen-tokens appear as a contiguous run
    inside `chain`'s hyphen-tokens, but `chain` itself is a longer, differently-
    shaped slug (e.g. `chain: "2026-07-04-strang-01-tc3-emission-port-facade-
    respin"` for `blocked_by: [strang-01]`) — this IS the completion-log
    corpus's real, observed naming convention (`workstream-complete` authors
    `chain` as either a bare stub-id or a `<date>-<stub-id-tokens>-<free-text-
    description>` slug, with no fixed convention chosen consistently across
    the corpus), but it is a heuristic, not an exact-key match — see
    `_resolve_blocker_via_completion_log`'s docstring for why this function
    treats "fuzzy" as evidence-for-a-human, never as auto-resolvable proof.
    """
    chain_tokens = _chain_tokens(chain)
    if not chain_tokens:
        return None
    if chain == blocker_id:
        return "exact"
    id_tokens = _chain_tokens(blocker_id)
    if id_tokens and _id_is_contiguous_subsequence(id_tokens, chain_tokens):
        return "fuzzy"
    return None


def _resolve_blocker_via_completion_log(
    blocker_id: str,
    completion_entries: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Resolve `blocker_id` against completion-log entries — closes the corpus
    gap between the handoff-only index (`_index_by_id`, over `state/handoffs/`
    + `archive/handoffs/`) and `archive/completed/` (workstream-complete's
    chain-terminal completion records), a DIFFERENT schema entirely
    (`chain`/`chain_terminal`/`status`/`commits`, no `id`/`stub_id`/
    `deployment_state` fields at all). `completion_entries` is the raw return
    shape of `coordinator_core.ops.ceremony.records_query.query_records(
    record_type="completion", ...)` — `[{"path": str, "frontmatter": dict},
    ...]` — passed through unmodified so callers reuse that canonical reader
    rather than a second hand-rolled completion-log walker.

    WHY THIS GAP IS REAL, NOT COSMETIC (the failure mode this function
    exists to close): a `blocked_by` id whose blocker shipped through the
    normal `/workstream-complete` path may NEVER get an
    `archive/handoffs/*.md` companion file with `stub_id`/`deployment_state:
    shipped` — its only durable shipped-evidence is the completion-log entry.
    Before this function existed, such a blocker read as `unresolved`
    (indistinguishable from "this id never existed") even though solid
    shipped-evidence was sitting in `archive/completed/` the whole time.

    TERMINAL-VS-`pending-release` DERIVATION (do not assume — decided here,
    stated explicitly): every completion-log entry observed in this repo's
    corpus carries `status: pending-release` (there is no other status value
    in current use — `/workstream-complete` never stamps anything past it).
    "pending-release" reads, on its face, as "not yet shipped" — but
    `coordinator_core.lifecycle_constants`'s own ratified precedent
    (`HANDOFF_TERMINAL_DEPLOYMENT` module docstring) already establishes that
    claude-klabauter's "shipped" bar is "terminal-with-resolvable-commit-evidence
    (shipped_in required), NOT 'released to users'" — a PM ruling
    (2026-07-25, cited there) against a proposal to require literal release.
    A `chain_terminal: true` completion entry backed by a non-empty `commits`
    list is EXACTLY that same bar: the work landed as commits (chain_terminal
    marks it as the workstream's FINAL entry, per the field's own frontmatter
    comment — "set to true on /pickup -> /workstream-complete"), it is simply
    not yet cut into a user-facing release. Treating such an entry as
    shipped-equivalent is consistent with the existing ratified precedent,
    not a new, weaker bar invented for this function. A completion entry that
    is NOT chain_terminal is explicitly excluded from this (an in-progress
    workstream session entry, not proof the whole chain landed) — see the
    `TestResolveBlockerViaCompletionLog` fixtures for the non-terminal case.

    EXACT-VS-FUZZY RESOLUTION POLICY (the actual "indeterminate vs freed"
    call the dispatch brief asked to be made deliberately):
      - EXACT chain match (`chain == blocker_id`) + `chain_terminal: true` +
        non-empty `commits` -> resolution="exact-shipped". This is as solid
        an identity match as any handoff-corpus `id`/`stub_id` lookup this
        module already trusts elsewhere — no heuristic involved — so it is
        allowed to contribute to a `freed` verdict, same as a real
        `deployment_state: shipped` handoff would.
      - FUZZY chain match (blocker_id's tokens embedded in a longer,
        differently-shaped chain slug) -> resolution="fuzzy-candidates",
        NEVER auto-resolved to shipped. The identity claim here rests on a
        token-membership heuristic over free-text slugs with no fixed
        authoring convention — silently trusting it would risk exactly the
        failure mode this whole exercise exists to eliminate, just inverted
        (a coincidental token match silently freeing a genuinely-still-
        blocked handoff). The evidence is still surfaced (strictly better
        than today's opaque "unresolvable"), but the verdict this feeds is
        `indeterminate`, honestly, per the dispatch brief's explicit
        permission to land there when that is the honest answer.
      - Multiple exact chain_terminal+commits-bearing matches for the SAME
        blocker_id -> ambiguous, never guess which — also
        resolution="fuzzy-candidates" (mirrors the prose-path's own
        "more than one candidate -> surface" rule elsewhere in this module).
      - No match at all -> resolution="none" (unchanged: genuinely
        unresolvable, same as before this function existed).

    Returns:
        {resolution: "exact-shipped"|"fuzzy-candidates"|"none",
         evidence: str|None, candidates: [{path, chain, chain_terminal,
         status, commits}, ...]}
    """
    exact_terminal_hits: List[Dict[str, Any]] = []
    other_candidates: List[Dict[str, Any]] = []

    for entry in completion_entries:
        frontmatter = entry.get("frontmatter") or {}
        chain = frontmatter.get("chain")
        match_kind = _completion_chain_match_kind(blocker_id, chain)
        if match_kind is None:
            continue
        commits = frontmatter.get("commits")
        candidate = {
            "path": entry.get("path"),
            "chain": chain,
            "chain_terminal": bool(frontmatter.get("chain_terminal")),
            "status": frontmatter.get("status"),
            "commits": list(commits) if isinstance(commits, list) else [],
        }
        if match_kind == "exact" and candidate["chain_terminal"] and candidate["commits"]:
            exact_terminal_hits.append(candidate)
        else:
            other_candidates.append(candidate)

    if len(exact_terminal_hits) == 1:
        hit = exact_terminal_hits[0]
        return {
            "resolution": "exact-shipped",
            "evidence": (
                f"{blocker_id} resolved via completion-log exact chain match "
                f"{hit['path']!r} (chain_terminal=True, status={hit['status']!r}, "
                f"commits={hit['commits']}) — treated as shipped-equivalent: "
                f"landed-as-commits bar, same as deployment_state=shipped "
                f"('pending-release' means not-yet-user-released, not "
                f"not-yet-landed — see function docstring)"
            ),
            "candidates": exact_terminal_hits,
        }

    all_candidates = exact_terminal_hits + other_candidates
    if all_candidates:
        return {
            "resolution": "fuzzy-candidates",
            "evidence": (
                f"{blocker_id} has completion-log candidate(s) but none is an "
                f"unambiguous, chain-terminal, commit-bearing EXACT chain match: "
                + ", ".join(
                    f"{c['path']!r} (chain={c['chain']!r}, "
                    f"chain_terminal={c['chain_terminal']}, status={c['status']!r})"
                    for c in all_candidates
                )
                + " — needs human confirmation, not auto-resolved to shipped"
            ),
            "candidates": all_candidates,
        }

    return {"resolution": "none", "evidence": None, "candidates": []}


#: Equality-checked I/O kinds (C3): `sibling_fact.resolve_leg` observes a
#: value, this module compares it against the leg's own authored `expected`.
#: Kebab-case (C1): the ratified authoring form (handoff.schema.json's
#: `gate_evidence.legs[].kind` closed enum) — a mixed-casing discriminator
#: is a live typo generator against a closed enum, so this module's own
#: dispatch vocabulary matches the schema exactly rather than the primitive
#: snake_case names `sibling_fact`'s internal kind vocabulary still uses
#: (translated by the caller before this module ever sees a leg).
_EVIDENCE_EQUALITY_KINDS = frozenset({"file-exists", "frontmatter-field"})

#: Boolean-observed I/O kinds: the caller's re-verification already reduces
#: to pass/fail (no `expected` value to compare against — there is nothing to
#: author an "expected" for a re-run pytest node or a re-checked commit SHA),
#: so `observed is True` alone is the predicate. `commit-ancestor` (C3) is the
#: original member; the C6 four (`test-node-id`, `probe-op-key`, `commit-sha`,
#: `sibling-commitment-ref`) join it unchanged from
#: `coordinator/schemas/cutover.schema.json`'s already-ratified
#: `confirmed_consumers[].verified_by.kind` discriminated union (DoE-claude,
#: docs/plans/2026-07-25-cutover-state-machine.md) — this module adopts the
#: SAME four names rather than inventing a parallel vocabulary for the same
#: "re-verifiable evidence, not free prose" concept. Resolution (running the
#: pytest node, re-invoking the op, `git show`-ing the SHA, or reading the
#: `state/cross-repo-commitments/*.yaml` FK — each a `repo:`-qualified leg,
#: mirroring `sibling_fact.resolve_leg`'s existing required field for the
#: other three kinds) is caller-side re-verification, exactly like
#: `cutover_gate.py`'s `_reverify_*` family; this module remains COMPUTE_ONLY
#: and performs none of it — see GATE_EVIDENCE PROJECTION in the module
#: docstring.
_EVIDENCE_BOOLEAN_KINDS = frozenset(
    {"commit-ancestor", "test-node-id", "probe-op-key", "commit-sha", "sibling-commitment-ref"}
)

_EVIDENCE_IO_KINDS = _EVIDENCE_EQUALITY_KINDS | _EVIDENCE_BOOLEAN_KINDS


def _evaluate_gate_evidence_leg(leg: Dict[str, Any]) -> Dict[str, Any]:
    """Apply this module's OWN verdict predicate to one caller-resolved
    `gate_evidence` leg — see module docstring "PER-LEG PREDICATE". `leg` is
    the caller's merge of its authored declaration (`leg_id`, `kind`,
    `expected` for I/O kinds, `reason` for `human`, `elapsed` for `deadline`)
    with `sibling_fact.resolve_leg`'s `{read_ok, observed, error}` observation
    for I/O kinds; this function never performs I/O itself.

    Returns {leg_id, kind, status: "satisfied"|"unsatisfied"|"indeterminate"|
    "review-due", reason}.
    """
    leg_id = leg.get("leg_id", "<unknown>")
    kind = leg.get("kind")

    if kind == "human":
        reason = leg.get("reason") or "no reason authored"
        return {
            "leg_id": leg_id,
            "kind": kind,
            "status": "indeterminate",
            "reason": f"human leg {leg_id!r}: {reason} — never machine-resolvable (D4)",
        }

    if kind == "deadline":
        if bool(leg.get("elapsed")):
            return {
                "leg_id": leg_id,
                "kind": kind,
                "status": "review-due",
                "reason": (
                    f"deadline leg {leg_id!r} elapsed — review due; excluded from the "
                    "AND-reduce toward freed (D3a), an elapsed date alone never frees a gate"
                ),
            }
        return {
            "leg_id": leg_id,
            "kind": kind,
            "status": "unsatisfied",
            "reason": f"deadline leg {leg_id!r} has not yet elapsed",
        }

    if kind not in _EVIDENCE_IO_KINDS:
        return {
            "leg_id": leg_id,
            "kind": kind,
            "status": "indeterminate",
            "reason": f"gate_evidence leg {leg_id!r} carries unrecognized kind {kind!r}",
        }

    if leg.get("read_ok") is False:
        error = leg.get("error")
        return {
            "leg_id": leg_id,
            "kind": kind,
            "status": "indeterminate",
            "reason": f"leg {leg_id!r} (kind={kind!r}) could not be resolved: {error}",
        }

    observed = leg.get("observed")
    if kind in _EVIDENCE_BOOLEAN_KINDS:
        satisfied = observed is True
    else:
        satisfied = observed == leg.get("expected")

    if satisfied:
        return {
            "leg_id": leg_id,
            "kind": kind,
            "status": "satisfied",
            "reason": f"leg {leg_id!r} (kind={kind!r}) observed={observed!r} matches expectation",
        }
    return {
        "leg_id": leg_id,
        "kind": kind,
        "status": "unsatisfied",
        "reason": f"leg {leg_id!r} (kind={kind!r}) observed={observed!r} does not (yet) match expectation",
    }


def reduce_gate_evidence(
    gate_evidence: Dict[str, Any],
) -> "tuple[str, List[str], List[Dict[str, Any]]]":
    """AND-reduce a `gate_evidence` block's legs into one overall status.

    Priority order (module docstring "AND-REDUCE"): any indeterminate leg beats
    any review-due leg beats any unsatisfied leg beats all-satisfied ->
    freed. An empty `legs` list is a malformed block, never vacuously freed.

    Returns (status, evidence_reason_strings, per_leg_results).

    Public rather than module-private because per-leg detail is part of this
    module's contract, not an implementation detail: `evaluate_gate` and
    `evaluate_gate_triage` both collapse the per-leg list away when prose is
    present without `covers_prose: True`, and a caller persisting per-leg
    results (AC9) needs that detail regardless of clearing authority.
    """
    legs = gate_evidence.get("legs") or []
    if not legs:
        return (
            "indeterminate",
            ["gate_evidence present but carries no legs — malformed, never vacuously freed"],
            [],
        )

    results = [_evaluate_gate_evidence_leg(leg) for leg in legs]
    evidence = [r["reason"] for r in results]

    if any(r["status"] == "indeterminate" for r in results):
        return "indeterminate", evidence, results
    if any(r["status"] == "review-due" for r in results):
        return "review-due", evidence, results
    if any(r["status"] == "unsatisfied" for r in results):
        return "still-blocked", evidence, results
    return "freed", evidence, results


def evaluate_gate_triage(
    handoff: Dict[str, Any],
    live_and_archived_handoffs: Sequence[Dict[str, Any]],
    completion_entries: Optional[Sequence[Dict[str, Any]]] = None,
    scan_incomplete: bool = False,
    scan_errors: Optional[Sequence[str]] = None,
    gate_evidence: Optional[Dict[str, Any]] = None,
    consult_prose_gates: bool = True,
) -> Dict[str, Any]:
    """Machine-resolvable TRIAGE classifier: freed / still-blocked / indeterminate.

    Purpose: `evaluate_gate` above answers "should `handoff.reconcile_open`
    auto-transition this handoff" (its four-way clear/narrow/surface/not-cleared
    is a mutation-routing contract, pinned by
    `coordinator_core/contract/handoff-reconcile-producer-contract.md` § 4 — do
    NOT change that enum, callers depend on the exact four strings). This
    function answers a DIFFERENT, narrower question for a human batch-triage
    pass over `awaiting_gate` handoffs sitting stale for weeks: *"is this
    handoff's STRUCTURED gate actually cleared right now, and can a machine
    tell?"* It is deliberately NOT wired to any auto-mutation path (per the
    dispatch brief: evaluation and mutation stay separate — this reports,
    `handoff_transition.py` mutates, and only a human applies the flip).

    Reuses `_classify_blocked_by`/`_has_asymmetry`/`_index_by_id` — the SAME
    per-blocker classification `_evaluate_structured_gate` uses — rather than
    re-walking `blocked_by` a second way. This is the "exactly ONE gate
    evaluator" constraint: a second file that reads handoff frontmatter and
    independently decides gate status would be the shape to avoid; this is an
    additional verdict PROJECTION inside the same module, over the same
    classification primitive.

    TERMINAL-PREDICATE DERIVATION (do not guess — this restates gate_eval's
    already-ratified rule 1/rule 2, rather than inventing a new one for this
    function): `coordinator_core.lifecycle_constants.HANDOFF_TERMINAL_DEPLOYMENT`
    = {shipped, abandoned, continued, closed} is the terminal set for STOPPING
    re-evaluation (a terminal blocker's chain segment is settled, one way or
    the other). But "terminal" and "the blocker is DONE" are NOT the same
    predicate: `abandoned`/`continued`/`closed` are terminal-but-NOT-done —
    the dependent's premise may now be moot (a `continued` blocker means the
    ORIGINAL work item was superseded by a successor; a `closed` blocker means
    it was deliberately stopped, never shipped) and that requires EM judgment,
    not a silent "the gate is freed" verdict. Only `shipped` — DR-084's
    "terminal-with-resolvable-commit-evidence (shipped_in required)" state —
    is evidence the blocked-on work actually landed. So:
        FREED predicate    = every blocked_by id resolves to `shipped`
                              SPECIFICALLY (or blocked_by is empty).
        STILL-BLOCKED       = every blocked_by id resolves, none is
                              abandoned/continued/closed, but at least one is
                              not yet shipped (includes a blocker that is
                              itself still `awaiting_gate`/`ready_to_fire`/
                              `in_flight` — genuinely still in the pipeline).
        INDETERMINATE       = anything the machine cannot confidently reduce
                              to the above two: a prose `gate_dependency`
                              gate (see precedence rule below), an
                              unresolvable blocker id, a `blocks`/`blocked_by`
                              asymmetry (data defect), or a blocker that
                              resolved to abandoned/continued/closed (dead-
                              blocker ambiguity — the dependent's premise may
                              be moot, needs a human to look, never silently
                              freed nor silently kept still-blocked).

    PROSE-DOMINANCE PRECEDENCE (dispatch brief rule 3 — load-bearing): when a
    handoff carries BOTH a non-empty `blocked_by` AND a non-empty prose
    `gate_dependency`, the prose gate ALWAYS wins and the verdict is
    `indeterminate`/`review-due` (see CONTRADICTION CARVE-OUT below), even
    when every structured `blocked_by` member is shipped. Getting this
    backwards (letting structured satisfaction alone produce `freed`) would
    auto-declare-freed a handoff whose REAL precondition is the untested
    prose clause — e.g. `strang-03`'s `gate_dependency` names "claude-klabauter action
    layer live" / "DoE-maximalist cutover W4.2 landed" as the actual gate,
    with `blocked_by` merely tracking the pattern-proof siblings; shipping
    those siblings says nothing about the cutover. A prose gate never
    resolves to `freed` by construction (per the brief) whether or not it
    stands alone — this function never attempts to parse or witness-resolve
    prose text (that is a human-evidence-audit lane, explicitly out of
    scope here).

    CONTRADICTION CARVE-OUT (mirrors `evaluate_gate`'s own `contradiction`
    key, module docstring "C1"; C2 of the same spec chunk re-routes it
    here): when `_all_blocked_by_shipped_evidence` confirms EVERY
    `blocked_by` member has independently resolved shipped, the dominant
    prose has gone stale under a structured graph nobody re-checked it
    against. This function has no `contradiction` key to add (its return
    shape is `status` + evidence, not `evaluate_gate`'s dict), so it
    re-routes that shape onto the EXISTING `review-due` status member
    instead of `indeterminate` — the SAME precondition `evaluate_gate` keys
    its `contradiction` on (both call `_all_blocked_by_shipped_evidence`
    directly), so the two evaluators cannot drift apart. `review-due` is
    already mapped by `handoff_gate_aging.classify_gate` to its own
    actionable `signal`, already given its own rc-1 by `scan_triage`, and
    already folded into `surface` by `evaluate_gate`'s own
    `_gate_evidence_status_to_verdict` — a re-route onto a consumed member,
    never a new parallel key nobody reads. The verdict never becomes
    `freed` — a human still decides whether stale prose is safe to retire.

    `blocking_notes` carries a DEMOTED dominance (C4 gate-dependency-template-
    emission-spec chunk; module docstring "BLOCKING_NOTES DOMINANCE") — unlike
    `gate_dependency` above, a non-empty `blocking_notes` no longer overrides a
    satisfied STRUCTURED graph: when `blocked_by` is non-empty, `blocking_notes`
    is not consulted at all and the structured walk's own outcome (freed/
    still-blocked/indeterminate) stands. It still prevents the empty-`blocked_
    by` vacuous-freed reading, checked ahead of `gate_evidence`/the vacuous-
    empty check — a handoff with `blocked_by: []` and only a non-empty
    `blocking_notes` is NOT vacuously `freed`.

    Empty-`blocked_by`-and-no-prose-and-no-blocking_notes case: for-all-over-
    the-empty-set is vacuously true, so an `awaiting_gate` handoff with
    `blocked_by: []`, no `gate_dependency`, and no `blocking_notes` has
    nothing structurally gating it and is reported
    `freed` — this is a signal the handoff's `awaiting_gate` state itself may
    be stale/wrong (nothing gates it), which is exactly the kind of thing a
    human batch-triage pass over stale `awaiting_gate` handoffs wants
    surfaced, not silently skipped.

    Args (new for C3):
        gate_evidence: optional caller-assembled `{"covers_prose": bool,
            "legs": [leg, ...]}` block — see module docstring "GATE_EVIDENCE
            PROJECTION" for the precedence rule and each leg's expected shape.
            `None` (the default) reproduces every pre-C3 code path byte-for-
            byte — this parameter is purely additive.
        consult_prose_gates: default True reproduces every existing caller's
            behaviour byte-for-byte (aging, surface, reconcile — untouched).
            `False` suppresses BOTH prose-reading branches ahead of the
            structured walk — the `_scaffold_sentinel_field` placeholder
            check and the demoted `blocking_notes`-dominance vacuous-clear
            branch — so the verdict is a pure function of the structured
            `blocked_by` graph alone. This is NOT a second evaluator: same
            function, same resolution walk, one parameter naming which legs
            a caller is entitled to (Review: eng-director/the Director of Engineering, Finding 1).
            `coordinator_core.reconcile.gate_eval.derive_readiness` (C1) is
            the ONLY caller that passes `False` — it must never let prose
            gate readiness (PM ruling 2026-08-19), and delegating with the
            default `True` would silently re-inherit the DR-259 vacuous-
            clear-dominance branch, permanently parking any `blocked_by: []`
            + non-empty `blocking_notes` record (41-record corpus exposure,
            reproduced against the two AC4-pinned records).

    Returns:
        {handoff_id, status: "freed"|"still-blocked"|"indeterminate"|
         "review-due", blocked_by: [str, ...], shipped_ids: [str, ...],
         still_open_ids: [str, ...], dead_ids: [str, ...] (abandoned/
         continued/closed blockers — NEVER a `shipped` id, stamped or not,
         C6), unresolved_ids: [str, ...], disposed_ids: [str, ...],
         unstamped_shipped_ids: [str, ...] (C6 — a `shipped` terminus,
         direct or chased, with no `shipped_in`; folded into `still-blocked`,
         never through `dead_ids`'s reason text), has_prose_gate: bool,
         has_gate_evidence: bool, gate_evidence_legs: [leg-result, ...]
         (only non-empty when the gate_evidence path was taken), reason:
         str, evidence: [str, ...]}

    Negative-spec:
      - Does NOT auto-mutate anything — pure compute, same COMPUTE_ONLY
        classification as `evaluate_gate`.
      - Does NOT change `evaluate_gate`'s four-way return contract — this is
        an additional function, not a replacement.
      - Does NOT parse or attempt to witness-resolve prose `gate_dependency`
        text — a prose gate is `indeterminate` unconditionally, by
        construction, never guessed at with keyword/heuristic matching,
        UNLESS `gate_evidence.covers_prose` is explicitly `True` (D2).
      - Does NOT treat `abandoned`/`continued`/`closed` as "the blocker is
        done" — only `shipped` clears; a dead blocker is `indeterminate`
        (ambiguous premise), never `freed`.
      - Does NOT walk more than one level of the `blocked_by` graph (mirrors
        `_evaluate_structured_gate` rule 5).
      - Does NOT perform sibling I/O or read the system clock for
        `gate_evidence` legs — every observation arrives caller-pre-resolved.
      - Does NOT free a gate on a partial `gate_evidence` backfill against a
        prose gate absent an explicit `covers_prose: True` (D2a).
    """
    handoff_id = handoff.get("id", "<unknown>")
    blocked_by_ids: List[str] = [
        b for b in (handoff.get("blocked_by") or []) if isinstance(b, str)
    ]
    has_prose = _has_prose_gate(handoff)
    gate_dependency = handoff.get("gate_dependency")
    has_blocking_notes = _has_blocking_notes(handoff)

    has_evidence = gate_evidence is not None
    covers_prose = has_evidence and bool(gate_evidence.get("covers_prose"))

    base: Dict[str, Any] = {
        "handoff_id": handoff_id,
        "blocked_by": list(blocked_by_ids),
        "shipped_ids": [],
        "still_open_ids": [],
        "dead_ids": [],
        "unresolved_ids": [],
        "disposed_ids": [],
        "unstamped_shipped_ids": [],
        "has_prose_gate": has_prose,
        "has_gate_evidence": has_evidence,
        "gate_evidence_legs": [],
    }

    # Review: eng-director/the Director of Engineering, Finding 1 — `consult_prose_gates=False`
    # suppresses this branch entirely so a prose scaffold placeholder never
    # parks a readiness verdict; every other caller keeps the default `True`
    # and this line is a no-op for them.
    sentinel_field = _scaffold_sentinel_field(handoff) if consult_prose_gates else None
    if sentinel_field is not None:
        # Rule SC (C2, module docstring "C2 SCAFFOLD SENTINEL"): checked
        # ahead of every other branch, mirroring `evaluate_gate`'s own
        # sentinel check — an unfilled scaffold placeholder is never `freed`,
        # and gets a distinct evidence line instead of the generic
        # prose/blocking_notes-dominance reason.
        return {
            **base,
            "status": "indeterminate",
            "reason": (
                f"{sentinel_field} carries an unfilled coordinator-doc-new "
                "scaffold placeholder, not authored prose — nobody has ever "
                "stated what gates this baton; never freed, and distinct "
                "from the ordinary prose/blocking_notes-dominance reason"
            ),
            "evidence": [
                f"{sentinel_field}={handoff.get(sentinel_field)!r} is an unfilled "
                "scaffold placeholder — never machine-resolvable, never freed"
            ],
        }

    # Review: eng-director/the Director of Engineering, Finding 1 — `consult_prose_gates=False`
    # suppresses the DR-259 demoted-dominance branch below as well, so an
    # empty `blocked_by` plus a gate NOTE (never a gate) does not read as
    # `indeterminate`. Default `True` leaves this branch reachable exactly
    # as before for every other caller.
    if consult_prose_gates and has_blocking_notes and not _is_structured_gate(handoff):
        # BLOCKING_NOTES DOMINANCE — DEMOTED (C4 gate-dependency-template-
        # emission-spec chunk; module docstring "BLOCKING_NOTES DOMINANCE"):
        # `blocking_notes` no longer overrides a SATISFIED STRUCTURED GRAPH —
        # it only prevents an EMPTY `blocked_by` from reading as vacuously
        # `freed`. `blocked_by_ids` is empty here by construction
        # (`_is_structured_gate` is False), mirroring `evaluate_gate`'s own
        # demotion above.
        blocking_notes = handoff.get("blocking_notes")
        return {
            **base,
            "status": "indeterminate",
            "reason": (
                "blocking_notes present and blocked_by is empty — nothing "
                "structural to defer to, so blocking_notes prevents the "
                "vacuous-freed reading (never freed by structured "
                "satisfaction alone when there IS a structured graph; "
                "blocking_notes is a human evidence-audit lane, not "
                "machine-resolvable here)"
            ),
            "evidence": [
                f"blocking_notes={blocking_notes!r} present (no blocked_by — "
                "blocking_notes-only gate)"
            ],
        }

    # D2 precedence: prose dominates UNLESS gate_evidence explicitly asserts
    # covers_prose:True, in which case evidence wins and prose is demoted to
    # commentary. Checked FIRST, before touching structured classification at
    # all, so the precedence is visible in the code, not merely in branch
    # ordering of a shared conditional.
    if has_prose and covers_prose:
        status, evidence, leg_results = reduce_gate_evidence(gate_evidence)
        return {
            **base,
            "status": status,
            "gate_evidence_legs": leg_results,
            "reason": (
                "gate_evidence covers_prose:True — evidence legs are authoritative, "
                "prose gate_dependency demoted to commentary (D2)"
            ),
            "evidence": [
                f"gate_dependency={gate_dependency!r} demoted to commentary — "
                "gate_evidence.covers_prose is True"
            ]
            + evidence,
        }

    if has_prose:
        # Rule 3 precedence, unchanged: a prose gate_dependency, when present
        # alongside blocked_by and NOT demoted by covers_prose:True, ALWAYS
        # dominates — indeterminate regardless of structured OR gate_evidence
        # outcome (a gate_evidence block without covers_prose:True is a
        # partial backfill and must not silently free the gate — D2a).
        #
        # CONTRADICTION CARVE-OUT (mirrors evaluate_gate's rule 1, module
        # docstring "C1"): when EVERY blocked_by member independently
        # resolves shipped, the dominant prose has gone stale under a
        # structured graph nobody re-checked it against — the same shape
        # `evaluate_gate` now names via its `contradiction` key. This
        # function has no key to add one, so it re-routes the status onto
        # the EXISTING `review-due` member instead of leaving it
        # indeterminate: `classify_gate` already maps `review-due` to its
        # own actionable `signal`, already reachable from `scan_triage`'s
        # rc-1 arm, and already folded into `surface` by `evaluate_gate`'s
        # own `_gate_evidence_status_to_verdict` — a re-route, not a new
        # parallel key nobody reads (module docstring "C2" chunk). The
        # verdict-carrier here is `status`, never a fifth value — same
        # precondition, same evidence shape as evaluate_gate's contradiction,
        # keyed on the SAME `_all_blocked_by_shipped_evidence` call so the
        # two evaluators cannot drift apart.
        staleness_evidence = _all_blocked_by_shipped_evidence(
            handoff, blocked_by_ids, live_and_archived_handoffs
        )
        if staleness_evidence is not None:
            return {
                **base,
                "status": "review-due",
                "reason": (
                    "prose gate_dependency present but every structured "
                    "blocked_by member has since shipped — the prose was "
                    "never re-checked against that; re-routed onto "
                    "review-due (not freed: a human still decides whether "
                    "stale prose is safe to retire)"
                ),
                "evidence": [
                    f"gate_dependency={gate_dependency!r} present alongside "
                    f"blocked_by={blocked_by_ids}, all shipped: "
                    + "; ".join(staleness_evidence)
                ],
            }
        return {
            **base,
            "status": "indeterminate",
            "reason": (
                "prose gate_dependency present — dominates any structured "
                "blocked_by outcome per precedence rule (never freed by "
                "structured satisfaction alone; prose gates are a human "
                "evidence-audit lane, not machine-resolvable here)"
            ),
            "evidence": [
                f"gate_dependency={gate_dependency!r} present"
                + (
                    f" alongside blocked_by={blocked_by_ids}"
                    if blocked_by_ids
                    else " (no blocked_by — prose-only gate)"
                )
                + (
                    " (gate_evidence present but covers_prose is not True — "
                    "partial backfill, prose still dominates per D2a)"
                    if has_evidence
                    else ""
                )
            ],
        }

    if has_evidence:
        status, evidence, leg_results = reduce_gate_evidence(gate_evidence)
        return {
            **base,
            "status": status,
            "gate_evidence_legs": leg_results,
            "reason": "no prose gate_dependency — gate_evidence legs are authoritative",
            "evidence": evidence,
        }

    if not _is_structured_gate(handoff):
        # Shared predicate (C4) with evaluate_gate's own vacuous-`clear`
        # branch — see module docstring "C4 RECONCILIATION".
        return {
            **base,
            "status": "freed",
            "reason": (
                "blocked_by is empty and no prose gate_dependency present — "
                "nothing structurally gates this handoff (vacuous ALL over "
                "an empty set); the awaiting_gate state itself may be stale"
            ),
            "evidence": ["blocked_by is empty — nothing to evaluate"],
        }

    all_index = _index_by_id(live_and_archived_handoffs)

    if _has_asymmetry(handoff, blocked_by_ids, all_index):
        return {
            **base,
            "status": "indeterminate",
            "reason": "blocks/blocked_by asymmetry detected — data defect, not auto-repaired",
            "evidence": [
                "blocks/blocked_by asymmetry detected — data defect, not auto-repaired"
            ],
        }

    dispositions = handoff.get("blocked_by_dispositions")
    (
        shipped_ids,
        _shipped_shas,
        dead_ids,
        unresolved_ids,
        still_open_ids,
        disposed_ids,
        unstamped_shipped_ids,
        evidence,
    ) = _classify_blocked_by(blocked_by_ids, all_index, dispositions)

    # Completion-log resolution pass (corpus-gap close): a blocker unresolved
    # against the handoff-only index may still have durable shipped-evidence
    # under archive/completed/ (workstream-complete's chain-terminal
    # completion records — a different schema entirely). Only an unambiguous
    # EXACT chain match promotes a blocker to shipped_ids; a fuzzy/ambiguous
    # match stays unresolved but its evidence is surfaced (see
    # `_resolve_blocker_via_completion_log`'s docstring for the full
    # exact-vs-fuzzy derivation).
    completion_entries = completion_entries or ()
    still_unresolved_ids: List[str] = []
    for uid in unresolved_ids:
        resolution = _resolve_blocker_via_completion_log(uid, completion_entries)
        if resolution["resolution"] == "exact-shipped":
            shipped_ids = shipped_ids + [uid]
            evidence.append(resolution["evidence"])
        else:
            still_unresolved_ids.append(uid)
            if resolution["evidence"]:
                evidence.append(resolution["evidence"])
    unresolved_ids = still_unresolved_ids

    result = {
        **base,
        "shipped_ids": shipped_ids,
        "still_open_ids": still_open_ids,
        "dead_ids": dead_ids,
        "unresolved_ids": unresolved_ids,
        "disposed_ids": disposed_ids,
        "unstamped_shipped_ids": unstamped_shipped_ids,
    }

    if unresolved_ids:
        evidence.append(_unresolved_reason(unresolved_ids, scan_incomplete, scan_errors or ()))
        return {
            **result,
            "status": "indeterminate",
            "reason": "one or more blocked_by ids could not be resolved (handoff index or completion-log)",
            "evidence": evidence,
        }

    if dead_ids:
        return {
            **result,
            "status": "indeterminate",
            "reason": (
                "one or more blocked_by ids resolved to abandoned/continued/"
                "closed — the blocker never shipped and the dependent's "
                "premise may be moot; needs EM judgment, never silently "
                "freed nor silently left still-blocked (a blocker whose "
                "terminus reads shipped, stamped or not, is never counted "
                "here — see unstamped_shipped_ids, C6)"
            ),
            "evidence": evidence,
        }

    if still_open_ids:
        return {
            **result,
            "status": "still-blocked",
            "reason": "every blocked_by id resolves, but at least one has not yet shipped",
            "evidence": evidence,
        }

    if disposed_ids:
        # C7 AC8: a disposed id is neither shipped nor genuinely dangling-
        # and-unexplained — but a disposition records why a ref won't
        # resolve, never that the blocked-on work shipped, so this can never
        # report `freed`. `still-blocked` (not `indeterminate`) so a batch
        # triage pass doesn't keep re-flagging an already-dispositioned edge
        # for human attention on every run.
        return {
            **result,
            "status": "still-blocked",
            "reason": (
                "every non-disposed blocked_by id resolves and has shipped, but "
                "at least one id carries an operator-asserted "
                "resolved_without_baton disposition — a disposition never "
                "frees a gate by itself"
            ),
            "evidence": evidence,
        }

    if unstamped_shipped_ids:
        # C6: a shipped-but-unstamped terminus (direct or chased) has no
        # clearing provenance to record — `still-blocked` (not
        # `indeterminate`, and never through `dead_ids`'s reason text, which
        # would falsely assert the blocker "never shipped") so a batch
        # triage pass keeps flagging it for the operator's terminating
        # action rather than re-surfacing it as an ambiguous dead blocker on
        # every run.
        return {
            **result,
            "status": "still-blocked",
            "reason": (
                "every other blocked_by id resolves and has shipped, but at "
                "least one id's terminus reads shipped with no shipped_in — "
                "stamp shipped_in via handoff.stamp kind: no-commit with the "
                "sanctioned substantively-shipped-no-commit:<YYYY-MM-DD> "
                "token before this can free"
            ),
            "evidence": evidence,
        }

    return {
        **result,
        "status": "freed",
        "reason": "every blocked_by id resolves to deployment_state=shipped",
        "evidence": evidence,
    }


#: `deployment_state` values that are lifecycle POSITIONS, not readiness (PM
#: ruling 2026-08-19, § Anti-scope) — `derive_readiness` has no opinion on any
#: of these and returns (None, None), basis="off-gate-axis". Distinct from
#: `HANDOFF_TERMINAL_DEPLOYMENT` (the blocked_by-classification terminal set):
#: `in_flight` is NOT terminal there but IS off the readiness axis here, and
#: `awaiting_gate`/`ready_to_fire` are ON the readiness axis but not terminal.
_READINESS_OFF_AXIS_STATES: frozenset = frozenset(
    {"in_flight", "shipped", "continued", "closed"}
)

#: The `basis` vocabulary `derive_readiness` emits — the CROSS-MODULE
#: contract a consumer branches on, so it is public rather than a private
#: literal each consumer re-spells. `coordinator_core.session.work_state.
#: build_work_state` partitions its `review_due`/`unclaimed` buckets on
#: exactly these values; duplicated bare string literals on both sides let
#: this producer's vocabulary drift while every test on both sides stays
#: green and the `review_due` bucket goes silently, permanently empty
#: (Review: staff-eng, Finding 9 — the same green-but-wrong class as the
#: three defects that shipped past this plan's own passing suite).
#:
#: `BASIS_BLOCKED_BY_UNRESOLVED` is the one NAMED predicate branch
#: `derive_readiness` evaluates (module docstring C1 brief:
#: "required_fields_empty" is deliberately NOT evaluated here — it moves to
#: the C9 promote call site, where DR-173 actually scopes it). Named so a
#: caller can tell an EM WHICH mechanical condition fired, per the C1
#: brief's basis-naming requirement.
BASIS_BLOCKED_BY_UNRESOLVED = "blocked_by_unresolved"

#: `derive_readiness` takes NO position: `evaluate_gate_triage` reported a
#: gate whose evidence deadline elapsed and wants a human recheck. Never
#: readiness, never blocked — its own bucket at the consumer.
BASIS_REVIEW_DUE = "review-due"

#: The record's `deployment_state` is a lifecycle POSITION, not readiness
#: (`_READINESS_OFF_AXIS_STATES` above); the readiness axis is never reached.
BASIS_OFF_GATE_AXIS = "off-gate-axis"


def derive_readiness(
    handoff: Dict[str, Any],
    all_handoffs: Sequence[Dict[str, Any]],
    *,
    scan_incomplete: bool = False,
) -> Dict[str, Any]:
    """Home the readiness derivation ONCE, on the existing gate resolver (C1,
    docs/plans/2026-08-19-gate-notes-are-advisory-blocked-by-derives-readiness.md § C1).

    Pure function: given one handoff's frontmatter dict plus the resolution
    index `evaluate_gate_triage` already takes (the live+archived handoff
    set), derives `deployment_state`/`pickup_ready` from MECHANICALLY-
    CHECKABLE conditions only, of which an unresolved `blocked_by` is the
    first (and, in this function, the only one evaluated — see
    "required_fields_empty" below).

    Delegates the graph verdict to `evaluate_gate_triage(..., consult_prose_
    gates=False)` — this function does NOT reimplement `blocked_by`
    resolution itself; that duplication is the AC7 failure mode this module's
    own docstring exists to prevent ("a second file that reads handoff
    frontmatter and independently decides gate status would be the shape to
    avoid"). `derive_readiness` is the ONLY caller in the repo entitled to
    pass `consult_prose_gates=False` — see `evaluate_gate_triage`'s own
    docstring for why (every other caller keeps the default `True`,
    unaffected).

    Rules (exactly, per the C1 dispatch brief):
        - `evaluate_gate_triage` status `still-blocked`/`indeterminate` ->
          `("awaiting_gate", False)`, basis="blocked_by_unresolved".
        - status `freed` -> `("ready_to_fire", True)`,
          basis="blocked_by_unresolved" (this covers BOTH a satisfied
          structured graph AND the vacuous empty-`blocked_by` case — an empty
          graph does not skip the predicate set, it is simply the trivial
          for-all-over-the-empty-set case the SAME predicate already handles;
          see `evaluate_gate_triage`'s own "vacuously freed" branch).
        - status `review-due` -> NO opinion, `(None, None)`,
          basis="review-due". The verdict is a prompt for a human recheck;
          auto-promoting it is the auto-promotion `classify_gate`'s own
          negative-spec already forbids.

    THE PREDICATES — the generalisation the PM's ruling asks for. Readiness
    derives from mechanically-checkable conditions, of which an unresolved
    `blocked_by` is merely the first:
        - `blocked_by_unresolved` — the graph verdict above (evaluated here).
        - `required_fields_empty` — DR-173's `category`/`summary`-unfilled
          check on a PROMOTED baton. NOT evaluated here (C9's promote call
          site owns it): this function sees only pure frontmatter and cannot
          tell a promoted baton from any other record — `kind: session-
          handoff` alone does not mean "promoted", so folding this predicate
          in here would either silently widen DR-173's promote-only gate to
          every call site (C3 scaffold, C5 brief, C6 transition) or need a
          caller-scoping parameter this function's pure-frontmatter contract
          forbids.

    NEGATIVE-SPEC (the durable half of the PM's ruling):
        - `deployment_state` already in {in_flight, shipped, continued,
          closed} -> `(None, None)`, basis="off-gate-axis". Those four are
          lifecycle POSITIONS, not readiness.
        - `blocking_notes` is READ BY NOTHING HERE — enforced by
          `consult_prose_gates=False` at the call into `evaluate_gate_triage`
          above, not merely by this function never mentioning the field. Not
          an input, not a tiebreak, not a predicate, and not eligible to
          become one. Prose is not mechanically derivable, so it cannot gate
          (PM ruling 2026-08-19, verbatim: "blocking_notes shouldn't give a
          mechanical block because it's not mechanically derivable, it's a
          query thing, because it can be anything in prose"). DR-173 and
          DR-259 do NOT license reading it here — both are reconciled at
          their own call sites (DR-173 -> C9's promote-time predicate;
          DR-259 -> the vacuous-clear-prevention leg `evaluate_gate_triage`
          already scopes off derived readiness via `consult_prose_gates=
          False`), neither reopens this function's own contract.

    A `(None, None)` return means "derivation has no opinion — leave what the
    author wrote". That is what makes every call site safe to wire
    unconditionally.

    Returns:
        {"deployment_state": <str|None>, "pickup_ready": <bool|None>,
         "basis": <str>}
    """
    deployment_state = handoff.get("deployment_state")
    if deployment_state in _READINESS_OFF_AXIS_STATES:
        return {
            "deployment_state": None,
            "pickup_ready": None,
            "basis": BASIS_OFF_GATE_AXIS,
        }

    triage = evaluate_gate_triage(
        handoff,
        all_handoffs,
        scan_incomplete=scan_incomplete,
        consult_prose_gates=False,
    )
    status = triage["status"]

    if status in ("still-blocked", "indeterminate"):
        return {
            "deployment_state": "awaiting_gate",
            "pickup_ready": False,
            "basis": BASIS_BLOCKED_BY_UNRESOLVED,
        }
    if status == "freed":
        return {
            "deployment_state": "ready_to_fire",
            "pickup_ready": True,
            "basis": BASIS_BLOCKED_BY_UNRESOLVED,
        }
    # status == "review-due": a prompt for a human recheck, never auto-
    # promoted — see rule above.
    return {"deployment_state": None, "pickup_ready": None, "basis": BASIS_REVIEW_DUE}


def derive_readiness_batch(
    handoffs: Sequence[Dict[str, Any]],
    all_handoffs: Sequence[Dict[str, Any]],
    *,
    scan_incomplete: bool = False,
) -> List[Dict[str, Any]]:
    """Batched `derive_readiness` — builds the resolution index ONCE and
    projects it over `handoffs`, rather than a corpus-keyed caller rebuilding
    it N times by calling `derive_readiness` per record in a loop (C1
    dispatch brief, "TWO CORRECTIONS FROM claude-klabauter-d3": their readout
    is ~148 live handoffs per repo and fleet-wide across siblings).

    `evaluate_gate_triage` itself takes the prewalked `all_handoffs` sequence
    per call and re-derives its own index internally (`_index_by_id`) each
    time — this function does not attempt to hoist THAT index construction
    out from under `evaluate_gate_triage`, since doing so would require
    reaching into its private `_index_by_id` call, i.e. a second place
    deciding how the index is built (the exact sibling-evaluator shape this
    module exists to avoid). What this function DOES hoist is `all_handoffs`
    itself: callers building a corpus-keyed batch pass the SAME sequence
    once, in the SAME order, for every record in `handoffs` — the cheap half
    of "build once, project over the set" available without touching
    `evaluate_gate_triage`'s own internals.

    Returns one `derive_readiness`-shaped dict per handoff in `handoffs`, in
    the same order.
    """
    return [
        derive_readiness(handoff, all_handoffs, scan_incomplete=scan_incomplete)
        for handoff in handoffs
    ]
