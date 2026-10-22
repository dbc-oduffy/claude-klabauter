---
title: "The typed alternative record — make a bare-override guard message unwritable, and address it to the right seat"
created: 2026-07-29
author: coordinator-content-repo-em
status: draft
branch: "work/machine-b/2026-07-21to26"
plan_id: "pln-typed-alternative-record-seam-a41f07"
deliverable_id: "dlv-duty-of-care-the-bash-alternatives-are-t-385ac5"
initiative: null  # FK to state/initiatives/<id>.yaml; null when no named initiative
scope_mode: architecture
problem_set: inline
scope:
  - coordinator_core/bash_guards/ (claude-klabauter)
  - coordinator/subagent-sandbox-policy.yaml
  - coordinator/docs/wiki/bash-guard-threat-model.md
execution_authorized_by: PM
execution_authorized_at: 2026-07-29
execution_authorized_sha: 8476eedf039e3af11cf8ff0c61d42f6fa9c396eb
execution_authorized_note: 'Authorize the whole plan — all 9 rows (S0–S6), selected at the execution gate after the cross-band reconciliation closed. Followed by: ''and handoff for execution, because this back and forth is too annoying.'''
---

# The typed alternative record

> **PM-ratified 2026-07-29**, spun out of
> `docs/plans/2026-07-29-duty-of-care-the-liveness-gate-and-the-l.md` row `D3` after two
> successive gate designs failed to discharge the duty-of-care ladder. This plan is the
> structural answer those gates were approximating.
>
> It is engine-subject and touches a surface the confinement session also owns. The PM has
> stated they will reconcile plans across sessions as a following step, so this plan is written
> to be the best version of itself rather than pre-negotiated down. Where a decision genuinely
> belongs to both bands it is named as such rather than assumed.

## Problem

A bash guard refuses or reshapes a command on an agent's behalf, and every refusal is a
caller-facing message. Those 94 messages were graded on a duty-of-care ladder: **A** hands over a
ready-to-run alternative, **B** an offer needing adaptation, **C** describes the problem and
offers only the `COORDINATOR_ALLOW_*` escape hatch, **D** refuses bare. The inventory found
A=6, B=27, C=45, D=16.

The defect is structural, not editorial: **in every one of the 45 rung-C messages, the override
line is the only copy-pasteable text.** The single easiest action the guard package offers an
agent is turning the guard off. Everything else it might do, it must re-derive.

Two attempts to fix this by inspection have now failed, and their failure is the argument for
this plan:

- **A liveness gate** — assert every named alternative still works — cannot fail a single rung-C
  or rung-D row. A message whose only actionable text is `export COORDINATOR_ALLOW_X=1` is
  *maximally live* the moment the guard genuinely reads that variable. Liveness and rung are
  orthogonal.
- **A rung-floor gate with a pinned per-guard baseline** cannot either. Pinning today's grade
  grandfathers all 61 known-bad rows, so on the day it lands it fails zero of them. It prevents
  regression; it does not produce improvement.

Both were killed by review for the same reason in different costumes: *the test of a gate is not
what it asserts, it is which currently-shipping rows it fails on the day it lands.* A gate reads
prose that has already been written. It complains afterwards. It cannot stop the prose being
written in the first place.

### Five findings that make the prose itself the problem

**The extraction is guesswork, permanently.** Both gates work by parsing our own emitted English
to discover what alternative a message named. That parser has been tuned once already; backticked
shape names and Python API references read as candidate executables. Every future message shape is
a new parsing risk, and the parser can never be more than a heuristic over free text we control.

**A false capability claim shipped inside the guard package itself.** `guard_grep_via_bash`
asserted to every caller that "there is no harness-native (zero-fork) search from this session"
long after `guard_inprocess_search` landed and began answering greps in-process. It was found by a
reviewer whose own greps were being answered in-process while it read the message saying they
could not be. Prose asserts capabilities; nothing binds the assertion to the capability.

**The offer is frequently impossible for the caller receiving it.** `coordinator:code-reviewer` is
granted exactly `ls, cat, head, tail, wc, find, file, stat, grep` plus read-only git. **`env` is
absent. `export` is absent.** The confining guard ships no escape hatch of its own. So *no*
override in the package is settable by that class — yet the liveness probe grades all of them
LIVE, because it sets the variable inside the gate's own process where no sandbox policy applies.
Rung is being treated as a property of a message when it is a property of a **(message, caller
class)** pair.

**Free-prose guard messages read as injected content to their own intended reader.** <!-- band
enumeration (2026-07-29): observed, not derived. --> A dispatched read-only subagent, working
this codebase with no knowledge of this plan, independently reported that several Bash tool
outputs "contained injected 'advisory'/'reminder' text (styled as coordinator-hook system
messages) urging me to set override environment variables or change search behavior. These did
not come from you or from the dispatching EM — I treated them as untrusted content embedded in
file/command output, took no action on them." The subagent had no way to distinguish a genuine
guard advisory from a prompt-injection attempt, because both arrive as free English text in tool
output with no structural marker separating them. Stated factually, not as an overclaimed
security finding: the point is that the message shape is indistinguishable from injection to its
own intended reader, whatever else is true of it. It also independently corroborates the PM
ruling below — that subagent both could not use the override it was shown and correctly
distrusted it.

**The unconfined audience the override line is legitimately addressed to does not use it
either.** <!-- Reconciliation close (archive/memos/2026-07-29-typed-alternative-record-band-
reconciliation-close.md § 5), 2026-07-29, attributed to the peer session's own session-log
self-report rather than an instrumented measurement. --> The peer session in the reconciliation
thread is the opposite audience from the subagent finding above: an unconfined EM, the one
reader the override is legitimately addressed to under the PM's EM-vs-IC ruling below. Across
that session it received on the order of dozens of guard advisories ending in a `To bypass:
export COORDINATOR_ALLOW_*` / `COORDINATOR_OVERRIDE_*` offer, and acted on zero of them — not
from distrust, since it was the authorized audience and could have used any, but because the
bypass was never the right move in a single case. Twice the advisory itself changed its
behaviour (it stopped chaining `cd` into git commands; it reached for the in-process search
path); the override was never the answer. So the override line has a weak claim on either
audience: unusable and injection-shaped to the confined caller, inert to the unconfined one.
**This is recorded as evidence for this plan's record shape; it does not change the design.**
Whether the EM rendering should carry the advisory without the override switch revisits the PM
ruling below and is an open question reserved for the PM — not decided here, and S2's design,
TA5, and its acceptance criterion are unchanged by it.

### The PM ruling that reframes the ladder

**"Set a variable to turn me off" is an EM call, not an IC call** (PM, 2026-07-29). A dispatched
subagent should never be handed the off-switch for a safety check it just tripped. The EM still
sees it.

This is not a politeness rule; it is a reading of who the caller actually is. The caller is a
capable, eager, amnesiac agent optimising to complete its task — not an adversary, and not a
careless operator. Handing that caller a sanctioned-looking bypass is the path of least resistance
pointed at the wrong exit. Removing it is worth more than promoting a dozen messages one grade,
because it changes what the easiest available action IS.

