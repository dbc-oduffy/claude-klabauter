---
title: "Bash-guard merged execution shape — one wave plan across the confinement band split and the duty-of-care ladder"
created: 2026-07-29
author: coordinator-content-repo-em
status: approved
branch: "work/machine-b/2026-07-21to26"
execution_authorized_by: PM
execution_authorized_at: 2026-07-29
execution_authorized_sha: e66d5ce3a3b43ebedb3c982c227854a5fe72271a
execution_authorized_note: 'PM authorized the merged shape 2026-07-29, superseding the duty-of-care plan''s own stamp and granting the confinement plan the execution authorization it never held. This document IS the cross-plan alignment pass the confinement plan named as its second gate, so that gate is discharged. The three questions in § Open for the PM are answered as recommended: F9 M1-widening ACCEPTED (extract check_head_tail_plumbing_rewrite as well, buying two waves off the critical path); F5 ACCEPTED (audience split routes to the typed seam as a coordination ask, standalone sweep is the fallback); F6 ACCEPTED (M12 rung ratchet CUT, re-raise only if the seam slips). Windows verification is explicitly deferred to a Windows host AFTER this plan succeeds -- PM direction, deferred not cancelled, and must not be reported as covered.'
plan_id: "pln-bash-guard-merged-execution-shape-a71e05"
deliverable_id: "dlv-bash-guard-merged-execution-shape-a71e05"
initiative: null  # FK to state/initiatives/<id>.yaml; null when no named initiative
scope_mode: architecture
problem_set: inline
supersedes_execution_of:
  - docs/plans/2026-07-29-confinement-band-split-and-command-position-layer.md
  - docs/plans/2026-07-29-duty-of-care-the-liveness-gate-and-the-l.md
predecessor_handoff: state/handoffs/2026-07-29_184517_confinement-band-split-ready-to-execute.md
scope:
  - <CLAUDE_KLABAUTER_ROOT>/coordinator_core/bash_guards/
  - state/audits/ (coordinator-content-repo, measurement rows only)
agent_sessions:
  - "23662891-fb27-4e41-af3f-7c6ed6721fec|working|2026-07-29T19:37:37Z"
---

# Bash-guard merged execution shape

> **This document does not replace either source plan — it replaces their *execution*.** Both
> plans keep their problem statements, oracles, acceptance criteria, anti-scope, and review
> history; those are still the authority on *what correct looks like*. This document is the
> authority on *what runs when, in which file, by which agent*. Where a row here cites `CBS-Cn`
> or `DoC-Cn`, the cited row's body and ACs bind the executor unchanged.
>
> **Authored as the second-reviewer pass** the duty-of-care plan carried forward as a
> recommendation, and as the PM cross-plan alignment pass the confinement plan names as its
> second gate. Findings are in § Merge findings; several change what should execute.
>
> **Row ids are re-keyed to `M1…M12` deliberately.** Both source plans have a `C1`, `C2`, `C3`,
> `C4`, `C5`, `D1` and `D2` meaning entirely different things — `C1` is "build the liveness gate"
> in one and "split the chain into three bands" in the other. A merged plan that kept either
> numbering would be actively dangerous to dispatch from.

## Authorization state — the one thing that blocks

| Plan | Execution authorization |
|------|------------------------|
| Duty-of-care | **Authorized** — `execution_authorized_by: PM`, `execution_authorized_at: 2026-07-29`, sha `ff9985f2` |
| Confinement band split | **Not authorized** — `status: draft`, no stamp |
| **This merged shape** | **Authorized** — `execution_authorized_by: PM`, `execution_authorized_at: 2026-07-29`, sha `e66d5ce3` |

The merge did not inherit the duty-of-care stamp. That stamp was granted against a row set this
document reshapes (rows folded, one row struck, one row widened beyond either plan's ratified
scope — see M1), so it was re-granted against the merged scope. **Both gates are now discharged:**
this document is the cross-plan alignment pass the confinement plan named as its second gate, and
it carries its own stamp.

**The three questions in § Open for the PM were answered as recommended** (PM, 2026-07-29): F9
M1-widening accepted; F5 accepted — the audience split routes to the typed seam as a coordination
ask, with the standalone sweep as fallback; F6 accepted — M12 is CUT.

**Windows verification is deferred to a Windows host after this plan lands** (PM direction).
Deferred, not cancelled, and must not be reported as covered.

## Reconcile — what changed under both plans while they waited

Verified against disk and git at authoring time, not carried from either handoff.

- **The claude-klabauter working tree is clean across `coordinator_core/bash_guards/`.** Both
  handoffs park scope around a peer's uncommitted work in `dispatch_checks.py`,
  `guard_multiprobe_banner.py`, and `_alternative_liveness.py`. That work has landed. The
  "parked pending transfer" reason is spent.
- **DoC-C5 and DoC-C6 are landed and committed** (`9760fd213`, `1959a8266`, `232be2ee1`). Both
  read as open in the duty-of-care handoff's row dispositions.
- **DoC-C1 and DoC-C7 are landed** — the liveness gate exists at
  `coordinator_core/bash_guards/_alternative_liveness.py` and its suite runs 67 passed, 0 failed.
- **DoC-C3 (`e74f7417`) and DoC-C4 (`e733fa0d`) are landed**, as their own bodies state.

**Both of the confinement plan's blocking coordination asks are therefore already answered on
disk, and the plan's own § Cross-plan coordination is stale in saying they are not:**

1. *"What property does the liveness gate assert, and what surface does it read?"* — It enumerates
   every guard in the package, classifies each named alternative
   (`command`/`executable`/`flag`/`harness_capability`/`override`), and probes liveness **by
   execution**, with an explicit named `UNTRIGGERED` set carrying written reasons and a count
   ceiling. Surface: `_alternative_liveness.py`; oracle:
   `tests/test_alternative_liveness_gate.py`. It is green. CBS-C1's band model and CBS-C3's
   wrapper table must come out the far side of it green — that obligation is now executable,
   because the gate is readable.
2. *"Which two confinement modules are transferring, by name?"* —
   **`block_reviewer_bash_outside_allowlist.py`** (three D-rung rows) and
   **`block_subagent_destructive_action.py`** (the git-mv row, C→B). Named in the frontmatter of
   `state/audits/2026-07-29-ladder-tail-confinement-rows-handover.md`.

CBS-C3 and CBS-C5 were held on those two asks. They are unblocked.

## Merge findings

Findings from reviewing the two plans against each other. F1, F2, F3 and F7 change what executes;
F4 is discharged above; F5 and F6 are scheduling calls that need a PM answer inside the same stamp.

**F1 — `DoC-C9` and `CBS-C1` are the same edit and must be one chunk.** DoC-C9 extracts
`check_offer_git_c` out of `dispatch_checks.py` and re-registers it in `dispatch.py`. CBS-C1
rewrites `dispatch.py`'s entire `guard_chain` into three bands. Run as separate chunks they
rewrite the same registration block twice, and — worse — each row instructs its executor to
"coordinate rather than land unilaterally" with a peer session that, post-merge, does not exist;
an executor reading either row in isolation will stall waiting for a counterparty. Merged as
**M1**. The merge also makes CBS's own AC-1 cheaper: with `check_offer_git_c` in its own module,
band membership is answerable by reading the file, which is exactly the O19 defect DoC-C9 exists
to close.

**F2 — `CBS-C5` is serialized behind `CBS-C3` in the unmerged shape and parallel to it in the
merged one. This is the merge's headline wallclock win.** CBS-C5 wires `find_git_segment` into
`check_offer_git_c`; CBS-C3 rewrites the seven cohort-A guards. Today both live in
`dispatch_checks.py`, so they cannot run concurrently in a shared tree. After M1's extraction they
are different files and run as separate lanes in the same wave. Ordering the extraction first
converts a serialization into a fan-out at zero extra cost.

**F3 — `DoC-C5`'s revert already happened, and the merged shape must schedule the *restore*, not
the handover.** DoC-C5 captured finished, tested promotions to the two confinement modules as a
diff and then reverted them (`git checkout --` on four paths) so a peer would inherit a clean
tree. The audit says plainly: *"None of it is in claude-klabauter's working tree anymore. This
document is the only surviving record."* Under one execution shape there is no peer and no tree to
keep clean — that revert was pure waste, and leaving the row as written strands working code in a
markdown fence. **The merged shape re-applies the captured diff** (row M9, folded into M8). Cheap:
the diff is complete and was verified live before reverting. Do not re-derive it.

**F4 — Both confinement coordination asks are discharged.** See § Reconcile. No action beyond
recording it.

**F5 — `DoC-C12` (audience split) and the spun-off typed seam are the same mechanism, and
building C12 standalone duplicates a peer's in-flight work.** C12's own body concedes this: *"an
alternative record carrying an audience field lets the composer decide EM-vs-subagent rendering in
one place instead of at 94 call sites… fold it into the seam's record shape when it is [ready]."*
The typed seam has its own plan
(`docs/plans/2026-07-29-typed-alternative-record-message-composition-seam.md`) and was claimed by
a peer session within a minute of being spun off. Building C12 as an independent 94-site sweep is
simultaneously the largest avoidable token cost in this plan and its largest merge-conflict
generator — a broad-shallow sweep across files every other lane is editing. **Recommendation: send
the audience field to the seam as a coordination ask, and keep the standalone sweep as a
dated fallback.** This is a PM call because it accepts schedule risk on a peer session; it is
posed as one in § Open for the PM.

**F6 — `DoC-C8`'s retirement condition is already in flight, so it may be a bridge shorter than
its own construction time.** C8's body: *"this gate's retirement condition is D3 (the typed
message-composition seam) landing… the per-guard baselines pinned here from prose extraction have
to be discarded, not migrated."* D3 is claimed and executing. Building a rung ratchet whose
baselines are guaranteed to be thrown away, concurrently with the thing that throws them away, is
negative work unless the seam slips materially. **Recommendation: cut C8 from this execution and
re-raise it only if the seam misses.** Also a PM call — C8 is regression prevention, and cutting it
accepts a window with no rung ratchet.

**F7 — `CBS-C2`'s `surface:` field understates it, and hides a serialization.** C2 is listed with
`surface: _command_tokenizer.py`, but its AC-8 half threads the resolved position through
`evaluate_payload_json` — a `dispatch.py` edit, in the same function CBS-C1 restructures. Two
chunks editing one function in one file is a collision the chunk table does not show. **Split in
the merged shape:** the tokenizer extension is M2 (wave 1, own file), the threading is folded into
M5 (wave 2, after M1 has settled `dispatch.py`).

**F8 — No `code-reviewer` pass has run on either workstream's diff.** Stated in the duty-of-care
handoff's § Carried Forward. The merged shape carries one review gate over the combined diff (M13)
rather than two per-plan passes. This is a gap being closed, not a new requirement.