The ruling also dissolves the reachability defect for the population it most affected: stop
advertising to confined subagents an override they could never type.

### What this plan builds

Guards stop returning free prose and return a **typed alternative record**. A bare-override
message then has no argument slot to exist in — the rung-C shape becomes *unwritable* rather than
detectable. Rung is computed from the record, not parsed from a sentence, which retires the whole
extraction false-positive class at the root instead of tuning it. And because the record is
structured, the composer can render it differently for the EM and for a subagent from one place,
rather than at 94 call sites.

This is the repo's discharge test applied to our own package: *for every rule, what artifact
discharges it? If the answer is "the author remembers," the work is not finished.* Today the rule
is "don't write a message whose only offer is the override," and the discharging artifact is the
author's memory. After this plan, it is the type.

## Oracle — the found facts this plan's slate must cover

> Read these as claims against a moving tree. The predecessor plan had nine of nineteen oracle
> rows go stale or invert within a day. Re-verify at source before building on a row.

| # | Found fact | Source |
|---|---|---|
| T1 | 45 rung-C messages where the override line is the only copy-pasteable text | `state/audits/2026-07-29-bash-guard-caller-text-inventory.md` |
| T2 | 16 rung-D messages offering nothing at all | same |
| T3 | A liveness gate cannot fail any C or D row — liveness and rung are orthogonal | the Director of Engineering review F1 |
<!-- Review: coordinator:plan-coverage-checker — T3 covered-by-design (no explicit slate citation): the design choice it motivates (typed record over gate-parsing) is the plan's whole premise, stated in ## Problem and discharged structurally by S1/S5. Cited here so a later pass does not read it as missed. -->
| T4 | A baseline-pinned rung gate fails zero of the 61 rows on the day it lands | the Staff Engineer review F0 |
<!-- Review: coordinator:plan-coverage-checker — T4 covered-by-design (no explicit slate citation): same background-rationale shape as T3 — the reason gate-based approaches were abandoned, discharged by the seam itself (S1/S5) rather than named in a task row. -->
| T5 | The rung criterion `{override}` cannot fail a rung-D message: an empty alternative set is not `{override}`, and the gate module's negative-spec forbids failing empty-set messages | the Staff Engineer review F1 |
<!-- Review: coordinator:plan-coverage-checker — T5 covered-by-design (no explicit slate citation): TA1's "no bare-string refusal envelope" and TA5's "never empty — carries an escalation route" jointly discharge the rung-D failure mode this row names. -->
| T6 | Gate population is guards (~25 triggers, 33 alternatives); ladder population is 94 messages. A per-guard grade samples ~1/4 of the population and is silent on the rest | the Staff Engineer review F7 |
| T7 | Prose extraction produced a false-DEAD class once already (backticked shape names, Python API references read as executables) | the Director of Engineering review F4 |
| T8 | A false harness-capability claim shipped live in `guard_grep_via_bash` while the gate reported green over it | the Staff Engineer review F3; fixed in claude-klabauter `6f3ae246` |
| T9 | `env` and `export` are both absent from `coordinator:code-reviewer`'s allowlist; the confining guard ships no hatch — no override in the package is typeable by that class | the Staff Engineer review F12, verified against `coordinator/subagent-sandbox-policy.yaml` |
| T10 | `probe_override` sets the variable in the gate's own process, so it certifies readability and never reachability | verified at `_alternative_liveness.py` `probe_override` |
| T11 | Rung is a property of a (message, caller class) pair, not of a message | the Staff Engineer review F13 |
| T12 | `guard_inprocess_search` returns a DENY as its SUCCESS shape ("already handled"), so any rung logic keying on verdict class grades the best-behaved guard as a hard refusal | the Staff Engineer review, edge cases |
| T13 | The effective caller-class type is resolved via the same OR-resolver `block_reviewer_bash_outside_allowlist.check()` uses — `agent_type = payload.get('agent_type') or ''` OR'd with `subagent_type` from the backpointer resolver, not the raw `agent_type` payload field alone; `subagent-sandbox-policy.yaml` is DoE-owned and the oracle needs no peer coordination | the Staff Engineer follow-up |
<!-- Review: the Director of Engineering (eng-director) — F10 (minor): T13's original wording ("the payload already carries agent_type... needs no peer coordination") understates the mechanism. `block_reviewer_bash_outside_allowlist.check()` ORs TWO sources — `agent_type = payload.get('agent_type') or ''` (verified 2026-07-29 at line 1525) and `subagent_type = _read_backpointer_subagent_type(...)` (line 1528) — then resolves confinement as `_is_confined_type(agent_type, policy) or _is_confined_type(subagent_type, policy)` (line 1544) and picks `effective_type` from whichever matched (line 1552). A resolver keyed on the raw payload field alone under-resolves exactly the confined-subagent population TA6/TA5 exist for. S2/S3 must specify the effective type via this same OR-resolver (or a shared helper extracted from it), not `payload['agent_type']` directly. -->

## Acceptance Criteria