**F9 — M1 is widened beyond either plan's ratified scope, and that is called out rather than
absorbed.** M1 extracts `check_head_tail_plumbing_rewrite` as well as `check_offer_git_c`.
Neither plan authorizes the second extraction. The rationale is the same one DoC-C9 already
carries (O19: an advisory-band guard sitting inside the confinement-heavy file makes the band cut
un-mechanical), and the payoff is concrete: without it, `dispatch_checks.py` is wanted by four
separate rows and becomes a four-wave serial chain on its own; with it, that chain is two waves.
**This is an addition to ratified scope and needs the PM's assent inside the same stamp.** If
declined, M7 collapses back into M5 and the plan runs one wave longer.

## The wave shape

All code lands in one shared claude-klabauter working tree. No worktrees. Parallelism is bounded
by **file-scope disjointness**, and every wave below is a set of lanes with pairwise-disjoint
file scopes. Agents edit only; the EM commits per lane with a path-limited commit.

**Critical path: M1 → M5 → M8 → M11 — four waves.** Fully serialized, the same twelve rows are
twelve units. The merge buys roughly 3× on wallclock, and F2 and F9 are where most of it comes
from.

### Wave 1 — four lanes, no interdependencies

| Lane | Row | File scope | Source rows |
|------|-----|-----------|-------------|
| α | **M1** — three-band chain split + two guard extractions | `dispatch.py`, `dispatch_checks.py` *(extraction only)*, new `guard_offer_git_c.py`, new `guard_head_tail_rewrite.py`, delete `tests/test_hard_denies_precede_rewrites.py` | CBS-C1 + DoC-C9 + F9 widening |
| β | **M2** — tokenizer: resolve-once entry point, `find_git_segment`, classified wrapper table | `_command_tokenizer.py` | CBS-C2 *(tokenizer half)* + CBS-C5 *(API half)* + CBS-C3 *(table half)* |
| γ | **M3** — drop the head/tail fork without recognizing the upstream generator | `guard_plumbing_and_loops.py` | DoC-C11 |
| δ | **M4** — re-derive the grep guard's target class over the 69,329-command corpus; return verdict (a) rewrite here / (b) belongs in the peer's answerer / (c) row dies | coordinator-content-repo `state/audits/` only — **read-only on claude-klabauter** | DoC-C10 |

M1 is the longest lane and sets the wave. M4 is in a different repo and cannot collide with
anything.

**M1 sequencing is load-bearing and is not the executor's to reorder.** Delete
`test_hard_denies_precede_rewrites.py` **last**, and only after band membership is asserted for
every *registered* guard rather than only the named ones (CBS AC-2, Finding 9). Preserve
`inprocess-search` at head-of-band in the advisory-rewrite band — reordering that band by any
other criterion silently makes that seam unreachable for exactly the commands it exists to answer.
Preserve `_crash_deny`'s immediate-return property.

### Wave 2 — three lanes, after M1 and M2

| Lane | Row | File scope | Source rows |
|------|-----|-----------|-------------|
| α | **M5** — thread resolve-once through `evaluate_payload_json`; migrate cohort A's seven legacy guards onto M2's entry point | `dispatch.py`, `dispatch_checks.py` | CBS-C2 *(threading half, per F7)* + CBS-C3 |
| β | **M6** — consume `find_git_segment` so `prefix1` and `git_prefix` derive from one walk | `guard_offer_git_c.py` | CBS-C5 *(consumer half)* |
| γ | **M7** — short-circuit the head slice, bound the tail slice, stop eating shell redirections as path operands | `guard_head_tail_rewrite.py` | DoC-C2 |

M5 carries both the threading and its first consumers in one agent deliberately — they have to
agree on the kwarg contract, and splitting them across lanes would race on that contract rather
than on a file.

**M5's two hard requirements, both from CBS anti-scope:** preserve
`COORDINATOR_OVERRIDE_NO_VERIFY` / `_GIT_CLEAN` / `_GIT_REVERT` / `_BLANKET_ADD` exactly, and do
not make them subagent-reachable as a side effect of restructuring how the guard reads its
environment. Record the before/after token-walk count — AC-8 is a *reduction* criterion, and a
non-regression-only reading of it passes without the reduction happening.

**M7's equivalence is proven differentially** against the pre-change emitter, not by running the
new one and liking the result. Both handoffs record that exact mistake being caught.

### Wave 3 — two lanes, after wave 2

| Lane | Row | File scope | Source rows |
|------|-----|-----------|-------------|
| α | **M8** — delete the five hand-maintained `_skip_wrapper_own_argv` copies; **M9** — restore the reverted ladder-tail promotions from the captured diff | `_sentinel_creation_guard.py`, `block_subagent_destructive_action.py`, `block_subagent_commit.py`, `block_worktree_creation.py`, `block_reviewer_bash_outside_allowlist.py`, `dispatch_checks.py` | CBS-C4 + DoC-C5 *(restore, per F3)* |
| β | **M10** — execute the ladder promotions on the three advisory guard modules, per the landed worklist | `guard_grep_via_bash.py`, `guard_multiprobe_banner.py`, `guard_plumbing_and_loops.py` | DoC-D2 |

M9 folds into M8's agent because both touch `block_subagent_destructive_action.py`. M8's measure
of success is **deleted lines, not added ones** — the five copies are at
`block_worktree_creation.py:204`, `dispatch_checks.py:864`,
`block_subagent_commit.py:816` (`_skip_wrapper_own_argv_for_commit`),
`_sentinel_creation_guard.py:281`, `block_subagent_destructive_action.py:1233`.

M10 consumes M4's verdict for the grep guard's composed-pipeline row. If M4 returned (b) or (c),
M10 drops that row and files the coverage ask against the peer's answerer instead of promoting a
second-best path — a plan whose subject is duty of care must not offer agents the slower route.

### Wave 4 — serial, tree-quiet

| Row | Disposition |
|-----|-------------|
| **M11** — audience split: a subagent gets an escalation route, never a switch to flip | **STRUCK, and the fallback is withdrawn** (2026-07-29). F5 routed it to the typed seam; the typed-seam session then asked for exactly this in writing and the two agreements were exchanged — see `state/2026-07-29-typed-alternative-record-band-reconciliation-response.md` § 3. The audience requirement is now an obligation on *their* record's shape, carrying the four constraints restated in that document. **No interim string-filter ships from this side**, because an interim would create the dual-path steady state their plan's anti-scope forbids. |
| **M12** — rung ratchet, regression prevention only | **CUT per F6** (PM, 2026-07-29). |
| **M14** — extend M1's extraction to the rest of `dispatch_checks.py`'s advisory tail | **NEW, agreed with the typed-seam session** — the durable answer to their sub-file-ownership ask, recommended over a line-range split (line ranges rot; that file moved 150–200 lines inside a day). Makes ownership of every function in the file filename-answerable. |
| **M15** — convert this session's guard modules' message construction to the `AlternativeRecord` type | **NEW, agreed** — a follow-on AFTER M3/M7/M10's promotions land, opened once their `S1` pins the import path and field set. Promotions land against prose now; records replace the prose later. Signal completion with a **named test** asserting no owned guard constructs an envelope by the prose path — their extractor deletion is gated on it. |
| **M13** — `code-reviewer` pass over the combined diff | Not optional — F8. One pass over the merged diff, not two per-plan passes. |

**M11 must never be implemented as deletion.** Removing the override line and leaving nothing
behind converts a rung-C message into a rung-D one for subagents — a downgrade wearing a safety
costume. The escalation route is mandatory.

## Gates that bind every wave

- **The 304-cell confinement corpus stays green throughout.** A cell that flips does so as an
  `XPASS`-forced conversion to a real assertion, never by editing the shape or the guard list.
  A cell turning red means the band line was crossed — that tripwire firing is it doing its job.
- **The liveness gate stays green.** M1's band model and M5's wrapper table are obligated to
  satisfy it (§ Reconcile ask 1). A band assignment that cannot satisfy the gate is a finding, not
  a reason to weaken the gate.
- **The override-unreachability boundary holds**, pinned by
  `tests/test_override_unreachability_boundary.py` (claude-klabauter) and
  `coordinator/tests/test_bash_guard_hook_stays_per_event.py` (coordinator-content-repo). CBS AC-9.
- **Before trusting any run of the liveness gate**, run `git status --porcelain` scoped to
  `_alternative_liveness.py` and its test file. That module has previously produced four different
  failure counts in one session while a peer was mid-write on it.
- **No new command parser.** A chunk that adds a resolution implementation without deleting the
  ones it replaces has failed, however green. The point is subtraction.
- **Windows is not covered and must not be reported as covered.** Every measurement and fix here
  is macOS-observed; the cross-platform branching is reasoned and unverified. PM-deferred to the
  capable-host milestone — deferred, not cancelled.

## Carried unchanged from both plans

Anti-scope and out-of-scope from both source plans bind unchanged. The ones most likely to be
crossed by an executor working from this document alone:

- Do not fix the twelve remaining `setsid`/`busybox` corpus cells (CBS-D1, deliberately declined).
- Do not invert the fail direction (CBS-D2 — needs its own PM ruling, must not land inside a
  refactor where its regressions would be indistinguishable from the refactor's).
- Do not evaluate skipping site-module loading (DoC-D1 — deferred, unratified; the safety audit
  *is* the work).
- Do not absorb the typed message-composition seam (DoC-D3 — spun off and claimed by a peer).
- Do not fix a guard the liveness gate finds dead. The gate reports; fixing is the owning row's
  job.
- Tool grants and agent policy, including the `security-audit-worker` bash-policy gap, are PM
  calls and out of scope.

## Open for the PM — one stamp, three questions inside it

1. **Authorize the merged scope?** This is the blocking gate. It also discharges the confinement
   plan's cross-plan alignment requirement.
2. **F9 — assent to M1's widening** (extracting `check_head_tail_plumbing_rewrite` as well)? It is
   beyond both plans' ratified scope and buys two waves off the critical path. Declining costs one
   extra wave, nothing else.
3. **F5 and F6 — the two scheduling calls:** send the audience split to the typed seam as a
   coordination ask rather than building it at 94 call sites (accepts peer-schedule risk), and cut
   the rung ratchet whose retirement condition is already in flight (accepts a window with no
   regression gate).

```yaml plan-tasks
- id: M1
  title: Split the chain into three bands and extract check_offer_git_c and check_head_tail_plumbing_rewrite into their own modules
  change_kind: code-edit
  surface: <CLAUDE_KLABAUTER_ROOT>/coordinator_core/bash_guards/dispatch.py
  queue_scope: project
  deferred: false
  body: |
    Wave 1, lane α. Merges CBS-C1 and DoC-C9, plus the F9 widening.
    Their bodies and ACs (CBS AC-1, AC-2; DoC O19) bind unchanged.

    Three bands: confinement-deny, then advisory-rewrite, then
    platform-conditioned-deny LAST. Band membership is an explicit `band` enum
    field, never derived from `fail_closed` -- that field is exception-routing
    policy and is orthogonal to banding. The three platform guards carry
    fail_closed=True AND must run after the rewrite band; promoting them ahead
    was tested empirically and reverted for inverting their behaviour on both
    platforms.

    Sequencing inside this chunk is load-bearing and not the executor's to
    reorder: delete test_hard_denies_precede_rewrites.py LAST, only after band
    membership is asserted for every REGISTERED guard rather than only the named
    ones, and only after the band-membership assertion carries the retired test's
    third leg -- failing on a misspelled or stale guard name in any band's named
    set, so a typo cannot silently turn the check into a no-op.

    Preserve inprocess-search at head-of-band in the advisory-rewrite band.
    Preserve _crash_deny's immediate-return property. First deny wins within a
    band; later denies in the same band are discarded.

    F9: the second extraction (check_head_tail_plumbing_rewrite) is beyond both
    plans' ratified scope. It removes two waves from the critical path by giving
    DoC-C2 its own file. If the PM declines it, drop it and M7 folds back into M5.
- id: M2
  title: Extend _command_tokenizer.py with the resolve-once entry point, find_git_segment, and the classified wrapper table
  change_kind: code-edit
  surface: <CLAUDE_KLABAUTER_ROOT>/coordinator_core/bash_guards/_command_tokenizer.py
  queue_scope: project
  deferred: false
  body: |
    Wave 1, lane beta. Merges CBS-C2's tokenizer half, CBS-C5's API half, and
    CBS-C3's wrapper-table half into the one file all three touch.

    Mostly a PROMOTION, not new code -- cohort B already contains a near-correct
    implementation smeared across five files. If this reads as greenfield
    authoring, it has gone wrong.

    Two axes on the wrapper table, and collapsing them into one is the failure
    mode: semantic class (execs-its-argv / inspects-without-execing /
    applet-dispatcher) governs whether a word may EVER be rewrite-facing;
    rewrite-facing membership is a NARROWER explicitly-initialized subset,
    initialized to today's _FIND_WRAPPER_WORDS verbatim and widened only by
    explicit decision. setsid/strace/doas/ionice are execs-its-argv AND
    deliberately NOT rewrite-facing. Building this single-axis would widen the
    rewrite-facing view by construction, which Anti-scope forbids.

    Before deleting any of the six source literals, diff the built table against
    all six so a per-word omission fails loudly rather than silently narrowing a
    confinement guard. `time` was dropped from an earlier draft of this table and
    is present in all six on-disk allowlists.

    Streams carry a resolution confidence (resolved / peeled / unresolved).
    Reported here, consumed by nobody in this plan -- that is intentional, and
    makes the fail-direction question decidable later without another refactor.
- id: M3
  title: Drop the head/tail fork without needing to recognize the upstream generator
  change_kind: code-edit
  surface: <CLAUDE_KLABAUTER_ROOT>/coordinator_core/bash_guards/guard_plumbing_and_loops.py
  queue_scope: project
  deferred: false
  body: |
    Wave 1, lane gamma. DoC-C11 unchanged. Self-contained to one module; no
    dependency on any other wave-1 lane.
- id: M4
  title: Re-derive the grep guard's target class over the 69,329-command corpus and return a verdict
  change_kind: verification
  surface: state/audits/2026-07-29-grep-invocation-shape-corpus.md
  queue_scope: project
  deferred: false
  body: |
    Wave 1, lane delta. DoC-C10's measurement precondition, run as its own row
    because its outcome decides whether M10 carries a grep row at all.

    Read-only on claude-klabauter. Re-derive (grep-shaped traffic) MINUS (what
    search.answer already answers). The numbers exist: the answerer's docstring
    states 14.65% coverage against the rewrite's 1.068%. Return one of:
    (a) the residue is worth a rewrite in this guard;
    (b) the coverage belongs in _STAGE_BUILDERS / search.answer, which is the
        peer's surface and a coordination ask rather than a row we execute;
    (c) the row dies.
    (b) is the likeliest and is strictly better for callers.
- id: M5
  title: Thread resolve-once through evaluate_payload_json and migrate cohort A's seven legacy guards
  change_kind: code-edit
  surface: <CLAUDE_KLABAUTER_ROOT>/coordinator_core/bash_guards/dispatch_checks.py
  queue_scope: project
  deferred: false
  body: |
    Wave 2, lane alpha. Depends on M1 and M2. Merges CBS-C2's threading half
    (per F7 -- its surface field understated it as a tokenizer-only edit) with
    CBS-C3. One agent owns both because they must agree on the kwarg contract;
    splitting them across lanes would race on that contract rather than on a file.

    Guards: check_no_verify, check_destructive_git_orphan, check_destructive_rm,
    check_destructive_git_clean, check_destructive_git_revert,
    check_blanket_git_add, check_runaway_find. Strangler-style, one at a time,
    corpus as the gate after each.

    Thread the resolution exactly as policy_file and host_is_windows are threaded
    today. Record a before/after token-walk count -- AC-8 is a positive reduction
    criterion, and a non-regression-only reading passes without the reduction
    happening.

    HARD REQUIREMENT: COORDINATOR_OVERRIDE_NO_VERIFY / _GIT_CLEAN / _GIT_REVERT /
    _BLANKET_ADD are preserved exactly and must not become subagent-reachable as
    a side effect of restructuring how the guard reads its environment.

    Add cells for `cd X && which git status`, `cd X && busybox git status`, and
    `cd X && setsid git status` asserting no auto-rewrite is emitted.
- id: M6
  title: Consume find_git_segment so prefix1 and git_prefix derive from one walk
  change_kind: code-edit
  surface: <CLAUDE_KLABAUTER_ROOT>/coordinator_core/bash_guards/guard_offer_git_c.py
  queue_scope: project
  deferred: false
  body: |
    Wave 2, lane beta. CBS-C5's consumer half. Runs in parallel with M5 only
    because M1 extracted this guard into its own file -- see F2, the merge's
    headline wallclock win.

    The proof of this chunk is the single-walk consolidation, not a rung
    promotion: check_offer_git_c already auto-rewrites the prefixed spelling as
    of 2d12c6dc. The remaining deliberate non-promotion is the prefix0 case,
    which stays at deny+offer; if that restraint reads as wrong, it is a finding
    to report, not to act on.
- id: M7
  title: Short-circuit the head slice, bound the tail slice, and stop eating shell redirections as path operands
  change_kind: code-edit
  surface: <CLAUDE_KLABAUTER_ROOT>/coordinator_core/bash_guards/guard_head_tail_rewrite.py
  queue_scope: project
  deferred: false
  body: |
    Wave 2, lane gamma. DoC-C2, in its own file post-M1. Covers DoC AC-8, AC-9,
    O5 and O7.

    The redirection defect is the more serious half: an auto-applied rewrite
    silently ran a different command than the caller typed. Where a redirection
    changes observable output the guard must DECLINE to rewrite rather than
    rewrite approximately -- the package already has that pattern in
    _seam_confirmed_rewrite.

    Equivalence for the short-circuit is proven DIFFERENTIALLY against the
    pre-change emitter, not by observing that the new one runs. Both source
    workstreams recorded catching exactly that mistake.

    If the PM declines F9, this row folds into M5 and the plan runs one wave
    longer.
- id: M8
  title: Delete the five hand-maintained wrapper-walk copies, and restore the reverted ladder-tail promotions
  change_kind: code-edit
  surface: <CLAUDE_KLABAUTER_ROOT>/coordinator_core/bash_guards/
  queue_scope: project
  deferred: false
  body: |
    Wave 3, lane alpha. CBS-C4 plus DoC-C5-restore (M9, folded here because both
    touch block_subagent_destructive_action.py).

    Copies to delete: block_worktree_creation.py:204, dispatch_checks.py:864,
    block_subagent_commit.py:816 (_skip_wrapper_own_argv_for_commit),
    _sentinel_creation_guard.py:281, block_subagent_destructive_action.py:1233.
    Plus _normalize_windows_argv0_head_path_with_spaces in three copies and the
    bundled-`-c` fix in at least five. The measure of success is deleted lines.

    RESTORE, per F3: state/audits/2026-07-29-ladder-tail-confinement-rows-handover.md
    carries the complete diff of finished, tested promotions to
    block_reviewer_bash_outside_allowlist.py (three D-rung rows) and
    block_subagent_destructive_action.py (git-mv row C to B), which were reverted
    so a peer session would inherit a clean tree. There is no peer under the merge.
    Re-apply the captured diff; do not re-derive the work.
- id: M10
  title: Execute the ladder promotions on the three advisory guard modules
  change_kind: code-edit
  surface: <CLAUDE_KLABAUTER_ROOT>/coordinator_core/bash_guards/guard_grep_via_bash.py
  queue_scope: project
  deferred: false
  pm_approved: true
  body: |
    Wave 3, lane beta. DoC-D2, PM-ratified and un-deferred. Primary carrier of
    O1 and O2 -- the 45 rung-C and 16 rung-D messages are discharged by promoting
    them, not by any gate. The `D` prefix on the source row was retained only so
    cross-references resolved; read it as active.

    Worklist is landed at state/audits/2026-07-29-guard-module-ladder-worklist.md
    (1959a8266, 232be2ee1) -- this executes cold from it.

    The stale "there is no harness-native zero-fork search" claim in the grep
    guard's own message was already corrected (claude-klabauter 6f3ae246), so the
    blocking reason this row originally carried is discharged.

    Consumes M4's verdict, WHICH IS IN: **(c) -- the row dies.** M4 landed
    (`29665cd57`, `state/audits/2026-07-29-grep-guard-target-class-residue.md`) and
    returned (c) rather than the plan's own (b) leaning, with numbers: rewriting
    only the grep segment saves ZERO forks in exactly the cases `search.answer`
    declines, so the row is strictly dominated rather than merely superseded. Drop
    the composed-pipeline row. The narrow real residue (~0.6-0.9% of grep-shaped
    traffic -- `sed`/`awk`/`xargs` absorption) belongs to `_STAGE_BUILDERS`, which
    is the peer's surface: file it as a coordination ask, do not build it here.

    HARD CONSTRAINT ON EVERY PROMOTION IN THIS ROW -- read
    `state/lessons/2026-07-29-a-guard-message-offering-an-override-reads-as-prompt-injection.yaml`
    before starting. **Promotion means ADDING A WORKING ALTERNATIVE. It never means
    making the override line clearer, more prominent, or better worded.** Without
    that stated, "promote this message" invites exactly the polish that makes the
    message MORE discardable.

    The evidence, which is empirical rather than theoretical: a read-only Explore
    subagent, dispatched for an unrelated enumeration and with no knowledge of any
    plan, spontaneously reported this package's own guard advisories as prompt
    injection -- "styled as coordinator-hook system messages, urging me to set
    override environment variables... I treated them as untrusted content." Its
    intended reader classified our working-as-designed message as an attack and
    discarded it. A prose imperative arriving inside tool output, unsigned, telling
    the reader to disable a safety check, has no feature distinguishing it from a
    genuine attack -- because the distinguishing feature would have to be
    structural, and prose has no structure.

    So the override line fails in both directions at once: a well-behaved caller
    distrusts it and gets nothing, and a caller that trusts it gets the off-switch.
    Never put a disable-the-check instruction in a message addressed to a confined
    caller.

    Corroborating data point from the executing session, worth knowing because it
    widens the finding: this EM -- the unconfined, authorized audience the override
    line is legitimately addressed to under the EM-vs-IC ruling -- received dozens
    of these advisories in one session and acted on ZERO overrides. Not from
    distrust, but because the *advisory* was useful and the *switch* was never what
    it wanted. Twice the advisory changed behaviour; the bypass never did. Do not
    assume the EM rendering needs the switch either.

    SETTLE-NOTE OBLIGATION: when this row commits, publish a one-line note to the
    reconciliation thread. The typed-seam session captures this side's message
    branches into its S3C differential-render baseline only after THIS row lands
    (not after M3 -- this row touches `guard_plumbing_and_loops.py` a second time,
    so an earlier capture would baseline a half-promoted message). They infer
    settlement from the note, not from the module being touched.
- id: M11
  title: Address the override line to the EM only — a subagent gets an escalation route, never a switch to flip
  change_kind: code-edit
  surface: <CLAUDE_KLABAUTER_ROOT>/coordinator_core/bash_guards/
  queue_scope: project
  deferred: true
  pm_approved: true
  disposition: wont_do
  disposition_detail: "STRUCK 2026-07-29 in favour of the typed-seam plan's S2, and the standalone-sweep fallback is withdrawn with it. F5 routed the audience split to the seam; the typed-seam session independently asked for exactly that, offering strike-or-label-interim as its two exits, and this session took the strike. An interim string-filter here would create the dual-path steady state their anti-scope forbids. The requirement did not disappear -- it is now an obligation on their record's shape, with the four constraints restated in the response document (escalation route not deletion; the verified untypeable-override basis; rung as a (message, caller class) pair; reuse the shipped discriminator)."
  body: |
    STRUCK -- see disposition_detail. Body retained as the rationale record so the
    reasoning is not re-derived, NOT as a work instruction. Do not build this.

    Wave 4. DoC-C12, PM-ratified. Highest-leverage row on the board: it does not
    promote 45 messages one at a time, it removes the worst property of all of
    them at once -- that the single easiest action a guard offers is disabling
    the guard -- for the caller population where that property does most damage.

    Verified: coordinator/subagent-sandbox-policy.yaml grants
    coordinator:code-reviewer exactly ls/cat/head/tail/wc/find/file/stat/grep
    plus read-only git. `env` is absent. `export` is absent. So no override in
    the package is typeable by that class, and every message advertising one to
    it is offering something structurally impossible.

    DO NOT implement as deletion. Removing the override line and leaving nothing
    behind converts rung C to rung D for subagents -- a downgrade wearing a
    safety costume. The escalation route naming what to report upward is
    mandatory.

    SCHEDULING, per F5 -- PM call inside the authorization stamp. Preferred: send
    the audience field to the typed seam
    (docs/plans/2026-07-29-typed-alternative-record-message-composition-seam.md,
    claimed by a peer) as a coordination ask, so the composer decides rendering
    in one place rather than at 94 call sites. Fallback: standalone sweep, placed
    in wave 4 because a broad-shallow sweep must run against a quiet tree or it
    serializes every other lane behind itself.

    The EM-vs-subagent discriminator already ships --
    block_reviewer_bash_outside_allowlist.check() does it today: no agent_id on
    the payload means a top-level EM call. Do not build a second one.
- id: M12
  title: Build the rung ratchet — regression prevention only; it does NOT discharge the ladder
  change_kind: code-edit
  surface: <CLAUDE_KLABAUTER_ROOT>/coordinator_core/bash_guards/
  queue_scope: project
  deferred: true
  pm_approved: false
  disposition: wont_do
  disposition_detail: >-
    CUT by PM ruling 2026-07-29 (merge finding F6 accepted). Its own retirement condition was
    the typed seam landing, and that seam is now executing; its per-guard baselines are pinned
    from prose extraction and must be DISCARDED rather than migrated once the typed record
    replaces the prose, so building it concurrently is negative work. Re-raise only if the
    typed seam slips materially. Body retained as the rationale record, not a work instruction.
    Disposition added 2026-07-29 during consolidation: the row carried deferred:true with no
    disposition, so a PM-cut row read to the deferred-harvest as work still pending.
  body: |
    Wave 4. DoC-C8. **CUT by PM ruling, 2026-07-29** (F6 accepted). Do not build
    it in this execution. Re-raise only if the typed seam slips materially. The
    body below is retained as the rationale record so the reasoning is not
    re-derived, not as a work instruction.

    Its own retirement condition is the typed seam landing, and the seam is
    claimed and executing. Its per-guard baselines are pinned from prose
    extraction and must be DISCARDED, not migrated, once the typed record
    replaces the prose. Building it concurrently with the thing that throws its
    baselines away is negative work unless the seam slips materially.

    If built: it covers regression prevention ONLY. Its earlier claim to carry
    O1/O2/O11 is withdrawn -- a baseline pinning each guard's current rung
    grandfathers all 61 known-bad rows and fails ZERO of them on landing day.
    Its criterion also cannot fail a rung-D message at all, since an empty
    alternative set is not {override}. Do not quietly re-claim O2.

    Never land red. Never let the suppression list decay into "whatever exists
    today" either. Both failure directions apply.
- id: M14
  title: Extend M1's extraction to the rest of dispatch_checks.py's advisory tail
  change_kind: code-edit
  surface: <CLAUDE_KLABAUTER_ROOT>/coordinator_core/bash_guards/dispatch_checks.py
  queue_scope: project
  deferred: false
  pm_approved: false
  body: |
    NEW, agreed with the typed-seam session 2026-07-29 (see
    state/2026-07-29-typed-alternative-record-band-reconciliation-response.md § 2).
    Not yet PM-ratified -- it is an addition to the merged scope on the same
    rationale the PM already accepted for M1's widening, and should be confirmed
    in the same breath as that precedent rather than assumed.

    M1 extracts the two worst cases (check_offer_git_c,
    check_head_tail_plumbing_rewrite). It does NOT empty the advisory tail: the
    remainder of the contiguous tail block still sits inside a
    confinement-majority file, so a bare filename handoff would still land two
    concurrent sessions in the same file -- the exact collision the by-guard cut
    exists to prevent.

    Extract the rest of the advisory tail so ownership of every function in that
    file is answerable by reading a filename. This is the discharge-test answer:
    an artifact, rather than two sessions remembering where a line was.

    Chosen over a line-range ownership split deliberately. Line ranges rot, and
    this file is hot -- it moved 150-200 lines inside a single day while the two
    source plans were waiting for review. A function-list split is available as an
    interim if the peer needs to start before this lands; a line-range split is
    not on the table.

    Same constraints as M1: pure move, behaviour unchanged, every importer
    enumerated BEFORE the move and repointed in the same pass, tree importable at
    every step. Read the lesson at
    state/lessons/2026-07-29-a-dispatch-onto-a-live-shared-surface-needs-the-whole-importer-set.yaml
    before dispatching this -- it was written from this workstream wedging the
    fleet on exactly this operation.
- id: M15
  title: Convert this session's guard modules' message construction to the AlternativeRecord type
  change_kind: code-edit
  surface: <CLAUDE_KLABAUTER_ROOT>/coordinator_core/bash_guards/
  queue_scope: project
  deferred: true
  pm_approved: false
  body: |
    NEW, agreed with the typed-seam session 2026-07-29 (see the reconciliation
    response § 5, and the thread close § 4). Deferred on a NAMED reason, not
    appetite: it cannot start until the typed-seam plan's S1 pins the import path
    and field set. There is nothing to convert TO before that.

    THIS PLAN CLOSES OUT WITHOUT THIS ROW, deliberately. S1 is gated on a PM
    execution authorization that the typed-seam plan does not yet hold and that
    neither session controls -- it came back REQUIRES_CHANGES with eleven findings
    and sits at its execution gate. Holding this plan open on another plan's
    unauthorized row would make this close-out hostage to their gate and would
    misreport these waves as unfinished.

    The commitment does NOT lapse with the close-out. It is recorded here with S1
    as its unblock condition and in the reconciliation thread; when their signature
    is published to that thread, this row is picked up as its own dispatch. Their
    extractor-deletion condition (both halves) survives this plan closing -- the
    close-out is not an escape from it.

    If their authorization lands before wave 3, fold the conversion into M10
    instead of deferring, which avoids the double-authoring entirely.

    Scope: this session's owned guard modules stop building message envelopes by
    the prose path and build AlternativeRecords instead. Consume their type; do
    NOT re-declare it -- two shapes would be worse than one.

    Verdict behaviour must not change. When a guard fires and what it decides are
    out of scope for the conversion; a verdict change riding inside a message
    migration is unreviewable.

    SEQUENCING, and this is the part that moves their S5's date: M3, M7 and M10
    are rewriting message text in the exact modules this row converts. Those
    promotions land against PROSE, in the current waves. This conversion is the
    follow-on. Converting messages while simultaneously rewriting them would
    author the same message twice. This is the same sequencing their own parent
    plan prescribes from the other direction -- promotions land on top of the
    typed record where the seam has reached, and against prose where it has not.

    Two obligations inherited with the type:
      - The construction-time invariant: a record whose only actionable member is
        an override raises at construction. Runtime invariant on the emission
        path, not a static type property.
      - The differential-render obligation (their TA9): every converted site's
        rendered output is diffed against a committed pre-migration baseline, so a
        conversion cannot silently downgrade a message already at rung A or B.
        This side's message branches must be in their S3C trigger corpus --
        treat their ABSENCE from it as a blocker, never as coverage.

    Completion signal is a NAMED TEST asserting that no guard module in this
    session's ownership constructs a message envelope by the prose path -- a test,
    not a report, so the signal cannot rot. Their prose-extractor deletion (S5) is
    gated on it, so this is a cross-plan commitment, not an internal checkbox.
- id: M13
  title: code-reviewer pass over the combined merged diff
  change_kind: verification
  surface: <CLAUDE_KLABAUTER_ROOT>/coordinator_core/bash_guards/
  queue_scope: project
  deferred: false
  body: |
    Wave 4, closing gate. F8: no code-reviewer pass has run on either
    workstream's diff -- the duty-of-care handoff states this plainly and warns
    against letting those commits read as reviewed. One pass over the merged
    diff, not two per-plan passes.

    Verification to date on both sides was direct execution plus plan-level
    reviews (the Director of Engineering on the confinement plan, the Staff Engineer on the duty-of-care plan).
    Neither is a diff review.
```