| ID | Criterion | Test | Binding-Class | Status |
|----|-----------|------|---------------|--------|
| TA1 | A guard cannot emit a caller-facing refusal except by constructing an alternative record; free-string emission is removed from the guard-facing API, not merely discouraged | no guard module constructs a refusal envelope from a bare string; asserted by introspection over the package | mechanical | pending realization |
<!-- prior-art-check (2026-07-29): TA1/TA8's completeness argument — whole-package introspection proving no unconverted site remains, not a per-site review — is backed by `state/lessons/2026-07-29-closing-one-shape-of-a-bypass-is-not-closing-the-class.yaml` (universal scope): "Closing one shape of a bypass reads as closing the class — enumerate the class, then attack every member... unless N was enumerated." -->
<!-- Review: the Director of Engineering (eng-director) — F4 (major, cited again at TA8 below): the same lesson class applies to the named-exception-set problem — an uncapped exception list is the allowlist-degeneration shape that lesson family names. -->
<!-- Review: the Director of Engineering (eng-director) — F1 (critical): the guard-facing `check()` contract returns the harness's own nested envelope (`{"hookSpecificOutput": {...}}`), with the caller text a free string. So long as `check()` returns a dict, a guard can hand-build the envelope directly and bypass the record; "asserted by introspection over the package" is a heuristic over our own Python, the same class of artifact as the prose extractor TA3 deletes. Verified 2026-07-29 at claude-klabauter `coordinator_core/bash_guards/block_worktree_creation.py:437-443` (envelope literal unchanged at that line range) and `coordinator_core/write_guards/INTERFACE.md:34` (fidelity rule 2, "Preserve every permissionDecisionReason / advisory string BYTE-FOR-BYTE" — still present, still unamended, and it directly contradicts TA1's premise unless narrowed in the same change). -->
<!-- Review: the Director of Engineering (eng-director) — F3 (major): "type-level" overclaims — Python's type system cannot express this invariant, it is a runtime check on the construction path. Restated as a construction-time invariant, with an explicit statement of what it does and does not catch (bare override vs. degenerate companion). See also S1's body for the corresponding "load-bearing row" correction. -->
| TA2 | The record has no field in which a bare override can be the sole actionable content — an override is a distinct, optional, EM-audience field that cannot stand as the primary alternative | construction-time invariant, not type-level: nothing in Python's type system expresses "this field may not be the only one populated"; a frozen dataclass raises in `__post_init__` when `override` is populated and no other actionable member is, on every construction unconditionally, with a test asserting the failure. This catches the BARE-override shape; it does not catch a degenerate companion (e.g. boilerplate stuffed into `escalation` alongside the override) — that shape is caught by TA9's differential render, not by this invariant. | mechanical | pending realization |
| TA3 | Rung is computed from the record, never parsed from rendered text; the prose extractor is deleted, not retained as a fallback | no extraction path remains in the rung computation; asserted by absence plus a test that a record with adversarial prose in its free-text fields still grades on structure | mechanical | pending realization |
| TA4 | Rung is recorded against a (message, caller class) pair; a single-keyed grade fails the schema | schema test over the rung inventory | mechanical | pending realization |
| TA5 | A subagent-audience rendering never contains an override/disable set-instruction as actionable content, and is never empty — it carries an escalation route naming what to report upward | per-guard rendering test over both audiences | mechanical | pending realization |
| TA6 | An alternative is graded reachable only if expressible under the receiving caller class's `bash_policy` entry; `coordinator:code-reviewer` receiving an `env`-prefixed alternative fails | reachability probe resolving the policy table against the OR-resolved effective caller class (T13), with the verified `env`/`export` absence as a fixture | mechanical | pending realization |
| TA7 | A capability claim in a record is a reference to a capability oracle, not a sentence — a claim naming a capability that the oracle does not report fails at construction | test planting the exact `guard_grep_via_bash` false-capability defect and asserting it cannot be constructed | mechanical | pending realization |
<!-- Review: the Director of Engineering (eng-director) — F6 (major): TA7 as written depends on a capability oracle that
did not exist at review time -- `probe_harness_capability` (claude-klabauter
_alternative_liveness.py:979) returns UNVERIFIABLE unconditionally by deliberate design, and the
structured oracle TA7 needs was filed as an ask in
state/audits/2026-07-29-rewrite-path-api-asks.md rather than built. The Director of Engineering's recommendation was to
narrow TA7 to a hand-maintained registry-key reference instead of building the oracle. PM decision
(2026-07-29, follow-up pass): build the oracle in scope rather than narrow the criterion -- see new
row S0. TA7's text is therefore left exactly as written; it is correct-as-stated once S0 lands, not
aspirational. -->
<!-- Review: the Director of Engineering (eng-director) — F4 (major): a named exception set with no cap, no shrink schedule, and no written-reason schema is the per-file-allowlist shape `state/lessons/2026-07-22-a-gate-with-a-per-file-allowlist-enforce-534d431a970b.yaml` (universal scope) prohibits — "any per-file exemption list degenerates into 'whatever exists today'... hands a future agent the exact affordance the gate exists to deny." Same lesson the Staff Engineer turned on C8's baseline. Fix: exact-set equality test (not subset), a pinned maximum count that may only decrease, and a required written reason per member naming a physics/impossibility constraint, not an appetite. If the intended count is zero, pin it at zero. -->
| TA8 | Migration is complete and verifiable: every one of the ~94 message sites is converted, with any deliberate exception in an EXACT-SET-EQUALITY-tested named set, count pinned and monotonically non-increasing (0 unless a physics/impossibility reason is argued and written per member) | introspection asserting no unconverted site remains outside the named set; the named-set test is an equality check against the pinned count, not a subset check | mechanical | pending realization |
<!-- Review: the Director of Engineering (eng-director) — F5 (major, highest-value amendment on the list): TA9 as specified is a wish — it requires "old text vs new rendering, per site" for ~94 sites, but a guard message cannot be rendered without firing the branch that emits it, and the package's only trigger corpus (LIVE_TRIGGERS, ~25 triggers) reaches roughly a quarter of the 94 branches. The fix for that population gap (one trigger per known message branch, the Staff Engineer F7) was scoped to the now-retired C8, and its prerequisite is orphaned by that retirement. Re-homing it here (new row before S4, see Tasks) plus a committed rendered-baseline fixture converts TA9 from a judgment row (human-or-reviewer confirmed — "the reviewer remembers", the north star's named failure state) into a mechanical one: a test that renders every branch post-conversion and diffs against the fixture. Backed by `state/lessons/2026-07-29-verify-a-rewrite-differentially-not-by-running-it.yaml` (universal scope): "Test a rewrite by executing BOTH sides against the same fixtures and diffing the observable output byte-for-byte." -->
| TA9 | Rendered output for every converted site is reviewed against its pre-migration text, so the conversion does not silently downgrade a message that was already rung-A or rung-B | differential render: old text vs new rendering, per site, against a committed pre-migration baseline fixture built from a per-branch trigger corpus (see task row before S4) and captured PER BAND, immediately before that band's own conversion, not once globally up front — a global baseline mixes a peer band's in-flight promotion diffs into this criterion's conversion-regression signal and destroys it (reconciliation reply § 4, 2026-07-29; see S3C, S5); the corpus is keyed on chain-entry name, never module path or qualified function name, because a corpus that silently loses rows to a rename reports green over branches it can no longer find (reconciliation close § 3, 2026-07-29; see S3C); rung-A/rung-B sites require an explicit reviewed acknowledgement to change | mechanical | pending realization |
| TA10 | No cell in the 304-cell confinement attack corpus changes state | `test_confinement_attack_corpus.py` | mechanical | pending realization |
| TA11 | A guard whose success shape is a DENY (`guard_inprocess_search`) is graded on record content, not verdict class, and does not appear as a refusal in any rung report | test over that guard specifically | mechanical | pending realization |

## Anti-scope

- **Do not keep the prose extractor as a fallback.** A structured path plus a prose fallback is
  the worst of both: the fallback becomes the path of least resistance for the next author, and
  the false-positive class this plan exists to retire survives inside it. If a site cannot be
  converted, it goes in the named exception set with a reason — not down a parsing path.
- **Do not let the record become a string with extra steps.** A single free-text field that
  callers stuff a whole message into reproduces today's problem behind a type. Actionable content
  is structured; free text is for context only and must not be where the alternative lives.
- **Never edit the shared tokenizer, the env/wrapper peeler, or `_shape_classifier.py`**, and
  never hand-roll a parallel command parser. This package found five quote-blind matchers in a
  single day; an N+1th is the exact defect class this work removes.
- **Do not widen a guard's verdict while converting it.** Migration changes how a message is
  constructed and rendered, not when a guard fires or what it decides. A verdict change riding
  along inside a 94-site migration is unreviewable.
- **Do not report Windows as covered.** Every measurement in this workstream is macOS-observed;
  cross-platform branching is reasoned and unverified. Deferred by PM direction — deferred, not
  cancelled.
- **Do not treat the audience split as cosmetic rendering.** If the subagent rendering is produced
  by string-filtering the EM rendering, a future message shape leaks the override through. The
  audience is a property of the field, decided at construction.

## Out of scope

- **Promoting individual messages up the ladder.** That is the predecessor plan's `D2`, running in
  parallel. This plan changes the medium; that one improves the content. They meet where a
  promotion lands on top of a converted site rather than under it.
- **The liveness gate.** It survives this plan unchanged — a typed record still names something
  that can be dead, and only execution can tell. It is not retired here.
- **The rung ratchet** (predecessor `C8`). Its retirement condition is this plan landing; its
  baselines are expected to be discarded rather than migrated.
- **Bypass hardening and the confinement band's guards.** <!-- band enumeration (2026-07-29):
  the `block_*` prefix is NOT the band criterion — `block_illegal_filename.py` is
  `CLASS = "advisory"` (its own docstring: "Posture: ADVISORY-ONLY — this arm NEVER blocks")
  and carries the prefix while being advisory, not confinement; conversely
  `check_raw_pid_liveness.py`, `check_test_suite_invocation.py`, and (see S4's body) three
  `guard_*`-named hard-deny modules are confinement without the prefix. The actual criterion is
  the wiki's own, per `coordinator/docs/wiki/bash-guard-threat-model.md` § Two bands: "Confinement
  guards answer *may this run at all*; advisory/rewrite guards answer *is there a cheaper way to
  run it*" — mechanically, `fail_closed=True` in `dispatch.py`'s `guard_chain`. Naming the prefix
  as the boundary would mis-scope it in both directions; stated here so a later reader does not
  re-derive the same mistake. -->
- **Tool grants and agent policy**, except read-only consumption of the `bash_policy` table for
  reachability.

## Tasks

```yaml plan-tasks
- id: S0
  title: Build the structured harness-capability oracle TA7 depends on
  change_kind: script-edit
  surface: coordinator_core/bash_guards/
  queue_scope: project
  deferred: false
  body: |
    New row (the Director of Engineering/eng-director review, F6, major; PM decision, follow-up
    pass: build the oracle in scope rather than narrow TA7 -- the Director of Engineering's
    narrowing recommendation was NOT taken). Discharges TA7's dependency and
    must land before S1, since S1's negative-construction test for TA7
    references this oracle.

    Filed status, read at source before this row was written: the ask is
    state/audits/2026-07-29-rewrite-path-api-asks.md § "Ask: a structured
    (non-prose) harness-capability oracle for the alternative-liveness
    gate" -- it proposes "a structured capability manifest -- e.g. a
    YAML/JSON mapping of capability_id -> {available: true|false|unknown,
    per host/agent-type if it varies}". This row SATISFIES that filed ask
    (does not supersede it): the ask's shape is sound and unbuilt, not
    wrong or stale, so this row builds what it specifies rather than
    replacing its design. Close the ask entry (git mv to archive per this
    repo's queue-closure convention) once the manifest lands.

    Build the manifest as a small, hand-maintained, committed
    capability_id -> {available, per-host/per-agent-type if it varies}
    mapping, seeded from the currently-known true capability set (at
    minimum: the guard_inprocess_search in-process-search capability that
    guard_grep_via_bash falsely denied, per T8/state/audits/2026-07-29-
    search-capability-roster-truth.md).

    `probe_harness_capability` (claude-klabauter
    coordinator_core/bash_guards/_alternative_liveness.py:979) currently
    returns UNVERIFIABLE unconditionally, by deliberate design (its own
    docstring: a prior revision keyed a substring match against a prose
    audit document as corroboration, and the Director of Engineering's review on that revision
    caught it inverting the gate's founding defect -- a REFUTATION document
    read as agreement). Read at source before writing this row. That
    function does NOT get replaced by this row -- it becomes the oracle's
    CONSUMER: it looks up `alt.detail`'s capability id in the manifest and
    returns LIVE/DEAD when the manifest has an entry, and continues
    returning the same honest UNVERIFIABLE (never a guessed verdict) when
    the id is absent from the manifest. The manifest is keyed by id, never
    by prose match, so the exact inversion defect the docstring records
    cannot recur through this path.

    Negative-spec (the failure mode this row exists to prevent, named by
    the Director of Engineering's F6 and by the Staff Engineer on the predecessor plan's AC5): a FIXTURE
    oracle that only asserts the test harness itself works satisfies TA7's
    letter while discharging nothing -- it proves a stub answers a stub.
    The oracle built here must report REAL capability state for the
    package's actual guards, and TA7's test -- planting the exact
    `guard_grep_via_bash` false-capability defect (T8) and asserting it
    cannot be constructed -- must fail when run against this REAL oracle,
    not against a stand-in. A green TA7 test that only exercises a fixture
    is not a discharge of this row.

    Cost, named plainly for the record: this row adds a previously-unbuilt
    subsystem (a capability manifest plus its lookup path) to a plan that
    already carries a ~94-site migration (S4/TA8). That is a deliberate
    scope increase the PM accepted in the follow-up pass that produced this
    row, in preference to the Director of Engineering's narrower registry-key alternative -- not
    scope creep discovered later.
- id: S1
  title: Design the alternative-record type and prove a bare-override message is unconstructible
  change_kind: script-edit
  surface: coordinator_core/bash_guards/
  queue_scope: project
  deferred: false
  body: |
    Covers TA2, TA7. (TA1's structural enforcement is discharged by the new
    row before S4 that narrows check() -- see below.) This row makes the fix
    EXPRESSIBLE; it does not by
    itself make anything happen. Review correction (the Director of Engineering/eng-director, F3,
    major): applying the plan's own test -- which currently-shipping rows
    does a gate fail on the day it lands? -- S1 fails ZERO rows, because on
    landing day no site yet constructs the record. That is fine: S1 is not a
    gate, it is the thing that makes a gate unnecessary. The row that
    actually discharges the obligation, by failing the 45 rung-C rows as they
    convert, is S4, gated on TA8 -- S4/TA8 is the load-bearing row, not this
    one.
    The record distinguishes, at minimum: a ready-to-run command; an offer
    needing adaptation; a capability reference resolved against an oracle
    rather than asserted in prose; an escalation route; and an override, which
    is EM-audience, optional, and CANNOT stand as the sole actionable member.
    Rung falls out of which members are populated, which is what makes TA3
    possible.
    Acceptance argument is a negative one and must be written first: a test
    that TRIES to construct the 45 rung-C shape and fails. A type that merely
    discourages the shape has not discharged anything.
    Homing (the Director of Engineering/eng-director review, F8, major): coordinator_core/bash_guards/
    is where the record is FIRST CONSUMED, not where it is OWNED. The
    identical envelope is constructed at 44 sites in
    coordinator_core/write_guards/ and at 27 DoE-side hook scripts under
    coordinator/hooks/scripts/ -- two of which already ship the same
    "To bypass: export COORDINATOR_ALLOW_*" shape this plan exists to make
    unconstructible. Those populations were never graded because the 94-
    message audit was bash_guards-scoped by construction; their absence from
    the count is a scoping artifact, not a clean bill. Home the type one
    level up (coordinator_core/guard_messages/ or equivalent) so write_guards
    and any future PreToolUse cohort can consume the same type without an
    upward dependency on bash_guards. Converting the sibling cohorts is out
    of scope for this plan; making them unable to consume the type is not.
- id: S2
  title: Add the audience dimension — EM sees the override, a subagent gets an escalation route
  change_kind: script-edit
  surface: coordinator_core/bash_guards/
  queue_scope: project
  deferred: false
  body: |
    Covers TA5 and the PM ruling. Audience is a property of a record FIELD,
    resolved at construction, never a filter applied to rendered text — a
    string filter leaks the next message shape through.
    The caller-class oracle already exists on our side of the band cut (T13):
    the payload carries agent_type and the policy table is DoE-owned.

    Do NOT build a new discriminator. `block_reviewer_bash_outside_allowlist.
    check()` already does this discrimination in production, and its sequence
    is the precedent: NO `agent_id` on the payload means a top-level EM call
    (it returns early before paying identity-resolution cost); an `agent_id`
    present means a subagent, and `_resolve_subagent_identity` +
    `_read_backpointer_subagent_type` resolve which type.
    That gives three tiers, not two: EM; subagent of known type (whose
    bash_policy entry says what it can actually express); and subagent whose
    identity does not resolve. Decide the third deliberately — that guard fails
    OPEN there, correct for a confinement check and wrong for an audience
    decision. An unresolvable caller gets the SUBAGENT rendering: handing the
    override to an unknown caller is the exact failure this ruling prevents.
    Negative-spec: removing the override from the subagent rendering and
    leaving nothing behind converts rung C to rung D for that audience. The
    escalation route is mandatory, not a nicety — name what to report upward
    and what needs authorizing.
    Review correction (the Director of Engineering/eng-director, F2, critical): the prior wording
    here claimed this row was "independently shippable ahead of the full
    migration". That contradicts the anti-scope's own ban on the filter shape
    (below): the audience property is a record FIELD, resolved at
    construction, so it cannot exist before S1's record does. The fix is not
    to reorder task ids -- S1 already precedes S2 in this spine -- it is that
    S1 must land FIRST in wall-clock time rather than being treated as
    deferrable while S2 goes first. S1 is small (one frozen dataclass plus a
    constructor invariant, hours not a wave), so landing it first does not
    defer the PM ruling's value: S1 then S2 still land in the same wave.

    <!-- Reconciliation response (archive/memos/2026-07-29-typed-alternative-record-band-
    reconciliation-response.md § 3), 2026-07-29: C12 is STRUCK, not labelled --
    exit 1 of the two offered, taken. Not a concession on reading our input:
    the decision had already landed independently, via the cross-plan review
    finding F5 (docs/plans/2026-07-29-bash-guard-merged-execution-shape.md),
    which concluded C12 and this plan's seam are the same mechanism -- C12's
    own body already said as much ("fold it into the seam's record shape when
    it is [ready]"). The merged plan routes the audience field to this seam as
    a coordination ask, with a standalone 94-site sweep kept only as a
    fallback if this seam slips; the PM accepted F5 explicitly. No interim
    string-filter implementation ships from the peer side, so the dual-path
    steady state this plan's anti-scope forbids does not arise from C12's
    direction. The audience requirement itself does not disappear -- it
    becomes an obligation on THIS record's shape, already carried by S2 above
    (escalation route mandatory, never an override, never implemented as
    deletion). C12 and this plan's S1-then-S2 sequencing are no longer an
    open cross-plan dependency. -->
    Prior open cross-plan dependency (RESOLVED, see note above): the parent
    plan (docs/plans/2026-07-29-duty-of-care-the-liveness-gate-and-the-l.md,
    row C12) and this plan's HANDOFF both instructed "ship the audience split
    first, independently" -- the same ordering this finding corrects here.
    C12 is struck in favor of S2; no further reconciliation is owed on this
    point.
- id: S3
  title: Build the reachability probe — an alternative is live only if the receiving caller can express it
  change_kind: script-edit
  surface: coordinator_core/bash_guards/_alternative_liveness.py
  queue_scope: project
  deferred: false
  body: |
    Covers TA6, T9, T10. Today probe_override sets the variable inside the
    gate's own process, where no sandbox policy applies, so it proves the guard
    READS the variable and never that the caller can SET it.
    Resolve the receiving caller class's bash_policy entry and assert the
    alternative is expressible under it. The verified env/export absence for
    coordinator:code-reviewer is the fixture — it is a real, currently-shipping
    example, not a synthetic one.
    Keep this SEPARATE from the existing liveness assertion. Different actor,
    different oracle; folding them lets one green cell imply a property it
    never tested.
- id: S3B
  title: Narrow check() to return AlternativeRecord | None, with the dispatcher as the single envelope-renderer
  change_kind: script-edit
  surface: coordinator_core/bash_guards/
  queue_scope: project
  deferred: false
  body: |
    New row (the Director of Engineering/eng-director review, F1, critical). Covers TA1. Discharges
    the enforcement TA1 requires as a structural property rather than an
    introspection heuristic.
    Today check() returns the harness's own nested envelope
    ({"hookSpecificOutput": {...}}), a dict, with the caller text a free
    string. So long as check() returns a dict, a guard can hand-build an
    envelope directly and bypass the record entirely -- TA1's "asserted by
    introspection over the package" is then a grep over our own Python, the
    same class of artifact as the prose extractor TA3 deletes. Verified
    2026-07-29 at claude-klabauter
    coordinator_core/bash_guards/block_worktree_creation.py:437-443 (one of
    18 envelope-construction sites in the package).
    Narrow the guard-facing return type to AlternativeRecord | None. The
    DISPATCHER becomes the single site that renders a record into the
    harness envelope. "No guard constructs a bare envelope" is then enforced
    by the dispatcher's signature and by the absence of hookSpecificOutput
    anywhere except one module -- structural, not a heuristic. It is an
    18-site change in bash_guards, not a project.
    Amend coordinator_core/write_guards/INTERFACE.md fidelity rule 2 in the
    same change: it currently states "Preserve every permissionDecisionReason
    / advisory string BYTE-FOR-BYTE" (verified still present at line 34,
    2026-07-29). That rule was a porting-fidelity rule for the bash-to-Python
    migration; left unamended it is a documented, written reason for the next
    author to keep emitting bare strings. Note write_guards is a sibling
    cohort, not this plan's surface (S1's scope is bash_guards/) -- this row
    edits only the fidelity-rule DOCUMENT that governs both cohorts, not
    write_guards' own 44 call sites, which remain out of scope per this
    plan's own "record homed under its first consumer" scoping (see S1/S6
    generic-substrate note).
- id: S3C
  title: Extend the trigger registry to one trigger per known message branch, and capture a rendered pre-migration baseline fixture
  change_kind: script-edit
  surface: coordinator_core/bash_guards/
  queue_scope: project
  deferred: false
  body: |
    New row (the Director of Engineering/eng-director review, F5, major -- the single highest-value
    amendment on this review). Covers TA9's prerequisite; re-homes the Staff Engineer F7
    now that the predecessor's C8 (which F7 was originally scoped to) is
    retired and would otherwise orphan it.
    A guard message cannot be rendered without firing the branch that emits
    it, and the package's only trigger corpus today (LIVE_TRIGGERS) reaches
    roughly a quarter of the 94 message branches (~25 triggers; T6). TA9 as
    originally specified compares against a corpus that does not exist.
    Extend LIVE_TRIGGERS to one trigger per known message branch (the Staff Engineer
    F7's fix), then capture the RENDERED TEXT of all ~94 branches as a
    committed baseline fixture, taken before S4 converts any site.
    This flips TA9 from a judgment row ("human-or-reviewer confirmed") to a
    mechanical one: a test that renders every branch post-conversion and
    diffs against this fixture, with rung-A/rung-B sites requiring an
    explicit reviewed acknowledgement to change. Backed by
    state/lessons/2026-07-29-verify-a-rewrite-differentially-not-by-running-it.yaml
    (universal scope): a rewrite is verified by diffing both sides against
    the same fixtures, not by running the new side and eyeballing it.
    <!-- Reconciliation reply (state/2026-07-29-typed-alternative-record-
    band-reconciliation-reply.md § 4), 2026-07-29: the baseline is captured
    PER BAND, immediately before that band's conversion -- not once,
    globally, up front. The peer band's promotions (their M3/M7/M10) land
    against prose in waves already in flight, agreed correct in S5's body;
    their message text keeps changing until those waves settle. A single
    global baseline captured before that settling would freeze the
    pre-promotion text, so when the peer's conversion eventually runs, the
    differential render shows diffs that are their INTENDED promotions, not
    conversion regressions -- the signal TA9 exists to produce drowns in
    expected noise, exactly the "reviewer remembers" failure state TA9 was
    rewritten to escape. The ordering in S5 is not the defect; the baseline
    timing is. This band's own branches enter the corpus before this band's
    conversion; the peer's branches enter after their promotions settle. The
    corpus is shared, the capture is staged per band. -->

    <!-- Reconciliation close (archive/memos/2026-07-29-typed-alternative-record-band-
    reconciliation-close.md §§ 2, 3), 2026-07-29: two precisions, one of them
    load-bearing for this row's correctness. -->

    **The corpus MUST key on the guard's chain-entry name (e.g. `offer-git-c`,
    `head-tail-plumbing-rewrite`, `grep-via-bash-guard`), never on module path or
    fully-qualified function name.** The peer session's merged plan carries a row (`M14`) that
    extends an in-flight extraction moving message-bearing guard functions between modules --
    that is the row's whole purpose, and an earlier row in the same plan (`M1`) already did this
    to two guards today (`check_offer_git_c` -> `guard_offer_git_c.py`,
    `check_head_tail_plumbing_rewrite` -> `guard_head_tail_rewrite.py`). If this corpus keys on
    module path or qualified function name, `M14` renames every key underneath it -- and the
    failure is the dangerous one: the baseline does not fail loudly, it goes MISSING, so a corpus
    that silently loses rows reports green over the branches it can no longer find. This is our
    own `UNTRIGGERED`-suppression hazard arriving from a direction this plan does not otherwise
    watch. Verified, not assumed: the peer session checked `M1`'s extraction differentially
    before committing it, and the 28 chain entries are in byte-identical order at HEAD and after
    the extraction, across a change that moved two guards between files -- chain-entry name is
    the one identifier in this package that survives the refactor this baseline has to outlive.
    Corollary: `test_guard_band_membership.py` (landed with the peer's `M1`) fails when a named
    guard matches nothing registered, so a chain-entry name that genuinely must change becomes a
    loud cross-plan failure rather than a silent gap.

    **Negative-spec: a corpus that can silently lose rows is worse than no corpus at all**, because
    it converts an absent signal into a green one -- a reviewer or gate reading the corpus as
    complete has no way to tell "covered" from "used to be covered, key renamed out from under
    it." Key on chain-entry name specifically because it is the identifier verified above to
    survive the extraction that would otherwise trigger this failure.

    **The peer's branches enter the corpus after their `M10`, not after their `M3`.** M3, M7, and
    M10 land in separate waves and M10 touches `guard_plumbing_and_loops.py` a second time, so
    capturing after M3 would baseline a half-promoted message and then diff a fully-promoted one
    against it -- see S5's body for the full sequencing detail; `M10` is this row's settle point
    for the peer's population, signalled by a one-line note the peer publishes to the
    reconciliation thread when it lands, not inferred from a module being touched.
- id: S4
  title: Migrate all ~94 message sites to the record, with a named exception set
  change_kind: script-edit
  surface: coordinator_core/bash_guards/
  queue_scope: project
  deferred: false
  body: |
    Covers TA8, TA9. Sized by review at ~94 sites, mechanically similar, most
    one-line constructor swaps — an afternoon to a day of executor work under
    this repo's operating assumptions, NOT a quarter.
    Fan out by disjoint file scope with per-file executor dispatch and
    EM-serial commits; never one sequential grind, never a worktree per module.
    The real risk is not the swap, it is a silent downgrade of the 33 messages
    already at rung A or B — hence TA9's differential render (built by S3C),
    which is the actual work and should be scoped as such.
    Cross-band shape is SETTLED, not an open question (the Director of Engineering/eng-director
    review, F9, major, ruling with DoE authority): the type lands
    package-wide as a shared API, and each band converts its own guards. This
    follows directly from coordinator/docs/wiki/bash-guard-threat-model.md
    § Two bands, and the shared-helper seam between them ("Editing a shared
    resolver is editing both bands. Check the effect on the other one and say
    what you found -- in the commit.") and § Dividing the work between two
    sessions ("Ownership runs by guard, end to end"), both already-ratified
    doctrine -- it does not need PM reconciliation to become correct. Follow
    the shared-helper rule's own instruction: disclose the cross-band effect
    of landing the type in the commit that lands it.
    The confinement-band conversion (this band's own block_* guard modules
    are DIFFERENT from the confinement band's) is a FIRST-WAVE, concretely
    specified ask carried into the PM's stated reconciliation step, not a
    post-hoc flag: name the AlternativeRecord type and its import path, the
    constructor invariant, the specific confinement-band guard modules, and
    the differential-render obligation (TA9-shape) those conversions
    inherit, so the confinement-band session can accept, decline, or
    counter-propose in one round-trip. Coordinator-content-repo's standing direct-commit
    grant into claude-klabauter would technically permit converting the
    confinement band's guards unilaterally; do NOT use it that way here --
    the by-guard cut is PM-ratified and end-to-end ownership includes the
    message, so unilateral conversion would be the split-a-guard-down-the-
    middle shape that doctrine kills by name. What happens if the peer band
    declines is an open dependency, surfaced to the PM separately, not
    resolved here.

    <!-- Reconciliation response (state/2026-07-29-typed-alternative-record-
    band-reconciliation-response.md §§ 2, 7), 2026-07-29: both mixed-module
    questions below are resolved, not open. commit_tripwires.py: agreed as a
    function-list split, no objection from either side -- check_machine_path_leak
    is the peer session's, check_schema_version_bump and check_bin_sh_polyglot
    are this plan's. dispatch_checks.py: the peer is closing it structurally
    rather than by sub-file agreement -- extracting the two guards that make
    the file mixed (check_offer_git_c -> guard_offer_git_c.py,
    check_head_tail_plumbing_rewrite -> guard_head_tail_rewrite.py; pure move,
    behaviour unchanged, importers repointed) is already in flight under the
    merged plan's row M1. That extraction removes the two worst cases but does
    not by itself empty the advisory tail, so a bare filename handoff would
    still collide on the remainder. This session's chosen answer: the DURABLE
    option, not the interim one -- extend the same extraction to the rest of
    the advisory tail so ownership of every function in the file is answerable
    by filename, per the peer's own recommendation. Reason: this plan is not
    execution-authorized (no schedule pressure that would justify taking the
    function-list interim instead), and the extraction is the discharge-test
    answer named in this plan's own north star -- an artifact rather than two
    sessions remembering where a line was. The function-list interim (this
    plan owns the confinement functions plus the extracted pair; the peer
    owns the named advisory tail functions) remains available if this plan's
    schedule changes and needs to start before the fuller extraction lands. -->
    Mixed-file constraint (band enumeration, 2026-07-29) -- RESOLVED BY
    EXTRACTION, not an open sub-file agreement (see note above): two modules
    were MIXED at function level, not file level, so a filename handoff did
    not apply to them as originally written -- the threat-model wiki already
    names this as a known weakness of its own cut ("it is file-level
    everywhere except the one module holding both bands, where the
    advisory/rewrite family is a contiguous tail block but a single rewrite
    guard sits orphaned in the middle of the confinement run").
    `dispatch_checks.py`: 8 confinement functions, then `check_offer_git_c`
    (the orphaned advisory/rewrite guard the wiki names), then a contiguous
    advisory tail block of 9 -- being closed by extending the peer session's
    in-flight extraction to the full tail, per the note above.
    `commit_tripwires.py`: `check_machine_path_leak` is confinement (peer
    session's); `check_schema_version_bump` and `check_bin_sh_polyglot` are
    advisory/warn-only (this plan's) -- agreed as a function-list split.

    <!-- SUPERSEDED READING -- reconciliation response (state/2026-07-29-
    typed-alternative-record-band-reconciliation-response.md § 1), 2026-07-29.
    What we read (kept verbatim below): guard_grep_via_bash.py,
    guard_multiprobe_banner.py, and guard_plumbing_and_loops.py are
    CLASS = "hard-deny" / fail_closed = True and genuinely deny on Windows,
    so under the wiki's binary band criterion they read as confinement, even
    though dispatch.py's CONFINEMENT_HARD_DENIES comment excludes them on
    narrower adversarial-evasion grounds -- two different operative senses of
    "confinement" (band cut vs. one ordering test) coexisting.
    The peer session verified this evidence and the wiki-vs-ordering-test
    distinction as CORRECT at source. But the question does not resolve in
    either direction -- it DISSOLVES: the merged plan's row M1 splits the
    chain into THREE bands, not two -- confinement-deny, advisory-rewrite,
    and platform-conditioned-deny -- with an explicit `band` enum field per
    chain entry. A two-band model keyed on fail_closed cannot express "hard-
    deny that must run AFTER the rewrites", which is what these three modules
    actually are. Promoting them into the confinement band was tried and
    EMPIRICALLY REVERTED -- it inverted their behaviour on both platforms.
    That empirical revert, not a re-reading of the evidence, is the
    substantive reason the two-band answer below was wrong.
    All three modules are the entire membership of the new third band and are
    owned end-to-end by the peer session (merged plan rows M3, M10). No
    conversion-ownership question remains for them from this plan's side.
    Recorded with the supersession rather than deleted, so a later reader
    does not re-derive the two-band question from the evidence alone. -->
    Superseded reading (band enumeration, 2026-07-29), kept for record:
    `guard_grep_via_bash.py`, `guard_multiprobe_banner.py`, and
    `guard_plumbing_and_loops.py` are `CLASS = "hard-deny"` /
    `fail_closed = True` and genuinely deny on Windows, satisfying the
    wiki's "may this run at all" test -- so under the wiki's band criterion
    they ARE confinement, even though `dispatch.py` carries an inline
    comment that they are "Deliberately NOT added to
    `CONFINEMENT_HARD_DENIES`" in `test_hard_denies_precede_rewrites.py`.
    That set is scoped narrower, to guards whose whole purpose is confining
    a caller against adversarial evasion (identity gates, git-history
    protection, the subagent-commit ban); these three are machine-load
    guards with no adversarial-evasion shape, per the comment's own
    reasoning. Two different operative senses of "confinement" coexist: the
    wiki's binary band definition, which governs the BAND CUT, and this
    narrower engineering-internal set, which governs one ORDERING TEST. They
    answer different questions, and the exclusion is about adversarial-
    evasion scope rather than band membership. This reading is SUPERSEDED --
    see the note above -- these three modules are their own third band
    (platform-conditioned-deny), owned by the peer session, not conversion-
    owned by this plan.
- id: S5
  title: Compute rung from the record and delete the prose extractor
  change_kind: script-edit
  surface: coordinator_core/bash_guards/
  queue_scope: project
  deferred: false
  body: |
    Covers TA3, TA4, TA11. Depends on S4 being complete for the population
    being graded — a half-migrated package needs both paths, which the
    anti-scope forbids as a steady state but is unavoidable transiently.
    <!-- Reconciliation response (state/2026-07-29-typed-alternative-record-
    band-reconciliation-response.md § 4), 2026-07-29: the completion
    condition below is CONCRETE, not the vague "depends on both bands
    converting" it replaces -- two artifact-tied conditions, granted by the
    peer session. Deliberately not a date, and deliberately not "when the
    peer session says it is done". -->
    Completion condition (concrete, per reconciliation response § 4,
    superseding the prior honest-but-vague "depends on both bands
    converting"): the prose extractor is deleted when BOTH hold. (a) Both
    bands' guards construct records -- the peer session signals its half of
    this with a NAMED TEST asserting that no guard module in its ownership
    constructs a message envelope by the prose path (a test, not a report,
    so the signal cannot rot). (b) The differential-render baseline (this
    plan's TA9 / S3C) is green across both bands' message branches -- the
    peer session's branches must be in this plan's TA9/S3C trigger corpus;
    treat their absence from it as a blocker on S5, not as coverage.
    <!-- Reconciliation reply § 4, 2026-07-29: timing qualification on
    condition (b) -- the blocker itself is unchanged and stands exactly as
    agreed above. The peer session's branches must enter the S3C corpus
    AFTER their promotions (M3/M7/M10, see the sequencing note below) have
    settled, not before. Capturing them earlier would satisfy this
    condition's letter -- their branches would be "in the corpus" -- while
    making TA9's signal worthless: the baseline would freeze pre-promotion
    text, and the peer's later conversion would then show their own intended
    promotions as diffs indistinguishable from conversion regressions. See
    S3C for the staged-per-band capture mechanism this condition depends
    on. -->
    <!-- Reconciliation close (archive/memos/2026-07-29-typed-alternative-record-band-
    reconciliation-close.md § 2), 2026-07-29: precision on the settle point named
    "M3/M7/M10" above. -->
    **The settle point is `M10`, specifically -- not `M3`.** The peer's three promoting rows do
    not land together: `M3` (`guard_plumbing_and_loops.py`) is in flight now (wave 1 remainder);
    `M7` (`guard_head_tail_rewrite.py`) is wave 2; `M10` (`guard_grep_via_bash.py`,
    `guard_multiprobe_banner.py`, and `guard_plumbing_and_loops.py` a SECOND time) is wave 3.
    Because `M10` touches `guard_plumbing_and_loops.py` again, capturing after `M3` would baseline
    a half-promoted message and then diff a fully-promoted one against it -- worse than capturing
    before `M3` at all. Settlement is not inferred from a module being touched; it is signalled by
    a one-line note the peer publishes to the reconciliation thread when `M10` lands.
    <!-- Reconciliation close (archive/memos/2026-07-29-typed-alternative-record-band-
    reconciliation-close.md § 4), 2026-07-29: provenance of the peer's M15 commitment once
    their plan closes. -->
    **The peer's conversion-to-records row (`M15`) is deferred, named-reason, and its plan closes
    without it.** `M15` cannot start until this plan's `S1` lands, and `S1` is gated on a PM
    authorization neither session controls -- so the peer's merged plan defers `M15` with that
    named reason and closes out without it, deliberately, so their close-out is not held hostage
    to this plan's gate and their own waves are not misreported as unfinished. The commitment does
    not lapse with that close-out: it is recorded in the peer's plan task spine as a deferred row
    naming `S1` as its unblock condition, and in the reconciliation thread
    (archive/memos/2026-07-29-typed-alternative-record-band-reconciliation-close.md § 4; the
    four-document thread was archived on settlement -- input, response, reply, close) -- so a closed
    peer plan is not misread as a withdrawn commitment. This plan's extractor-deletion condition
    (both halves, above) is unchanged by the peer's close-out.
    Asymmetry priced in by the peer session: condition (a) cannot start on
    their side until this plan's S1 pins the import path and field set --
    until then they have nothing to convert to. So S5's critical path runs
    through S1, and the sooner S1's signature lands the sooner the peer's
    half of condition (a) can start.
    <!-- Reconciliation response § 5, 2026-07-29: sequencing correction that
    moves this row later. The merged plan's waves 2-3 are ALREADY rewriting
    message text in the exact modules this plan wants converted (merged-plan
    rows M3 guard_plumbing_and_loops.py, M7 the extracted head/tail rewrite,
    and M10 the ladder promotions across guard_grep_via_bash.py,
    guard_multiprobe_banner.py, guard_plumbing_and_loops.py). Those rows are
    PM-ratified and in flight. If this plan's type landed mid-flight, the
    peer session would be converting messages it is simultaneously
    rewriting -- the same message authored twice. Agreed shape: the peer
    session's promotions land against PROSE in their current waves, and their
    conversion to records is a FOLLOW-ON row, opened once S1 pins the
    signature -- so the peer's half of condition (a) completes AFTER their
    current waves, not during them. This is the same shape this plan's own
    Out-of-scope section already prescribes from the other direction: a
    promotion lands on top of the record where the seam has reached a guard,
    against prose where it has not, because that is cheaper than blocking the
    ladder on a plan still being written. Known schedule dependency, not an
    open one. -->
    Delete the extractor rather than retaining it behind a flag. Retention is
    how the false-positive class survives.
    Key rung by (message, caller class) per TA4 — the same text is a real offer
    to the EM and a dead end to a confined subagent, and a single-keyed grade
    cannot express that.
    Handle T12 explicitly: guard_inprocess_search returns DENY as its success
    shape, so grade on record content, never verdict class, or the
    best-behaved guard in the package reports as a hard refusal.
- id: S6
  title: Record the seam and the audience rule in the shared threat-model wiki
  change_kind: doc-edit
  surface: coordinator/docs/wiki/bash-guard-threat-model.md
  queue_scope: project
  deferred: false
  body: |
    The wiki is the surface both sessions read, and the audience rule is a
    doctrine change with reach beyond this package — it says what a guard may
    offer an IC versus an EM.
    State the reasoning, not just the rule: why two gate designs failed, why
    the medium rather than the content was the problem, and the general test
    (a gate is judged by which shipping rows it fails on landing day).
    Framing (the Director of Engineering/eng-director review, F11, minor): do not write the audience
    rule up as free-standing new doctrine. § Two bands, and the shared-helper
    seam between them already carries the rules this plan's cross-band shape
    depends on, and its own closing paragraph already anticipates this exact
    seam ("a message-composition seam where a guard supplies a typed
    alternative rather than free prose makes a rung-C message unwritable").
    Amend that existing closing paragraph IN PLACE, promoting its prediction
    to a description of what shipped, and state the audience rule as an
    extension of the shared-helper rule rather than a new section. One page,
    one argument.
    Follow-on check (the Director of Engineering/eng-director review, F8, major): record whether the
    DoE-side hook scripts (coordinator/hooks/scripts/) can import from
    claude-klabauter at hook runtime. If they can, they are a named follow-on
    population for the record type; if they cannot, that is a fleet-shaped
    gap worth stating in this same wiki edit rather than discovering later.
    Percolation check before writing: this wiki page must stand alone for a
    reader who has the plugin and none of our substrate. No bare decision-record
    ids, no dates-as-changelog, no citations to non-seed pages.

    Follow-on note (band enumeration, 2026-07-29): the wiki's own band
    criterion (§ Two bands) and the narrower engineering-internal
    `CONFINEMENT_HARD_DENIES` set (`test_hard_denies_precede_rewrites.py`)
    answer different questions -- the wiki cut is binary band membership,
    the narrower set is scoped to guards whose purpose is confining a
    caller against adversarial evasion -- but nothing on the page states
    that distinction, and resolving it for S4's three ambiguous modules
    (`guard_grep_via_bash.py`, `guard_multiprobe_banner.py`,
    `guard_plumbing_and_loops.py`) cost a source-level round-trip that a
    one- or two-sentence addition to § Two bands would have made
    unnecessary. Worth closing in the doctrine rather than re-deriving.
```
