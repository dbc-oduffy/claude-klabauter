"""
coordinator_core.authz.classification — op write-semantics classification registry.

Purpose: An op is COMPUTE_ONLY if it only reads state and returns a computed value;
it is MUTATING if it writes, deletes, or reorders any coordinator substrate (state files,
queues, git objects/refs, on-disk records). Ambiguous cases classify MUTATING (fail-closed).
See the canonical criterion, the dual-write ban, and the MUTATING-default discipline in
DR-208: docs/decisions/DR-208-invoke-op-authz-model.md

Dual-write ban (content-invariant on MUTATING ops): a MUTATING op MUST write coordinator
substrate (claude-klabauter's disk-truth custody) ONLY. It MUST NOT write into rag's workstate_store —
a derived, rebuildable projection over claude-klabauter's disk-truth, not a system-of-record claude-klabauter may
also write. This is the per-op corollary of the dual-write ban drawn under DR-047, the
governing DoE/claude-klabauter boundary authority
DoE-claude docs/decisions/DR-047-doe-claude-klabauter-boundary-redraw-contract-vs-e.md, reconciled at
the custody-vs-projection level by
docs/decisions/DR-236-state-is-disk-truth-workstate-store-is-pro.md — successor to the
tree-local docs/decisions/2026-07-03-tri-plane-ownership-boundary.md § Design Decision #1,
which was never ratified into DoE's tree — see that doc's superseded-by header.

Spec backlink: pln-pcore-05-invoke-op-write-seman-80eecd § C1
Decision:      docs/decisions/DR-208-invoke-op-authz-model.md
"""

from __future__ import annotations

import enum
import types


class OpClass(enum.Enum):
    """Write-semantics classification for a coordinator op.

    COMPUTE_ONLY — the handler only reads in-memory index or on-disk state and
                   returns a computed value; safe to expose to a read-only token.
    MUTATING     — the handler writes, deletes, or reorders coordinator substrate
                   (state files, queues, git objects/refs, on-disk records); requires
                   a privileged read-write token and single-writer-queue serialization
                   before HTTP exposure.
    """

    COMPUTE_ONLY = "compute_only"
    MUTATING = "mutating"


# ---------------------------------------------------------------------------
# Classification registry — op-name → OpClass
#
# Seeded with every op present at pcore-03 completion (all COMPUTE_ONLY; verified
# by reading each handler — see plan § Ground truth verified against disk 2026-07-04).
# Future op authors MUST add an entry here before the op is registered; the drift-guard
# test (coordinator_core/authz/tests/test_authz_contract.py) will fail loud otherwise.
# New ops default to MUTATING until a reviewer explicitly affirms COMPUTE_ONLY —
# see DR-208 § Classification correctness discipline.
#
# DR-211 SANCTIONED ARCHIVAL-WRITER SUB-CATEGORY (handlers now exist — entries below)
# -------------------------------------------------------------------------------
# fleet.archive_completed_plans, fleet.archive_completed_handoffs, and
# fleet.prune_closed_bugs form a DR-211-ratified MUTATING archival-writer sub-category.
# They MAY write reserved substrate nouns (archive/ tree + scoped git commit) subject
# to the five conditions of DR-211 § D2 (all affirmed per handler code cited below):
#   D2-1. Per-record idempotent (re-archiving the same record is a no-op).
#   D2-2. Commutative (archive order does not alter the final state).
#   D2-3. Git-reversible (archive/ is git-tracked; git revert recovers any record).
#   D2-4. Act-time-terminality-re-verifying (handler re-checks terminal status at the
#          moment of archival to guard against TOCTOU races between plan/decision).
#   D2-5. Ungated-UDS-only (reachable only over the ungated UDS; HTTP surface is excluded,
#          not merely ungated — DR-211 D2(v)).
# Review: code-reviewer (slice-A F2) — strengthened from token-level wording to
# transport-surface exclusion; the prior text could be read as permitting ungated HTTP.
#
# Authority: docs/decisions/DR-211-fleet-op-substrate-write-boundary.md § D2
#            docs/decisions/DR-208-invoke-op-authz-model.md § 5
# ---------------------------------------------------------------------------
# Review: code-reviewer — wrapped in MappingProxyType so mutation attempts raise TypeError
# rather than silently corrupting the classification surface (test-pollution and runtime-
# privilege-escalation risk). Any caller doing OP_CLASSIFICATION["x"] = ... now fails loud.
OP_CLASSIFICATION: types.MappingProxyType[str, OpClass] = types.MappingProxyType({
    "ping": OpClass.COMPUTE_ONLY,
    # invoke.from_argv — MUTATING (fail-closed default, DR-208 § Classification
    # correctness discipline): a generic CLI-argv-to-op dispatcher
    # (ops/invoke_from_argv.py::_invoke_from_argv) — its whole purpose is to
    # run WHATEVER op the caller's argv names, including MUTATING ones, so it
    # cannot itself be affirmed COMPUTE_ONLY under the five-question test; its
    # own effective write-semantics are exactly those of the dispatched op.
    # Not an HTTP-exposed surface either way — reachable only over the same
    # ungated warm-server pipe the whole CLI already trusts (no privilege
    # escalation beyond what a caller could already do via
    # `python -m coordinator_core.invoke <argv...>` cold).
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  Depends
    #      entirely on the dispatched op — assume yes.
    #   2. Writes into rag's relational store?                                 No, itself;
    #      inherits the dispatched op's answer.
    #   3. Opens any file for write (including sentinel creation)?             No, itself;
    #      inherits the dispatched op's answer.
    #   4. Mutates shared mutable state outside its own module?                No, itself
    #      (repo_root is unused — see the handler's own docstring).
    #   5. Persistent state changes observable across process boundaries?      Depends
    #      entirely on the dispatched op — assume yes.
    "invoke.from_argv": OpClass.MUTATING,
    # cutover.gate — COMPUTE_ONLY: the cutover state machine's read-only
    # coverage verdict (ops/cutover_gate.py::_cutover_gate) — re-derives a
    # cutover record's consumer set at call time and evaluates the two-way
    # AGREEMENT test (empty-derivation, subset-agreement, non-shrinking,
    # foreign-repo fail-closed, signal-2 verified_by re-verification).
    # DR-208 five-question affirmation (citing this handler; full text lives
    # in the handler's own module header, ops/cutover_gate.py, per house
    # convention):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?    No.
    #      Reads the record and re-derives/re-verifies read-only; the computed
    #      derivation_history entry is RETURNED to the caller, never written to
    #      the record — persisting it is cutover.advance's (MUTATING) job.
    #   2. Writes into rag's relational store?                                   No.
    #   3. Opens any file for write (including sentinel creation)?               No.
    #   4. Mutates shared mutable state outside its own module?                  No.
    #      The in-process probe-op-key call (get_op_handler + await) mirrors
    #      tail_ops.py::run_coverage_gate's own COMPUTE_ONLY-compatible shape.
    #   5. Persistent state changes observable across process boundaries?       No.
    #   Git-shelling-is-read-only precedent: the commit-sha re-verification leg
    #   uses `git cat-file -e`, same read-only-git-query profile as
    #   coverage.gate's affirmed subprocess reads. The test-node-id
    #   re-verification leg re-runs pytest (a departure from the git-only
    #   precedent) but is itself read-only with respect to coordinator
    #   substrate — required by the plan's Signal-2 design (Review:
    #   the Director of Engineering-cutover-review F7, PM-approved in full 2026-07-25).
    # Spec: docs/plans/2026-07-25-cutover-state-machine.md § C4b
    "cutover.gate": OpClass.COMPUTE_ONLY,
    # cutover.advance — MUTATING: the sole writer of a cutover record's `phase`
    # field (ops/cutover_advance.py::_cutover_advance). Calls cutover.gate
    # internally and writes the phase bump + appended derivation_history entry
    # ONLY on a clean gate PASS; REFUSE/INDETERMINATE/setup-error leave the
    # record unchanged. Do NOT copy cutover.gate's COMPUTE_ONLY affirmation —
    # this op's whole purpose is the write cutover.gate deliberately does not
    # perform (see that module's own C4b docstring point 3).
    # DR-208 five-question affirmation (full text in the handler's own module
    # header, ops/cutover_advance.py, per house convention):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  Yes —
    #      rewrites the cutover record's `phase` + `derivation_history` on PASS.
    #   2. Writes into rag's relational store?                                 No.
    #   3. Opens any file for write (including sentinel creation)?             Yes —
    #      the one record file named by the caller, via frontmatter rebuild().
    #   4. Mutates shared mutable state outside its own module?                No.
    #   5. Persistent state changes observable across process boundaries?      Yes —
    #      the rewritten record is the intended, sole persistent effect.
    # Spec: docs/plans/2026-07-25-cutover-state-machine.md § C5, D1, D4
    "cutover.advance": OpClass.MUTATING,
    # handoff.blocked_by_dependents — COMPUTE_ONLY: reverse-membership query over
    # `state/handoffs/` (live + archived) via `_collect_all_handoffs_for_gate_index`;
    # the op's own module docstring is explicit — "Does NOT mutate any coordinator
    # substrate — read-only, same as the rest of this module."
    # Spec: docs/plans/2026-08-02-roadmap-baton-supersession-hazard.md § C1 (PIN-1)
    "handoff.blocked_by_dependents": OpClass.COMPUTE_ONLY,
    # backlog.record — MUTATING: appends backlog-depth rows to the per-machine JSONL shard
    # <central_state_root>/backlog-snapshots.<machine>.jsonl (an on-disk state write). Writes
    # coordinator substrate ONLY, never rag's relational store (dual-write ban, DR-208 /
    # tri-plane DD#1). Registered in ops/__init__.py (emit.recorder) in the same chunk.
    # Spec: docs/plans/2026-07-04-tc3-emission-stack-python-port-and-backlog-history.md § C4.
    "backlog.record": OpClass.MUTATING,
    # hooks.nudge_foreground_agent_dispatch — MUTATING (reclassified 2026-07-29; was
    # COMPUTE_ONLY under the affirmation below, which no longer holds for this op).
    #
    # What reclassifies the op is a session-scoped sentinel under
    # .git/coordinator-sessions/<sid>/ — .harness-bg-capable, which carries
    # run_in_background calibration between processes. That is a sentinel creation, so
    # question 3 below is YES. (The op briefly wrote a second sentinel,
    # .foreground-reroute-noticed, for the 2026-07-29 updatedInput reroute; the reroute was
    # reverted to deny on 2026-07-30 — it did not bind on harness 2.1.220 — and that marker
    # is no longer written. The classification is unchanged either way.)
    #
    # The marker is deliberately file-backed and must stay that way. Each PreToolUse fire
    # is a fresh process (DR-215 retired the resident daemon), so in-memory state would
    # re-initialize empty every call and the "once" would become "every time" — the exact
    # regression recorded for hooks.postuse_advisory_dispatch further down this file.
    # Reverting the marker to module state to win COMPUTE_ONLY back reintroduces it.
    #
    # Question 2 (rag's relational store) stays NO: the write target is
    # .git/coordinator-sessions/ (session-runtime), outside the dual-write ban's set
    # entirely — same rationale as the hooks.* bookkeeping ops below.
    #
    # Carries no runtime cost: DR-208's single-writer serialization for MUTATING ops was an
    # HTTP-exposure concern, and DR-215 retired the HTTP/UDS daemon (see op_scopes.py).
    "hooks.nudge_foreground_agent_dispatch": OpClass.MUTATING,
    # hooks.* advisory ops — affirmed COMPUTE_ONLY per DR-208 § "Default: MUTATING until affirmed".
    #
    # Affirmation rationale (five-question checklist, all "no" for all 3 ops below):
    #   1. Does it write, delete, or reorder any state file, queue, or git object?  No.
    #   2. Does it write into rag's relational store?                                No.
    #   3. Does it open any file for write (including sentinel creation)?            No.
    #   4. Does it mutate shared mutable state outside its own module?               No.
    #   5. Does it produce side effects observable across process boundaries?        No.
    #      (In-memory module state is process-local; external reads are not writes.)
    #
    # These ops read transcript/params fields and compute an advisory or deny envelope.
    # All file I/O is read-only (file existence checks, reads, env reads).
    "hooks.suggest_sonnet_research": OpClass.COMPUTE_ONLY,
    "hooks.nudge_em_code_dispatch": OpClass.COMPUTE_ONLY,
    "hooks.nudge_unauthorized_handoff": OpClass.COMPUTE_ONLY,
    # hooks.nudge_named_agent_report_delivery — COMPUTE_ONLY on the same five-question
    # checklist, and the strictest case of it in this group: the op performs NO file I/O
    # whatsoever (not even the read-only existence checks the three above do). It reads
    # tool_name/tool_input off params, matches two regexes, and returns an advisory
    # envelope. Its own negative-spec forbids the suppression sentinel that reclassified
    # nudge_foreground_agent_dispatch and postuse_advisory_dispatch to MUTATING — adding
    # per-session suppression state here would both violate that spec and flip this class.
    "hooks.nudge_named_agent_report_delivery": OpClass.COMPUTE_ONLY,
    # hooks.postuse_advisory_dispatch — MUTATING (reclassified; was COMPUTE_ONLY).
    #
    # B-F1 had re-plumbed this op's throttle/bark-once/dedup guards from /tmp
    # sentinel writes to in-memory module state specifically so this op could
    # stay COMPUTE_ONLY. That missed that this op is dispatched via a FRESH
    # process per PostToolUse fire (DR-215 retired the resident daemon), so the
    # in-memory state never survived past the call that created it — a confirmed
    # regression: none of the guards ever suppressed a repeat firing. The fix
    # restores durable file-backed state (a JSON sidecar + two touch-file
    # sentinels in tempdir, keyed by session_id — see
    # coordinator_core/hooks/postuse_advisory_dispatch.py's module-level
    # state-management comment above _advisory_state_path). This op now opens
    # files for write, which fails question 3 of the checklist above and is
    # exactly DR-208 § 2's named ambiguous case ("cache writes, lock files, temp
    # files, advisory markers" → classify MUTATING, fail-closed).
    #
    # This reclassification carries no runtime cost today: DR-208's
    # single-writer-queue serialization for MUTATING ops is an HTTP-exposure-time
    # concern, and DR-215 retired the HTTP/UDS daemon entirely — see op_scopes.py
    # ("HTTP/UDS gating vacated by DR-215 ... MUTATING ops are serial-by-
    # construction in the in-process model").
    "hooks.postuse_advisory_dispatch": OpClass.MUTATING,
    # hooks.* bookkeeping ops — classified MUTATING per DR-208 § "Default: MUTATING until affirmed".
    #
    # Affirmation rationale (five-question checklist; all four ops share identical answers):
    #   1. Does it write, delete, or reorder any state file, queue, or git object?  YES.
    #      Each op opens a file under .git/coordinator-sessions/ for write (touched.txt,
    #      meta.json, agent-audit.jsonl, dispatched-agents.txt respectively).
    #   2. Does it write into rag's relational store?                                No.
    #      Write target is .git/coordinator-sessions/ (session-runtime layer), which is
    #      neither coordinator substrate (state/) nor rag's relational store.
    #      DR-208 Invariant-1 (dual-write ban) is satisfied because the write target is
    #      session-runtime, outside the ban's prohibited set entirely.
    #      See: docs/decisions/DR-236-state-is-disk-truth-workstate-store-is-pro.md
    #      (successor to docs/decisions/2026-07-03-tri-plane-ownership-boundary.md § DD#1).
    #   3. Does it open any file for write (including sentinel creation)?            YES.
    #      (Question 1 is YES; write is the primary side-effect of each of these ops —
    #      that is precisely what distinguishes them from pcore-04's advisory population.)
    #   4. Does it mutate shared mutable state outside its own module?               YES.
    #      On-disk files under .git/coordinator-sessions/ are shared across sessions.
    #   5. Does it produce side effects observable across process boundaries?        YES.
    #      The on-disk writes are read by external consumers (greppers of
    #      agent-audit.jsonl and dispatched-agents.txt; readers of meta.json).
    #
    # Spec backlink: pln-pcore-08-async-bookkeeping-hoo-7920d5 § D2, C0.
    "hooks.track_touched_files": OpClass.MUTATING,
    "hooks.session_heartbeat": OpClass.MUTATING,
    # hooks.receiver_state_sensor — MUTATING: writes the receiver-state sibling file
    # (.git/coordinator-sessions/<sid>/receiver-state.json), same session-runtime write
    # class as session_heartbeat immediately above, just a different sibling artifact.
    # Spec backlink: docs/plans/2026-08-14-receiver-state-sensor.md § C3
    "hooks.receiver_state_sensor": OpClass.MUTATING,
    "hooks.agent_completion_log": OpClass.MUTATING,
    # Fan-in over agent_completion_log + track_dispatched_agents: MUTATING by union.
    "hooks.agent_postuse_dispatch": OpClass.MUTATING,
    "hooks.track_dispatched_agents": OpClass.MUTATING,
    "hooks.subagent_zero_tool_use": OpClass.MUTATING,
    # hooks.subagent_review_mark — MUTATING, same session-runtime write class as
    # hooks.subagent_zero_tool_use directly above (same SubagentStop event, same
    # shim-engine-durable-write shape): it appends a review mark to the commit
    # ledger under <git_common_dir>/coordinator-sessions/.commit-ledger/, an
    # on-disk write read across process boundaries.
    "hooks.subagent_review_mark": OpClass.MUTATING,
    "hooks.subagent_zero_tool_use_surface": OpClass.COMPUTE_ONLY,
    "hooks.subagent_zero_tool_use_resolve": OpClass.COMPUTE_ONLY,
    "hooks.subagent_arrival_check": OpClass.COMPUTE_ONLY,
    "hooks.subagent_fabrication_check": OpClass.COMPUTE_ONLY,
    # hooks.subagent_sidecar_fill_check — MUTATING despite being an advisory op,
    # same reasoning as hooks.nudge_foreground_agent_dispatch above: it writes
    # a per-session advisory-dedupe marker under gitdir
    # (_advisory_dedupe.mark_advised) on every flagged firing, so it is not a
    # pure read like its sibling COMPUTE_ONLY hooks.subagent_* ops.
    "hooks.subagent_sidecar_fill_check": OpClass.MUTATING,
    # hooks.context_pressure_precompact — MUTATING, same bookkeeping class as the four
    # above (its write target is tempdir rather than .git/coordinator-sessions/, which
    # changes the location, not the class).
    #
    # Affirmation rationale (five-question checklist):
    #   1. Does it write, delete, or reorder any state file, queue, or git object?  YES.
    #      Creates {tempdir}/compaction-occurred-{session_id} (sentinel) and writes
    #      {tempdir}/compaction-state-{session_id}.md (state snapshot).
    #   2. Does it write into rag's relational store?                                No.
    #   3. Does it open any file for write (including sentinel creation)?            YES.
    #      Sentinel creation is the op's entire product — see the module docstring's
    #      "the product is the write side-effect" contract.
    #   4. Does it mutate shared mutable state outside its own module?               YES.
    #      Both files are consumed by hooks.postuse_advisory_dispatch in a later process.
    #   5. Does it produce side effects observable across process boundaries?        YES.
    #
    # Spec backlink: coordinator_core/hooks/context_pressure_precompact.py module docstring
    # (W4b, recipe § 2.6; landing convention per 08-claude-klabauter-landing-contract.md § 1).
    "hooks.context_pressure_precompact": OpClass.MUTATING,
    # goal.append — MUTATING: appends a goal-event JSON line to the per-machine append-only
    # JSONL shard <central_state_root>/goals-log.<machine>.jsonl (an on-disk state write).
    # Writes coordinator substrate ONLY, never rag's relational store (dual-write ban,
    # DR-208 / tri-plane DD#1). A DISTINCT WRITER op; NOT the P06 goals reader (read-only).
    # Spec backlink: pln-tc-3-emission-stack-python-por-c9595b § C6
    "goal.append": OpClass.MUTATING,
    # goal.set_kr_status — MUTATING: locked read-modify-write of one
    # key_results[].status scalar in a state/goals/*.yaml artifact (via
    # locked_write.locked_rmw, cross-process flock). Distinct from goal.append
    # (per-machine JSONL event log) and from goals.reassess_krs (proposal-only,
    # never overwrites the live status field).
    # Spec backlink: state/handoffs/2026-07-25_001109_roadmap-seed-concurrency-residuals.md § Item A
    "goal.set_kr_status": OpClass.MUTATING,
    # goals.reassess_krs — MUTATING: writes goal KR files.
    "goals.reassess_krs": OpClass.MUTATING,
    # orientation.regenerate_cache — MUTATING: this op WRITES state/orientation_cache.md,
    # so it is MUTATING, not COMPUTE_ONLY. The drift-guard test
    # (coordinator_core/authz/tests/test_authz_contract.py) will fail loud if this entry
    # is omitted once the op is registered.
    "orientation.regenerate_cache": OpClass.MUTATING,
    # memo.transition — MUTATING: native Python port (strang-09) that writes memo
    # frontmatter in-place (claim/action/release verbs). No subprocess / node reach-back —
    # byte-faithful port of the DoE memo-transition.js oracle, not a delegation to it.
    # Review: code-reviewer (F8) — DR-208 five-question affirmation added to match the file's
    # established affirmation discipline (citing ops/memo_transition.py; plan strang-09).
    # DR-208 five-question affirmation:
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      memo_transition.py:claim()/action()/release() write memo frontmatter on disk
    #      directly (claim/action/release verbs) using coordinator_core frontmatter
    #      primitives — no node CLI spawn.
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only the target memo file (cross-repo/inbox/ or state/memos/); no rag write.
    #      Dual-write ban (DR-208 / tri-plane DD#1) satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      Each verb opens the memo file for write on invocation.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      Memo files under cross-repo/ and state/ are coordinator substrate shared across repos.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      Mutated memo frontmatter is read by the sibling repo EM and the cross-repo workflow.
    # Spec backlink: pln-strang-09-memo-transition-op-s-fec3a1 § C1,
    #   docs/plans/2026-07-06-memo-transition-native-python-port.md (native-port cutover)
    "memo.transition": OpClass.MUTATING,
    # ---------------------------------------------------------------------------
    # fleet.* DR-211 archival-writer sub-category — all three ops classified MUTATING
    # (DR-211 five-bound affirmed per handler code; DR-208 § 5 per-op affirmation below)
    # ---------------------------------------------------------------------------
    # fleet.archive_completed_plans — MUTATING: git-mv terminal plans (status ∈
    # {implemented, superseded, abandoned}) from docs/plans/ into archive/specs/YYYY-MM/.
    # DR-208 five-question affirmation (citing ops/fleet/archive_plans.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      archive_plans.py:367 — archive_and_commit() calls git-mv + git-commit
    #      on each candidate plan (primary + sidecars) into archive/specs/YYYY-MM/.
    #   2. Writes into rag's relational store?                                 No.
    #      Only writes archive/specs/ tree under the repo worktree and a git commit
    #      object — neither rag's relational store nor any rag-owned surface.
    #      Dual-write ban (DR-208 / tri-plane DD#1) satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      archive_plans.py:188 — Move.dst archive dir is created (mkdir) and git-mv
    #      writes the destination file inside archive_and_commit().
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      git commit is a repo-shared state mutation observable by all clients.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The git commit + archived files are observable by any git client,
    #      cockpit, and the coordinator shell layer after the handler returns.
    # DR-211 D2 five-bound affirmed:
    #   D2-1 (idempotent): already-archived source-gone → skipped "already-archived"
    #         (archive_plans.py:327 _handle_act, source exists check).
    #   D2-2 (commutative): git-mv order does not change final archive/ contents.
    #   D2-3 (git-reversible): archive/ is git-tracked; git revert recovers any plan.
    #   D2-4 (act-time re-verify): archive_plans.py:333 re-reads status at T3
    #         (D1 terminality re-verify); archive_plans.py:340 live-reference guard at T3.
    #   D2-5 (no remote route): DR-215 retired the UDS/HTTP transport outright;
    #     no HTTP route was ever added, negative-spec in archive_plans.py:48.
    # fleet.handoffs_for_plan — COMPUTE_ONLY: pure read, the "which handoffs did this
    # plan mint, live and archived" aggregation, built entirely on two unmodified
    # query_records() calls (ops/fleet/plan_handoffs.py). Same DR-208 five-question
    # posture as memo.list:
    #   1. Writes/deletes/reorders any state file, queue, or git object?      No.
    #      No open(..., "w")/os.replace/git-write call anywhere in the handler.
    #   2. Writes into rag's relational store?                               No.
    #   3. Opens any file for write (including sentinel creation)?           No.
    #   4. Mutates shared mutable state outside its own module?              No.
    #   5. Persistent state changes observable across process boundaries?   No.
    #      Returns a build_dry_run_result/build_setup_error_result envelope only.
    "fleet.handoffs_for_plan": OpClass.COMPUTE_ONLY,
    # fleet.archive_completed_handoffs — MUTATING: git-mv terminal, childless, unclaimed
    # handoffs from state/handoffs/ into archive/handoffs/YYYY-MM/, cap-bounded per
    # invocation (C1b of docs/plans/2026-08-25-the-handoff-auto-archive-comes-back-
    # capped.md repointed this entry's handler module onto
    # ops/fleet/archive_terminal_handoffs.py; the op key is unchanged).
    # DR-208 five-question affirmation (citing ops/fleet/archive_terminal_handoffs.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      archive_terminal_handoffs.py's _handle_act — archive_and_commit() git-mv +
    #      git-commit moves each validated handoff into archive/handoffs/YYYY-MM/.
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only into archive/handoffs/ tree and git commit objects.
    #      Dual-write ban (DR-208 / tri-plane DD#1) satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      Git-mv via archive_and_commit() writes destination files.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      Git commit is a repo-shared state mutation visible to all git clients.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The git commit and moved files are observable by any git client after return.
    # DR-211 D2 five-bound affirmed:
    #   D2-1 (idempotent): archive_terminal_handoffs.py's _handle_act — source-gone or
    #         absent from the terminal set → skipped "already-archived".
    #   D2-2 (commutative): git-mv order does not change final archive/ contents.
    #   D2-3 (git-reversible): archive/ is git-tracked; git revert recovers any handoff.
    #   D2-4 (act-time re-verify): archive_terminal_handoffs.py's _handle_act re-checks
    #         terminality, reverse_membership, and live session/claim at act time.
    #   D2-5 (no remote route): DR-215 retired the UDS/HTTP transport outright;
    #     no HTTP route was ever added, negative-spec in archive_terminal_handoffs.py.
    "fleet.archive_completed_handoffs": OpClass.MUTATING,
    # housekeeping.cycle — MUTATING: the rebuilt cycle
    # (coordinator_core/housekeeping/cycle.py) clears finished gates, files
    # terminal handoffs into archive/handoffs/, and lands one commit for the
    # set through fleet/_common.py :: archive_and_commit. Admitted to DR-211
    # § D1's sanctioned-writer list by DR-384. `handoff.housekeeping` — the
    # job this replaced — is deleted; kill means kill forever (PM 2026-08-23).
    "housekeeping.cycle": OpClass.MUTATING,
    # fleet.aggregate_capability_index — MUTATING, same single-derived-feed-file shape
    # as strategic.emit: it reads every registered sibling's authored capability
    # manifest and writes ONE aggregated projection into the invoking repo's own tree.
    #
    # Affirmation rationale (five-question checklist):
    #   1. Does it write, delete, or reorder any state file, queue, or git object?  YES.
    #      Writes state/capabilities/fleet-index.json via atomic_write_bytes.
    #   2. Does it write into rag's relational store?                                No.
    #   3. Does it open any file for write (including sentinel creation)?            YES.
    #      The index file is the op's entire product.
    #   4. Does it mutate shared mutable state outside its own module?               YES.
    #      DoE's review pre-flight reads the persisted index through the op seam.
    #   5. Does it produce side effects observable across process boundaries?        YES.
    #
    # Read-only against every OTHER repo it enumerates — the single write lands in the
    # invoking worktree, never a sibling's (module negative-spec).
    #
    # Spec backlink: coordinator_core/ops/fleet/capability_index.py module docstring
    "fleet.aggregate_capability_index": OpClass.MUTATING,
    # spec_backlink.resolve / spec_backlink.rewrite — one module registers both
    # (coordinator_core/ops/spec_backlink_resolve.py) and they classify OPPOSITELY;
    # do not copy one's affirmation onto the other.
    #
    # spec_backlink.resolve — COMPUTE_ONLY, affirmed against the handler per DR-208
    # § Classification correctness discipline rather than defaulted to MUTATING.
    # Affirmation rationale (five-question checklist, all "no"):
    #   1. Does it write, delete, or reorder any state file, queue, or git object?   No.
    #      Derives a path for a pln-/dlv- id and returns a typed hit/miss/ambiguity.
    #   2. Does it write into rag's relational store?                                No.
    #   3. Does it open any file for write (including sentinel creation)?            No.
    #      The module carries no write primitive at all.
    #   4. Does it mutate shared mutable state outside its own module?               No.
    #      The resolve index is rebuilt lazily per invocation, not persisted.
    #   5. Does it produce side effects observable across process boundaries?        No.
    "spec_backlink.resolve": OpClass.COMPUTE_ONLY,
    # spec_backlink.rewrite — MUTATING: delegates to
    # coordinator_core.ops.rewrite_spec_backlinks, which converts path-form citations
    # to id-form IN PLACE over a caller-supplied file list.
    # Affirmation rationale (five-question checklist):
    #   1. Does it write, delete, or reorder any state file, queue, or git object?  YES.
    #      Rewrites citation lines in place in every path the caller supplies.
    #   2. Does it write into rag's relational store?                                No.
    #   3. Does it open any file for write (including sentinel creation)?            YES.
    #   4. Does it mutate shared mutable state outside its own module?               YES.
    #      The rewritten files are tracked source read by every later consumer.
    #   5. Does it produce side effects observable across process boundaries?        YES.
    #
    # Spec backlink: coordinator_core/ops/spec_backlink_resolve.py handler docstrings
    "spec_backlink.rewrite": OpClass.MUTATING,
    # decision_record.mint_id / decision_record.release_id — MUTATING: both create
    # or delete a reservation marker file under state/decision-record-reservations/
    # (coordinator_core/ops/decision_record_mint.py).
    # Affirmation rationale (five-question checklist, both ops):
    #   1. Does it write, delete, or reorder any state file, queue, or git object?  YES.
    #      mint_id creates a reservation marker (os.open O_CREAT|O_EXCL|O_WRONLY);
    #      release_id deletes one (os.unlink). mint_id's TTL sweep also deletes
    #      any stale reservation files it encounters along the way.
    #   2. Does it write into rag's relational store?                                No.
    #   3. Does it open any file for write (including sentinel creation)?            YES.
    #   4. Does it mutate shared mutable state outside its own module?               YES.
    #      Reservation files are visible to every session sharing this tree
    #      (CLAUDE.md § Engineering Defaults — "parallel agents share one tree").
    #   5. Does it produce side effects observable across process boundaries?        YES.
    #
    # Spec backlink: coordinator_core/ops/decision_record_mint.py module docstring
    "decision_record.mint_id": OpClass.MUTATING,
    "decision_record.release_id": OpClass.MUTATING,
    # ---------------------------------------------------------------------------
    # fleet.* DR-218 review-trail-findings-reap sub-family — two ops, both
    # classified MUTATING (DR-218 D2/D2a delete-specific five-bound affirmed
    # per handler code; DR-208 § 5 per-op affirmation below). Unlike the
    # DR-211 archival-writer trio above, these ops delete via `git rm` (never
    # `git mv` into archive/) — DR-218 is a distinct DR because DR-211 § D5
    # explicitly EXCLUDES state/review-trail/ from its own carve-out and its
    # § D1 sanctioned-ops list is closed and does not name either op. The
    # five-bound framework is reused (adapted, not inherited) — DR-218 § D2
    # restates each DR-211 § D2 bound for the delete noun rather than citing
    # DR-211's git-mv-specific D2 bounds directly.
    # ---------------------------------------------------------------------------
    # fleet.reap_unintegrated_findings — MUTATING: git-rm of AGED,
    # UNINTEGRATED review-findings sidecars (marker-ABSENT AND authored >14d
    # ago) from state/review-trail/findings/. Leg (b) of the DR-218 two-leg
    # split; see coordinator_core/ops/fleet/reap_unintegrated_findings.py.
    # DR-208 five-question affirmation (citing ops/fleet/reap_unintegrated_findings.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      _reap() → _findings_reap.reap_findings() → _common.rm_and_commit()
    #      performs `git rm` + `git commit` on each aged, marker-absent sidecar.
    #   2. Writes into rag's relational store?                                 No.
    #      Only deletes files under state/review-trail/findings/ and writes a
    #      git commit object; no rag-owned surface is touched.
    #      Dual-write ban (DR-208 / tri-plane DD#1) satisfied.
    #   3. Opens any file for write (including sentinel creation)?             No.
    #      classify_unintegrated() only opens candidates for READ; the delete
    #      path is `git rm`, not an open-for-write. rm_and_commit's own
    #      commit-object write is the only "write," answered in Q1.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      state/review-trail/findings/*.md is coordinator substrate shared
    #      across EM sessions and repos; the git commit is repo-shared state.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The git commit + the sidecar's absence are observable by any git
    #      client, the coordinator shell layer, and future review-trail reads.
    # DR-218 D2 delete-specific five-bound affirmed (adapted from DR-211 § D2,
    # NOT DR-211's git-mv-specific bounds — see DR-218 § D2 for the reasoning):
    #   D2-i  (per-record idempotent): reap_unintegrated_findings.py's
    #         classify_unintegrated() re-evaluates each candidate; an
    #         already-deleted or never-aged file is simply absent from the
    #         next scan — no error on re-run.
    #   D2-ii (commutative): set-difference delete semantics over the
    #         candidate list — order does not change the resulting tree.
    #   D2-iii (cwd-scope-guarded): main_worktree_root(common_dir) confines
    #         the op to the repo's own state/review-trail/findings/; a
    #         missing dir degrades to [] (nothing to do), never an error.
    #   D2-iv (act-time-terminality-re-verifying): _reap() re-scans via
    #         _scan_reapable() immediately before rm_and_commit at
    #         dry_run:false — reap_unintegrated_findings.py:266-267 — closing
    #         the scan→act race window (DR-218 § D2(iv)).
    #   D2-v  (fail-closed-to-keep): classify_unintegrated() returns None
    #         (KEEP) on unparseable filename date, unreadable file, or
    #         marker-present — never deletes on ambiguity (DR-218 § D2(v)).
    # Spec backlink: docs/decisions/DR-218-review-trail-aged-unintegrated-reap-boundary.md § D1/D2,
    #   docs/plans/2026-07-14-review-findings-aged-unintegrated-reaper.md
    "fleet.reap_unintegrated_findings": OpClass.MUTATING,
    # fleet.reap_integrated_findings — MUTATING: git-rm of INTEGRATED
    # review-findings sidecars (marker-PRESENT, age-independent) from
    # state/review-trail/findings/. Leg (a) of the DR-218 two-leg split
    # (DR-218 C0 amendment); see
    # coordinator_core/ops/fleet/reap_integrated_findings.py.
    # DR-208 five-question affirmation (citing ops/fleet/reap_integrated_findings.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      reap_findings() → _common.rm_and_commit() performs `git rm` +
    #      `git commit` on each marker-present sidecar (reap_integrated_findings.py:222-228).
    #   2. Writes into rag's relational store?                                 No.
    #      Only deletes files under state/review-trail/findings/ and writes a
    #      git commit object; no rag-owned surface is touched.
    #      Dual-write ban (DR-208 / tri-plane DD#1) satisfied.
    #   3. Opens any file for write (including sentinel creation)?             No.
    #      classify_integrated() only opens candidates for READ; the delete
    #      path is `git rm`, not an open-for-write.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      state/review-trail/findings/*.md is coordinator substrate shared
    #      across EM sessions and repos; the git commit is repo-shared state.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The git commit + the sidecar's absence are observable by any git
    #      client, the coordinator shell layer, and future review-trail reads.
    # DR-218 D2a delete-specific five-bound affirmed (marker-PRESENT /
    # age-INDEPENDENT predicate — restated, not the § D2 aged predicate):
    #   D2a-i  (per-record idempotent): classify_integrated() re-evaluates
    #         each candidate on every scan; an already-deleted file is simply
    #         absent — no error on re-run.
    #   D2a-ii (commutative): set-difference delete semantics over the
    #         candidate list — order does not change the resulting tree.
    #   D2a-iii (cwd-scope-guarded): main_worktree_root(common_dir) confines
    #         the op to the repo's own state/review-trail/findings/; a
    #         missing dir degrades to [] (nothing to do), never an error.
    #   D2a-iv (act-time-terminality-re-verifying, marker-VANISHED direction):
    #         handler re-scans via scan_findings(classify_integrated)
    #         immediately before reap_findings at dry_run:false
    #         (reap_integrated_findings.py:221-228) — skips a candidate whose
    #         marker vanished between scan and act (DR-218 § D2a(iv)).
    #   D2a-v  (fail-closed-to-keep, marker-ABSENT-means-not-this-op's-file):
    #         classify_integrated() returns None (KEEP) on unreadable file or
    #         marker-absence — never deletes on ambiguity (DR-218 § D2a(v)).
    # Spec backlink: docs/decisions/DR-218-review-trail-aged-unintegrated-reap-boundary.md § D1/D2a,
    #   docs/decisions/DR-211-fleet-op-substrate-write-boundary.md § D3/D4 (shared git mechanics)
    "fleet.reap_integrated_findings": OpClass.MUTATING,
    # ---------------------------------------------------------------------------
    # handoff.* DR-212 lifecycle in-place frontmatter-mutation sub-category —
    # all three ops classified MUTATING (DR-212 five-bound affirmed per handler code;
    # DR-208 § Classification correctness discipline five-question affirmation below)
    # ---------------------------------------------------------------------------
    # handoff.transition — MUTATING: in-place frontmatter mutation of a single
    # state/handoffs/*.md file (consume / supersede / ship / repark / gate-recheck verbs).
    # DR-208 five-question affirmation (citing ops/handoff_transition.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      handoff_transition.py:212 — path.write_text(rebuild(split, fm)) writes the
    #      in-place updated frontmatter back to state/handoffs/*.md.
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only the single target state/handoffs/*.md file; no rag store write.
    #      Dual-write ban (DR-208 / tri-plane DD#1) satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      path.write_text(...) at handoff_transition.py:212 opens the file for write.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      state/handoffs/*.md is coordinator substrate shared across EM sessions.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The mutated handoff file is read by shell consumers, other ops, and rag.
    # DR-212 D2 five-bound affirmed (DR-212 § D2):
    #   (i)   Per-file idempotent: apply-and-re-apply yields the same frontmatter state.
    #   (ii)  Single-target-file-only: exactly one state/handoffs/*.md per call.
    #   (iii) Scope-limited to state/handoffs/*.md frontmatter — body untouched; no
    #         other state/ path; no archive/ write (DR-212 D2(iii)).
    #   (iv)  No git commit issued by the handler (DR-212 D2(iv)).
    #   (v)   Reachable only over the ungated UDS; no HTTP route (DR-212 D2(v)).
    # Authority: docs/decisions/DR-212-handoff-lifecycle-inplace-frontmatter-mutation-carveout.md § D1/D2
    #            docs/decisions/DR-208-invoke-op-authz-model.md § 5
    #
    # C1 addendum — gate-recheck / repark verb paths (2026-07-13, claude-klabauter auto-reconcile
    # plan § C1; DR-208 §5 / DR-212 §Impl-slice obligations). Same op key
    # ("handoff.transition"), same OpClass.MUTATING entry above — classification is
    # keyed per-op-name, not per-verb, so no new dict key is added here; this addendum
    # affirms the same five DR-208 questions + five DR-212 D2 bounds against the new
    # verb handler code specifically.
    # DR-208 five-question affirmation (citing ops/handoff_transition.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      _repark's mutate() closure calls replace_fm_field then returns rebuild(split, fm);
    #      _gate_recheck's mutate() closure calls replace_fm_field/insert_fm_field/
    #      remove_fm_field then returns rebuild(split, fm) — both routed through
    #      locked_rmw(path, mutate, repo_root=repo_root), which performs the single
    #      atomic path.write_text of the rebuilt frontmatter (same write primitive as
    #      consume/supersede/ship above).
    #   2. Writes into rag's relational store?                                 No.
    #      Both verbs write only the single target state/handoffs/*.md file passed in
    #      via handoff_path; no rag store write. Dual-write ban satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      Via locked_rmw's path.write_text call inside the shared mutate/write path.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      state/handoffs/*.md is coordinator substrate shared across EM sessions.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The mutated handoff file (deployment_state + last_gate_recheck +
    #      gate_dependency changes) is read by shell consumers, other ops, and rag.
    # DR-212 D2 five-bound affirmed (DR-212 § D2):
    #   (i)   Per-file idempotent: _repark no-ops (applied=False, byte-identical write
    #         skip) when deployment_state is already ready_to_fire; _gate_recheck
    #         no-ops under the same condition when cleared=True. Re-applying either
    #         verb at the terminal state yields the same frontmatter.
    #   (ii)  Single-target-file-only: exactly one state/handoffs/*.md per call
    #         (both verbs take a single handoff_path param, same as consume/ship).
    #   (iii) Scope-limited to state/handoffs/*.md frontmatter — body untouched; no
    #         other state/ path; no archive/ write.
    #   (iv)  No git commit issued by either handler.
    #   (v)   Reachable only over the ungated UDS via the same @register_op("handoff.transition")
    #         dispatch surface as the other verbs; no HTTP route.
    # Authority: docs/decisions/DR-212-handoff-lifecycle-inplace-frontmatter-mutation-carveout.md § D1/D2
    #            docs/decisions/DR-208-invoke-op-authz-model.md § 5
    #            docs/plans/2026-07-13-claude-klabauter-auto-reconcile-open-handoffs.md § C1
    #
    # C8 addendum — gate-cascade-clear verb path (2026-07-13, claude-klabauter auto-reconcile
    # plan § C8; DR-208 §5 / DR-212 §Impl-slice obligations). Same op key
    # ("handoff.transition"), same OpClass.MUTATING entry above — classification is
    # keyed per-op-name, not per-verb, so no new dict key is added here; this addendum
    # affirms the same five DR-208 questions + five DR-212 D2 bounds against the new
    # verb handler code specifically.
    # DR-208 five-question affirmation (citing ops/handoff_transition.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      _gate_cascade_clear's mutate() closure calls replace_fm_field/
    #      insert_fm_field/remove_fm_field on blocked_by/gate_dependency/
    #      gate_cleared_by/deployment_state, then returns rebuild(split, fm) —
    #      routed through locked_rmw(path, mutate, repo_root=repo_root), the same
    #      atomic path.write_text primitive as every other verb in this file.
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only the single target state/handoffs/*.md file passed in via
    #      handoff_path; _resolve_blocker_deployment_state performs READ-ONLY
    #      scans of state/handoffs/*.md + archive/handoffs/*.md (act-time
    #      re-verification) — no write to any blocker file. Dual-write ban satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      Via locked_rmw's path.write_text call inside the shared mutate/write path.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      state/handoffs/*.md is coordinator substrate shared across EM sessions.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The mutated handoff file (blocked_by + gate_dependency +
    #      gate_cleared_by + deployment_state changes) is read by shell
    #      consumers, other ops, and rag.
    # DR-212 D2 five-bound affirmed (DR-212 § D2):
    #   (i)   Per-file idempotent: no-op (applied=False, byte-identical write
    #         skip) when blocked_by is already empty AND deployment_state is
    #         already ready_to_fire (full target state for an empty-removal
    #         replay). Act-time re-verification re-reads disk fresh on every
    #         call, so a re-apply after a prior success naturally lands on the
    #         idempotent branch.
    #   (ii)  Single-target-file-only: exactly one state/handoffs/*.md is
    #         WRITTEN per call (the act-time blocker re-scan reads — never
    #         writes — additional files, consistent with (ii)'s write-target
    #         bound).
    #   (iii) Scope-limited to state/handoffs/*.md frontmatter — body untouched;
    #         no other state/ path is written; no archive/ write.
    #   (iv)  No git commit issued by the handler.
    #   (v)   Reachable only over the ungated UDS via the same
    #         @register_op("handoff.transition") dispatch surface as the other
    #         verbs; no HTTP route.
    # Authority: docs/decisions/DR-212-handoff-lifecycle-inplace-frontmatter-mutation-carveout.md § D1/D2
    #            docs/decisions/DR-208-invoke-op-authz-model.md § 5
    #            docs/plans/2026-07-13-claude-klabauter-auto-reconcile-open-handoffs.md § C8
    "handoff.transition": OpClass.MUTATING,
    # handoff.stamp — MUTATING: in-place frontmatter mutation of a single
    # state/handoffs/*.md file (inserts shipped_in: <SHA> field).
    # DR-208 five-question affirmation (citing ops/handoff_stamp.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      handoff_stamp.py:161 — p.write_text(rebuilt) writes the updated frontmatter
    #      back to state/handoffs/*.md.
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only the single target state/handoffs/*.md file; no rag store write.
    #      Dual-write ban (DR-208 / tri-plane DD#1) satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      p.write_text(...) at handoff_stamp.py:161 opens the file for write.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      state/handoffs/*.md is coordinator substrate shared across EM sessions.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The mutated handoff file is read by shell consumers, other ops, and rag.
    # DR-212 D2 five-bound affirmed (DR-212 § D2):
    #   (i)   Per-file idempotent: idempotent — already-stamped returns exit_code 0 applied False.
    #   (ii)  Single-target-file-only: exactly one state/handoffs/*.md per call.
    #   (iii) Scope-limited to state/handoffs/*.md frontmatter — body untouched; no
    #         other state/ path; no archive/ write (DR-212 D2(iii)).
    #   (iv)  No git commit issued by the handler (DR-212 D2(iv)).
    #   (v)   Reachable only over the ungated UDS; no HTTP route (DR-212 D2(v)).
    # Authority: docs/decisions/DR-212-handoff-lifecycle-inplace-frontmatter-mutation-carveout.md § D1/D2
    #            docs/decisions/DR-208-invoke-op-authz-model.md § 5
    "handoff.stamp": OpClass.MUTATING,
    # handoff.stamp_phase — MUTATING: in-place frontmatter mutation of a single
    # state/handoffs/*.md file (writes handoff_phase: {continuation|execution} +,
    # execution-only, execution_authorized_{by,at,sha,note} sourced from the
    # cited plan's frontmatter). Fourth addition to the DR-212 sanctioned
    # handoff.* population.
    # DR-208 five-question affirmation (citing ops/handoff_phase_stamp.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      handoff_phase_stamp.py's _stamp_phase() routes _mutate() through
    #      locked_rmw(handoff_path, _mutate, repo_root=repo_root), which performs
    #      the atomic path.write_text of the rebuilt frontmatter back to the
    #      single target state/handoffs/*.md file (same write primitive as
    #      handoff_stamp.py / handoff_transition.py).
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only the single target state/handoffs/*.md file passed in via
    #      handoff_path. plan_path is READ-ONLY (_read_plan_exec_fields reads
    #      the plan's frontmatter to source the four-field stamp; never
    #      writes it). Dual-write ban (DR-208 / tri-plane DD#1) satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      Via locked_rmw's path.write_text call inside _stamp_phase's mutate/
    #      write path — exactly one file, the target handoff.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      state/handoffs/*.md is coordinator substrate shared across EM sessions.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The mutated handoff file (handoff_phase + execution_authorized_*
    #      fields) is read by /pickup, shell consumers, other ops, and rag.
    # DR-212 D2 five-bound affirmed (DR-212 § D2):
    #   (i)   Per-file idempotent: full-target-state convergence — _stamp_phase's
    #         _mutate() closure no-ops (applied=False, byte-identical write
    #         skip) ONLY when handoff_phase already equals the requested phase
    #         AND (for phase=execution) every one of the four
    #         execution_authorized_* fields already equals its plan-sourced
    #         intended value. A partial prior stamp converges to the full stamp
    #         on re-run rather than being skipped (see module docstring).
    #   (ii)  Single-target-file-only: exactly one state/handoffs/*.md file is
    #         WRITTEN per call (plan_path is a second file read, never written —
    #         consistent with (ii)'s write-target bound, same pattern as
    #         gate-cascade-clear's read-only blocker re-scan).
    #   (iii) Scope-limited to state/handoffs/*.md frontmatter — body untouched;
    #         no other state/ path is written; no archive/ write.
    #   (iv)  No git commit issued by the handler.
    #   (v)   Reachable only over the ungated in-process command-type surface
    #         via @register_op("handoff.stamp_phase"); no HTTP route
    #         (_OP_KEY_SCOPE["handoff.stamp_phase"] = "common_dir", ipc.py).
    # Authority: docs/decisions/DR-212-handoff-lifecycle-inplace-frontmatter-mutation-carveout.md § D1/D2
    #            docs/decisions/DR-208-invoke-op-authz-model.md § 5
    #            docs/plans/2026-07-17-claude-klabauter-handoff-phase-execution-emit-leg.md § C4
    "handoff.stamp_phase": OpClass.MUTATING,
    # handoff.normalize — MUTATING: in-place frontmatter normalization of all
    # state/handoffs/*.md files (six canonical fields; dry-run capable).
    # DR-208 five-question affirmation (citing ops/handoff_normalize.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      handoff_normalize.py:350 — file_path.write_text(result["rebuilt"]) writes the
    #      normalized frontmatter back to each state/handoffs/*.md (when write=True).
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only state/handoffs/*.md files; no rag store write.
    #      Dual-write ban (DR-208 / tri-plane DD#1) satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      file_path.write_text(...) at handoff_normalize.py:350 opens files for write.
    #      When write=False (dry-run) no file write occurs; MUTATING classification
    #      is correct regardless — the op is capable of writes and must be gated as such.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      state/handoffs/*.md is coordinator substrate shared across EM sessions.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The mutated handoff files are read by shell consumers, other ops, and rag.
    # DR-212 D2 five-bound affirmed (DR-212 § D2):
    #   (i)   Per-file idempotent: normalizing an already-normalized file is a no-op
    #         (no changes emitted, applied=False).
    #   (ii)  D2(ii) batch-normalize exception: handoff.normalize is a scan-all
    #         canonicalize op designed to sweep all state/handoffs/*.md files per invocation
    #         (ported from normalize-handoff-frontmatter.js; batch scope is intrinsic to the
    #         verb). DR-212 D2(ii) amended to permit this bounded batch: N per-file idempotent
    #         writes each atomically, no cross-file state accumulation, no archive/handoffs/
    #         touch, no git commit. handoff.transition and handoff.stamp remain strictly
    #         single-target-file-only. Review: code-reviewer (F2) — corrects prior false claim
    #         of single-target-file-only compliance; doctrine updated in DR-212 D2(ii) + Inv 3.
    #   (iii) Scope-limited to state/handoffs/*.md frontmatter — body untouched; NEVER
    #         archive/handoffs/ (negative-spec: handoff_normalize.py:6); no other state/ path.
    #   (iv)  No git commit issued by the handler (DR-212 D2(iv)).
    #   (v)   Reachable only over the ungated UDS; no HTTP route (DR-212 D2(v)).
    # Authority: docs/decisions/DR-212-handoff-lifecycle-inplace-frontmatter-mutation-carveout.md § D1/D2
    #            docs/decisions/DR-208-invoke-op-authz-model.md § 5
    "handoff.normalize": OpClass.MUTATING,
    # handoff.ship_and_archive — MUTATING: event-driven single-handoff composite that
    # (1) stamps shipped_in (handoff.stamp) + deployment_state:shipped (handoff.transition
    # ship verb), then (2) git-mv's the handoff into archive/handoffs/YYYY-MM/ via the
    # fleet.archive_shipped_handoffs act path. Unlike the pure-frontmatter handoff.* ops,
    # this op DOES issue a git commit (through the archival step).
    # DR-208 five-question affirmation (citing ops/handoff_ship_archive.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      Frontmatter write (via handoff.stamp/ship internals) + archive_and_commit()
    #      git-mv + git-commit into archive/handoffs/YYYY-MM/.
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only state/handoffs/*.md frontmatter, archive/handoffs/ tree, and git
    #      commit objects. Dual-write ban (DR-208 / tri-plane DD#1) satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      Frontmatter write + git-mv destination write.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      state/handoffs/*.md and the git commit are coordinator substrate shared across
    #      EM sessions and all git clients.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The stamped frontmatter, moved file, and git commit are observable after return.
    # DR-211 D2 five-bound affirmed (archival-writer sub-category, inherited from the
    # fleet.archive_shipped_handoffs act path it delegates to):
    #   D2-1 (idempotent): already-shipped ship is a no-op; already-archived replay →
    #         terminal already-archived result; already-stamped shipped_in → no-op.
    #   D2-2 (commutative): single-candidate archival; ordering-independent.
    #   D2-3 (git-reversible): archive/ is git-tracked; git revert recovers the handoff.
    #   D2-4 (act-time re-verify): the archival internal re-checks deployment_state +
    #         shipped_in reachability at act time (graceful skip when shipped_in absent).
    #   D2-5 (no remote route): DR-215 retired the UDS/HTTP transport outright;
    #     no HTTP route was ever added, negative-spec in handoff_ship_archive.py.
    # Authority: docs/decisions/DR-211-fleet-op-substrate-write-boundary.md § D2
    #            docs/decisions/DR-208-invoke-op-authz-model.md § 5
    "handoff.ship_and_archive": OpClass.MUTATING,
    # handoff.close_origin_stub — MUTATING: closes a shipped roadmap-stub's origin
    # handoff by composing handoff.stamp (shipped_in) + handoff.transition ship verb
    # in-process, joining on (roadmap_id, stub_id) via a walk_forward DFS over ancestor
    # handoffs/batons. No git mv (stamp-only);
    # the stub stays in state/handoffs/ for a later archival pass to pick up.
    # DR-208 five-question affirmation (citing ops/handoff_close_origin_stub.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      Delegated handoff.stamp (frontmatter shipped_in write) + handoff.transition
    #      ship verb (deployment_state → shipped), both in-process, in place.
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only state/handoffs/*.md frontmatter via the delegated ops. Dual-write
    #      ban (DR-208 / tri-plane DD#1) satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES
    #      (via delegation). Frontmatter write happens inside the composed
    #      handoff.stamp / handoff.transition calls.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      state/handoffs/*.md is coordinator substrate shared across EM sessions.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The stamped shipped_in + shipped deployment_state are observable after return.
    # Authority: docs/decisions/DR-208-invoke-op-authz-model.md § 5
    "handoff.close_origin_stub": OpClass.MUTATING,
    # handoff.backfill_claim_stamp — MUTATING: stamps claim provenance onto a
    # worked-but-unstamped baton's frontmatter, gated on --evidence-commit SHAs that
    # must resolve in-tree before any write (ops/handoff_backfill_claim_stamp.py AC2).
    # Authority: docs/decisions/DR-208-invoke-op-authz-model.md § 5
    "handoff.backfill_claim_stamp": OpClass.MUTATING,
    # handoff.repoint_origin — MUTATING: DR-208-default classification (new op, not
    # yet reviewer-affirmed COMPUTE_ONLY). The handler writes updated frontmatter back
    # to a state/handoffs/*.md file (Path.write_text) to repoint its origin — see
    # coordinator_core/ops/handoff_repoint_origin.py.
    # Authority: docs/decisions/DR-208-invoke-op-authz-model.md § "New ops default to
    # MUTATING until a reviewer affirms COMPUTE_ONLY."
    "handoff.repoint_origin": OpClass.MUTATING,
    # initiative.serve_set — COMPUTE_ONLY: reads state/initiatives/*.yaml under the
    # main worktree and returns the attachable-initiative set as a JSON payload. No
    # file writes, no git ops, no subprocess spawns. Handler: ops/initiatives_serve.py.
    # DR-208 five-question affirmation:
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  No.
    #      initiatives_serve.py:_collect_initiatives — reads only via fpath.read_text().
    #   2. Writes into rag's relational store?                                 No.
    #   3. Opens any file for write (including sentinel creation)?             No.
    #   4. Mutates shared mutable state outside its own module?                No.
    #   5. Persistent state changes observable across process boundaries?     No.
    # Spec backlink: pln-claude-klabauter-served-initiative-roadm-8e0492 § C2
    "initiative.serve_set": OpClass.COMPUTE_ONLY,
    # goal.match_candidates — COMPUTE_ONLY: reads state/goals/*.yaml under the main worktree,
    # ranks active goals by difflib.SequenceMatcher similarity, and returns a computed ranked
    # list. No file writes, no git ops, no subprocess spawns. Handler: ops/goals_match.py.
    # DR-208 five-question affirmation:
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  No.
    #      goals_match.py:_collect_goals — reads only via fpath.read_text().
    #   2. Writes into rag's relational store?                                 No.
    #   3. Opens any file for write (including sentinel creation)?             No.
    #   4. Mutates shared mutable state outside its own module?                No.
    #   5. Persistent state changes observable across process boundaries?     No.
    # Spec backlink: DoE-claude:pln-per-repo-okr-goal-setting-syst-80bced § C3
    "goal.match_candidates": OpClass.COMPUTE_ONLY,
    # goal.close_day — COMPUTE_ONLY: reads the collapsed goals-log wire via
    # coordinator_core.goals.wire_read.read_and_collapse (no second glob/collapse
    # copy) and returns a computed today/stale partition of open period="day" rows.
    # No file writes, no git ops, no subprocess spawns. Handler: ops/goal_close_day.py.
    # DR-208 five-question affirmation:
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  No.
    #      goal_close_day.py:collect_open_day_goals — reads only via
    #      wire_read.read_and_collapse (Path.glob + read_text).
    #   2. Writes into rag's relational store?                                 No.
    #   3. Opens any file for write (including sentinel creation)?             No.
    #   4. Mutates shared mutable state outside its own module?                No.
    #   5. Persistent state changes observable across process boundaries?     No.
    # Spec backlink: pln-day-scoped-goal-close-out-life-69a25c § C2
    "goal.close_day": OpClass.COMPUTE_ONLY,
    # goal.close_day_apply — MUTATING: re-appends one row per close-out decision to
    # the per-machine goals-log JSONL shard via goal_append.append_goal (same write
    # path as goal.append). Handler: ops/goal_close_day.py::close_day_goals.
    # DR-208 five-question affirmation:
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  Yes —
    #      appends a line to <central_state_root>/goals-log.<machine>.jsonl.
    #   2. Writes into rag's relational store?                                 No.
    #   3. Opens any file for write (including sentinel creation)?             Yes —
    #      the goals-log shard, append mode only, never rewritten.
    #   4. Mutates shared mutable state outside its own module?                No.
    #   5. Persistent state changes observable across process boundaries?      Yes.
    # Spec backlink: pln-day-scoped-goal-close-out-life-69a25c § C3
    "goal.close_day_apply": OpClass.MUTATING,
    # plan.match_candidates — COMPUTE_ONLY: reads docs/plans/*.md frontmatter under the main
    # worktree, ranks plans by difflib.SequenceMatcher similarity, and returns a computed ranked
    # list. No file writes, no git ops, no subprocess spawns. Handler: ops/plan_match.py.
    # DR-208 five-question affirmation:
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  No.
    #      plan_match.py:_collect_plans — reads only via fpath.read_text().
    #   2. Writes into rag's relational store?                                 No.
    #   3. Opens any file for write (including sentinel creation)?             No.
    #   4. Mutates shared mutable state outside its own module?                No.
    #   5. Persistent state changes observable across process boundaries?     No.
    # Spec backlink: pln-claude-klabauter-fork-provenance-creatio-01c09f § C2
    "plan.match_candidates": OpClass.COMPUTE_ONLY,
    # handoff.match_candidates — COMPUTE_ONLY: reads state/handoffs/*.md frontmatter under the
    # main worktree, ranks handoffs by difflib.SequenceMatcher similarity, and returns a computed
    # ranked list. No file writes, no git ops, no subprocess spawns. Handler: ops/handoff_match.py.
    # DR-208 five-question affirmation:
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  No.
    #      handoff_match.py:_collect_handoffs — reads only via fpath.read_text().
    #   2. Writes into rag's relational store?                                 No.
    #   3. Opens any file for write (including sentinel creation)?             No.
    #   4. Mutates shared mutable state outside its own module?                No.
    #   5. Persistent state changes observable across process boundaries?     No.
    # Spec backlink: pln-claude-klabauter-fork-provenance-creatio-01c09f § C2
    "handoff.match_candidates": OpClass.COMPUTE_ONLY,
    # handoff.author_fork — MUTATING: creates a new ``state/handoffs/*.md`` fork artifact with
    # provenance fields (origin_session, origin_handoff, origin_plan_id, origin_goal_id) populated
    # at spawn time. Additive-create semantics (DR-213): atomic locked_rmw(missing_ok=True) write
    # of a new uniquely-named file; never overwrites an existing handoff.
    # DR-208 five-question affirmation (citing ops/handoff_author_fork.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      locked_rmw(missing_ok=True) creates state/handoffs/<timestamp>-<uuid>.md.
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only coordinator state/handoffs/; dual-write ban (DR-208 / tri-plane DD#1) satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      locked_rmw opens a temp file and os.replace-atomically creates the new handoff.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      state/handoffs/*.md is coordinator substrate shared across EM sessions.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The new handoff file is read by git, the shell layer, and rag ingestion.
    # Spec backlink: pln-claude-klabauter-fork-provenance-creatio-01c09f § C3
    "handoff.author_fork": OpClass.MUTATING,
    # plan.persist_capture — MUTATING: scaffolds a new docs/plans/*.md plan artifact
    # (via a coordinator-doc-new subprocess) from a captured harness plan-mode body,
    # and may unlink a superseded frontmatter-less duplicate under docs/plans/.
    # DR-208 five-question affirmation (citing ops/plan_capture_persist.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      Writes a new docs/plans/<date>-<slug>.md; may unlink a duplicate raw dump
    #      at a different docs/plans/*.md path.
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only coordinator docs/plans/; dual-write ban (DR-208) satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      Reads the coordinator-doc-new subprocess's own write, then re-writes it
    #      in place with the spliced body/task-spine.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      docs/plans/*.md is coordinator substrate shared across EM sessions.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The new plan file is read by git, other sessions, and rag ingestion.
    # Spec backlink: state/handoffs/2026-08-13-vanilla-plan-mode-capture-safety-net.md § Part 2
    "plan.persist_capture": OpClass.MUTATING,
    # handoff.lineage_ancestry — COMPUTE_ONLY: reads state/handoffs/*.md frontmatter under the
    # main worktree via dag.walk_forward(edge_kinds={'origin_handoff'}), and returns a computed
    # ordered ancestry list. No file writes, no git ops, no subprocess spawns.
    # Handler: ops/handoff_lineage_ancestry.py.
    # DR-208 five-question affirmation:
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  No.
    #      dag.walk_forward / dag._read_meta — reads only via Path.read_bytes().
    #   2. Writes into rag's relational store?                                 No.
    #   3. Opens any file for write (including sentinel creation)?             No.
    #   4. Mutates shared mutable state outside its own module?                No.
    #   5. Persistent state changes observable across process boundaries?     No.
    # Spec backlink: pln-claude-klabauter-fork-provenance-creatio-01c09f § C4
    "handoff.lineage_ancestry": OpClass.COMPUTE_ONLY,
    # plan.tasks.mutate — MUTATING: in-place mutation of a docs/plans/*.md file's
    # '## Tasks' fenced body block (add-task / stamp verbs). Same scope class and
    # write-shape as the handoff.* DR-212 lifecycle sub-category.
    # DR-208 five-question affirmation (citing ops/plan_tasks_mutate.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      locked_rmw(path, mutate, ...) writes the mutated plan text back to
    #      docs/plans/*.md in place.
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only the single target docs/plans/*.md file; no rag store write.
    #      Dual-write ban (DR-208 / tri-plane DD#1) satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      locked_rmw opens the target file for the atomic rewrite.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      docs/plans/*.md is coordinator substrate shared across EM sessions.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The mutated plan file is read by shell consumers, other ops, and rag.
    # Spec backlink: pln-pcli-need-1-plan-tasks-engine--53c00d § C3
    "plan.tasks.mutate": OpClass.MUTATING,
    # plan.tasks.grouping_digest — COMPUTE_ONLY, the read-only sibling of plan.tasks.mutate
    # immediately above. It computes the digest a PENDING resolve write is about to produce;
    # producing that write is plan.tasks.mutate's job, and the split is the whole point of
    # this module (see its docstring: "never opens the plan file for write, never takes the
    # file lock, never calls locked_rmw"). Five-question checklist all "no": it parses the
    # '## Tasks' block, applies a prospective cut in memory, and returns a digest.
    # Reintroducing a write here would collapse the separation this op exists to enforce.
    # Spec backlink: pln-pcli-need-1-plan-tasks-engine--53c00d § C3
    "plan.tasks.grouping_digest": OpClass.COMPUTE_ONLY,
    # plan.list_orphaned — COMPUTE_ONLY: scans <repo_root>/docs/plans/*.md read-only
    # (frontmatter + resolve_plan_owner's own read-only state/handoffs/*.md scan) and
    # returns the computed tiered orphan census. C2 of the plan-orphan-ownership-resolver
    # deliverable; sibling of plan.list_stale_executing immediately above it in
    # draft_plan_aging.py (that op predates this classification table and carries no
    # entry to copy — this one templates plan.tasks.grouping_digest's shape instead).
    # DR-208 five-question affirmation (citing ops/draft_plan_aging.py's list_orphaned):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  No.
    #      Reads plan frontmatter and handoff frontmatter via path.read_text() only.
    #   2. Writes into rag's relational store?                                 No.
    #   3. Opens any file for write (including sentinel creation)?             No.
    #   4. Mutates shared mutable state outside its own module?                No.
    #   5. Persistent state changes observable across process boundaries?     No.
    # Spec backlink: pln-plan-orphan-ownership-resolver-3e68bb § C2
    "plan.list_orphaned": OpClass.COMPUTE_ONLY,
    # plan.suggest_completion_steps — COMPUTE_ONLY: scans
    # <repo_root>/docs/plans/*.md (frontmatter reads) and
    # state/review-trail/*.json + archive/review-trail/*.json (read-only,
    # via list_review_trail_records._collect() rooted at the explicit
    # repo_root) plus `git log`/`git rev-list` queries, returning the
    # computed completion-steps list. Assist surface for the vanilla-plan-
    # mode safety net (Part 3, state/handoffs/
    # 2026-08-13-vanilla-plan-mode-capture-safety-net.md); sibling of
    # plan.list_orphaned immediately above.
    # DR-208 five-question affirmation:
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  No.
    #      Reads plan frontmatter, review-trail JSON, and git history only.
    #   2. Writes into rag's relational store?                                 No.
    #   3. Opens any file for write (including sentinel creation)?             No.
    #   4. Mutates shared mutable state outside its own module?                No.
    #   5. Persistent state changes observable across process boundaries?     No.
    # Spec backlink: state/handoffs/2026-08-13-vanilla-plan-mode-capture-safety-net.md § Part 3
    "plan.suggest_completion_steps": OpClass.COMPUTE_ONLY,
    # commit.anchors — COMPUTE_ONLY: derives git-trailer text (Plan/Plan-Id/Deliverable/
    # Nature/Anchor) from the staged diff + on-disk read-model and RETURNS it; the git-message
    # write is done by the prepare-commit-msg hook, NOT this op (causal-direction test,
    # ipc.py:28-31). DR-208 five-question affirmation (citing ops/commit_anchors.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  No.
    #      Handler only reads: `git diff --cached --name-only` (read-only), plan frontmatter,
    #      state/handoffs/*.md scan, COMMIT_EDITMSG read. No git write, no state write.
    #   2. Writes into rag's relational store?                                 No.
    #   3. Opens any file for write (including sentinel creation)?             No.
    #      Returns trailer text to the caller; stages nothing, writes no temp file.
    #   4. Mutates shared mutable state outside its own module?                No.
    #   5. Persistent state changes observable across process boundaries?     No.
    # Read-only git subprocess (git diff --cached) is permitted per the ipc.py:44 carve-out.
    # Spec: docs/plans/2026-07-04-claude-klabauter-commit-anchor-stamper.md § D2/AC2.
    "commit.anchors": OpClass.COMPUTE_ONLY,
    # ---------------------------------------------------------------------------
    # queue.* DR-213 additive-create sub-category — both ops classified MUTATING
    # (DR-213 D2 bounds derived from additive-create semantics; DR-208 § 5 affirmation below)
    # ---------------------------------------------------------------------------
    # queue.append — MUTATING: writes a per-entry YAML file to one of four named state/ subdirs:
    # state/debt-backlog/, state/bug-backlog/, state/improvement-queue/, or state/lessons/.
    # Additive-create semantics: each entry is a new file keyed by date+slug; same-date-same-slug
    # overwrites (idempotent-by-filename-overwrite, last-write-wins via os.replace — ONE file).
    # Writes coordinator substrate ONLY, never rag's relational store (dual-write ban, DR-208 /
    # tri-plane DD#1). Cite DR-213 § D2 for the additive-create D2 bounds.
    # DR-208 five-question affirmation (citing ops/queue_append.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      queue_append.py:append_queue_entry — atomic mkstemp+os.replace writes
    #      <state>/<schema>/<date>-<slug>.yaml in the caller's repo or claude-klabauter state/.
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only coordinator state/ YAML; no rag store write.
    #      Dual-write ban (DR-208 / tri-plane DD#1) satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      mkstemp+os.replace in queue_append.py:append_queue_entry opens a temp file.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      state/<schema>/*.yaml is coordinator substrate shared across EM sessions.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The written YAML files are read by git, the shell layer, and rag ingestion.
    # DR-213 D2 five-bound affirmed (DR-213 § D2, additive-create — NOT DR-211 archival-move):
    #   D2-1 (idempotent-by-filename-overwrite): same date+slug → os.replace → one file.
    #   D2-2 (commutative): write order does not change final state/ contents.
    #   D2-3 (git-reversible): state/ is git-tracked; git revert recovers any entry.
    #   D2-4 (NO terminality-re-verify): additive-create has no terminality concept;
    #         DR-211's act-time-terminality-re-verify does NOT apply here — explicitly excluded.
    #   D2-5 (no remote route): DR-215 retired the UDS/HTTP transport outright;
    #     no HTTP route was ever added, negative-spec in queue_append.py module docstring.
    # Authority: docs/decisions/DR-213-queue-write-substrate-carveout.md § D2
    #            docs/decisions/DR-208-invoke-op-authz-model.md § 5
    "queue.append": OpClass.MUTATING,
    # peer_notice.send — writes a notice file under state/peer-notices/.
    "peer_notice.send": OpClass.MUTATING,
    # peer_notice.check — read-only listing of unread notices addressed to a session.
    "peer_notice.check": OpClass.COMPUTE_ONLY,
    # queue.promote — MUTATING: writes a per-entry YAML file to state/lessons-outbox/
    # (central claude-klabauter state; outbox for /learn-lessons --central drain). Distinct from
    # queue.append: lessons-outbox schema (not lesson-entry alias), uuid id field, ISO-ts
    # filename, no system block, argparse-choices validation.
    # Writes coordinator substrate ONLY, never rag's relational store (dual-write ban, DR-208 /
    # tri-plane DD#1). Cite DR-213 § D2 for the additive-create D2 bounds.
    # DR-208 five-question affirmation (citing ops/queue_promote.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      queue_promote.py:promote_lesson — atomic mkstemp+os.replace writes
    #      <claude-klabauter>/state/lessons-outbox/<ISO-ts-safe>-<slug>.yaml.
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only coordinator state/lessons-outbox/ YAML; no rag store write.
    #      Dual-write ban (DR-208 / tri-plane DD#1) satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      mkstemp+os.replace in queue_promote.py:promote_lesson opens a temp file.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      state/lessons-outbox/*.yaml is coordinator substrate shared across EM sessions.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The written YAML files are drained by /learn-lessons --central and read by rag.
    # DR-213 D2 five-bound affirmed (DR-213 § D2, additive-create — NOT DR-211 archival-move):
    #   D2-1 (idempotent-by-filename-overwrite): same ISO-ts+slug → os.replace → one file.
    #   D2-2 (commutative): write order does not change final state/ contents.
    #   D2-3 (git-reversible): state/ is git-tracked; git revert recovers any entry.
    #   D2-4 (NO terminality-re-verify): additive-create has no terminality concept;
    #         DR-211's act-time-terminality-re-verify does NOT apply here — explicitly excluded.
    #   D2-5 (no remote route): DR-215 retired the UDS/HTTP transport outright;
    #     no HTTP route was ever added, negative-spec in queue_promote.py module docstring.
    # Authority: docs/decisions/DR-213-queue-write-substrate-carveout.md § D2
    #            docs/decisions/DR-208-invoke-op-authz-model.md § 5
    "queue.promote": OpClass.MUTATING,
    # ---------------------------------------------------------------------------
    # queue.cluster — READ-class queue-triage primitive, classified COMPUTE_ONLY
    # (DR-208 § 5 affirmation below)
    # ---------------------------------------------------------------------------
    # queue.cluster — COMPUTE_ONLY: clusters a caller-scoped queue family's records
    # (loaded via queue_family.load_family_records → records_query.query_records,
    # read-only) over a caller-selectable signal set and returns a bare list. No
    # write path anywhere in coordinator_core/ops/queue_cluster.py.
    # DR-208 five-question affirmation (citing ops/queue_cluster.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  No.
    #      Handler only reads via queue_family.load_family_records (query_records
    #      under the hood); no write, no git subprocess.
    #   2. Writes into rag's relational store?                                 No.
    #   3. Opens any file for write (including sentinel creation)?             No.
    #      Returns a computed list to the caller; nothing is staged or written.
    #   4. Mutates shared mutable state outside its own module?                No.
    #   5. Persistent state changes observable across process boundaries?     No.
    # Authority: docs/decisions/DR-208-invoke-op-authz-model.md § 5
    # Spec: docs/plans/2026-07-23-queue-triage-terminus-ops.md § C3
    "queue.cluster": OpClass.COMPUTE_ONLY,
    # ---------------------------------------------------------------------------
    # handoff.scaffold_from_queue — MUTATING: turns a queue-triage selection into a
    # new state/handoffs/*.md baton (DR-208 § 5 affirmation below)
    # ---------------------------------------------------------------------------
    # handoff.scaffold_from_queue — MUTATING: creates one new state/handoffs/*.md
    # file under the caller's main-worktree-rooted state/handoffs/ via
    # locked_rmw(missing_ok=True) (atomic mkstemp+os.replace under flock, mirroring
    # handoff.author_fork). Op-proposes only — it does NOT close, delete, or mutate
    # any source queue row; the caller's EM disposes those separately using the
    # echoed source_entries.
    # DR-208 five-question affirmation (citing ops/queue_scaffold_baton.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      queue_scaffold_baton.py's handler writes one new state/handoffs/*.md file
    #      via locked_rmw.
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only coordinator state/handoffs/ frontmatter; no rag store write.
    #      Dual-write ban (DR-208 / tri-plane DD#1) satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      locked_rmw's mkstemp+os.replace opens a temp file for the new baton.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      state/handoffs/*.md is coordinator substrate shared across EM sessions.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The written handoff is read by session pickup, ceremony ops, and the
    #      caller's own EM.
    # DR-212 handoff-lifecycle in-place-mutation carve-out does NOT apply here — this
    # op CREATES a new file (MutateAbort on any collision), it does not mutate an
    # existing handoff in place; classified on first principles per DR-208 § 5
    # above, same as handoff.author_fork's own MUTATING classification.
    # Authority: docs/decisions/DR-208-invoke-op-authz-model.md § 5
    # Spec: docs/plans/2026-07-23-queue-triage-terminus-ops.md § C5
    "handoff.scaffold_from_queue": OpClass.MUTATING,
    # handoff.correct_body — MUTATING: bounded body-correction door for a
    # `status: claimed` (or legacy `status: consumed`) state/handoffs/*.md file
    # — a single caller-supplied old_string -> new_string in-place body
    # replacement, applied via the same locked_rmw seam handoff.stamp uses.
    # DR-208 five-question affirmation (citing ops/handoff_correct_body.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      handoff_correct_body.py's _handler routes the replacement through
    #      locked_rmw(p, _mutate, repo_root=repo_root), which writes the
    #      rebuilt text back to the single target state/handoffs/*.md file.
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only the single target state/handoffs/*.md file; no rag
    #      store write. Dual-write ban (DR-208 / tri-plane DD#1) satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      locked_rmw's write path opens the target file for write.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      state/handoffs/*.md is coordinator substrate shared across EM sessions.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The corrected body (plus the stamped correction-log note) is read by
    #      shell consumers, other ops, and rag.
    # Unlike the DR-212 frontmatter-only handoff.* population above, this op
    # mutates the BODY, never the frontmatter (module docstring: "the ONLY
    # handoff.* verb DR-212 D2(iii)/D4's frontmatter-only carve-out does not
    # bind"); classified on DR-208 § 5 first principles per DR-247, not folded
    # into the DR-212 handoff-lifecycle sub-category above.
    # Authority: docs/decisions/DR-247-bounded-body-write-carveout-for-claimed-handoff.md
    #            docs/decisions/DR-208-invoke-op-authz-model.md § 5
    # Spec: docs/plans/2026-07-31-claimed-baton-body-correction-route.md § C2
    "handoff.correct_body": OpClass.MUTATING,
    # handoff.discharge_criteria — MUTATING: bounded wrapper over
    # handoff.correct_body for ticking (or splitting) an `## Acceptance criteria`
    # checkbox in a claimed handoff body, resolving the target box by criterion
    # identity or structural position rather than by raw line text. It owns no
    # write path of its own — it delegates wholesale to correct_body's handler,
    # so it inherits that op's ownership arms, archive-follow resolution,
    # terminal-state refusal, D2 size caps, and stamped paper trail.
    # DR-208 five-question affirmation (citing ops/handoff_discharge_criteria.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      Transitively, via handoff_correct_body._handler's locked_rmw seam.
    #   2. Writes into rag's relational store?                                 No.
    #      Same single-target write as correct_body; dual-write ban satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      Transitively, through the delegated locked_rmw write path.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      The handoff body is coordinator substrate shared across EM sessions.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The ticked/split criteria list is what leg A's completeness gate reads.
    # Classified MUTATING on its own account rather than by delegation alone: an
    # op that reaches substrate through a sibling is still the op the caller
    # invoked, and authz resolves on the invoked name.
    # Authority: docs/decisions/DR-274-possession-replaces-authorship-as-handof.md § D3
    #            docs/decisions/DR-208-invoke-op-authz-model.md § 5
    # Spec: docs/plans/2026-08-06-executing-session-can-discharge-criteria.md § C7
    "handoff.discharge_criteria": OpClass.MUTATING,
    # handoff.author_lint — COMPUTE_ONLY: reports the hand-typed values in a
    # handoff/spinoff body whose real gate fires much later, on someone else's
    # session (a zero-checkbox `## Acceptance criteria`, a Session Ledger row
    # the canonical grammar silently drops, an unreplaced placeholder or
    # over-cap `summary:`). It is the read-only sibling of the two MUTATING
    # body verbs above and deliberately owns no repair path: an op that
    # silently fixed a body would re-create the defect it exists to surface,
    # because the author would keep typing the wrong shape and keep not
    # learning.
    # DR-208 five-question affirmation (citing ops/handoff_author_lint.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  No.
    #      Reads one path and returns findings; no write path, delegated or own.
    #   2. Writes into rag's relational store?                                 No.
    #   3. Opens any file for write (including sentinel creation)?             No.
    #      `read_text` only. No sentinel, no lock file, no temp file.
    #   4. Mutates shared mutable state outside its own module?                No.
    #   5. Persistent state changes observable across process boundaries?      No.
    #      Its entire output is the returned envelope.
    # COMPUTE_ONLY over MUTATING is load-bearing, not a formality: the whole
    # point of moving enforcement earlier is that consulting the lint costs an
    # author nothing and risks nothing, so it can be invoked freely at author
    # time. An authz class implying substrate mutation would make callers
    # hesitate to run exactly the check this op exists to make cheap.
    # Authority: docs/decisions/DR-208-invoke-op-authz-model.md § 5
    # Spec: state/handoffs/2026-08-21-handoffs-and-spinoffs-minimal-for-hand-rolling.md
    #       docs/reference/handoff-authoring-surface-classification.md
    "handoff.author_lint": OpClass.COMPUTE_ONLY,
    # handoff.append_session_ledger — MUTATING: bounded wrapper over
    # handoff.correct_body for appending the caller's own machine-resolved
    # `## Session Ledger` row (date/sid6/tshirt/dispatch-counts computed;
    # only `summary` is caller-supplied) to a claimed handoff body. Same
    # shape as handoff.discharge_criteria immediately above: it owns no
    # write path of its own, delegating wholesale to correct_body's handler.
    # DR-208 five-question affirmation (citing
    # ops/handoff_append_session_ledger.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      Transitively, via handoff_correct_body._handler's locked_rmw seam.
    #   2. Writes into rag's relational store?                                 No.
    #      Same single-target write as correct_body; dual-write ban satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      Transitively, through the delegated locked_rmw write path.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      The handoff body is coordinator substrate shared across EM sessions.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The appended row is what chain-LoE aggregation and downstream
    #      readers of the ledger sum.
    # Classified MUTATING on its own account rather than by delegation alone,
    # same rationale as handoff.discharge_criteria's own entry above.
    # Authority: DR-247 (handoff.correct_body) / DR-274 § D3 (a second body-
    #            mutating wrapper verb built the same way) / DR-208 § 5.
    # Spec: state/handoffs/2026-08-21-handoffs-and-spinoffs-minimal-for-hand-rolling.md AC-5
    "handoff.append_session_ledger": OpClass.MUTATING,
    # handoff.propagate — MUTATING: peer-delivery door into a `status: claimed`
    # (or legacy `status: consumed`) state/handoffs/*.md file's `## Propagated`
    # section, with no authorship gate (the sibling verb DR-247's `handoff.
    # correct_body` structurally excludes for the non-author/peer case).
    # DR-208 five-question affirmation (citing ops/propagate_body.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      propagate_body.py's _handler routes the append through
    #      locked_rmw(p, _mutate, repo_root=repo_root), which writes the
    #      rebuilt text back to the single target state/handoffs/*.md file,
    #      AND git-commits that single file (AC12) via commit-tree/update-ref.
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only the single target state/handoffs/*.md file plus the
    #      commit that lands it; no rag store write. Dual-write ban
    #      (DR-208 / tri-plane DD#1) satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      locked_rmw's write path opens the target file for write.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      state/handoffs/*.md is coordinator substrate shared across EM sessions.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The appended delivery note (plus the landed commit) is read by
    #      shell consumers, other ops, and rag.
    # Authority: docs/plans/2026-08-01-baton-spine-information-integrity.md § Part B
    #            docs/decisions/DR-247-bounded-body-write-carveout-for-claimed-handoff.md § 3
    #            docs/decisions/DR-208-invoke-op-authz-model.md § 5
    "handoff.propagate": OpClass.MUTATING,
    # plan.propagate — MUTATING: B2 (AC8) sibling registration of the SAME
    # propagate_body.py op against a different target root — a live
    # docs/plans/**.md plan body's `## Propagated` section instead of a
    # state/handoffs/*.md baton. Same DR-208 five-question affirmation as
    # handoff.propagate immediately above (same module, same locked_rmw +
    # git commit-tree/update-ref write path); no additional dual-write-ban
    # consideration — this verb still writes only the single target file
    # plus its own scoped commit.
    # Authority: docs/plans/2026-08-01-baton-spine-information-integrity.md § Part B (B2)
    #            docs/decisions/DR-208-invoke-op-authz-model.md § 5
    "plan.propagate": OpClass.MUTATING,
    # roadmap.link_stubs — MUTATING: the first op that AUTHORS a roadmap-
    # dependency edge — writes `blocked_by` on the dependent stub and the
    # reciprocal `blocks` on the dependency stub, a two-file compound
    # transaction on state/handoffs/*.md roadmap-baton frontmatter, with NO
    # authorship gate (a roadmap baton's authoring_session is a path,
    # state/roadmap/<id>/, by construction — see DR-264 "General principle").
    # DR-208 five-question affirmation (citing ops/roadmap_link_stubs.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      roadmap_link_stubs.py's _write_edge_field routes each of the two
    #      writes through locked_rmw(path, mutate, repo_root=repo_root), which
    #      writes the rebuilt frontmatter back to the single target
    #      state/handoffs/*.md file (dependent's blocked_by, dependency's blocks).
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only the two resolved state/handoffs/*.md files; no rag
    #      store write. Dual-write ban (DR-208 / tri-plane DD#1) satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      locked_rmw's write path opens each target file for write.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      state/handoffs/*.md is coordinator substrate shared across EM sessions.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The written blocked_by/blocks edge is read by run_check_mode,
    #      the roadmap-DAG assembler, shell consumers, other ops, and rag.
    # DR-212 D2 five-bound affirmed, AS EXTENDED by DR-264 (this op is NOT
    # handoff.*-named, so it cannot inherit DR-212's carve-out by citation —
    # DR-264 restates and satisfies all five bounds for this op's two-file
    # compound-transaction shape; see DR-264 § "## Decision" for the full
    # per-bound argument):
    #   (i)   Per-file idempotent, with REPAIR (not skip) of a half-present
    #         edge — see roadmap_link_stubs.py module docstring.
    #   (ii)  Two-target-file compound-transaction exception (DR-264's named,
    #         bounded second exception to D2(ii), distinct from and
    #         structured like handoff.normalize's N-independent-writes
    #         exception — argued on its own safety case, not inherited).
    #   (iii) Scope-limited to the `blocked_by`/`blocks` array fields only, on
    #         roadmap-baton kind only — narrower than DR-212's general
    #         frontmatter-block scope. No body-content write.
    #   (iv)  No git commit issued by the handler (DR-212 Invariant 4 /
    #         DR-247 § (vi) / DR-264 (iv)).
    #   (v)   Reachable only over the ungated UDS; no HTTP route.
    #   (D4)  No archive/ write — an endpoint resolving only in the archived
    #         corpus is a REFUSAL, never a write target.
    # Authority: docs/decisions/DR-264-roadmap-link-stubs-frontmatter-mutation-.md
    #            docs/decisions/DR-212-handoff-lifecycle-inplace-frontmatter-mutation-carveout.md
    #            docs/decisions/DR-247-bounded-body-write-carveout-for-claimed-handoff.md § 3
    #            docs/decisions/DR-208-invoke-op-authz-model.md § 5
    # Spec: docs/plans/2026-08-05-roadmap-graph-enforcement-gap.md § C4
    "roadmap.link_stubs": OpClass.MUTATING,
    # ---------------------------------------------------------------------------
    # memo.send DR-214 (send-class) cross-tree write op — classified MUTATING
    # (DR-214-send-class D2 bounds affirmed per handler code; DR-208 § 5 affirmation below)
    # ---------------------------------------------------------------------------
    # Row restored 2026-08-25 with the op's rebuild (C2). The kill of 2026-08-23 removed the
    # entry but left this header block standing, so the op read as classified while
    # OP_CLASSIFICATION.get("memo.send") returned None.
    # Spec: docs/plans/2026-08-25-memo-send-three-writes-and-one-commit-th.md § C2/AC8
    "memo.send": OpClass.MUTATING,
    # deliverable.rollup — COMPUTE_ONLY: scans docs/plans/*.md, state/handoffs/*.md, and
    # archive/handoffs/**/*.md frontmatter for artifacts whose deliverable_id FK equals the
    # queried value, unions their non-null initiative FKs, and resolves each to its
    # state/initiatives/<id>.yaml entry. Returns structured fields only; no prose composed.
    # DR-208 five-question affirmation (citing ops/deliverable_rollup.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  No.
    #      Handler only reads plan/handoff frontmatter via _read_meta() and initiative
    #      YAML via read_text() — no git write, no state write, no subprocess of any kind
    #      except the env-miss-only, non-git machine-local registry-resolution subprocess
    #      (read-only, non-mutating, memoized at most once per process).
    #   2. Writes into rag's relational store?                                 No.
    #   3. Opens any file for write (including sentinel creation)?             No.
    #      Returns structured payload to the caller; stages nothing, writes no temp file.
    #   4. Mutates shared mutable state outside its own module?                No.
    #   5. Persistent state changes observable across process boundaries?     No.
    # Zero git subprocess — stricter than commit.anchors (no staged-index reads to perform;
    # the commit.anchors read-only git carve-out does NOT apply here and does NOT transfer).
    # Spec: docs/plans/2026-07-06-claude-klabauter-deliverable-spine-factsupply-op.md § C2/C3
    "deliverable.rollup": OpClass.COMPUTE_ONLY,
    # deliverable.fork_detect — COMPUTE_ONLY. Landed by a sibling session (d94775334,
    # C7 of the baton-closes-when-its-plan-ships plan) with an _OP_KEY_SCOPE and an
    # OP_MODULE_MAP entry but no classification, leaving check_registration_quad() red
    # at HEAD for every session in this tree. Classified here from the op's own
    # documented negative-spec rather than inferred:
    #   Q1 file opened for write?   NO  ("never opens state/deliverable-equivalence.yaml
    #                                    in any write mode, and never imports a writer of it")
    #   Q2 git write command?       NO.
    #   Q3 queue/backlog mutation?  NO  (no automatic row write; C1g deleted the `entries:`
    #                                    map and its canonicalize()/load_equivalence_map()
    #                                    writer symbols outright, so there is no equivalence
    #                                    map left to write into. The file's surviving
    #                                    `ledger:` side is read-only via seed_deliverable_ledger_rows).
    #   Q4 subprocess spawned?      NO.
    #   Q5 write-vs-read branch?    NO  (single read path: calls seed_deliverable_ledger_rows
    #                                    for its rows, then clusters them in memory).
    # Its transitive callee is the SEEDER's read path; if that ever grows a write, this
    # entry is the thing that must change with it.
    # Spec: docs/plans (baton-closes-when-its-plan-ships) § C7
    "deliverable.fork_detect": OpClass.COMPUTE_ONLY,
    # ---------------------------------------------------------------------------
    # memo.list / memo.draft / memo.compose (strang-0x memo-tool-rebuild C7) —
    # memo.list is COMPUTE_ONLY (pure read); memo.draft/memo.compose are MUTATING
    # (each writes a file, even though the write is confined to the calling repo's
    # OWN cross-repo/outbox/ tree — own-tree confinement narrows privilege, it does
    # not change the class; see the correction note below).
    #   - memo.list: pure registry read/enumeration; never writes, commits, or
    #     touches the network (repo_root unused — see memo_list.py handler docstring).
    #   - memo.draft: creates ONE file in the CALLING repo's OWN cross-repo/outbox/
    #     (never a receiver's inbox — that write is memo.send's exclusive surface).
    #   - memo.compose: edits an existing draft in the CALLING repo's own outbox
    #     (status must be "draft"; refuses on any other status).
    # DR-208 five-question affirmation (memo.list, citing ops/fleet/memo_list.py):
    #   1. Writes/deletes/reorders any state file, queue, or git object?      No.
    #      No open(..., "w")/os.replace/git-write call anywhere in the handler.
    #   2. Writes into rag's relational store?                               No.
    #   3. Opens any file for write (including sentinel creation)?           No.
    #   4. Mutates shared mutable state outside its own module?              No.
    #   5. Persistent state changes observable across process boundaries?   No.
    #      Returns a build_dry_run_result/build_setup_error_result envelope only.
    "memo.list": OpClass.COMPUTE_ONLY,
    # DR-208 five-question affirmation (memo.blitz_buckets, citing
    # ops/fleet/memo_blitz_buckets.py):
    #   1. Writes/deletes/reorders any state file, queue, or git object?      No.
    #      The handler's only filesystem calls are Path.glob and read_text; it
    #      reports buckets and supersession CANDIDATES and never flips a memo's
    #      lifecycle (that stays with the /pickup memo branch).
    #   2. Writes into rag's relational store?                               No.
    #   3. Opens any file for write (including sentinel creation)?           No.
    #   4. Mutates shared mutable state outside its own module?              No.
    #   5. Persistent state changes observable across process boundaries?   No.
    #      Returns a build_dry_run_result/build_setup_error_result envelope only,
    #      and rejects dry_run:false rather than acquiring an act mode.
    "memo.blitz_buckets": OpClass.COMPUTE_ONLY,
    # CORRECTION (2026-07-21 review, Finding 1 of the memo-clean-split-op-coverage
    # slice review): this entry was previously classified COMPUTE_ONLY despite its
    # own Q1 answer being YES — a self-contradicting entry that also inverted its
    # own cited precedent (queue.append/backlog.record are BOTH classified MUTATING
    # elsewhere in this file, not COMPUTE_ONLY as the prior comment claimed). Per
    # DR-208's fail-closed rule ("ambiguous cases classify MUTATING"), and because
    # this case is not even ambiguous (both handlers unambiguously open a path for
    # write), memo.draft/memo.compose are corrected to MUTATING here.
    # DR-208 five-question affirmation (memo.draft/memo.compose, citing
    # ops/fleet/memo_draft.py and ops/fleet/memo_compose.py):
    #   1. Writes/deletes/reorders any state file, queue, or git object?     YES.
    #      Each writes one file under the CALLING repo's OWN cross-repo/outbox/
    #      tree (memo.draft: create, O_EXCL, no clobber; memo.compose: edit an
    #      existing draft in place via os.replace). Neither ever opens a path
    #      under a receiver repo's cross-repo/inbox/ — that boundary is memo.send's
    #      alone.
    #   2. Writes into rag's relational store?                              No.
    #      Writes only coordinator outbox substrate; no rag store write.
    #      Dual-write ban (DR-208 / tri-plane DD#1) satisfied.
    #   3. Opens any file for write (including sentinel creation)?          YES.
    #      memo.draft: os.open(..., O_CREAT | O_EXCL | O_WRONLY) create.
    #      memo.compose: os.replace(tmp_path, target_path) atomic in-place edit.
    #   4. Mutates shared mutable state outside its own module?             YES.
    #      state/memo-outbox/*.md is coordinator substrate shared across the
    #      SAME repo's own subsequent memo.compose/memo.send calls.
    #   5. Persistent state changes observable across process boundaries?   YES.
    #      The written draft file is read by the same repo's subsequent
    #      memo.list_outbox/memo.compose/memo.send invocations.
    # Narrower-privilege carve-out NOTE (own-tree-confined, mirrors queue.append/
    # backlog.record's confinement documentation): the write is bounded to the
    # CALLING repo's own cross-repo/outbox/ tree — never a receiver's inbox, and
    # never rag's relational store. This confinement is why memo.draft/memo.compose
    # do not carry the DR-214-send-class cross-tree D2 seven-bound (that applies
    # only to memo.send, which crosses into another repo's tree) — but confinement
    # narrows privilege, it does NOT downgrade the class. Both remain MUTATING:
    # they write, and a read-only-token caller must not be able to invoke them.
    # Negative-spec: this is NOT a reversal of memo.send's MUTATING call — memo.send
    # remains the only op in this trio that crosses into ANOTHER repo's tree; that
    # crossing (and only that crossing) carries the DR-214-send-class D2 seven-bound.
    # memo.draft/memo.compose are MUTATING for the ordinary reason (they write a
    # file), not for the cross-tree reason.
    # Spec: docs/plans/2026-07-21-memo-tool-rebuild-full-ownership.md § C7
    "memo.draft": OpClass.MUTATING,
    "memo.compose": OpClass.MUTATING,
    # DR-208 five-question affirmation (memo.reconcile_outbox, citing
    # ops/fleet/memo_reconcile_outbox.py):
    #   1. Writes/deletes/reorders any state file, queue, or git object?     YES.
    #      Every already-delivered entry (frontmatter `status:` not "draft") is
    #      MOVED out of the CALLING repo's own state/memo-outbox/ into that same
    #      tree's sent/ subdirectory. Never a delete, never a clobber (an existing
    #      sent/<name> is skipped), never a receiver repo's inbox — the same
    #      own-tree confinement memo.draft/memo.compose carry above.
    #   2. Writes into rag's relational store?                              No.
    #      Moves coordinator outbox substrate only; no rag store write.
    #      Dual-write ban (DR-208 / tri-plane DD#1) satisfied.
    #   3. Opens any file for write (including sentinel creation)?          YES.
    #      os.replace(path, sent_dir / path.name), plus the sent/ mkdir that
    #      precedes the first move.
    #   4. Mutates shared mutable state outside its own module?             YES.
    #      state/memo-outbox/ is the queue the workday-start surface, the pickup
    #      skill, and a handoff's undelivered-drafts line all read as work depth —
    #      the very readers whose over-count this op exists to correct.
    #   5. Persistent state changes observable across process boundaries?   YES.
    #      The moved paths are what the same repo's subsequent memo.list_outbox
    #      invocations enumerate, and the op returns them for the CALLER to commit
    #      (it commits nothing itself — module docstring, Negative-spec).
    # Classified MUTATING for the ordinary reason memo.draft/memo.compose are — it
    # writes — not for a cross-tree reason: this op never leaves the calling repo,
    # so the DR-214-send-class D2 seven-bound (memo.send's alone) does not apply.
    # Spec: state/bug-backlog/2026-08-25-the-memo-outbox-does-not-clean-itself-up-after-a-send.yaml
    "memo.reconcile_outbox": OpClass.MUTATING,
    # DR-208 five-question affirmation (memo.list_outbox, citing
    # ops/fleet/memo_list_outbox.py) — COMPUTE_ONLY, structural sibling to
    # memo.list (distinct data source: CALLING repo's own state/memo-outbox/
    # tree, not the machine-local receiver registry):
    #   1. Writes/deletes/reorders any state file, queue, or git object?      No.
    #      No open(..., "w")/os.replace/git-write call anywhere in the handler.
    #   2. Writes into rag's relational store?                               No.
    #   3. Opens any file for write (including sentinel creation)?           No.
    #      Only read_text() on existing outbox drafts.
    #   4. Mutates shared mutable state outside its own module?              No.
    #   5. Persistent state changes observable across process boundaries?   No.
    #      Returns a build_dry_run_result/build_setup_error_result envelope only.
    # Spec: docs/plans/2026-07-21-memo-tool-rebuild-full-ownership.md
    "memo.list_outbox": OpClass.COMPUTE_ONLY,
    # memo.check_addressee (check-addressee-verb spinoff, ratifying
    # state/handoffs/2026-07-21_184526_claude_klabauter-check-addressee-verb.md) —
    # COMPUTE_ONLY: pure read that resolves a path-based MATCH/MISMATCH/UNRESOLVED
    # addressee verdict via the shared _memo_resolver; never writes, commits, or
    # reaches the network (repo_root is read-only used to derive self_root via
    # main_worktree_root — see memo_check_addressee.py handler docstring).
    # Review: code-reviewer (Finding 4) — moved out of the memo.list/draft/compose
    # trio's shared comment block (whose header says "all COMPUTE_ONLY: none of
    # the three...") so that header's "three" framing stays accurate; this is a
    # separate op, not a fourth member of that trio.
    "memo.check_addressee": OpClass.COMPUTE_ONLY,
    # ---------------------------------------------------------------------------
    # strang-10 A+B residual writer strangle — changelog / completion / review-trail write ops.
    # All MUTATING (each writes reserved coordinator substrate). Sanctioned by the DR-216
    # substrate-write carve-out (state/week-changelog/, state/review-trail/, docs/plans/*.md
    # completion sections). Spec: docs/plans/2026-07-06-strang-10-residual-writer-strangle-command-type.md
    # ---------------------------------------------------------------------------
    # changelog.append_day — MUTATING: additive-create write (or in-place append) of
    # state/week-changelog/{date}-{machine}.md. Atomic mkstemp+os.replace per DR-216 D3.
    # DR-208 five-question affirmation (citing ops/changelog_ops.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      changelog_ops.py:append_day — atomic _atomic_write (mkstemp+os.replace) creates
    #      or replaces state/week-changelog/{date}.md in the caller's worktree.
    #      Review: code-reviewer (Finding 3) — corrected stale {date}-{machine}.md filename
    #      shape to match the per-day filename collapse (PM ruling 2026-07-19).
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only coordinator state/week-changelog/ markdown. Dual-write ban satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      _atomic_write opens a tempfile; os.replace atomically renames it to the target.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      state/week-changelog/ files are coordinator substrate read by the shell layer,
    #      git history, and the review pipeline.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The written markdown file is read by subsequent sessions and git log.
    # DR-216 D2 five-bound affirmed (changelog additive-create sub-category):
    #   D2(i) (per-record idempotent): same date+machine → os.replace → one file.
    #   D2(ii) (git-reversible): additive content; git revert recovers the entry.
    #   D2(iii) (content-additive): append-to-existing path is now atomic read-modify-write.
    #   D2(iv) (confined noun): writes only state/week-changelog/ (DR-216-ratified noun).
    #   D2(v) (no git commit): handler writes file only; EM retains commit responsibility.
    # Authority: docs/decisions/DR-216-changelog-completion-reviewtrail-write-carveout.md § D2
    #            docs/decisions/DR-208-invoke-op-authz-model.md § 5
    "changelog.append_day": OpClass.MUTATING,
    # changelog.backfill_gaps — MUTATING: in-place mutation (additive backfill entries) of
    # state/week-changelog/ for gap dates in the current week. Atomic write per DR-216 D3.
    # DR-208 five-question affirmation (citing ops/changelog_ops.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      changelog_ops.py:backfill_gaps — _atomic_write creates
    #      state/week-changelog/{date}-{host}-backfill.md per gap date.
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only coordinator state/week-changelog/ markdown. Dual-write ban satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      _atomic_write opens a tempfile per gap date; os.replace renames each.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      state/week-changelog/ backfill files are coordinator substrate shared across sessions.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      Backfill files are read by subsequent sessions, the changelog render, and git log.
    # DR-216 D2 five-bound affirmed (changelog in-place-mutation sub-category):
    #   D2(i) (per-record idempotent): same date → _has_daily_file guard → skip if present.
    #   D2(ii) (git-reversible): new files; git rm recovers individually.
    #   D2(iii) (content-additive): creates new per-gap files; does not rewrite existing ones.
    #   D2(iv) (confined noun): writes only state/week-changelog/ (DR-216-ratified noun).
    #   D2(v) (no git commit): handler writes files only; EM retains commit responsibility.
    # Authority: docs/decisions/DR-216-changelog-completion-reviewtrail-write-carveout.md § D2
    #            docs/decisions/DR-208-invoke-op-authz-model.md § 5
    "changelog.backfill_gaps": OpClass.MUTATING,
    # changelog.inject_anchor — MUTATING: in-place content-additive append of a
    # covered_tip_sha/covered_machine anchor block into an existing
    # archive/daily-summaries/<date>-<machine>.md summary.
    # DR-208 five-question affirmation (citing ops/changelog_ops.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      changelog_ops.py:_inject_anchor_handler — _atomic_write rewrites
    #      archive/daily-summaries/<date>-<machine>.md with the anchor block appended.
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only the coordinator archive/ markdown. Dual-write ban satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      _atomic_write opens a tempfile in the target dir; os.replace renames it.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      archive/daily-summaries/ is coordinator substrate shared across sessions.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The injected anchor is read by the workday-complete backfill scan (greps
    #      '^covered_tip_sha:') and by git log.
    # DR-216 D2 six-bound affirmed (archive/daily-summaries/ in-place sub-category,
    # admitted by the 2026-07-28 D2(iv) amendment):
    #   D2(i) (per-record idempotent): an equal anchor short-circuits to
    #      already_anchored; a bump is convergent and reaches a fixed point (once
    #      advanced to the tip, the next call sees equality and no-ops).
    #   D2(ii) (git-reversible): additive or a two-line in-place substitution; a
    #      scoped git checkout -- <path> undoes either.
    #   D2(iii) (content-additive) + D2(iii-b) (coverage-anchor bump, the named
    #      rewrite exception): injection appends the anchor block only. The bump path
    #      rewrites the covered_tip_sha:/covered_machine: values in place, permitted
    #      by D2(iii-b) ONLY when the recorded anchor is a strict ancestor of the
    #      target tip or is unresolvable — equal, descendant, and divergent anchors
    #      are left byte-identical, so the op never bumps backwards or across a fork.
    #      The human-authored summary body and prose note are never rewritten.
    #   D2(iv) (confined noun): writes only archive/daily-summaries/<date>-<machine>.md,
    #      the second archive/ sub-noun D2(iv) permits, and only for this op.
    #   D2(v) (no git commit): handler writes the file only; caller retains the commit.
    #   D2(vi) (single-writer-per-file): not locked; safe under DR-215 command-type,
    #      serial-by-construction execution.
    # Authority: docs/decisions/DR-216-changelog-completion-reviewtrail-write-carveout.md
    #            § D2(iv) amendment (2026-07-28) + its partial-suspension note
    #            docs/decisions/DR-208-invoke-op-authz-model.md § 5
    "changelog.inject_anchor": OpClass.MUTATING,
    # changelog.compute_day_fields — COMPUTE_ONLY: read-only sibling of changelog.append_day
    # (Zone-A build #4). Reads git log history + state/handoffs/*.md + state/review-trail/
    # + archive/review-trail/ + state/week-changelog/HEADER.md under the caller's worktree
    # and returns a derived field bundle; never opens any path for write.
    # DR-208 five-question affirmation (citing ops/changelog_ops.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  No.
    #      compute_day_fields() only invokes read-only git subprocesses (log) and Path.read_text/
    #      glob calls; no _atomic_write, no os.replace, no subprocess git write command.
    #   2. Writes into rag's relational store?                                 No.
    #   3. Opens any file for write (including sentinel creation)?             No.
    #   4. Mutates shared mutable state outside its own module?                No.
    #   5. Persistent state changes observable across process boundaries?     No — returns an
    #      in-memory dict to the caller; the caller (changelog.append_day / the step9 facade)
    #      owns any subsequent write.
    # Authority: docs/decisions/DR-216-changelog-completion-reviewtrail-write-carveout.md § D2
    #            docs/decisions/DR-208-invoke-op-authz-model.md § 5
    "changelog.compute_day_fields": OpClass.COMPUTE_ONLY,
    # changelog.upsert_reviewed — MUTATING: surgical single-field upsert of the
    # **Reviewed:** line(s) inside state/week-changelog/{date}.md's `## {date} — {machine}`
    # section. Curation-preserving counterpart to changelog.append_day (which recomposes
    # the whole section and would clobber human-curated Scope:/Commits:).
    # DR-208 five-question affirmation (citing ops/changelog_ops.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      changelog_ops.py:upsert_reviewed — atomic _atomic_write (mkstemp+os.replace)
    #      rewrites state/week-changelog/{date}.md in place.
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only coordinator state/week-changelog/ markdown. Dual-write ban satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      _atomic_write opens a tempfile; os.replace atomically renames it to the target.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      state/week-changelog/ files are coordinator substrate read by the shell layer,
    #      git history, and the review pipeline.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The written markdown file is read by subsequent sessions and git log.
    # DR-216 D2 five-bound affirmed (same reserved-noun carve-out as changelog.append_day):
    #   D2(i) (per-record idempotent): unchanged derivation -> "unchanged", no rewrite.
    #   D2(ii) (git-reversible): in-place edit; git revert recovers the prior line.
    #   D2(iii) (content-additive): touches only the Reviewed: line-block; every other
    #      line of the section (and every other section) is left byte-identical.
    #   D2(iv) (confined noun): writes only state/week-changelog/ (DR-216-ratified noun).
    #   D2(v) (no git commit): handler writes file only; EM retains commit responsibility.
    # Authority: docs/decisions/DR-216-changelog-completion-reviewtrail-write-carveout.md § D2
    #            docs/decisions/DR-208-invoke-op-authz-model.md § 5
    # Spec: cross-repo/inbox/2026-07-21-claude-central-em-reviewed-line-surgical-upsert.md
    "changelog.upsert_reviewed": OpClass.MUTATING,
    # completion.flip_to_released — MUTATING: in-place flip of release fields on a
    # caller-supplied archive/completed/**/*.md entry. Locked read-modify-write
    # (coordinator_core.locked_write.locked_rmw) + atomic os.replace, locking per DR-216's
    # 2026-08-06 D2(vi) amendment — this op has the widest read-to-write gap of the module's
    # three writers (four git subprocesses), so the locking requirement binds it hardest.
    # Admitted into DR-216 D1 by the 2026-08-06 amendment: it had been writing under this
    # carve-out's bounds without ever appearing in D1's enumeration, so it previously held no
    # admitting record. Its absence here was the matching gap — classify() fails closed, so the
    # op was registered and dispatchable but DENIED at every classification checkpoint.
    # AMENDED by EM under the execution authorization of
    # docs/plans/2026-08-06-writer-side-commit-ownership-lock-gap.md — PM counter-signature
    # pending.
    # DR-208 five-question affirmation (citing ops/completion_ops.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      completion_ops.py:_flip_one_entry — locked read-modify-write sets released_* fields
    #      on the caller-supplied completion entry.
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only the named archive/completed/**/*.md entry. Dual-write ban satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      tempfile.mkstemp + os.replace atomically renames onto the entry path.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      archive/completed/ entries are coordinator substrate read by the EM and the
    #      review/release pipeline.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The flipped entry is read by subsequent sessions and by release-notes derivation.
    # DR-216 D2 five-bound affirmed (completion in-place-mutation sub-category):
    #   D2(i) (per-record idempotent): "already released (idempotent no-op)"; an entry already
    #         released with DIFFERENT fields is refused rather than re-flipped, so a second
    #         invocation never clobbers a divergent prior release.
    #   D2(ii) (git-reversible): additive field flip; git checkout -- <entry> recovers.
    #   D2(iii) (content-additive): _apply_flip_fields sets release fields; no reorder or delete.
    #   D2(iv) (confined noun): archive/completed/**, already within D2(iv)'s extension for
    #          completion.reconcile_commits — this admits no new noun.
    #   D2(v) (no git commit): handler writes the entry only; commit responsibility stays with
    #         the caller, per completion_ops.py's own module docstring.
    # Authority: docs/decisions/DR-216-changelog-completion-reviewtrail-write-carveout.md
    #            § D1 (2026-08-06 amendment) and § D2
    #            docs/decisions/DR-208-invoke-op-authz-model.md § 5
    "completion.flip_to_released": OpClass.MUTATING,
    # plan.append_session — MUTATING: in-place append of a session-tracking entry to the
    # agent_sessions: YAML list of a caller-supplied docs/plans/<plan>.md. Locked
    # read-modify-write (coordinator_core.locked_write.locked_rmw) + atomic os.replace,
    # locking per DR-216's 2026-08-06 amendment. Handler enforces noun confinement at
    # runtime (DR-216 D2(iv)).
    # DR-208 five-question affirmation (citing ops/completion_ops.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      completion_ops.py:append_plan_session — locked read-modify-write writes the
    #      caller-supplied docs/plans/<plan>.md with the new session entry appended.
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only docs/plans/<plan>.md (caller-supplied path). Dual-write ban satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      Uses atomic temp+os.replace; opens a tempfile for the modified plan content.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      docs/plans/*.md agent_sessions entries are coordinator substrate shared across
    #      sessions and read by the EM, git history, and the review pipeline.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The modified plan file is read by subsequent sessions and the facade.
    # DR-216 D2 five-bound affirmed (plan in-place-mutation sub-category):
    #   D2(i) (per-record idempotent): same session_id → no-op.
    #   D2(ii) (git-reversible): additive session line; git checkout -- <plan> recovers.
    #   D2(iii) (content-additive): appends only; never rewrites or reorders existing entries.
    #   D2(iv) (confined noun): handler enforces plan_path in docs/plans/ at runtime.
    #   D2(v) (no git commit): handler writes plan file only; EM retains commit responsibility.
    # Authority: docs/decisions/DR-216-changelog-completion-reviewtrail-write-carveout.md § D2
    #            docs/decisions/DR-208-invoke-op-authz-model.md § 5
    "plan.append_session": OpClass.MUTATING,
    # review.freeze_diff — MUTATING: writes two files per invocation —
    # state/review-trail/diffs/<slice_id>.diff and <slice_id>.head.sha — under the
    # caller's worktree. DR-208 five-question affirmation (citing
    # ops/review_freeze_diff.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      freeze_diff() writes state/review-trail/diffs/{slice_id}.{diff,head.sha}.
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only state/review-trail/diffs/ files. Dual-write ban satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      Path.write_text on both output files.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      state/review-trail/diffs/ entries are read by review-dispatch gates and
    #      their synthesizers (parity with review_trail.write's own trail entries).
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      Frozen diff/sha files are read by a later-dispatched reviewer/synthesizer
    #      process, not just the writer.
    # Same-slice_id re-freeze overwrites the prior pair (last-write-wins, matching
    # review_trail.write's DR-216 D2(i) posture) — never rotated/deleted here.
    # Authority: docs/decisions/DR-208-invoke-op-authz-model.md § 5
    "review.freeze_diff": OpClass.MUTATING,
    # ---------------------------------------------------------------------------
    # Backfill: 3 ops registered by concurrent sessions (records.query / ceremony.*) without a
    # classification entry, leaving the drift-guard gate RED on HEAD.
    # ---------------------------------------------------------------------------
    # records.query — COMPUTE_ONLY: read-only dict-parse query over the records in-memory
    # store (strang-11 C1a). No file writes, no rag store write, no subprocess. COMPUTE_ONLY
    # is the correct class — it is also the LESS permissive class for this op (fail-closed
    # default is MUTATING; choosing COMPUTE_ONLY requires explicit justification below).
    # DR-208 five-question affirmation (citing records.query handler, strang-11 C1a):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  No.
    #      records.query performs a read-only dict-parse over the in-memory records store;
    #      no file is opened for write, no git object is created, no queue is mutated.
    #   2. Writes into rag's relational store?                                 No.
    #      Returns structured query results to the caller; no rag store interaction.
    #   3. Opens any file for write (including sentinel creation)?             No.
    #      Read-only: reads in-memory data only; no tempfile, no sentinel, no open-for-write.
    #   4. Mutates shared mutable state outside its own module?                No.
    #      The in-memory records dict is read-only from this op's perspective.
    #   5. Persistent state changes observable across process boundaries?     No.
    #      A read-only query leaves no trace; output is the return dict, not any disk write.
    # COMPUTE_ONLY justification: all five questions answered No — the op is a pure read. The
    #   fail-closed default (MUTATING) would be safe but incorrect; COMPUTE_ONLY is warranted
    #   by the confirmed zero-write profile. DR-208 § Fail-closed does not forbid COMPUTE_ONLY
    #   when the affirmation explicitly justifies it. Spec: strang-11 C1a.
    # Authority: docs/decisions/DR-208-invoke-op-authz-model.md § 5
    "records.query": OpClass.COMPUTE_ONLY,
    # records.history — COMPUTE_ONLY: derives per-file lifecycle events (creation, rename
    # chains, frontmatter field transitions) for a record type from a single `git log -p -U0`
    # pass over that type's directory pathspec (record_history.py :: derive_type_history) and
    # returns the parsed result verbatim. This op reads git history and writes nothing,
    # anywhere.
    # DR-208 five-question affirmation:
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  No.
    #      `git log -p -U0` is a read-only history walk; no git object is created, no
    #      ref is moved, no working-tree file is touched.
    #   2. Writes into rag's relational store?                                 No.
    #   3. Opens any file for write (including sentinel creation)?             No.
    #      Read-only: reads the collected file set off disk and parses git's patch-text
    #      output; no tempfile, no sentinel, no open-for-write.
    #   4. Mutates shared mutable state outside its own module?                No.
    #   5. Persistent state changes observable across process boundaries?     No.
    #      Returns a list of {"path", "created_at", "created_by", "events"} dicts only.
    # Spec: docs/plans/2026-08-20-a-time-axis-for-any-record-type.md, chunk C2.
    "records.history": OpClass.COMPUTE_ONLY,
    # handoff.columns — the four-column projection over live + (opt-in) archived handoff
    # records, serving a cross-repo fleet-board consumer that reads on a page request rather
    # than on our ceremony close (DR-287 § Open direction — push vs. pull).
    #   1. Writes, deletes, or reorders coordinator substrate?               No.
    #      Reads frontmatter via records_query's own collectors; writes nothing.
    #   2. Writes outside its own module's return value?                     No.
    #      Output is the returned {"records": [...]} dict; no path is opened for writing.
    #   3. Shells out to a mutating command?                                 No.
    #      One `git log` for shipped_in SHA→date resolution — a read, and batched to exactly
    #      one spawn per query rather than one per record (the per-record loop would have been
    #      a read-path repeat of the emit cost DR-287 halted the cadence over).
    #   4. Mutates shared mutable state outside its own module?              No.
    #   5. Persistent state changes observable across process boundaries?    No.
    # COMPUTE_ONLY justification: all five No — a pure read, same profile as records.query
    #   whose collection path it reuses unmodified. The fail-closed MUTATING default would be
    #   safe but wrong here, and this affirmation is what DR-208 § Fail-closed requires in
    #   order to depart from it.
    # Authority: docs/decisions/DR-208-invoke-op-authz-model.md § 5
    # Spec: docs/plans/2026-08-11-pull-surface-four-columns-and-the-archive.md § C3
    "handoff.columns": OpClass.COMPUTE_ONLY,
    # ceremony.commit_v2 — MUTATING: a thin envelope over `commit.commit_paths`
    # (coordinator_core/git/commit.py), whose entire purpose is writing git
    # objects and moving the branch ref (ops/ceremony/commit_v2.py docstring).
    # DR-208 five-question affirmation (citing ceremony.commit_v2 handler):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      `commit_paths` writes tree/commit objects and moves the branch ref.
    #   2. Writes into rag's relational store?                                 No.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      Splices the git index (`index_write.splice_index`) before committing.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      The landed commit and moved ref are read by every subsequent
    #      dispatch against this repo.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The commit sha and ref move persist across the whole box.
    # Authority: docs/decisions/DR-208-invoke-op-authz-model.md § 5
    # Spec: docs/plans/2026-08-27-something-must-commit-ceremony-commit-v2.md § C3
    "ceremony.commit_v2": OpClass.MUTATING,
    # push.outstanding — MUTATING: it pushes refs to a remote. The decision half
    # is a zero-spawn read, but the act half is an outward-facing publish.
    "push.outstanding": OpClass.MUTATING,
    # ceremony.session_instructions was KILLED 2026-08-27 under DR-344's kill bar,
    # measured at 31470ms against 500ms. Do not re-add a classification entry for it;
    # a name in a string-keyed table outlives the op it names, which is the failure
    # `ops/tests/test_registration_annotations_resolve.py` exists to catch.
    # → state/audits/2026-08-27-session-instructions-has-never-served-a-real-request.md
    # ceremony.render_handoff_tracker was retired 2026-08-14 along with the
    # handoff-tracker render path -- see docs/plans/2026-08-14-retire-the-
    # handoff-tracker-and-project-tracker-renders.md § C2. Its OP_CLASSIFICATION
    # entry is removed here too (test_no_stale_classification_entries).
    # ---------------------------------------------------------------------------
    # strang-11 B8 new ops — session.boot_sweep, fleet.archive_actioned_memos,
    # session.reap. All MUTATING.
    # fleet.archive_shipped_handoffs (Class-A, DR-211 archival-writer sub-category)
    # was the third strang-11 B8 op landed alongside these two; its module
    # (ops/fleet/archive_shipped_handoffs.py) and this entry were DELETED
    # 2026-08-25 (C1b, docs/plans/2026-08-25-the-handoff-auto-archive-comes-back-
    # capped.md § "The sibling op is subsumed") — the op it was subsumed into,
    # fleet.archive_completed_handoffs, has carried the SHA-gate, live-claim
    # gate, and handoff_claim_dir reuse this op required since C1a.
    # Class-B ops (session.reap, session.boot_sweep) sanctioned by the strang-11-B8 non-git safety spec
    # (per-record idempotency, recency liveness, fail-closed-to-keep; no git commit).
    # Spec: docs/plans/2026-07-06-strang-11-b8-session-init-op-absorption.md § C5 / AC5
    # ---------------------------------------------------------------------------
    # fleet.archive_terminal_sizings — MUTATING: git-mv terminal sizing objects into
    # archive/sizings/YYYY-MM/, via the shared archive_and_commit batch path
    # (ops/fleet/archive_sizings.py, dest-collision handling + git-mv + commit).
    # Authority: docs/decisions/DR-208-invoke-op-authz-model.md § 5
    "fleet.archive_terminal_sizings": OpClass.MUTATING,
    # fleet.backfill_dispositionless_memos (C5,
    # docs/plans/2026-07-26-memo-disposition-flip-op-and-hand-edit-hole.md) — MUTATING:
    # writes realized_by/actioned_note frontmatter fields onto memos already resident
    # in cross-repo/archive/. NOT a DR-211 archival-writer op — it performs no git mv
    # and no git commit, so DR-211's five bounds do not apply; its classification
    # mirrors "memo.transition" above (a plain frontmatter write via locked_rmw), not
    # the DR-211 trio.
    # DR-208 five-question affirmation:
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      backfill_memo_disposition.py:_apply_one writes memo frontmatter on disk
    #      via coordinator_core frontmatter primitives inside a locked_rmw closure.
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only the target memo file under cross-repo/archive/; no rag write.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      Each applied backfill entry opens its memo file for write.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      Memo files under cross-repo/archive/ are coordinator substrate shared
    #      across repos (the sibling EM that sent the memo reads the disposition).
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      Written realized_by/actioned_note fields are read by cockpit's
    #      cross_repo_memos projection and any git client after return.
    # Spec backlink: pln-give-the-memo-disposition-flip-e580c2 § C5
    "fleet.backfill_dispositionless_memos": OpClass.MUTATING,
    # session.reap — MUTATING (Class B): cadence-gated (12h .last-reap mtime file marker) reaper
    # for stale sessions, stale agent dirs, and orphaned claim dirs inside
    # .git/coordinator-sessions/. No git commit (untracked substrate). Per-record idempotent;
    # recency liveness via resolve_live_session_ids / cs_claim_holder_live (never raw pid);
    # TOCTOU re-read before rm; fail-closed-to-keep on any liveness ambiguity (AC3).
    # DR-208 five-question affirmation (citing ops/session/reap.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      Moves stale session/agent dirs to .git/coordinator-sessions/.archive/; rm -rf
    #      orphaned claim dirs after TOCTOU re-read. No git commit (untracked substrate).
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only .git/coordinator-sessions/ (session-runtime layer, outside rag's store).
    #      Dual-write ban (DR-208 / tri-plane DD#1) satisfied (session-runtime is not rag's noun).
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      Creates/updates the .last-reap cadence gate file; moves/removes dirs.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      .git/coordinator-sessions/ dirs are shared across all coordinator sessions.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      Reaped dirs / removed claims are observable by subsequent session-init runs.
    # Class-B non-git safety spec (NOT DR-211):
    #   Per-record idempotent (archive-dest-exists → skip); recency-based liveness (never raw
    #   pid); TOCTOU re-read before rm; fail-closed-to-keep on liveness error or ambiguity;
    #   12h .last-reap cadence gate (mtime-based file marker, NOT flock — session-state-contract.md § Neg-Spec).
    "session.reap": OpClass.MUTATING,
    "session.reap_claims_for_repos": OpClass.MUTATING,
    # session.guard_settings_integrity — MUTATING: restores a corrupted/missing settings
    # file from backup via atomic same-dir temp+os.replace (_atomic_copy). Was registered
    # (ops/__init__ + _registry_map) without a classification, leaving the authz fail-closed
    # drift-guard (test_all_registered_ops_are_classified) RED on HEAD — pre-existing gap,
    # unrelated to the engine-migration ports; classified here (writes settings = MUTATING,
    # also the fail-closed default) to green the gate.
    "session.guard_settings_integrity": OpClass.MUTATING,
    # session.guard_hooks_kill_switch_detail — COMPUTE_ONLY: on-demand, full-detail
    # kill-switch text (verbose=True) for the routine boot router's
    # `_KS_DETAIL_COMMAND` door. Sibling of session.guard_settings_integrity in
    # the same module but a DISTINCT handler — do not conflate the two; this
    # one is read-only (guard_settings_integrity.py::_handler_kill_switch_detail
    # calls evaluate_hooks_kill_switch_full_detail, itself only
    # _read_kill_switch_marker + format_kill_switch_full_detail — no write call
    # anywhere in that path).
    # DR-208 five-question affirmation:
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  No.
    #      Only reads the kill-switch marker file; never writes to it or to
    #      settings.json (handler docstring: "Read-only; never writes to the
    #      marker or to settings.json").
    #   2. Writes into rag's relational store?                                 No.
    #   3. Opens any file for write (including sentinel creation)?             No.
    #   4. Mutates shared mutable state outside its own module?                No.
    #   5. Persistent state changes observable across process boundaries?     No.
    #      Returns {"text": str} only.
    "session.guard_hooks_kill_switch_detail": OpClass.COMPUTE_ONLY,
    # session.resolve_address — COMPUTE_ONLY: resolves a session UUID to its live
    # SendMessage address by parsing coordinator_core.session.harness_registry.
    # snapshot() (a pure directory scan over sessions/*.json). Pre-existing gap:
    # this op landed (077440e23) without a classification entry; fixed here
    # (break-class, not deferred) rather than added to the frozen
    # _KNOWN_UNCLASSIFIED_OPS_DEBT baseline.
    # DR-208 five-question affirmation:
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  No.
    #      reachability.resolve_address / harness_registry.snapshot() are pure
    #      parsers — no write call anywhere in either module.
    #   2. Writes into rag's relational store?                                 No.
    #   3. Opens any file for write (including sentinel creation)?             No.
    #   4. Mutates shared mutable state outside its own module?                No.
    #   5. Persistent state changes observable across process boundaries?     No.
    #      Returns {"outcome", "session_id", "address", "candidates"} only.
    "session.resolve_address": OpClass.COMPUTE_ONLY,
    # session.peer_roster — COMPUTE_ONLY: cwd-filtered live peer roster, built
    # over the same harness_registry.snapshot() as session.resolve_address just
    # above, plus reachability.resolve_candidates() (both pure parsers/readers).
    # DR-208 five-question affirmation:
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  No.
    #      peer_roster.build_roster() only reads harness_registry.snapshot()
    #      and reachability.resolve_candidates(); persists nothing (spec
    #      Anti-scope: "no durable roster file, cache, or published registry").
    #   2. Writes into rag's relational store?                                 No.
    #   3. Opens any file for write (including sentinel creation)?             No.
    #   4. Mutates shared mutable state outside its own module?                No.
    #   5. Persistent state changes observable across process boundaries?     No.
    #      Returns {"rows": [...]} only.
    "session.peer_roster": OpClass.COMPUTE_ONLY,
    # session.work_state — COMPUTE_ONLY: reads state/handoffs/*.md and the
    # claim-state ledger via build_work_state() (coordinator_core.session.
    # work_state), which itself only reads disk (frontmatter, claim ledger,
    # live-session verdicts) and writes nothing anywhere on any path.
    # DR-208 five-question affirmation:
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  No.
    #      build_work_state() and its helpers (_scan_handoff_dir,
    #      _resolve_ledger_first_holder, derive_readiness_batch) are all pure
    #      readers; no write call anywhere in the chain.
    #   2. Writes into rag's relational store?                                 No.
    #   3. Opens any file for write (including sentinel creation)?             No.
    #   4. Mutates shared mutable state outside its own module?                No.
    #   5. Persistent state changes observable across process boundaries?     No.
    #      Returns {"held": [...], "unclaimed": [...], "review_due": [...]} only.
    # Spec: docs/plans/2026-08-19-fleet-work-state-who-holds-which-baton.md, chunk C3.
    "session.work_state": OpClass.COMPUTE_ONLY,
    # fleet.work_state — COMPUTE_ONLY: fans build_work_state() out across
    # every registered active sibling (read via _memo_resolver.
    # read_registry_repos(), itself read-only) and returns the aggregated
    # answer verbatim. Unlike fleet.aggregate_capability_index (MUTATING —
    # persists state/capabilities/fleet-index.json), this op persists
    # nothing.
    # DR-208 five-question affirmation:
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  No.
    #      Every per-repo call is build_work_state() (pure reader, see
    #      session.work_state's own affirmation above); the walk-only git-repo
    #      pre-check and the harness_registry.snapshot() hoist are both reads.
    #   2. Writes into rag's relational store?                                 No.
    #   3. Opens any file for write (including sentinel creation)?             No.
    #   4. Mutates shared mutable state outside its own module?                No.
    #   5. Persistent state changes observable across process boundaries?     No.
    #      Returns {"repos": {...}, "errors": [...]} only.
    # Spec: docs/plans/2026-08-19-fleet-work-state-who-holds-which-baton.md, chunk C5.
    "fleet.work_state": OpClass.COMPUTE_ONLY,
    # fleet.record_history — COMPUTE_ONLY: fans records.history's own
    # derive_type_history() out across every registered active sibling (read
    # via _memo_resolver.read_registry_repos(), itself read-only) and returns
    # the aggregated answer verbatim, the same shape as fleet.work_state above.
    # DR-208 five-question affirmation:
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  No.
    #      Every per-repo call is derive_type_history() (pure `git log -p -U0`
    #      reader, see records.history's own affirmation above).
    #   2. Writes into rag's relational store?                                 No.
    #   3. Opens any file for write (including sentinel creation)?             No.
    #   4. Mutates shared mutable state outside its own module?                No.
    #   5. Persistent state changes observable across process boundaries?     No.
    #      Returns {"repos": {...}, "errors": [...]} only.
    # Spec: state/dispatch-briefs/2026-08-20-a-counted-fleet-answer-for-record-history/C2.md.
    "fleet.record_history": OpClass.COMPUTE_ONLY,
    # session.artifact_owner — COMPUTE_ONLY: opens the caller-supplied
    # artifact_path for READ ONLY, extracts owner id(s) via frontmatter
    # primitives, and resolves each through reachability.resolve_address()
    # (already COMPUTE_ONLY above) — every call in the chain is a pure
    # reader.
    # DR-208 five-question affirmation:
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  No.
    #      artifact_owner.resolve_artifact_owner() opens the artifact with
    #      mode "r" only; no write call anywhere in the module.
    #   2. Writes into rag's relational store?                                 No.
    #   3. Opens any file for write (including sentinel creation)?             No.
    #   4. Mutates shared mutable state outside its own module?                No.
    #   5. Persistent state changes observable across process boundaries?     No.
    #      Returns {"artifact_path", "owners": [...], "file_error"} only.
    "session.artifact_owner": OpClass.COMPUTE_ONLY,
    # session.self_probe_hook_generation — NO ENTRY HERE, BY RESOLUTION, and this
    # comment is the record so the next reader does not re-derive it or re-add one.
    # ops/session/guard_hook_generation_self_probe.py carried @register_op with an
    # otherwise-empty registration quad — no _registry_map.py, ops/__init__.py, or
    # op_scopes.py row — so the op was never dispatchable. Classifying it was tried on
    # 2026-07-29 and correctly went red: test_no_stale_classification_entries forbids
    # an authz surface for an op that cannot be dispatched. The decorator was dropped
    # as vestigial instead (nothing invoked the op by name; SessionStart calls
    # run_self_probe() directly), which cleared the two guards it had been holding red.
    # There is no op here to classify any more — an entry would re-break the stale-
    # entry guard. If it is ever genuinely wired up, it is MUTATING: run_self_probe
    # writes a sentinel via _atomic_write_text.
    # session.record_pickup — MUTATING (Class B): DR-059 defense-in-depth port of bash's
    # cs_session_shape_set pickup write. Writes an append-only/versioned pickup record
    # (flat `pickup` field SET + `pickup_history[]` APPENDED) to
    # .git/coordinator-sessions/<sid>/session-shape.json under the SAME mkdir-based lock
    # convention bash's cs_session_shape_set uses (NOT locked_write.locked_rmw's fcntl
    # sidecar — the two remaining bash call sites, plan-claim + actioned_memos, target the
    # same file and do not exclude against fcntl). No git commit (untracked substrate).
    # DR-208 five-question affirmation (citing ops/session/record_pickup.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      Read-modify-writes .git/coordinator-sessions/<sid>/session-shape.json (pickup
    #      + pickup_history fields only). No git commit (untracked substrate).
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only .git/coordinator-sessions/ (session-runtime layer, outside rag's store).
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      mkdir-lock metadata files (pid/session_id/claimed_at) + atomic
    #      session-shape.json replace via mktemp+os.replace.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      session-shape.json is shared with the remaining bash writers (plan-claim,
    #      actioned_memos) and branch_resolution's reader — hence the shared-lock requirement.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      branch_resolution reads pickup.happened/pickup.handoff on a later WSC resolve.
    "session.record_pickup": OpClass.MUTATING,
    # session.boot_sweep — GRAVESTONED 2026-08-27, K-059: no classification row,
    # because there is no op to classify. The rebuild this block used to describe
    # missed its own bar and its requirement was retired by measurement (198
    # terminal handoffs archived in 7 days without it). Drained here to the same
    # shape as ceremony.scoped_git_commit, the killed-op precedent: gone from
    # _REGISTRY, OP_CLASSIFICATION, _OP_KEY_SCOPE, OP_MODULE_MAP and the eager
    # list, surviving only as a SUSPENDED_OPS name.
    # session.scope_report — COMPUTE_ONLY: the op's own module docstring names it
    # a "read-only session-scope reporter" — reports THIS session's own scope, no
    # writes. Spec: coordinator_core/ops/session/scope_report.py module docstring.
    "session.scope_report": OpClass.COMPUTE_ONLY,
    # session.safe_commit_offer — MUTATING, and the plainest case in this table:
    # the op commits and pushes this session's claimed dirty paths, so it writes
    # git objects and refs outright. It also appends to the diagnostics log its
    # own `_log_*_diagnostic` sinks own. Registered 2026-08-27, replacing the
    # module's `main()` CLI door; scope "none", same target-resolution
    # convention as session.scope_report just above, which composes this
    # module's own `compute_offer`.
    "session.safe_commit_offer": OpClass.MUTATING,
    # workday.drain_pending_push — MUTATING: delegates to
    # `coordinator_core.hooks.auto_push.drain_pending_push`, which pushes and removes
    # the pending-push record on success — a real write/mutation, not a read. Kept a
    # separate op from the pure-read `workday.surface_auto_push_failure_stats` per
    # that op's own docstring contract. Spec: docs/plans/2026-08-03-check5-owner-
    # attribution-liveness.md § AC14/AC14a.
    "workday.drain_pending_push": OpClass.MUTATING,
    # percolate.run — MUTATING: dispatches the percolation engine's phase functions
    # (path/substitute/stem/depersonalize content rewrites + basename-rename + inject/preserve),
    # which write, rename, and copy files under the target tree (§ engine.py phase model,
    # ops/percolate_run.py). Ambiguous-defaults-MUTATING is moot — this op plainly mutates.
    "percolate.run": OpClass.MUTATING,
    # percolate.validate_store — COMPUTE_ONLY: reads a consumer store file (open mode 'r') and
    # the vendored schema, returns a validation/drift report; performs no write, rename, or
    # commit (ops/percolate_validate.py — the shrunken --refresh-skeleton, read-only by design).
    "percolate.validate_store": OpClass.COMPUTE_ONLY,
    # delegation.check — COMPUTE_ONLY: reads one small JSON grant file under the
    # settings home and probes one pid for liveness via psutil; performs no write,
    # rename, or commit (ops/delegation_check.py, wrapping fleet_delegation's own
    # read-only check_fleet_delegation). Mirrors plugin_health.drift's own-machine-
    # probe classification rationale.
    "delegation.check": OpClass.COMPUTE_ONLY,
    # percolate.run_identity_check — COMPUTE_ONLY: subprocesses a publish target's
    # own `check-persona-names.py` and returns its exit code + captured stdout;
    # performs no write, rename, or commit of its own (ops/percolate_identity_check.py).
    # Given a real entry here rather than joining percolate.run_ci_smoke_check in
    # `_KNOWN_UNCLASSIFIED_OPS_DEBT` (authz/registration_quad.py) — that baseline is
    # frozen and extending it is a plan amendment, never a local executor call.
    "percolate.run_identity_check": OpClass.COMPUTE_ONLY,
    # MUTATING despite its "none" scope and its COMPUTE_ONLY percolate siblings:
    # this op builds the token index ON DISK (mkdir + write_text + atomic replace
    # into the dest tree), so a COMPUTE_ONLY declaration would be false. Landed
    # 2026-08-26 (C3, payload-parity-asks-an-index-not-the-payload) with three of
    # the four registration surfaces filled; this row closes the quad.
    "percolate.build_token_index": OpClass.MUTATING,
    # engine.drift — COMPUTE_ONLY: read-only drift probe. Resolves the running engine SHA
    # (resolve_engine_sha) and runs `git merge-base --is-ancestor` against MIN_KNOWN_GOOD_SHA
    # in the engine's own checkout; no state write, no rag store write (dual-write ban).
    "engine.drift": OpClass.COMPUTE_ONLY,
    # cruft_sweep.run — MUTATING: with apply=True this op deletes coordinator substrate
    # (stale harness/scratch/orphan dirs and files via rm -rf/unlink) and appends
    # per-class rows to the cruft-sweep log — fail-closed default applies regardless,
    # but this is a genuine, not merely conservative, MUTATING classification (delete +
    # log-append are real substrate mutations, not read-only).
    "cruft_sweep.run": OpClass.MUTATING,
    # plugin_health.drift — COMPUTE_ONLY: read-only drift probe (git-state/venv-state/
    # SHA-sentinel checks against the operator's plugin.mirrors registry and live
    # installs). Writes nothing — every leg is a read (git status/fetch/rev-list/
    # rev-parse, file existence/content reads, JSON/TOML parses). Mirrors engine.drift's
    # classification rationale exactly.
    "plugin_health.drift": OpClass.COMPUTE_ONLY,
    # plugin_health.forwarder_drift — COMPUTE_ONLY: WARN-only staleness probe for
    # generated agent-helper bin/ forwarders (coordinator_core/plugin_health/
    # forwarder_drift.py). Registered but missing from this table since landing
    # (a41aaaad0) — classify() fails closed on the omission, denying a purely
    # advisory, non-raising probe.
    # DR-208 five-question affirmation (citing plugin_health/forwarder_drift.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  No.
    #      check_forwarder_drift/_diff_one_location/_derive_names/_cited_entrypoint_sites
    #      only read directory listings and file contents (settings-home bin/, the
    #      retired ~/.claude/bin compat mirror, coordinator/bin/, DoE-claude's prompt-
    #      surface trees); no `open(..., "w")`, no `write_text`, no `mkdir`. Grepped the
    #      module for any write call — none found.
    #   2. Writes into rag's relational store?                                 No.
    #      No I/O of any kind beyond directory/file reads. Dual-write ban satisfied.
    #   3. Opens any file for write (including sentinel creation)?             No.
    #      Same grep as (1); the handler (`_plugin_health_forwarder_drift`) returns
    #      an in-memory `ok/skipped/lines/stderr_lines` envelope only.
    #   4. Mutates shared mutable state outside its own module?                No.
    #      Never raises (module docstring "Contract"); any resolution failure is a
    #      clean skip. Advisory only, mirrors plugin_health.drift's own contract.
    #   5. Persistent state changes observable across process boundaries?     No.
    #      Nothing written; the reply is transient advisory text consumed by the
    #      /workday-start Addon Health step.
    "plugin_health.forwarder_drift": OpClass.COMPUTE_ONLY,
    # plugin_health.scan — COMPUTE_ONLY: read-only addon-health sentinel scanner
    # (reads <plugin>/data/doctor-last-run.json + hooks/hooks.json across the
    # plugin/consumer roots; emits advisory notice strings). Writes nothing — every
    # leg is a read (JSON parse, file/dir existence checks, ISO-8601 timestamp parse).
    # Mirrors plugin_health.drift's classification rationale.
    "plugin_health.scan": OpClass.COMPUTE_ONLY,
    # plugin_health.sentinel — MUTATING (NOT compute-only, unlike its two siblings):
    # in --full mode this op WRITES ~/.claude/plugins/coordinator-claude/data/
    # doctor-last-run.json (the cross-plugin sentinel schema
    # every other plugin's own doctor reads). Every other mode (triage/cluster/probe/
    # symptom) is read-only and prints only, but the op as a whole must classify at
    # the level of its most-privileged mode.
    "plugin_health.sentinel": OpClass.MUTATING,
    # cartography.tree — COMPUTE_ONLY: thin RPC wrapper over
    # coordinator_core.cartography.tree.build_tree (ops/cartography_tree.py).
    # DR-208 five-question affirmation:
    #   1. Does the handler open any file for write (including append)?          No.
    #      build_tree -> list_tracked_files (git ls-files, read query) and
    #      _loc_for -> Path.read_bytes() (read-only). No open(..., "w") anywhere.
    #   2. Does the handler call any git write command?                         No.
    #      Sole git invocation is `git ls-files` — a read-only query, the same
    #      precedent DR-208's own table cites for coverage.gate ("all subprocess
    #      calls are read-only git queries").
    #   3. Does the handler enqueue any state mutation (queue/backlog/etc.)?     No.
    #   4. Does the handler invoke any subprocess that may do any of the above?  No.
    #      The sole subprocess call is `git ls-files` (read-only; see #2).
    #   5. Is the handler's I/O behavior conditional (reads under some paths,
    #      writes under others)?                                                No.
    #      Every code path is a read; no branch opens a file for write.
    # Spec: docs/plans/2026-07-12-claude-klabauter-cartography-substrate-strand-a.md § C2
    "cartography.tree": OpClass.COMPUTE_ONLY,
    # cartography.file_index — COMPUTE_ONLY: thin RPC wrapper over
    # coordinator_core.cartography.file_index.build_file_index (ops/cartography_file_index.py).
    # DR-208 five-question affirmation:
    #   1. Does the handler open any file for write (including append)?          No.
    #      build_file_index -> list_tracked_files (git ls-files, read query). No
    #      write/append call anywhere in this module or cartography/file_index.py.
    #   2. Does the handler call any git write command?                         No.
    #      The only git invocation, transitively via cartography.tree.list_tracked_files,
    #      is `git ls-files` — read-only (same coverage.gate precedent, DR-208's own
    #      table: "all subprocess calls are read-only git queries").
    #   3. Does the handler enqueue any state mutation (queue/backlog/etc.)?     No.
    #   4. Does the handler invoke any subprocess that may do any of the above?  No.
    #      The sole subprocess call (via list_tracked_files) is `git ls-files`.
    #   5. Is the handler's I/O behavior conditional (reads under some paths,
    #      writes under others)?                                                No.
    #      build_file_index/system_for_path are pure string/dict ops over the
    #      list_tracked_files result; no branch opens a file for write.
    # Spec: docs/plans/2026-07-12-claude-klabauter-cartography-substrate-strand-a.md § C2
    "cartography.file_index": OpClass.COMPUTE_ONLY,
    # ---------------------------------------------------------------------------
    # cartography.symbols — MUTATING (2026-08-20: DR-228 § D6 scratch-tier
    # sanctioned category, amended to a 5th op alongside distill.scope /
    # distill.curation_status / memo.fate_partition / cartography.chunk_table).
    # ---------------------------------------------------------------------------
    # cartography.symbols — MUTATING: when params["emit"] is truthy, writes
    # exactly one whole-JSON artifact to
    # <target_root>/state/scratch/cartography-symbols/<run_id>/symbols.json
    # and stops — no delete, no in-place mutation of an existing file, no git
    # command (DR-228 § D6(ii)-(iv)). Classified MUTATING unconditionally
    # (per-op, not per-call, per DR-208's classification granularity) even
    # though a bare compute-and-return call (emit absent/false) performs no
    # write — see this repo's cartography.chunk_table entry immediately
    # below, whose shape this affirmation copies; a writing op sitting at
    # COMPUTE_ONLY, even for calls that don't write, is the authz hole
    # DR-208's fail-closed default exists to close.
    # DR-208 five-question affirmation (citing ops/cartography_symbols.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      write_symbols_artifact (mkstemp+os.replace) writes
    #      <target_root>/state/scratch/cartography-symbols/<run_id>/symbols.json
    #      when emit is truthy.
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only the local scratch-tier JSON artifact; no rag store write.
    #      Dual-write ban (DR-208 / tri-plane DD#1) satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      mkstemp+os.replace in write_symbols_artifact opens a temp file.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      target_root/state/scratch/ is coordinator substrate (global
    #      ~/.claude/CLAUDE.md § "state/ vs tasks/" — scratch/ is enumerated
    #      load-bearing substrate, not an ephemeral tempdir; DR-228 § D6
    #      resolves this explicitly, now extended to this op's own named
    #      subdirectory).
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The written artifact is readable back off disk by any caller that
    #      supplied emit:true — the entire reason this op's emit path exists
    #      (source_memo: 2026-08-19-doe-claude-em-cartography-symbols-needs-
    #      artifact-emission.md).
    # DR-228 § D6(i)-(v) scratch-tier bound affirmed (per handler code):
    #   D6(i)  (write-confined): the emit write target is built from a
    #          `safe_id`-validated run_id interpolated ONLY into
    #          <target_root>/state/scratch/cartography-symbols/<run_id>/
    #          symbols.json — no other state/ path is ever touched.
    #   D6(ii) (create-or-full-rewrite only): write_symbols_artifact is a
    #          single mkstemp+os.replace whole-payload write; no existing
    #          file is opened for partial/appending edit.
    #   D6(iii) (no delete): this handler contains no unlink/rmtree/git-rm call.
    #   D6(iv) (no commit): this handler issues no git subprocess of any kind.
    #   D6(v)  (schema_version-pinned): the artifact's first key is
    #          cartography_symbols.SCHEMA_VERSION; an unknown forward version
    #          is a fail-loud consumption error at the reading end
    #          (cartography_symbols.check_schema_version).
    # Read-side profile unchanged from the prior COMPUTE_ONLY affirmation:
    # this handler shells out to nothing on either the `.py` (ast.parse) or
    # foreign (symbol_extract.extract) path — pure-Python plus a third-party
    # tree-sitter parse over already-read source text, a strictly narrower
    # profile than coverage.gate's affirmed-COMPUTE_ONLY git subprocess reads
    # (DR-208's own table: "all subprocess calls are read-only git queries").
    # Authority: docs/decisions/DR-228-distill-disposal-substrate-writer-category.md § D6 (amended)
    #            docs/decisions/DR-208-invoke-op-authz-model.md § 5
    # Spec: docs/plans/2026-07-12-claude-klabauter-cartography-substrate-strand-a.md § C4
    #       docs/plans/2026-08-20-symbols-emits-its-own-artifact.md § C1
    "cartography.symbols": OpClass.MUTATING,
    # cartography.edges — COMPUTE_ONLY: thin RPC wrapper over
    # coordinator_core.cartography.edges.build_edges (ops/cartography_edges.py) — static
    # import + intra-module call graph. Self-describes its known incompleteness in-band
    # (static_only: true / excludes: ["register_op_dynamic_dispatch"]) so no consumer
    # mistakes the static graph for complete (AC7).
    # DR-208 five-question affirmation (citing this handler):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?    No.
    #      Only reads *.py source text (Path.read_text, mode 'r') and returns a
    #      computed dict; no file is opened for write, no git object is touched.
    #   2. Writes into rag's relational store?                                   No.
    #      Returns a structured dict to the caller; no rag interaction of any kind.
    #   3. Opens any file for write (including sentinel creation)?               No.
    #      Every file touched is opened read-only; no tempfile, no sentinel, no
    #      os.replace.
    #   4. Mutates shared mutable state outside its own module?                  No.
    #      ast.parse/ast.walk operate on an in-memory string/tree; no shared/global
    #      state write.
    #   5. Persistent state changes observable across process boundaries?       No.
    #      Nothing is written to disk; the only observable effect is the return value.
    #   Git-shelling-is-read-only precedent: this handler shells out to nothing — pure-
    #   Python ast.parse/ast.walk over already-read source text, a strictly narrower
    #   profile than coverage.gate's affirmed-COMPUTE_ONLY git subprocess reads (DR-208's
    #   own table: "all subprocess calls are read-only git queries"). No subprocess call
    #   is made here at all.
    # Spec: docs/plans/2026-07-12-claude-klabauter-cartography-substrate-strand-a.md § C4, AC7
    "cartography.edges": OpClass.COMPUTE_ONLY,
    # cartography.op_edges — COMPUTE_ONLY: thin RPC wrapper over
    # coordinator_core.cartography.op_edges.build_op_edges (ops/cartography_op_edges.py)
    # — registry-dispatch producer(register_op site)/consumer(get_op_handler /
    # dispatch_message literal site) edge graph, the one edge class
    # cartography.edges' own excludes marker names as categorically absent.
    # DR-208 five-question affirmation (citing this handler):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?    No.
    #      Only reads *.py source text (Path.read_text, mode 'r') and returns a
    #      computed dict; no file is opened for write, no git object is touched.
    #   2. Writes into rag's relational store?                                   No.
    #      Returns a structured dict to the caller; no rag interaction of any kind.
    #   3. Opens any file for write (including sentinel creation)?               No.
    #      Every file touched is opened read-only; no tempfile, no sentinel, no
    #      os.replace.
    #   4. Mutates shared mutable state outside its own module?                  No.
    #      ast.parse/ast.walk operate on an in-memory string/tree; no shared/global
    #      state write.
    #   5. Persistent state changes observable across process boundaries?       No.
    #      Nothing is written to disk; the only observable effect is the return value.
    #   Git-shelling-is-read-only precedent: this handler shells out to nothing — pure-
    #   Python ast.parse/ast.walk over already-read source text, the same profile as
    #   cartography.edges' own affirmed-COMPUTE_ONLY posture.
    # Spec: cross-repo memo, 2026-08-06 architecture survey.
    "cartography.op_edges": OpClass.COMPUTE_ONLY,
    # workflow.validate — COMPUTE_ONLY: thin RPC wrapper over
    # coordinator_core.ops._workflow_contract.run_checks (ops/workflow_validate.py) —
    # regex/line-based correctness-contract lint over a fleet Workflow `.mjs` script.
    # DR-208 five-question affirmation (citing this handler):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?    No.
    #      Only reads the script text at `script_path` (Path.read_text, mode 'r')
    #      and returns a computed dict; no file is opened for write, no git object
    #      is touched.
    #   2. Writes into rag's relational store?                                   No.
    #      Returns a structured dict to the caller; no rag interaction of any kind.
    #   3. Opens any file for write (including sentinel creation)?               No.
    #      The only file touched is opened read-only; no tempfile, no sentinel, no
    #      os.replace.
    #   4. Mutates shared mutable state outside its own module?                  No.
    #      run_checks operates on an in-memory string and returns a fresh list of
    #      Finding dataclasses; no shared/global state write.
    #   5. Persistent state changes observable across process boundaries?       No.
    #      Nothing is written to disk; the only observable effect is the return value.
    #   Git-shelling-is-read-only precedent: this handler shells out to nothing — pure-
    #   Python regex/line checks over already-read source text, a strictly narrower
    #   profile than coverage.gate's affirmed-COMPUTE_ONLY git subprocess reads (DR-208's
    #   own table: "all subprocess calls are read-only git queries"). No subprocess call
    #   is made here at all.
    # Spec: docs/plans/2026-07-12-workflow-skeleton-stamper-claude-klabauter-engine.md § C2
    "workflow.validate": OpClass.COMPUTE_ONLY,
    # workflow.scaffold — COMPUTE_ONLY: pure-generation RPC that composes a
    # green-by-construction fleet Workflow .mjs skeleton from caller-supplied
    # name/description/phases + one of the four HOUSE_PATTERNS templates
    # (coordinator_core.ops._workflow_patterns), returned as TEXT — no file
    # write (ops/workflow_scaffold.py).
    # DR-208 five-question affirmation (citing this handler):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?    No.
    #      Builds a string in memory from caller-supplied params and the
    #      module-level HOUSE_PATTERNS dict; no file is opened, no git object
    #      is touched.
    #   2. Writes into rag's relational store?                                   No.
    #      Returns a structured dict to the caller; no rag interaction of any kind.
    #   3. Opens any file for write (including sentinel creation)?               No.
    #      No file I/O of any kind — pure string composition in memory.
    #   4. Mutates shared mutable state outside its own module?                  No.
    #      Reads (never mutates) the module-level HOUSE_PATTERNS dict; every
    #      other value is a fresh local string built per call.
    #   5. Persistent state changes observable across process boundaries?       No.
    #      Nothing is written to disk; the only observable effect is the return
    #      value. Placing the text into a file is the CALLER's responsibility.
    #   Git-shelling-is-read-only precedent: this handler shells out to nothing
    #   and reads nothing — pure-Python string composition, a strictly narrower
    #   profile than coverage.gate's affirmed-COMPUTE_ONLY git subprocess reads
    #   (DR-208's own table: "all subprocess calls are read-only git queries").
    #   No subprocess call, and no file read, is made here at all.
    # Spec: docs/plans/2026-07-12-workflow-skeleton-stamper-claude-klabauter-engine.md § C3
    "workflow.scaffold": OpClass.COMPUTE_ONLY,
    # compute_layer.scaffold — COMPUTE_ONLY for BOTH its wire modes. mode=emit
    # composes a Sub-shape B producer-skeleton TEXT from caller-supplied
    # skill_name/verbs (ops/compute_layer_scaffold/emit.py), no file write.
    # mode=check scores the five Sub-shape B producers' own source against the
    # conformance clauses and RETURNS the rendered report TEXT to the caller
    # (ops/compute_layer_scaffold/check.py) — it reads producer source files
    # read-only and writes NOTHING (no sidecar, no cache, no state file), so it
    # stays under this single COMPUTE_ONLY entry rather than a separate
    # classification. If a future check-mode revision starts writing a report
    # sidecar, that write path must be split into its own op name and its own
    # affirmation block — not folded into this entry.
    # DR-208 five-question affirmation (citing ops/compute_layer_scaffold/op.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?    No.
    #      emit builds a string in memory; check reads producer source read-only
    #      and renders a report string. No file is opened for write.
    #   2. Writes into rag's relational store?                                   No.
    #      Returns a structured dict to the caller; no rag interaction of any kind.
    #   3. Opens any file for write (including sentinel creation)?               No.
    #      emit performs no file I/O; check opens producer source read-only via
    #      Path.read_text (ops/compute_layer_scaffold/check.py).
    #   4. Mutates shared mutable state outside its own module?                  No.
    #      Every returned value is freshly built per call; no shared/global
    #      state is written by this handler.
    #   5. Persistent state changes observable across process boundaries?       No.
    #      Nothing is written to disk by either mode; the only observable effect
    #      is the return value handed back to the caller.
    # Spec: docs/plans/2026-08-13-compute-layer-scaffolder.md § C4
    "compute_layer.scaffold": OpClass.COMPUTE_ONLY,
    # memo.triage — COMPUTE_ONLY: deterministic pre-filter over cross-repo/archive/*.md
    # memos (frontmatter score + already-captured cross-check + legacy backfill +
    # observability). Does NOT call Haiku/Sonnet and does NOT decide final promotion —
    # that judgment belongs to DoE's C6 background-Workflow LLM triage wave.
    # DR-208 five-question affirmation (citing ops/memo_triage.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?    No.
    #      Every path read is opened read-only (Path.read_text); no write/append call
    #      anywhere in this module.
    #   2. Writes into rag's relational store?                                   No.
    #      Returns a structured dict to the caller; no rag interaction of any kind.
    #   3. Opens any file for write (including sentinel creation)?               No.
    #      No tempfile, no sentinel, no os.replace — read-only throughout.
    #   4. Mutates shared mutable state outside its own module?                  No.
    #      No shared/global state is written by this handler.
    #   5. Persistent state changes observable across process boundaries?       No.
    #      Nothing is written to disk; the only observable effect is the return value
    #      (the triage_memos outcome dict) handed back to the caller.
    #   Git-shelling-is-read-only precedent: this handler shells out to nothing at all —
    #   a strictly narrower profile than coverage.gate's affirmed-COMPUTE_ONLY git
    #   subprocess reads (DR-208's own table: "all subprocess calls are read-only git
    #   queries").
    # Spec: docs/plans/2026-07-12-distill-ceremony-mechanical-substrate-joint-design.md § C5
    "memo.triage": OpClass.COMPUTE_ONLY,
    # ---------------------------------------------------------------------------
    # memo.fate_backfill (2026-08-06, cross-repo ask item 1(a)) — COMPUTE_ONLY.
    # DR-208 five-question affirmation (citing coordinator_core/ops/memo_fate_backfill.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object? NO.
    #      No _atomic_write_json, no register_op write path — the handler reads
    #      cross-repo/archive/*.md and returns a derived-fate report only. Never
    #      calls memo.transition or any other mutating op.
    #   2. Any subprocess call? Only `git cat-file -e`/`git rev-parse` via
    #      `delete_guard.resolve_realized_by` reuse — read-only git object
    #      existence checks, same profile coverage.gate's affirmed-COMPUTE_ONLY
    #      git reads use (DR-208's own table).
    #   3. Any network/external I/O? NO.
    #   4. Any side effect observable outside the return value? NO — nothing is
    #      written to disk; the only observable effect is the returned
    #      backfill_fates outcome dict.
    #   5. Idempotent, safe to re-run without operator review? YES — pure
    #      derivation over on-disk frontmatter, same corpus each call.
    # Spec: cross-repo/inbox/2026-08-06-example-retrieval-repo-em-distill-fate-coverage-and-legacy-log-reader.md § 1(a)
    "memo.fate_backfill": OpClass.COMPUTE_ONLY,
    # ---------------------------------------------------------------------------
    # distill.scope — MUTATING: DR-228 § D6 scratch-tier writer (one of the six
    # ops DR-228 admits under coordinator_core/ipc.py's substrate negative-spec).
    # DR-208 five-question affirmation (citing coordinator_core/ops/distill_scope.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      write_scope_manifest() writes state/scratch/artifact-distillation/
    #      <run-id>/input.json via mkstemp + os.replace (atomic create-or-full-
    #      rewrite, never a partial edit — distill_scope.py's write_scope_manifest).
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only the one JSON file above; no rag-owned surface touched.
    #      Dual-write ban (DR-208 / tri-plane DD#1) satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      write_scope_manifest()'s tempfile.mkstemp + os.write + os.replace.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      state/scratch/artifact-distillation/ is coordinator substrate (per
    #      DR-228 § D6's resolution of the reserved-noun question — the global
    #      CLAUDE.md "state/ vs tasks/" doctrine names this exact subtree as
    #      always-on session substrate, never ephemeral tempdir).
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The written input.json is observable by any subsequent reader
    #      (the artifact-distillation Workflow, a sibling EM session) once the
    #      handler returns.
    # DR-228 § D6(i)-(v) scratch-tier bounds affirmed per handler code:
    #   D6-i   (write-confined): write_scope_manifest() derives its target path
    #          solely from worktree_root/"state"/"scratch"/"artifact-distillation"/
    #          manifest["run_id"] — never a sibling run-id's directory, never any
    #          other state/ path.
    #   D6-ii  (create-or-full-rewrite only): the manifest dict is serialized whole
    #          via manifest_schema.canonical_manifest_bytes() and written in one
    #          mkstemp+os.replace — no existing file is opened for partial/append
    #          edit.
    #   D6-iii (no delete): the handler never removes any file.
    #   D6-iv  (no commit): the handler issues no git command of any kind; landing
    #          the artifact in git (if warranted) is the calling EM/session's own
    #          later action, per DR-228 § D6(iv).
    #   D6-v   (schema_version-pinned): the emitted manifest carries schema_version
    #          as its first key via manifest_schema.make_scope_manifest() (C9).
    # Spec: docs/plans/2026-07-23-claude-klabauter-driven-ceremony-redesign.md § C10
    # Governing DR: docs/decisions/DR-228-distill-disposal-substrate-writer-category.md § D6
    # ---------------------------------------------------------------------------
    "distill.scope": OpClass.MUTATING,
    # strategic.generate — MUTATING: writes state/strategic/self-description.draft.yaml
    # under the caller-supplied target_root (ops/strategic/draft_writer.py write_draft —
    # unconditional yaml.safe_dump to the draft path). Mirrors percolate.run
    # (MUTATING + scope "none") — this op plainly mutates, ambiguous-defaults-MUTATING
    # is moot. classify() fails closed on a missing entry; this entry is required.
    # Spec: docs/plans/2026-07-11-claude-klabauter-strategic-self-description-generation-leg.md § C1
    "strategic.generate": OpClass.MUTATING,
    # strategic.emit — MUTATING: writes state/strategic-emission.json under the
    # caller-supplied target_root's central_state_root when the curated canonical is present
    # (ops/strategic/emit_writer.py emit_strategic_feed — unconditional json write to the
    # feed path). Mirrors strategic.generate (MUTATING + scope "none"). Canonical-absent path
    # is a typed no-op that writes nothing, but the op is still classified MUTATING because it
    # writes disk on its primary (canonical-present) path — same ambiguous-defaults-MUTATING
    # posture as strategic.generate. classify() fails closed on a missing entry; this entry is
    # required.
    # Spec: tasks/strategic-feed-emission/stub.md
    "strategic.emit": OpClass.MUTATING,
    # session_hierarchy.derive — MUTATING: writes state/session-hierarchy.<slug>.json
    # (full-rebuild atomic temp+rename). Read side only queries claude-klabauter's own
    # state/handoffs + archive/handoffs via coordinator_core.ops.ceremony.records_query
    # (no git subprocess, no rag relational-store access).
    # Spec: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T3a-g3c
    "session_hierarchy.derive": OpClass.MUTATING,
    # session_ledger.aggregate_chain_loe — COMPUTE_ONLY: read-only chain walk over
    # state/handoffs + archive/handoffs, no writes/mutation. Byte-parity port
    # (read-only/idempotent per the oracle's own header).
    # Port of: aggregate-chain-loe.sh (DoE b644d5a9, 2026-07-22).
    # Spec: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T3a-g3d
    "session_ledger.aggregate_chain_loe": OpClass.COMPUTE_ONLY,
    # deferral.detect_orphan_memo — COMPUTE_ONLY: read-only hidden-deferral detector —
    # scans cross-repo/inbox/*.md plus docs/plans/*.md, state/handoffs/*.md, and
    # docs/decisions/*.md for owning-artifact references. Never mutates any file.
    # DR-208 five-question affirmation (citing ops/deferral_detect_orphan_memo.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?    No.
    #      Every path read is opened read-only (Path.read_text); no write/append call
    #      anywhere in this module.
    #   2. Writes into rag's relational store?                                   No.
    #      Returns a structured dict to the caller; no rag interaction of any kind.
    #   3. Opens any file for write (including sentinel creation)?               No.
    #      No tempfile, no sentinel, no os.replace — read-only throughout.
    #   4. Mutates shared mutable state outside its own module?                  No.
    #      No shared/global state is written by this handler.
    #   5. Persistent state changes observable across process boundaries?       No.
    #      Nothing is written to disk; the only observable effect is the return value
    #      (the findings/offer dict) handed back to the caller.
    #   Git-shelling-is-read-only precedent: this handler shells out to nothing at all —
    #   a strictly narrower profile than coverage.gate's affirmed-COMPUTE_ONLY git
    #   subprocess reads (DR-208's own table: "all subprocess calls are read-only git
    #   queries").
    # Spec: tasks/hidden-deferral-detectors/design.md § Detector 2
    "deferral.detect_orphan_memo": OpClass.COMPUTE_ONLY,
    # deferral.detect_partial_strangle — COMPUTE_ONLY: read-only hidden-deferral
    # detector — scans docs/decisions/*strangl* / docs/plans/*strang* for
    # strangler-endpoint manifest blocks and cross-checks declared_verbs against
    # shipped_native_op paths + a plain-text docs/plans/*.md grep for "planned".
    # Never mutates any file.
    # DR-208 five-question affirmation (citing ops/deferral_detect_partial_strangle.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?    No.
    #      Every path read is opened read-only (Path.read_text); no write/append call
    #      anywhere in this module.
    #   2. Writes into rag's relational store?                                   No.
    #      Returns a structured dict to the caller; no rag interaction of any kind.
    #   3. Opens any file for write (including sentinel creation)?               No.
    #      No tempfile, no sentinel, no os.replace — read-only throughout.
    #   4. Mutates shared mutable state outside its own module?                  No.
    #      No shared/global state is written by this handler.
    #   5. Persistent state changes observable across process boundaries?       No.
    #      Nothing is written to disk; the only observable effect is the return value
    #      (the findings/notices/offer dict) handed back to the caller.
    #   Git-shelling-is-read-only precedent: this handler shells out to nothing at all —
    #   a strictly narrower profile than coverage.gate's affirmed-COMPUTE_ONLY git
    #   subprocess reads (DR-208's own table: "all subprocess calls are read-only git
    #   queries").
    # Spec: tasks/hidden-deferral-detectors/design.md § Detector 1
    "deferral.detect_partial_strangle": OpClass.COMPUTE_ONLY,
    # schema.describe / schema.validate — COMPUTE_ONLY: both wrap
    # schema_validate.describe()/validate(), which only read the vendored
    # coordinator_core/frontmatter/schemas/ tree; neither writes any file,
    # issues any git command, or mutates coordinator substrate. See
    # coordinator_core/frontmatter/schema_cli.py's "Dual registration" block.
    "schema.describe": OpClass.COMPUTE_ONLY,
    "schema.validate": OpClass.COMPUTE_ONLY,
    # ---------------------------------------------------------------------------
    # memo.fate_partition — MUTATING (C16, DR-228 § D6 scratch-tier sanctioned category).
    # ---------------------------------------------------------------------------
    # memo.fate_partition — MUTATING: writes exactly one whole-JSON shard to
    # state/scratch/artifact-distillation/<run_id>/fate-partition.json and stops — no
    # delete, no in-place mutation of an existing file, no git command (DR-228 § D6(ii)-(iv)).
    # DR-208 five-question affirmation (citing ops/memo_fate_partition.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      _atomic_write_json (mkstemp+os.replace) writes
    #      state/scratch/artifact-distillation/<run_id>/fate-partition.json.
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only the local scratch-tier JSON shard; no rag store write.
    #      Dual-write ban (DR-208 / tri-plane DD#1) satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      mkstemp+os.replace in _atomic_write_json opens a temp file.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      state/scratch/artifact-distillation/ is coordinator substrate (global
    #      ~/.claude/CLAUDE.md § "state/ vs tasks/" — scratch/ is enumerated load-bearing
    #      substrate, not an ephemeral tempdir; DR-228 § D6 resolves this explicitly).
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The written shard is read by the downstream log-append / LLM-specialist wave.
    # DR-228 § D6(i)-(v) scratch-tier bound affirmed (per handler code):
    #   D6(i)  (write-confined): shard write target is built from a `safe_id`-validated
    #          run_id interpolated ONLY into
    #          state/scratch/artifact-distillation/<run_id>/fate-partition.json — no
    #          other state/ path is ever touched by this handler.
    #   D6(ii) (create-or-full-rewrite only): _atomic_write_json is a single
    #          mkstemp+os.replace whole-payload write; no existing file is opened for
    #          partial/appending edit.
    #   D6(iii) (no delete): this handler contains no unlink/rmtree/git-rm call.
    #   D6(iv) (no commit): this handler issues no git subprocess of any kind.
    #   D6(v)  (schema_version-pinned): the shard's first key is
    #          manifest_schema.SCHEMA_VERSION (C9 convention); an unknown forward
    #          version is a fail-loud consumption error at the reading end
    #          (manifest_schema.check_schema_version).
    # Authority: docs/decisions/DR-228-distill-disposal-substrate-writer-category.md § D6
    #            docs/decisions/DR-208-invoke-op-authz-model.md § 5
    "memo.fate_partition": OpClass.MUTATING,
    # ---------------------------------------------------------------------------
    # distill.curation_status — MUTATING (C11, DR-228 § D6 scratch-tier sanctioned category).
    # ---------------------------------------------------------------------------
    # distill.curation_status — MUTATING: when params["emit"] is truthy, writes exactly
    # one whole-JSON artifact to the single fixed path
    # state/ceremony/curation-status.json and stops — no delete, no in-place mutation
    # of an existing file, no git command (DR-228 § D6(ii)-(iv)). Classified MUTATING
    # unconditionally (per-op, not per-call, per DR-208's classification granularity)
    # even though a bare compute-and-return call (emit absent/false) performs no write.
    # DR-208 five-question affirmation (citing ops/distill_curation_status.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      locked_rmw (mkstemp+os.replace under a cross-process flock) writes
    #      state/ceremony/curation-status.json when emit is truthy.
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only the local fixed-path JSON artifact; no rag store write.
    #      Dual-write ban (DR-208 / tri-plane DD#1) satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      locked_rmw's mkstemp+os.replace opens a temp file in state/ceremony/.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      state/ceremony/ is coordinator substrate (global ~/.claude/CLAUDE.md §
    #      "state/ vs tasks/"; DR-228 § D6 resolves state/scratch/ and its siblings
    #      as inside the reserved substrate set, not an ephemeral tempdir).
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The emitted artifact is read by cockpit/rag/siblings (DEC-1 fleet
    #      visibility) and by this op's own next invocation (last_run_id/age math).
    # DR-228 § D6(i)-(v) scratch-tier bound affirmed (per handler code):
    #   D6(i)  (write-confined): the emit write target is the single fixed constant
    #          CURATION_STATUS_REL_PATH ("state/ceremony/curation-status.json") — not
    #          run-scoped (a point-in-time derived snapshot, not a per-run artifact,
    #          per DR-228 § D6's explicit citation of this op) — no other state/ path
    #          is ever touched by this handler.
    #   D6(ii) (create-or-full-rewrite only): locked_rmw's mutate callback returns the
    #          whole serialized payload unconditionally; no existing file is opened
    #          for partial/appending edit.
    #   D6(iii) (no delete): this handler contains no unlink/rmtree/git-rm call.
    #   D6(iv) (no commit): this handler issues no git subprocess of any kind.
    #   D6(v)  (schema_version-pinned): the artifact's first key is
    #          manifest_schema.SCHEMA_VERSION (C9 convention), validated via
    #          manifest_schema.validate_curation_status before every write; an unknown
    #          forward version is a fail-loud consumption error at the reading end
    #          (manifest_schema.check_schema_version).
    # Authority: docs/decisions/DR-228-distill-disposal-substrate-writer-category.md § D6
    #            docs/decisions/DR-208-invoke-op-authz-model.md § 5
    "distill.curation_status": OpClass.MUTATING,
    # ---------------------------------------------------------------------------
    # cartography.chunk_table — MUTATING (DR-228 § D6 scratch-tier sanctioned
    # category, amended to a 4th op alongside distill.scope /
    # distill.curation_status / memo.fate_partition).
    # ---------------------------------------------------------------------------
    # cartography.chunk_table — MUTATING: when params["emit"] is truthy, writes
    # exactly one whole-JSON artifact to
    # <target_root>/state/scratch/cartography-chunk-table/<run_id>/chunk-table.json
    # and stops — no delete, no in-place mutation of an existing file, no git
    # command (DR-228 § D6(ii)-(iv)). Classified MUTATING unconditionally
    # (per-op, not per-call, per DR-208's classification granularity) even
    # though a bare compute-and-return call (emit absent/false) performs no
    # write.
    # DR-208 five-question affirmation (citing ops/cartography_chunk_table.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      write_chunk_table (mkstemp+os.replace) writes
    #      <target_root>/state/scratch/cartography-chunk-table/<run_id>/chunk-table.json
    #      when emit is truthy.
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only the local scratch-tier JSON artifact; no rag store write.
    #      Dual-write ban (DR-208 / tri-plane DD#1) satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      mkstemp+os.replace in write_chunk_table opens a temp file.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      target_root/state/scratch/ is coordinator substrate (global
    #      ~/.claude/CLAUDE.md § "state/ vs tasks/" — scratch/ is enumerated
    #      load-bearing substrate, not an ephemeral tempdir; DR-228 § D6
    #      resolves this explicitly, now extended to this op's own named
    #      subdirectory).
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The written artifact is read by DoE-claude's
    #      /coordinator:architecture-survey consumer
    #      (fanout.poll_scratch_dir) across the LLM-transport + process
    #      boundary — the entire reason this op exists (see module docstring).
    # DR-228 § D6(i)-(v) scratch-tier bound affirmed (per handler code):
    #   D6(i)  (write-confined): the emit write target is built from a
    #          `safe_id`-validated run_id interpolated ONLY into
    #          <target_root>/state/scratch/cartography-chunk-table/<run_id>/
    #          chunk-table.json — no other state/ path is ever touched.
    #   D6(ii) (create-or-full-rewrite only): write_chunk_table is a single
    #          mkstemp+os.replace whole-payload write; no existing file is
    #          opened for partial/appending edit.
    #   D6(iii) (no delete): this handler contains no unlink/rmtree/git-rm call.
    #   D6(iv) (no commit): this handler issues no git subprocess of any kind.
    #   D6(v)  (schema_version-pinned): the artifact's first key is
    #          cartography_chunk_table.SCHEMA_VERSION; an unknown forward
    #          version is a fail-loud consumption error at the reading end
    #          (cartography_chunk_table.check_schema_version). Recomputable
    #          from disk truth (D6 sufficiency statement): a chunk table over
    #          an unmodified tree with the same systems/chunk_size params is
    #          byte-identical run over run.
    # Authority: docs/decisions/DR-228-distill-disposal-substrate-writer-category.md § D6 (amended)
    #            docs/decisions/DR-208-invoke-op-authz-model.md § 5
    "cartography.chunk_table": OpClass.MUTATING,
    # ---------------------------------------------------------------------------
    # distill.assemble_disposal_manifest — MUTATING (C12, DR-228 § D1 disposal-tier
    # sanctioned category; write shape is scratch-tier-shaped per § D3).
    # ---------------------------------------------------------------------------
    # distill.assemble_disposal_manifest — MUTATING: writes exactly one whole-JSON
    # artifact to state/scratch/artifact-distillation/<run_id>/disposal-manifest.json
    # and stops — no delete, no in-place mutation of an existing file, no git
    # command (DR-228 § D3 — "assemble_disposal_manifest ... do[es] not commit at
    # all; ... writes are scratch-tier-shaped ... landed on disk only").
    # DR-208 five-question affirmation (citing ops/distill_disposal_manifest.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      write_disposal_manifest() writes state/scratch/artifact-distillation/
    #      <run-id>/disposal-manifest.json via mkstemp + os.replace (atomic
    #      create-or-full-rewrite, never a partial edit).
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only the one JSON file above; no rag-owned surface touched.
    #      Dual-write ban (DR-208 / tri-plane DD#1) satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      write_disposal_manifest()'s tempfile.mkstemp + os.write + os.replace.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      state/scratch/artifact-distillation/ is coordinator substrate (DR-228
    #      § D6's resolution of the reserved-noun question, carried forward for
    #      this op by § D3's scratch-tier-shaped characterization).
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The written disposal-manifest.json is observable by any subsequent
    #      reader (distill.stamp_disposal, distill.apply_disposal, a sibling EM
    #      session) once the handler returns.
    # Write-confinement affirmed (per handler code, mirroring DR-228 § D6(i)-(iii)
    # even though this op is D1/D3-sanctioned rather than one of D6's three named
    # scratch-tier writers — its write shape is identical in kind):
    #   - write-confined: write_disposal_manifest() derives its target path solely
    #     from worktree_root/"state"/"scratch"/"artifact-distillation"/
    #     manifest["run_id"] — never a sibling run-id's directory, never any
    #     other state/ path.
    #   - create-or-full-rewrite only: the manifest dict is serialized whole and
    #     written in one mkstemp+os.replace — no existing file is opened for
    #     partial/append edit.
    #   - no delete: the handler never removes any file (delete is C14's
    #     apply_disposal, gated on a stamped manifest — DR-228 § D2a/D2b).
    #   - no commit: the handler issues no git command of any kind (commit is
    #     C14's apply_disposal only, per § D3's single-committer model).
    #   - schema_version-pinned: the emitted manifest carries schema_version as
    #     its first key via manifest_schema.make_disposal_manifest() (C9); the
    #     additive deletion_groups key (when present) does not disturb this.
    # Spec: docs/plans/2026-07-23-claude-klabauter-driven-ceremony-redesign.md § C12
    # Governing DR: docs/decisions/DR-228-distill-disposal-substrate-writer-category.md § D1, D3
    # ---------------------------------------------------------------------------
    "distill.assemble_disposal_manifest": OpClass.MUTATING,
    # ---------------------------------------------------------------------------
    # distill.stamp_disposal — MUTATING (C13, DR-228 § D2b(vi)/D3 disposal-tier
    # PM-authorization stamp; DEC-2 mirrors handoff.stamp_phase).
    # ---------------------------------------------------------------------------
    # distill.stamp_disposal — MUTATING: writes the four-field
    # disposal_authorized_{by,at,sha,note} stamp group IN PLACE onto C12's own
    # disposal-manifest.json (create-or-full-rewrite via mkstemp + os.replace,
    # never a partial in-place edit of the file bytes) — no delete, no git
    # command (DR-228 § D3 — "assemble_disposal_manifest and stamp_disposal do
    # not commit at all").
    # DR-208 five-question affirmation (citing ops/distill_stamp_disposal.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      write_stamped_manifest() overwrites state/scratch/artifact-distillation/
    #      <run-id>/disposal-manifest.json via mkstemp + os.replace.
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only the one JSON file above; no rag-owned surface touched.
    #      Dual-write ban (DR-208 / tri-plane DD#1) satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      write_stamped_manifest()'s tempfile.mkstemp + os.write + os.replace.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      state/scratch/artifact-distillation/ is coordinator substrate
    #      (DR-228 § D6's resolution of the reserved-noun question, carried
    #      forward for this op by § D3's scratch-tier-shaped characterization).
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The stamped disposal-manifest.json is observable by any subsequent
    #      reader (distill.apply_disposal, a sibling EM session, the PM's own
    #      re-check) once the handler returns.
    # Write-confinement affirmed (per handler code):
    #   - write-confined: manifest_path_for_run() derives its target path
    #     solely from worktree_root/"state"/"scratch"/"artifact-distillation"/
    #     run_id/"disposal-manifest.json" — the SAME path C12 wrote to, never
    #     a sibling run-id's directory, never any other state/ path.
    #   - create-or-full-rewrite only (in place): the manifest dict is
    #     serialized whole and written in one mkstemp+os.replace over the
    #     existing file — no partial/append edit of file bytes. This op
    #     REFUSES to run if the target manifest is absent (it stamps an
    #     EXISTING C12 output only; it never creates a fresh manifest).
    #   - no delete: the handler never removes any file (delete is C14's
    #     apply_disposal, gated on this stamp — DR-228 § D2a/D2b).
    #   - no commit: the handler issues no git command of any kind (commit is
    #     C14's apply_disposal only, per § D3's single-committer model).
    #   - schema_version-pinned: load_disposal_manifest() gates every read on
    #     manifest_schema.check_schema_version before any decision is made;
    #     the stamp write only ever adds/confirms the additive STAMP_FIELDS
    #     group via manifest_schema.apply_stamp (C9), never disturbing
    #     schema_version's first-key position.
    #   - sha never caller-supplied (DR-228 § D2b(vi)): disposal_authorized_sha
    #     is always computed via manifest_schema.compute_manifest_sha over the
    #     CURRENT on-disk manifest body; a caller-supplied sha/stamp-field
    #     param is a fail-loud ValueError (refuse-injection), never accepted
    #     or silently ignored.
    # Spec: docs/plans/2026-07-23-claude-klabauter-driven-ceremony-redesign.md § C13
    # Governing DR: docs/decisions/DR-228-distill-disposal-substrate-writer-category.md § D2b(vi), D3
    # ---------------------------------------------------------------------------
    "distill.stamp_disposal": OpClass.MUTATING,
    # ---------------------------------------------------------------------------
    # distill.apply_disposal — MUTATING (C14, DR-228 § D2a/D2b/D3/D4
    # disposal-tier Phase-5 delete — the ONLY deleting/committing member of
    # the six-op family this DR admits).
    # ---------------------------------------------------------------------------
    # distill.apply_disposal — MUTATING: re-verifies a stamped disposal-manifest
    # (gates: stamp-complete, sha-unstirred, throttle-acknowledged,
    # drain-ordering-verified), re-runs delete_guard per row at execute time
    # (TOCTOU), then git-rm's tracked survivors + appends
    # state/distillation-log.md + issues ONE scoped commit
    # (_delete_tracked_and_append_log), and Path.unlink()s untracked survivors
    # directly (no commit for that half). Also writes a JSON ceremony receipt
    # to state/ceremony/distill-apply-disposal/.
    # DR-208 five-question affirmation (citing ops/distill_apply_disposal.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      _delete_tracked_and_append_log() runs `git rm` per tracked survivor,
    #      rewrites state/distillation-log.md (log_append.append_rows), and
    #      issues one `git commit` over the union pathspec; untracked survivors
    #      are Path.unlink()ed; write_apply_receipt() writes a JSON receipt.
    #   2. Writes into rag's relational store?                                 No.
    #      Every write above is a git object or a file under this repo's own
    #      state/ tree. Dual-write ban (DR-208 / tri-plane DD#1) satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      log_append.append_rows() rewrites state/distillation-log.md;
    #      write_apply_receipt()'s mkstemp + os.replace.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      state/distillation-log.md and state/scratch/artifact-distillation/
    #      (read, not written, by this op) are coordinator substrate (DR-228 §
    #      D6's resolution of the reserved-noun question); the deleted
    #      artifact paths themselves (handoffs/memos under archive/ or
    #      state/) are also coordinator substrate.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The commit, the deleted files' absence, and the receipt are all
    #      observable by any subsequent reader once the handler returns.
    # Write-confinement + D2a/D2b bound-to-test citations affirmed (per handler
    # code; C9b's impl-slice obligation — this module docstring is the
    # authoritative bound->test mapping):
    #   - D2a-i (per-record idempotent): compute_apply_plan() skips any row
    #     whose path is already absent on disk ("already-deleted") — never
    #     re-deleted, never re-logged. Test: test_replay_is_idempotent_no_op.
    #   - D2a-ii (commutative): compute_apply_plan() sorts survivors by path
    #     before any git/log operation runs. Test: test_order_shuffled_is_commutative.
    #   - D2a-iii (cwd-scope-guarded): a missing manifest (load_disposal_manifest)
    #     or repo_root=None (handler) is refused/absent, never an error against
    #     the wrong tree. Test: test_handler_raises_on_repo_root_none,
    #     test_apply_disposal_manifest_absent_run_raises.
    #   - D2a-iv (act-time-terminality-re-verifying): compute_apply_plan()
    #     re-runs evaluate_candidate_receipts (delete_guard's own dispatch,
    #     imported not reimplemented) per survivor. Test: test_toctou_reblock_skips_newly_blocked_row.
    #   - D2a-v (fail-closed-to-keep): verify_stamp_and_throttle raises before
    #     any write on unstamped/sha-drifted/throttle-unacknowledged manifests.
    #     Test: test_verify_stamp_and_throttle_refuses_unstamped,
    #     test_verify_stamp_and_throttle_refuses_sha_drift,
    #     test_verify_stamp_and_throttle_refuses_unacknowledged_mass_throttle.
    #   - D2b-vi (stamped-manifest-gated): same tests as D2a-v above.
    #   - D2b-vii (drain-ordering-verified): verify_drain_ordering() uses
    #     `git merge-base --is-ancestor` (never bare `git cat-file -e`) PLUS a
    #     `git show --name-only` containment check. Test:
    #     test_verify_drain_ordering_refuses_non_ancestor_sha,
    #     test_verify_drain_ordering_refuses_ancestor_without_containment.
    #   - D3 (scoped commit, never git add -A): _delete_tracked_and_append_log()
    #     commits an exact pathspec (tracked survivors + the log path) via a
    #     private GIT_INDEX_FILE, never a directory prefix or -A/-. flag.
    #   - D4 (awaited async subprocess): every git call in this module is
    #     `asyncio.create_subprocess_exec` via the shared `_run_git` helper,
    #     never blocking `subprocess.run`.
    # Spec: docs/plans/2026-07-23-claude-klabauter-driven-ceremony-redesign.md § C14
    # Governing DR: docs/decisions/DR-228-distill-disposal-substrate-writer-category.md § D2a, D2b, D3, D4
    # ---------------------------------------------------------------------------
    "distill.apply_disposal": OpClass.MUTATING,
    # crossrepo.closure_status — COMPUTE_ONLY: joins the memo corpus
    # (cross-repo/inbox|archive) against the commitment ledger
    # (state/cross-repo-commitments/*.yaml) and returns a computed per-memo closure
    # verdict. No file is opened for write; the one non-pure-Python step
    # (delete_guard.resolve_realized_by's SHA-shape leg) shells out to a read-only
    # `git cat-file -e`, never a mutating git command.
    # DR-208 five-question affirmation (citing ops/crossrepo_closure_status.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  No.
    #      Only reads (read_text/scandir/exists) plus a read-only `git cat-file -e`.
    #   2. Writes into rag's relational store?                                No.
    #   3. Opens any file for write (including sentinel creation)?            No.
    #   4. Mutates shared mutable state outside its own module?               No.
    #   5. Persistent state changes observable across process boundaries?    No.
    #      Returns a computed JSON payload only; emits no artifact to disk.
    # Authority: docs/decisions/DR-208-invoke-op-authz-model.md § 5
    # Spec: docs/plans/2026-07-23-claude-klabauter-driven-ceremony-redesign.md § C15
    "crossrepo.closure_status": OpClass.COMPUTE_ONLY,
    # ---------------------------------------------------------------------------
    # ceremony.update_docs_scan — COMPUTE_ONLY (C17; AC8).
    # ---------------------------------------------------------------------------
    # Emits the update-docs mechanical work-manifest (Phase 1 state-detection, Phase 8
    # lineage-backstop, Phase 8b prune classification) as a computed return value only.
    # Archival/pruning EXECUTION stays with the existing fleet.* archival-writer ops and
    # the LLM/EM judgment tier (Negative spec, AC12) — this op never performs either.
    # DR-208 five-question affirmation (citing ops/ceremony/update_docs_scan.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  No.
    #      Every filesystem touch is a read (DIRECTORY.md/.py mtimes, docs/plans/*.md,
    #      cross-repo/archive/*.md, tasks/*.md frontmatter) or a read-only `git log`
    #      invocation (no `git commit`/`git rm`/ref mutation of any kind).
    #   2. Writes into rag's relational store?                                No.
    #   3. Opens any file for write (including sentinel creation)?            No.
    #   4. Mutates shared mutable state outside its own module?               No.
    #      No locked_rmw, no artifact.emit-style write path is invoked.
    #   5. Persistent state changes observable across process boundaries?     No.
    #      Returns a computed schema_version-pinned dict only; emits no artifact to disk.
    # Authority: docs/decisions/DR-208-invoke-op-authz-model.md § 5
    # Spec: docs/plans/2026-07-23-claude-klabauter-driven-ceremony-redesign.md § C17
    "ceremony.update_docs_scan": OpClass.COMPUTE_ONLY,
    "deliverable.cascade_retract": OpClass.MUTATING,
    "deliverable.cascade_backstop_sweep": OpClass.COMPUTE_ONLY,
    # ceremony.chunk_commits — COMPUTE_ONLY: pure git-log read (resolve_chunk_commits
    # composes git_native.log_diff_filter + a range `git log` call; no write_text/
    # locked_rmw/unlink anywhere in coordinator_core/ops/ceremony/chunk_commits.py,
    # verified). Answers "did chunk X commit?" — never mutates the repo it queries.
    # Filed 2026-08-10 (state/bug-backlog/2026-08-10-chain-ancestry-waivers-reap-and-
    # ceremony-e5afd3e0e7ab.yaml): both were registered+module-mapped but missing this
    # entry, outside the frozen _KNOWN_UNCLASSIFIED_OPS_DEBT baseline — a genuine
    # registration-quad regression, not baseline debt (see that entry for detail on why
    # widening the baseline would have been the wrong fix).
    "ceremony.chunk_commits": OpClass.COMPUTE_ONLY,
    # sizing.decline — MUTATING: writes `status: declined` under locked_rmw
    # (2026-08-10, docs/plans/2026-08-10-a-terminal-status-for-a-declined-sizing.md § C2).
    "sizing.decline": OpClass.MUTATING,
    # sizing.ship — MUTATING: writes `status: shipped` under locked_rmw
    # (2026-08-13, PM ruling; see coordinator_core/ops/sizing_ship.py docstring).
    "sizing.ship": OpClass.MUTATING,
    # sizing.record_spike_verdict — MUTATING: writes `premise.spike_verdict` under
    # locked_rmw (2026-08-14; see coordinator_core/ops/sizing_spike_verdict.py docstring).
    "sizing.record_spike_verdict": OpClass.MUTATING,
    # ---------------------------------------------------------------------------
    # distill.curate_clusters — COMPUTE_ONLY: a pure structural verdict over a
    # caller-supplied {system_tag: count} map. Reads no file, opens no path, and
    # holds no module state; repo scope is "none" for the same reason. Never
    # consults a model — determinism is the op's whole purpose.
    "distill.curate_clusters": OpClass.COMPUTE_ONLY,
    # distill.workflow_input — COMPUTE_ONLY: pure producer/consumer field
    # translation plus self-validation of its own output. No path is opened.
    "distill.workflow_input": OpClass.COMPUTE_ONLY,
    # updatedocs.gates — MUTATING, despite reading as a read-only verdict battery.
    # The 11i queue-prune gate SPAWNS the prune CLIs (see _gate_queue_prune_sweep):
    # the legacy leg rewrites the queue file in place and the YAML leg archives and
    # commits entries. Persistent state changes are observable across process
    # boundaries, so question 5 of the DR-208 checklist answers YES and no
    # COMPUTE_ONLY reading survives it. The mutation is inherited verbatim from the
    # update-docs-probes queue-prune-sweep subcommand this op absorbed, not
    # introduced here — but a gate that writes while being asked for a verdict is a
    # surprise worth retiring rather than preserving; see the module docstring.
    # Authority: docs/decisions/DR-208-invoke-op-authz-model.md § 5
    "updatedocs.gates": OpClass.MUTATING,
    # git.push_failure_verdict — COMPUTE_ONLY: classifies a non-fast-forward push
    # failure into peer_staged / half_applied_merge / simple_lag / resolved_since /
    # indeterminate so DoE's Stop-hook advisory renders a verdict we computed
    # rather than re-deriving one from a regex. Every git call is a read
    # (`diff --cached --name-only`, `diff --name-only`, `rev-parse`); no write, no
    # index mutation, no recovery action — the op deliberately never runs stash,
    # reset, or merge --abort, since each destroys a peer's work when the
    # peer_staged reading is true. Classified here rather than by its author: the
    # op landed at f6f455525 without an OP_CLASSIFICATION entry, leaving the
    # registration-quad baseline test red on HEAD.
    "git.push_failure_verdict": OpClass.COMPUTE_ONLY,
    # ---------------------------------------------------------------------------
    # tracker.advance_status — MUTATING: in-place rewrite of one or more status
    # cells in a plan chunk directory's tracker README (a markdown status table),
    # per coordinator_core/ops/tracker/advance_status.py.
    #
    # WRITE-TARGET CLASSIFICATION — READ BEFORE CITING THIS ENTRY AS PRECEDENT.
    # RATIFIED (write target): unlike every other MUTATING entry in this file
    # that crosses ipc.py's "handlers MUST NOT write coordinator substrate"
    # negative-spec, this op's write-target axis was, until 2026-07-25, the
    # one MUTATING entry with no DR-level carve-out (contrast DR-211 fleet
    # archival, DR-212 handoff frontmatter, DR-213 queue additive-create,
    # DR-214 memo.send, DR-216 changelog/completion/review-trail, DR-218
    # review-trail reap, DR-228 distill-disposal — each its own standalone,
    # PM-ratified decision record per DR-212's own framing, "a categorically
    # stronger crossing ... warrants its own named, greppable decision
    # record"). This op's write target — a caller-supplied tracker-README
    # path, mutated in-place — was a genuinely new noun population that no
    # existing carve-out's bounds covered (DR-216's `docs/plans/*.md`
    # carve-out is `plan.append_session`'s append-only session record, the
    # opposite semantic of a status-cell REWRITE); it is now its own
    # standalone, PM-ratified decision record, DR-094, ratifying the write
    # target AS THIS OP IS CURRENTLY BUILT (single caller-supplied,
    # path-contained tracker README per call; status-cell only; ALL-OR-NONE
    # across requested rows; per-row idempotent; every other byte unchanged) —
    # no wider, and NOT precedent for a differently-shaped tracker.* op. See
    # advance_status.py's own module docstring, "Write-target classification"
    # section, for the full account.
    #
    # STILL PROVISIONAL (handler-issued commit): DR-094 explicitly does NOT
    # ratify a handler-issued git commit for this write — this op's
    # self-imposed no-self-commit restraint stands unchanged, same as it did
    # before DR-094. See advance_status.py's "Write-target classification"
    # section (STILL PROVISIONAL paragraph) for the full account — including
    # why this module deliberately does NOT self-commit despite
    # `coordinator/skills/enrich-and-review/SKILL.md`'s prose describing the op
    # as landing "one scoped commit (SC-DR-008)".
    #
    # This entry affirms the DR-208 §5 five questions (classification axis,
    # settled, unchanged by DR-094) AND the second, separate axis — ipc.py
    # reserved-noun-write permission — which DR-094 now grants for this op's
    # write target as currently built. Handler-issued commit remains a
    # separate, not-yet-made decision; do not read DR-094 as authorizing it.
    #
    # DR-208 five-question affirmation (citing ops/tracker/advance_status.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      advance_status() rewrites the status cell of one or more table rows
    #      and atomically replaces the tracker file via tempfile.mkstemp +
    #      os.replace.
    #   2. Writes into rag's relational store?                                No.
    #      Writes only the single caller-supplied, worktree-contained
    #      tracker_path file. Dual-write ban satisfied.
    #   3. Opens any file for write (including sentinel creation)?            YES.
    #      Via the mkstemp+os.replace write in advance_status().
    #   4. Mutates shared mutable state outside its own module?               YES.
    #      The tracker README is coordinator substrate read by
    #      /enrich-and-review, /execute-plan, and any EM session reading the
    #      chunk directory's status.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The rewritten status cells are read by subsequent EM sessions /
    #      pipeline phases.
    # Write confinement (self-imposed at build time; now also DR-094's
    # ratified bounds — see "Write-target classification" note above): exactly
    # one file per call (the resolved tracker_path), path-contained to the
    # caller's worktree (_path_guard.contained_path); ALL-OR-NONE per-row
    # resolution (a not-found or ambiguous stub_id aborts the whole call
    # before any byte is written); no git commit issued by the handler (still
    # self-imposed and NOT DR-094-ratified — see "STILL PROVISIONAL" note
    # above); no rag store write.
    # Authority: docs/decisions/DR-208-invoke-op-authz-model.md § 5 (classification axis only)
    # Authority (reserved-noun-write axis): DoE-claude
    #   docs/decisions/DR-094-tracker-advance-status-write-target-carveout.md
    #   (write target, as currently built, only — does NOT ratify
    #   handler-issued commit; see "STILL PROVISIONAL" note above)
    # Spec: DoE-claude coordinator/skills/enrich-and-review/SKILL.md § Phase 2.5/4.5/6
    # ---------------------------------------------------------------------------
    "tracker.advance_status": OpClass.MUTATING,
    # ---------------------------------------------------------------------------
    # tracker.fold_observed_set — MUTATING. Own justification (this plan
    # explicitly instructs NOT to lean on tracker.advance_status's comment
    # above, which disclaims being precedent for a differently-shaped
    # tracker.* op):
    #   1. Opens any file for write (including sentinel creation)?            YES.
    #      Appends one observed_set_fold marker event to the calling
    #      machine's own sovereign-tracker shard via
    #      coordinator_core.tracker_store.fold_observed_set ->
    #      tracker_store.append_event's locked_rmw + os.replace write.
    #   2. Writes into rag's relational store?                                No.
    #      Writes only state/sovereign-tracker/ (via EVENTS_DIR_RELPATH),
    #      never rag's store. Dual-write ban satisfied.
    #   3. Mutates shared mutable state outside its own module?               YES.
    #      The sovereign-tracker shard is read by cockpit's cross-machine
    #      concurrent-apply detection.
    #   4. Persistent state changes observable across process boundaries?     YES.
    #      A future session/process reads the appended marker via
    #      resolve_observed_set / resolve_observed_set_for_event.
    #   5. Read-only, no side effects at all?                                 No.
    #      Question 1 alone already answers YES to "opens a file for write" —
    #      DR-208 §5's COMPUTE_ONLY checklist does not apply to a MUTATING op
    #      by construction (DR-241's Amendment corrects D1's original
    #      mis-citation of DR-208 §5 as the affirmation owed here; the
    #      affirmation actually owed is against DR-241 D2's own five bounds,
    #      discharged separately in DR-241's Amendment's bound-by-bound
    #      affirmation table).
    # Write confinement: opt-in-by-existence only (never mints
    # state/sovereign-tracker/), one marker append per call, idempotent on
    # its own terms (duplicate observed_set is a no-op, not a re-append), no
    # peer-shard write (fold reads peers, writes only its own shard — DEC-D),
    # no cross-repo write (DEC-11), no git commit issued by the handler.
    # Authority: docs/decisions/DR-241-sovereign-tracker-substrate-write-carveout.md
    #   § Amendment (2026-07-28) — the five-bound affirmation against real
    #   handler code this op's registration discharges.
    # Spec: docs/plans/2026-07-28-sat-01b-observed-set-fold-actuator.md § Tasks C5
    # ---------------------------------------------------------------------------
    "tracker.fold_observed_set": OpClass.MUTATING,
    # ---------------------------------------------------------------------------
    # tracker.mint_person — MUTATING. Own justification, not leaning on either
    # sibling tracker.* op's comment above (each explicitly disclaims being
    # precedent for a differently-shaped tracker.* op):
    #   1. Opens any file for write (including sentinel creation)?            YES.
    #      Appends a person_created event and one or more person_alias_added
    #      events to the calling repo's own sovereign-tracker event store via
    #      coordinator_core.tracker_entities.emit_person_created /
    #      emit_person_alias_added.
    #   2. Writes into rag's relational store?                                No.
    #      Writes only through tracker_entities' own emit functions, which are
    #      confined to the sovereign-tracker event store; dual-write ban
    #      satisfied.
    #   3. Mutates shared mutable state outside its own module?               YES.
    #      The person registry is read by handoff.normalize (minted_by
    #      resolution) and by any future consumer of resolve_alias/
    #      resolve_person.
    #   4. Persistent state changes observable across process boundaries?     YES.
    #      A future session/process resolves the minted person via
    #      tracker_projection.resolve_alias/resolve_person.
    #   5. Read-only, no side effects at all?                                 No.
    #      Question 1 alone already answers YES to "opens a file for write" —
    #      DR-208 §5's COMPUTE_ONLY checklist does not apply to a MUTATING op
    #      by construction (DR-241's Amendment corrects D1's original
    #      mis-citation of DR-208 §5; the affirmation actually owed is against
    #      DR-241 D2's own five bounds, discharged in
    #      coordinator_core/ops/tracker/mint_person.py's own module docstring
    #      compliance table).
    # Write confinement: per-repo only (DEC-11, PM ruling 2026-08-12) — no
    # write_root_for call, no cross-tree write, no claude-klabauter-tree default;
    # lock-free compare-and-retry idempotence, no second lock acquisition; no
    # anonymous person minted on an empty resolved alias bundle (DEC-41).
    # Authority: docs/decisions/DR-241-sovereign-tracker-substrate-write-carveout.md
    #   § D2 — Bounds of the sanction; § Amendment (2026-08-11) — the
    #   person-registry event handler affirmation this op's writes rely on.
    # Spec: docs/plans/2026-08-12-person-identity-primitive-first-slice.md § Tasks C4
    # ---------------------------------------------------------------------------
    "tracker.mint_person": OpClass.MUTATING,
    # ---------------------------------------------------------------------------
    # tracker.assign — MUTATING. Own justification, not leaning on
    # tracker.mint_person's comment above (each op is explicitly disclaimed
    # as precedent for a differently-shaped tracker.* op):
    #   1. Opens any file for write (including sentinel creation)?            YES.
    #      Appends an item_person_added or item_person_retracted event to
    #      the calling repo's own sovereign-tracker event store via
    #      coordinator_core.tracker_entities.emit_item_person_added /
    #      emit_item_person_retracted.
    #   2. Writes into rag's relational store?                                No.
    #      Writes only through tracker_entities' own emit functions, which
    #      are confined to the sovereign-tracker event store; dual-write ban
    #      satisfied.
    #   3. Mutates shared mutable state outside its own module?               YES.
    #      The item_person edge is read by any future consumer of the
    #      sat-02 relational-spine fold (item<->person membership).
    #   4. Persistent state changes observable across process boundaries?     YES.
    #      A future session/process reads the assigned/retracted edge back
    #      through the sat-02 fold surface.
    #   5. Read-only, no side effects at all?                                 No.
    #      Question 1 alone already answers YES to "opens a file for write" —
    #      DR-208 §5's COMPUTE_ONLY checklist does not apply to a MUTATING op
    #      by construction.
    # Write confinement: per-repo only (DEC-11, PM ruling 2026-08-12), same
    # bound tracker.mint_person carries — no write_root_for call, no
    # cross-tree write, no claude-klabauter-tree default. All role validation
    # (ITEM_PERSON_ROLES), the duplicate-triple refusal (AC9), applied_at
    # stamping (DEC-19), and event-id minting (DEC-20) stay in
    # tracker_entities.py — this op is a caller, not a reimplementation.
    # Spec: state/dispatch-briefs/2026-08-19-the-tracker-names-an-owner/C2.md
    # ---------------------------------------------------------------------------
    "tracker.assign": OpClass.MUTATING,
    # ---------------------------------------------------------------------------
    # tracker.render_status — MUTATING, by C2's ruling (sat-06 chunk C2, DR-241
    # amendment dated 2026-08-20), NOT a descriptive claim about this one op's
    # own read-only handler body:
    #   1. Opens any file for write (including sentinel creation)?            No.
    #      coordinator_core.ops.tracker.render_status delegates entirely to
    #      coordinator_core.tracker_projection.render_status, a pure read-time
    #      fold over tracker_store.read_events. Nothing in this op's own
    #      handler code opens a file for write.
    #   2. Writes into rag's relational store?                                No.
    #   3. Mutates shared mutable state outside its own module?               No.
    #   4. Persistent state changes observable across process boundaries?     No.
    #   5. Read-only, no side effects at all?                                 YES,
    #      on the merits of this op's own handler body alone.
    #   Despite a clean YES to question 5, this op is classified MUTATING —
    #   DR-241's Amendment (2026-08-20) rules the `tracker.` prefix's
    #   COMPUTE_ONLY carve-out conservative-by-construction rather than
    #   descriptive: no live claude-klabauter-internal consumer of `render_status`
    #   exists at HEAD (only a benchmark harness, test_tracker_projection, and
    #   assertions inside test_tracker_completion_policy reference it, and
    #   tracker_completion_policy itself does not call render_status), so no
    #   COMPUTE_ONLY exception was granted. See
    #   `coordinator_core/tests/test_tracker_store.py`'s
    #   `TestAffirmationEraBoundedRegistrationGuard::
    #   test_tracker_ops_are_classified_mutating_not_compute_only`, which
    #   asserts every `OP_CLASSIFICATION` key beginning `tracker.` is
    #   `OpClass.MUTATING` under DR-241 by construction.
    # Authority: docs/decisions/DR-241-sovereign-tracker-substrate-write-carveout.md
    #   § Amendment (2026-08-20) — the conservative-by-construction ruling this
    #   op's registration discharges.
    # Spec: docs/plans/2026-08-18-sat-06-cockpit-consumption-seam.md § Tasks C2/C3
    # ---------------------------------------------------------------------------
    "tracker.render_status": OpClass.MUTATING,
    # ---------------------------------------------------------------------------
    # tracker.assert_code_complete — MUTATING (C11, DR-318 §D2's routed op
    # surface for sat-04's tracker_completion_policy). Unlike
    # tracker.render_status this is not conservative-by-construction — this
    # op's own handler body performs a real write on every call, via
    # tracker_completion_policy.emit_code_complete_assert ->
    # tracker_transitions.emit_transition -> _emit (tracker_store.
    # append_event). A clean YES to DR-208 §5 question 1 ("opens a file for
    # write") by construction.
    # Authority: docs/decisions/DR-318-sat-04-completion-axis-policy-reachabili.md
    #   § D2 — the routed obligation this op discharges.
    # Spec: docs/plans/2026-08-18-sat-06-cockpit-consumption-seam.md § Tasks C11
    # ---------------------------------------------------------------------------
    "tracker.assert_code_complete": OpClass.MUTATING,
    # ---------------------------------------------------------------------------
    # tracker.push_suggestion — MUTATING (C4, DR-338's delivery op — sat-06's
    # producer-facing write seam). Not conservative-by-construction: every
    # call performs a real write, either tracker_store.append_event (local
    # arm) or a committed file write into a peer repo's cross-repo/inbox/
    # (peer arm, DR-338 D1(a)/D2). DR-208 §5 checklist against this op's own
    # handler body:
    #   1. Opens any file for write (including append)?             YES —
    #      tracker_store.append_event (local) / _write_envelope_file (peer).
    #   2. Calls any git write command?                              YES —
    #      _commit_envelope (peer arm).
    #   3. Enqueues any state mutation?                              No.
    #   4. Invokes a subprocess that may do any of the above?        YES —
    #      _commit_envelope's subprocess.run(["git", ...]).
    #   5. I/O behavior conditional (reads under some paths, writes under
    #      others)?                                                  YES —
    #      the local/peer fork is itself conditional I/O routing.
    # Authority: docs/decisions/DR-338-tracker-event-delivery-cross-tree-write.md § 2.
    # Spec: docs/plans/2026-08-18-sat-06-cockpit-consumption-seam.md § Tasks C4
    # ---------------------------------------------------------------------------
    "tracker.push_suggestion": OpClass.MUTATING,
    # ---------------------------------------------------------------------------
    # tracker.fold_ownership — MUTATING, by the SAME DR-241 amendment
    # (2026-08-20) tracker.render_status discharges above, NOT a descriptive
    # claim about this one op's own read-only handler body:
    #   1. Opens any file for write (including sentinel creation)?            No.
    #      coordinator_core.ops.tracker.fold_ownership delegates entirely to
    #      coordinator_core.tracker_projection.fold_person_membership /
    #      fold_person_registry, pure read-time folds. Nothing in this op's
    #      own handler code opens a file for write.
    #   2. Writes into rag's relational store?                                No.
    #   3. Mutates shared mutable state outside its own module?               No.
    #   4. Persistent state changes observable across process boundaries?     No.
    #   5. Read-only, no side effects at all?                                 YES,
    #      on the merits of this op's own handler body alone.
    #   Despite a clean YES to question 5, this op is classified MUTATING —
    #   DR-241's Amendment (2026-08-20) rules the `tracker.` prefix's
    #   COMPUTE_ONLY carve-out conservative-by-construction rather than
    #   descriptive: this is a brand-new external-consumer-facing surface
    #   with no live claude-klabauter-internal consumer at registration time, so no
    #   COMPUTE_ONLY exception is granted (same bar tracker.render_status was
    #   held to). See the sovereign-tracker substrate test module's
    #   `TestAffirmationEraBoundedRegistrationGuard::
    #   test_tracker_ops_are_classified_mutating_not_compute_only`, which
    #   asserts every `OP_CLASSIFICATION` key beginning `tracker.` is
    #   `OpClass.MUTATING` under DR-241 by construction.
    # Authority: docs/decisions/DR-241-sovereign-tracker-substrate-write-carveout.md
    #   § Amendment (2026-08-20) — the conservative-by-construction ruling this
    #   op's registration discharges.
    # Spec: state/dispatch-briefs/2026-08-19-the-tracker-names-an-owner/C3.md
    # ---------------------------------------------------------------------------
    "tracker.fold_ownership": OpClass.MUTATING,
    # priority.set — MUTATING: the sole writer of a priority-ledger entry
    # (<central-state>/priority-ledger/<target_id>.yaml). See
    # coordinator_core/ops/priority_set.py module docstring.
    "priority.set": OpClass.MUTATING,
    # priority.drain — MUTATING: drains example-cockpit-repo's priority-intent
    # inbox, applying each valid record through priority_set.set_priority()
    # (still the sole ledger writer) and moving every processed record file.
    # See coordinator_core/ops/priority_drain.py module docstring.
    "priority.drain": OpClass.MUTATING,
    # ---------------------------------------------------------------------------
    # diagnostics.* — the three write-free transport-failure probes
    # (coordinator_core/ops/diagnostics_probes.py). COMPUTE_ONLY is the
    # substantive claim here, not a formality: `op_scopes` carries "none" for
    # these, but that field keys REPO STATE, not write-freedom
    # (install.write_identity_file is also "none" and writes a file). This
    # entry is what carries the write-free property onto the authz surface,
    # and it is the entire reason the family may be fired at a live, dirty,
    # shared working tree.
    #
    # DR-208 five-question affirmation, derived by reading the module (all
    # three handlers share one affirmation because they share one body shape:
    # _always_succeeds returns a dict literal; _always_refuses and
    # _always_structural_pin each raise a module-local exception constructed
    # from a literal string — there is no third statement in any of them):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  No.
    #      No handler calls anything: no callee at all, so no transitive write
    #      surface to audit. The module's only import beyond typing/pathlib is
    #      `register_op`.
    #   2. Writes into rag's relational store?                                 No.
    #      No store client is imported or reachable. Dual-write ban trivially
    #      satisfied.
    #   3. Opens any file for write (including sentinel creation)?             No.
    #      No open(), no pathlib write, no tempfile, no os.replace. `Path` is
    #      imported for the `repo_root: Optional[Path]` signature-parity
    #      annotation only and is never constructed or dereferenced.
    #   4. Mutates shared mutable state outside its own module?                No.
    #      `params` and `repo_root` are accepted and deliberately ignored — not
    #      read, not echoed, not mutated. The two exception classes are
    #      module-local; `DiagnosticsStructuralPin.structurally_wedged` is a
    #      class attribute set at definition time, not mutated per call.
    #   5. Persistent state changes observable across process boundaries?      No.
    #      The only effects are the returned dict and the two raised
    #      exceptions, both of which die with the invoke child's stdout.
    #   Behaviour is unconditional in both directions: no param, env var, or
    #   repo state can change what any of the three does — see the module's
    #   negative-spec block, which forbids acquiring "useful" behaviour at all.
    # Spec: docs/plans/2026-08-07-safe-target-for-transport-failure-probes.md § C1b
    # ---------------------------------------------------------------------------
    "diagnostics.always_succeeds": OpClass.COMPUTE_ONLY,
    "diagnostics.always_refuses": OpClass.COMPUTE_ONLY,
    "diagnostics.always_structural_pin": OpClass.COMPUTE_ONLY,
    # scratchpad.sweep — MUTATING: reclaims (shutil.rmtree) dead harness
    # scratchpad directories under the OS temp root (ops/scratchpad_sweep.py).
    # Not a write to "coordinator substrate" in this module's literal sense
    # (state files, queues, git objects) — the target is ephemeral per-session
    # harness scratch, entirely outside state/ and outside any git repo — but
    # a real, irreversible disk-delete op is exactly DR-208's named ambiguous
    # case ("cache writes, lock files, temp files, advisory markers" -> MUTATING,
    # fail-closed), and dry_run defaults True with reclaim:true as the sole
    # destructive opt-in, so the classification must cover the destructive path.
    # DR-208 five-question affirmation (citing ops/scratchpad_sweep.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?
    #      Deletes directories, but under the OS temp root, never under state/
    #      or any tracked git path — the letter of Q1 is No, but the op is a
    #      real irreversible delete, so it is classified as if Q1 were Yes
    #      (see the ambiguous-case rationale above).
    #   2. Writes into rag's relational store?                                 No.
    #   3. Opens any file for write (including sentinel creation)?             No.
    #      Only shutil.rmtree (delete), never an open-for-write.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      Deletes directories a live harness session may still read from,
    #      shared across every session on this machine.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      A deleted scratchpad directory is gone for every future reader.
    # Two-gate safety (liveness AND age, both must hold before any delete) and
    # the per-project-slug liveness-scope fix are documented in the module's
    # own docstring — not restated here; this entry is the classification
    # affirmation only.
    # No local precedent: the closest sibling sweep, agent_worktree_sweep.py,
    # is consumed by direct import and appears in neither this table nor
    # ops/__init__.py/_registry_map.py — scratchpad.sweep is the first sweep
    # op registered for JSON-RPC dispatch, not a copy of an existing pattern.
    "scratchpad.sweep": OpClass.MUTATING,
    # dispatch.emit — MUTATING: writes the composed Workflow .mjs script text
    # to a caller-named path (ops/dispatch_emit/op.py). The only handler in
    # the dispatch-emit pipeline that touches disk — every upstream module
    # (spine_read.py, wave_map.py, pathspec.py, emit.py) is pure/COMPUTE_ONLY
    # in nature and none is separately registered as a JSON-RPC op.
    # DR-208 five-question affirmation (citing ops/dispatch_emit/op.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      op.py — guarded_path.write_text(script, encoding="utf-8") writes the
    #      emitted script to the caller-named, path-guarded output_path.
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only the single target output_path; no rag store write.
    #      Dual-write ban (DR-208 / tri-plane DD#1) satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      guarded_path.write_text(...) opens the target for write.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      output_path is caller-named repo/state, not scoped to this module's
    #      own package directory.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The written script is a durable file, readable by other sessions
    #      and by the Workflow tool once dispatched.
    #   Path containment: output_path is guarded via
    #   coordinator_core.ops._path_guard.contained_path against target_root
    #   (defaults to output_path's parent) BEFORE the write — same seam most
    #   write-ops in this package family use; see op.py module docstring.
    # Spec: docs/plans/2026-08-12-emitter-turns-a-spine-into-one-workflow.md § C5
    "dispatch.emit": OpClass.MUTATING,
    # workflow.fire — MUTATING: spawns one detached `claude -p` child per emitted
    # workflow script and writes the fire registry record that IS the run handle
    # (ops/workflow_fire/op.py -> fire.py::fire_workflow).
    # DR-208 five-question affirmation (citing ops/workflow_fire/fire.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      _write_record(...) persists the registry record under
    #      <git-common-dir>/coordinator-sessions/workflow-fires/<fire_id>.json, plus
    #      the cap lock (_acquire_cap_lock) and the child's log file.
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only its own registry directory; no rag interaction. Dual-write
    #      ban (DR-208 / tri-plane DD#1) satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      The record, the cap lock, and the spawned child's log are all created.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      The registry is common-dir-scoped and shared by every session on the
    #      repo — the live-fire concurrency cap is counted over that population.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      A detached OS process survives the call, and its record is readable by
    #      any other session.
    # Classified MUTATING per DR-208 § "Default: MUTATING until affirmed otherwise";
    # no COMPUTE_ONLY affirmation is possible for a handler that spawns and writes.
    # Spec: docs/plans/2026-08-18-claude-klabauter-fires-the-workflows-it-emits.md § C4
    "workflow.fire": OpClass.MUTATING,
    # workflow.fire_status — MUTATING: the REFRESHING read. It re-reads a record by
    # fire_id and, whenever the refreshed state differs, writes it back
    # (fire.py::fire_status -> _write_record) — the record is not written when the
    # child exits, so this op is what durably records a run's terminal state.
    # DR-208 five-question affirmation (citing ops/workflow_fire/fire.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      _write_record(path, refreshed) rewrites the registry record in place.
    #   2. Writes into rag's relational store?                                 No.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      Same _write_record path as above.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      The record it rewrites is common-dir-scoped, shared fleet-wide.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The refreshed state is what every later reader sees.
    # A read-shaped NAME is not a read-only handler — the refresh is the point.
    # Spec: docs/plans/2026-08-18-claude-klabauter-fires-the-workflows-it-emits.md § C4
    "workflow.fire_status": OpClass.MUTATING,
    # review.mint_workflow — MUTATING: writes the composed gated-review
    # Workflow .mjs script text to a caller-named path (ops/review_mint/op.py).
    # The only handler in this plan's surface that touches disk; roster.py
    # and compose.py are pure. DR-208 five-question affirmation (citing
    # ops/review_mint/op.py):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
    #      op.py — guarded_path.write_text(script, encoding="utf-8") writes the
    #      emitted script to the caller-named, path-guarded output_path.
    #   2. Writes into rag's relational store?                                 No.
    #      Writes only the single target output_path; no rag store write.
    #      Dual-write ban (DR-208 / tri-plane DD#1) satisfied.
    #   3. Opens any file for write (including sentinel creation)?             YES.
    #      guarded_path.write_text(...) opens the target for write.
    #   4. Mutates shared mutable state outside its own module?                YES.
    #      output_path is caller-named repo/state, not scoped to this module's
    #      own package directory.
    #   5. Persistent state changes observable across process boundaries?     YES.
    #      The written script is a durable file, readable by other sessions
    #      and by the Workflow tool once dispatched.
    #   Path containment: output_path is guarded via
    #   coordinator_core.ops._path_guard.contained_path against target_root
    #   (defaults to output_path's parent) BEFORE the write -- same seam
    #   dispatch.emit above uses; see op.py module docstring.
    # Spec: docs/plans/2026-08-19-review-mints-its-own-gated-workflow.md § C3
    "review.mint_workflow": OpClass.MUTATING,
    # gate.validate_invocable — MUTATING: merge-gate DoD checker
    # (ops/gate_validate_invocable.py). DR-208 five-question affirmation:
    #   1. Does the handler open any file for write (including append)?          YES.
    #      Not the handler itself, but the module unconditionally imports
    #      gate_dimension_types/_docstrings/_review/_latency at its own
    #      bottom-of-module import block, and gate_dimension_types.py's
    #      `_run_mypy` spawns `subprocess.run([mypy_path, ...])` with no
    #      `--cache-dir`/`--no-incremental`; mypy's default incremental mode
    #      writes a `.mypy_cache/` directory as a side effect of a normal run.
    #   2. Does the handler call any git write command?                         No.
    #   3. Does the handler enqueue any state mutation (queue/backlog/etc.)?     No.
    #   4. Does the handler invoke any subprocess that may do any of the above?  YES.
    #      gate_dimension_latency.py's own module docstring already says this
    #      op "shells mypy, ruff, interrogate, a pytest --cov run, and
    #      diff-cover" — a real subprocess spawn plus a disk cache write.
    #   5. Is the handler's I/O behavior conditional (reads under some paths,
    #      writes under others)?                                                YES.
    #      Whether a given invocation spawns mypy depends on which dimension
    #      checks run/are registered; the write is not guaranteed on every
    #      call but is not ruled out either.
    #   Review: coordinator:code-reviewer (wsc-D-registration) — the C1-era
    #   affirmation above was written before C2/C3/C5/C7 landed their real
    #   dimension implementations in this tree; those implementations are
    #   already unconditionally imported by this module (not deferred), so a
    #   COMPUTE_ONLY label here is a silently-widened permission. DR-208's
    #   fail-closed direction is MUTATING when a write cannot be ruled
    #   out — here it is demonstrated, not merely unruled-out.
    # Spec: docs/plans/2026-07-20-merge-gate-dod-engine-enforced.md § C1
    "gate.validate_invocable": OpClass.MUTATING,
    # gate_liveness.resolve — pure read + compute: joins external_gate[].
    # closure_key entries against cross-repo/inbox/ and cross-repo/archive/
    # memos' discharges: blocks. Never writes a file, never mutates plan
    # frontmatter (the schema's own description: "a reader ... may PROPOSE
    # the cleared: true flip with a citation — it never performs it").
    # Spec: docs/plans/2026-08-21-a-discharged-gate-tells-the-row-waiting.md § C1
    "gate_liveness.resolve": OpClass.COMPUTE_ONLY,
    # gate_liveness.reconcile — writes ONLY when apply: true is explicitly
    # passed (default False = dry run, proposes flips, writes nothing).
    # Classified MUTATING unconditionally: DR-208's fail-closed direction
    # applies to the op's CAPABILITY, not today's default param value — an
    # op that CAN write under a caller-supplied flag is MUTATING regardless
    # of how often it is invoked with that flag false.
    # Spec: docs/plans/2026-08-21-a-discharged-gate-tells-the-row-waiting.md § C2
    "gate_liveness.reconcile": OpClass.MUTATING,
    # ---------------------------------------------------------------------------
    # C9 leg (a) registration-quad gap sweep (2026-08-14) — 56 ops discovered by
    # gen_dod_backlog_fragment.py's build_rows() with classification: null.
    # Each entry below is a DR-208 five-question affirmation citing the handler
    # module named in its comment; grouped by shared write/read shape rather
    # than op-key alphabetical order. 6 further ops (percolate.run_ci_smoke_check
    # plus 5 already-covered) were left unfixed as genuinely ambiguous — see the
    # dispatching handoff/session report, not restated here.
    # ---------------------------------------------------------------------------
    # COMPUTE_ONLY group — every handler below is a pure read (git query,
    # in-memory computation, or explicit negative-spec) with no open(...,"w"),
    # no os.replace/rename/rmdir, no locked_rmw, and no subprocess call that
    # writes coordinator substrate. Five-question answers are uniformly No/No/
    # No/No/No for this group; per-op citation of the read-only evidence below.
    #   baton.resolve_path_and_repo — ops/resolve_baton_path.py: git rev-parse /
    #     remote-url reads only.
    #   baton.resolve_swept_in_archive — ops/resolve_swept_baton.py: `git log`
    #     read via subprocess.run, no write.
    #   bug_sweep.verify_fix_files_changed — ops/verify_fix_files_changed.py:
    #     `git diff` file-list read via subprocess.run, no write.
    #   cartography.count_references — ops/cartography_edges.py: pure in-memory
    #     reference counting, no I/O primitive at all.
    #   cartography.stack — ops/cartography_stack.py: pure in-memory stack
    #     derivation, no I/O primitive at all.
    #   ceremony.init_anchor_injection_state — ops/init_anchor_injection_state.py:
    #     module docstring/negative-spec is explicit — "Does NOT write any file
    #     — this is a pure in-memory resolve/init step."
    #   cli.parse_date_flags / cli.parse_flag — ops/parse_cli_args.py: pure
    #     argv/string parsing, no I/O primitive at all.
    #   dependency.detect_changed_manifests —
    #     ops/detect_changed_dependency_manifests.py: `git diff` read via
    #     subprocess.run, no write.
    #   detect.plugin_layout / detect.primary_languages —
    #     ops/detect_plugin_layout.py, ops/detect_primary_languages.py: read-only
    #     directory/file inspection, no I/O primitive that opens for write.
    #   doctrine.assert_cross_reference_counts —
    #     ops/assert_doctrine_cross_reference_counts.py: read-only count/assert,
    #     no I/O primitive that opens for write.
    #   fanout.poll_scratch_dir — ops/poll_scratch_dir.py: read-only directory
    #     listing/poll, no write.
    #   git_branch.compute_descendant_tip / git_branch.detect_unpushed_commits /
    #   git_branch.list_unmerged_work / git_branch.verify_commit_in_review_window
    #     — ops/orphan_branch_sweep.py: each registered handler (verified against
    #     the handler function specifically, not the module's `main()` sweep,
    #     which does hold branch-delete logic under a different, unregistered
    #     code path) routes only through `_git`/`_rev_parse`/`_rev_list_count`/
    #     `_is_ancestor` — read-only git-query helpers; no `git branch -D`,
    #     `git push`, or any write call reachable from these four handlers.
    #   install.detect_cmd_autorun_coverage — ops/cmd_autorun_guard.py
    #     `_detect_handler`: calls only `detect_cmd_autorun_coverage()` (read-only
    #     probe of HKCU AutoRun); the module's write/strip verbs are separate
    #     handlers (see MUTATING group below), not reachable from this one.
    #   install.detect_python3_appx_stub — ops/ensure_python3_exe_shim.py
    #     `_detect_python3_appx_stub`: calls only `_classify_python3()` (read-only
    #     PATH/exe inspection); `_install_shim()` is a distinct, unregistered
    #     function, not reachable from this handler.
    #   lessons.filter_undated_universal / lessons.reject_orphan_strip_entries —
    #     ops/lessons_filter.py: read-only filtering over in-memory record lists,
    #     no I/O primitive that opens for write.
    #   mcp.resolve_server_cli_path — ops/resolve_mcp_server_cli_path.py:
    #     read-only path resolution, no write.
    #   merge.quiet_activity_gate — ops/merge_quiet_activity_gate.py: `git log`
    #     epoch-seconds read via subprocess.run, no write.
    #   percolate.check_inverse_drift — ops/percolate_check_inverse_drift.py:
    #     `git` read-only queries via subprocess.run wrapper, no write.
    #   percolate.list_files_newer_than_marker —
    #     ops/list_files_newer_than_marker.py: read-only mtime comparison over a
    #     directory listing, no write.
    #   percolate.run_pre_ci_hooks — ops/run_pre_ci_hooks.py: subprocess.run of
    #     an external hook script for its exit code / output only; no file the
    #     handler itself opens for write.
    #   percolate.scan_content_leakage_tiers — ops/scan_content_leakage.py:
    #     read-only content scan, no write.
    #   plan.list_stale_executing — ops/draft_plan_aging.py: `git log` recency
    #     read via subprocess.run to list (not mutate) stale plans, no write.
    #   repo_setup.validate_target_root — ops/bootstrap_repo.py
    #     `_validate_target_root_op`: docstring is explicit — "Idempotency (AC7):
    #     purely read-only (dir-exists + is-a-git-repo check, no writes
    #     anywhere)"; the module's `main()` onboarding flow (which does commit)
    #     is a distinct, unregistered code path.
    #   research.verify_scout_inventory_completeness —
    #     ops/verify_scout_inventory_completeness.py: read-only inventory
    #     completeness check, no write.
    #   schema.drift_gate — ops/schema_drift_gate.py: read-only schema-drift
    #     comparison, no write.
    #   session.resolve_chain_terminal_disposition —
    #     ops/session/resolve_chain_terminal_disposition.py: `git` read-only
    #     query via subprocess.run, no write.
    #   update_docs.probe_fresh_repo_noop — ops/probe_fresh_repo_noop.py:
    #     handler name and module purpose are both explicit — a literal no-op
    #     probe, no write.
    #   workday.surface_auto_push_failure_stats —
    #     ops/workday_surface_auto_push_failure_stats.py: read-only stats
    #     surfacing over existing records, no write.
    #   ci.run_pip_audit / ci.run_semgrep_scan — ops/run_pip_audit.py,
    #     ops/run_semgrep_scan.py: subprocess.run of an external audit/scan
    #     binary against the repo tree for its JSON/text report only; neither
    #     handler opens a file for write (contrast ci.run_shellcheck_sweep below,
    #     which does and is classified MUTATING on that basis).
    "baton.resolve_path_and_repo": OpClass.COMPUTE_ONLY,
    "baton.resolve_swept_in_archive": OpClass.COMPUTE_ONLY,
    "bug_sweep.verify_fix_files_changed": OpClass.COMPUTE_ONLY,
    "cartography.count_references": OpClass.COMPUTE_ONLY,
    "cartography.stack": OpClass.COMPUTE_ONLY,
    "ceremony.init_anchor_injection_state": OpClass.COMPUTE_ONLY,
    "cli.parse_date_flags": OpClass.COMPUTE_ONLY,
    "cli.parse_flag": OpClass.COMPUTE_ONLY,
    "dependency.detect_changed_manifests": OpClass.COMPUTE_ONLY,
    "detect.plugin_layout": OpClass.COMPUTE_ONLY,
    "detect.primary_languages": OpClass.COMPUTE_ONLY,
    "doctrine.assert_cross_reference_counts": OpClass.COMPUTE_ONLY,
    "fanout.poll_scratch_dir": OpClass.COMPUTE_ONLY,
    "git_branch.compute_descendant_tip": OpClass.COMPUTE_ONLY,
    "git_branch.detect_unpushed_commits": OpClass.COMPUTE_ONLY,
    "git_branch.list_unmerged_work": OpClass.COMPUTE_ONLY,
    "git_branch.verify_commit_in_review_window": OpClass.COMPUTE_ONLY,
    "install.detect_cmd_autorun_coverage": OpClass.COMPUTE_ONLY,
    "install.detect_python3_appx_stub": OpClass.COMPUTE_ONLY,
    "lessons.filter_undated_universal": OpClass.COMPUTE_ONLY,
    "lessons.reject_orphan_strip_entries": OpClass.COMPUTE_ONLY,
    "mcp.resolve_server_cli_path": OpClass.COMPUTE_ONLY,
    "merge.quiet_activity_gate": OpClass.COMPUTE_ONLY,
    "percolate.check_inverse_drift": OpClass.COMPUTE_ONLY,
    "percolate.list_files_newer_than_marker": OpClass.COMPUTE_ONLY,
    "percolate.run_pre_ci_hooks": OpClass.COMPUTE_ONLY,
    "percolate.scan_content_leakage_tiers": OpClass.COMPUTE_ONLY,
    "plan.list_stale_executing": OpClass.COMPUTE_ONLY,
    "repo_setup.validate_target_root": OpClass.COMPUTE_ONLY,
    "research.verify_scout_inventory_completeness": OpClass.COMPUTE_ONLY,
    "schema.drift_gate": OpClass.COMPUTE_ONLY,
    "session.resolve_chain_terminal_disposition": OpClass.COMPUTE_ONLY,
    "update_docs.probe_fresh_repo_noop": OpClass.COMPUTE_ONLY,
    "workday.surface_auto_push_failure_stats": OpClass.COMPUTE_ONLY,
    "ci.run_pip_audit": OpClass.COMPUTE_ONLY,
    "ci.run_semgrep_scan": OpClass.COMPUTE_ONLY,
    # MUTATING group — each handler below writes, deletes, or reorders disk
    # state (coordinator substrate, a scratch/target repo tree, or an external
    # system the operator's own machine hosts); per-op citation follows.
    #   branch.merge_into_workstream — ops/merge_branch_into_workstream.py:
    #     `git merge` write via subprocess.run — mutates a branch ref.
    #   ceremony.scoped_git_commit — ops/ceremony/scoped_git_commit.py: the
    #     module's entire purpose is a path-scoped `git add && git commit`;
    #     the file's own docstring is explicit throughout.
    #   ceremony.commit — REMOVED (C3, docs/plans/2026-08-29-the-push-
    #     subsystem-leaves-and-then-the-pipeline-can-go.md): this op has no
    #     dict entry any more, and this comment no longer has one to
    #     justify. `ceremony.commit` (ops/ceremony/commit_op.py) was killed
    #     at the 200ms process-time bar (p50 421.9ms, n=241) and replaced by
    #     `ceremony.commit_v2` (see that entry, immediately above this
    #     block) -- `commit_op.py` is now a 101-line husk with no handler,
    #     unregistered, and nothing here classifies it. The original
    #     warrant (docs/plans/2026-08-26-the-commit-becomes-a-warm-served-
    #     op.md § AC2, classifying MUTATING because the op delegated to
    #     `run_commit_pipeline`'s stage+commit(+push) write) applied to a
    #     dict entry that is gone; do not resurrect it from this text.
    #   commit.exec_bit_change — ops/ceremony/commit_exec_bit.py: `git commit`
    #     (unrestricted, per DR-151) writing an exec-bit change.
    #   findings.self_persist_fallback — ops/self_persist_findings.py: writes
    #     via `coordinator_core.locked_write.locked_rmw` (mkstemp + os.replace);
    #     module docstring is explicit this is the native port of a
    #     Path.write_text-shaped fallback.
    #   fleet.archive_paper_trail / fleet.archive_queue_entry /
    #   fleet.archive_release_accumulator — ops/fleet/archive_paper_trail.py,
    #     ops/fleet/archive_queue_entry.py, ops/fleet/archive_release_accumulator.py:
    #     all three delegate to `ops/fleet/_common.py::archive_and_commit`
    #     (git-mv + commit), the same DR-211-affirmed archival-writer primitive
    #     as fleet.archive_completed_plans / fleet.archive_completed_handoffs /
    #     fleet.prune_closed_bugs above — same classification for the same
    #     reason (D2 five-bound not re-affirmed here; the underlying shared
    #     helper's affirmation is the fleet.archive_completed_plans entry).
    #   fleet.migrate_handoff_vocabulary — ops/fleet/migrate_handoff_vocabulary.py:
    #     `open(rec["_abs_path"], "w", ...)` — direct in-place rewrite of
    #     handoff files.
    #   install.write_cmd_autorun_guard / install.strip_cmd_autorun_guard —
    #     ops/cmd_autorun_guard.py `_write_handler`/`_strip_handler`: write or
    #     delete the operator's own HKCU AutoRun registry value
    #     (`_write_autorun`/`_delete_autorun`) — not coordinator substrate, but
    #     an unambiguous disk/registry write, classified MUTATING on that basis.
    #   install.write_identity_file — ops/write_identity_file.py: writes via
    #     `locked_write.locked_rmw` (mkstemp + os.replace); module docstring
    #     confirms the same native Path.write_text-via-locked_rmw shape as
    #     findings.self_persist_fallback above.
    #   machine.hibernate — ops/hibernate_machine.py: DEFAULTED to MUTATING.
    #     The handler touches no coordinator substrate (module's own "Scope:
    #     none" note), so DR-208's literal substrate criterion reads No/No/No/
    #     No/No — but the op's entire purpose is an irreversible, machine-wide
    #     OS power-state action (hibernate/suspend), which is not "safe to
    #     expose to a read-only token" in the spirit OpClass exists to gate.
    #     Classified MUTATING as the fail-closed default rather than stretching
    #     COMPUTE_ONLY to cover a side effect DR-208's substrate wording does
    #     not contemplate either way — flagged, not silently assumed.
    #   release.cut_tag / release.cut_tag_and_publish — ops/release_tagging.py:
    #     `_cut_tag` creates a git tag (write); `_cut_tag_and_publish` additionally
    #     calls `_publish_release` (`gh release create`), an external write.
    #   repo.clone_and_register — ops/repo_bootstrap.py
    #     `clone_and_register_sibling_repo`: clones a repo to disk AND calls
    #     `_machine_local_set` (writes the machine-local registry) — two
    #     independent writes.
    #   repo.create_and_push_remote — ops/create_github_remote.py: creates a
    #     GitHub remote and pushes to it via subprocess.run — an external,
    #     irreversible write.
    #   repo_setup.copy_console_subprocess_tripwire — ops/copy_plugin_template.py:
    #     `shutil.copy2(template, dest)` — writes a file into the target tree.
    #   research.archive_workdir — ops/research_archive_workdir.py: EXDEV-safe
    #     move via `shutil.copytree(src, tmp)` + `shutil.rmtree(src)` — writes
    #     and deletes.
    #   review.snapshot_diff_and_head — ops/ceremony/snapshot_diff_and_head.py:
    #     `diff_path.write_text(...)` and `head_sha_path.write_text(...)` —
    #     direct writes of two snapshot sidecar files.
    #   session.rotate_orphan_sweep_log — ops/session/rotate_orphan_sweep_log.py:
    #     writes via `locked_write.locked_rmw` under a repo-root lock; module
    #     docstring confirms mkstemp + os.replace atomic swap.
    #   workday.stitch_sidecar_into_summary —
    #     ops/workday_stitch_sidecar_summary.py: writes via `locked_write.locked_rmw`
    #     AND `sidecar_p.unlink()` on success — a write plus a delete.
    #   ci.run_shellcheck_sweep — ops/run_shellcheck_sweep.py: DEFAULTED to
    #     MUTATING per this file's own established precedent
    #     (hooks.postuse_advisory_dispatch entry above) for DR-208 § 2's named
    #     ambiguous case — "cache writes, lock files, temp files, advisory
    #     markers" classify MUTATING, fail-closed. The handler opens a
    #     `tempfile.NamedTemporaryFile(mode="w", ...)` scratch file to feed
    #     shellcheck, then unlinks it in a `finally`; the write target is
    #     system tempdir, not coordinator substrate, but question 3 of the
    #     five-question checklist ("opens any file for write, including
    #     sentinel creation?") is YES regardless of the target's durability,
    #     the same reading this file already applied to context_pressure_
    #     precompact's tempdir sentinel above.
    "branch.merge_into_workstream": OpClass.MUTATING,
    "commit.exec_bit_change": OpClass.MUTATING,
    "findings.self_persist_fallback": OpClass.MUTATING,
    "fleet.archive_paper_trail": OpClass.MUTATING,
    "fleet.archive_queue_entry": OpClass.MUTATING,
    "fleet.archive_release_accumulator": OpClass.MUTATING,
    "fleet.migrate_handoff_vocabulary": OpClass.MUTATING,
    # fleet.archive_actioned_memos — MUTATING, and a DR-211 archival writer of the
    # same sub-category as fleet.archive_completed_handoffs above: _handle_act calls
    # _common.archive_and_commit, the batched os.replace-plus-_commit_via_head_spine
    # mover every fleet sweep now shares (ops/fleet/archive_actioned_memos.py). It
    # moves ALREADY-committed terminal memos and is never the first committer of a
    # memo.transition write (DR-273 terminal-committer contract, named in that
    # module's own docstring) — which bounds what it writes, not whether it writes.
    "fleet.archive_actioned_memos": OpClass.MUTATING,
    # fleet.archive_sweep_status — COMPUTE_ONLY despite the "archive" in the name:
    # it reports on the sweeps, it does not run one. `_handler` (ops/fleet/
    # sweep_status.py) reads _sweep_receipt.receipt_path and summarizes the rows;
    # repo_root is None degrades to an empty-and-healthy answer rather than raising.
    #   1. Writes/deletes/reorders any state file, queue, or git object?      No.
    #   2. Writes into rag's relational store?                                No.
    #   3. Opens any file for write (including sentinel creation)?            No.
    #   4. Mutates shared mutable state outside its own module?               No.
    #   5. Persistent state changes observable across process boundaries?     No.
    "fleet.archive_sweep_status": OpClass.COMPUTE_ONLY,
    # session.audit_unreapable — COMPUTE_ONLY: the read-only naming half of
    # session.reap, added because that op's contract is fixed at a count and an
    # operator diagnosing hub growth needs the names (ops/session/reap.py). Its own
    # docstring states the affirmation directly: "Mutates NOTHING: no move, no rm,
    # no `.last-reap` touch, no cadence gate."
    #   1. Writes/deletes/reorders any state file, queue, or git object?      No.
    #   2. Writes into rag's relational store?                                No.
    #   3. Opens any file for write (including sentinel creation)?            No.
    #   4. Mutates shared mutable state outside its own module?               No.
    #   5. Persistent state changes observable across process boundaries?     No.
    #      A directory listing plus one stat per entry; git_common_dir resolves by
    #      pure-Python upward walk, so not even a spawn.
    "session.audit_unreapable": OpClass.COMPUTE_ONLY,
    # handoff.reconcile_open — the op was CUT by the 200ms sweep (5,546ms CPU) and is
    # dead. `coordinator_core/ops/handoff_reconcile.py` (the module that carried its
    # surviving compute as a library, reached only by the now-also-deleted
    # `handoff_housekeeping.py`) was deleted outright once C7's replacement
    # (`coordinator_core/housekeeping/cycle.py`) proved out — kill means kill forever
    # (PM 2026-08-23); this key never comes back and neither does that module. Do not
    # classify it, and do not add it to OP_MODULE_MAP/_OP_KEY_SCOPE/_EAGER_OP_MODULES —
    # there is no module left for such an entry to import.
    # fleet.mode_set / fleet.mode_show — the fleet-scoped settings plane's human-invoked
    # half (ops/fleet/mode_control.py; plan 2026-08-28-the-fleet-gets-one-file-and-the-
    # floor-moves-to-the-reader § C4). The pair splits across the class line because one
    # writes the record and one only renders it.
    #
    # NOT a DR-211 archival-writer. That sub-category exists for ops writing RESERVED
    # SUBSTRATE NOUNS (the archive/ tree plus a scoped git commit), and its five-bound
    # applies there. The fleet record is neither: session/fleet_mode.py resolves it
    # under `_settings_home.settings_home()`, outside every git worktree, touched by no
    # git object and covered by no substrate reservation. Classifying it MUTATING is the
    # write-semantics call below, not an invocation of DR-211 § D2.
    #
    # fleet.mode_set — MUTATING: `set_fleet_mode_key` read-modify-writes the record
    # through C1's atomic `write_fleet_mode`. Fails Q1/Q3/Q5 of the DR-208 five-question
    # test outright, so no COMPUTE_ONLY affirmation is available:
    #   1. Writes/deletes/reorders any state file, queue, or git object?      YES —
    #      write_fleet_mode() atomically replaces the settings-home record.
    #   2. Writes into rag's relational store?                                No.
    #   3. Opens any file for write (including sentinel creation)?            YES.
    #   4. Mutates shared mutable state outside its own module?               No.
    #   5. Persistent state changes observable across process boundaries?     YES —
    #      that is the op's entire purpose: every session on the box resolves against
    #      the record this op writes.
    "fleet.mode_set": OpClass.MUTATING,
    # fleet.mode_show — COMPUTE_ONLY: `show_fleet_mode` renders one `read_fleet_mode()`
    # against the local `_KNOWN_KEYS` table and returns it. Same DR-208 five-question
    # posture as fleet.handoffs_for_plan:
    #   1. Writes/deletes/reorders any state file, queue, or git object?      No.
    #      No open(..., "w")/os.replace/git-write call reachable from the handler; it
    #      does not call write_fleet_mode, and read_fleet_mode never creates the file
    #      it fails open on.
    #   2. Writes into rag's relational store?                                No.
    #   3. Opens any file for write (including sentinel creation)?            No.
    #   4. Mutates shared mutable state outside its own module?               No.
    #   5. Persistent state changes observable across process boundaries?     No.
    "fleet.mode_show": OpClass.COMPUTE_ONLY,
    "install.write_cmd_autorun_guard": OpClass.MUTATING,
    "install.strip_cmd_autorun_guard": OpClass.MUTATING,
    "install.write_identity_file": OpClass.MUTATING,
    "machine.hibernate": OpClass.MUTATING,
    "release.cut_tag": OpClass.MUTATING,
    "release.cut_tag_and_publish": OpClass.MUTATING,
    "repo.clone_and_register": OpClass.MUTATING,
    "repo.create_and_push_remote": OpClass.MUTATING,
    "repo_setup.copy_console_subprocess_tripwire": OpClass.MUTATING,
    "research.archive_workdir": OpClass.MUTATING,
    "review.snapshot_diff_and_head": OpClass.MUTATING,
    "session.rotate_orphan_sweep_log": OpClass.MUTATING,
    "workday.stitch_sidecar_into_summary": OpClass.MUTATING,
    "ci.run_shellcheck_sweep": OpClass.MUTATING,
    # research.restructure_for_repeat_topic — ops/research_dir_restructure.py:
    # `os.rename` moves both the dated result markdown and the archived
    # paper-trail dir; module docstring confirms this is a native replacement
    # for a shell `mv`-based fence. MUTATING.
    "research.restructure_for_repeat_topic": OpClass.MUTATING,
    # C17 (docs/plans/2026-08-20-a-refusal-cannot-exit-zero.md) — the 14
    # registered-but-unclassified ops closing the OP_CLASSIFICATION gap
    # against ipc._REGISTRY (measure after `import coordinator_core.ops`,
    # never on a bare import — see coordinator_core/tests/
    # test_registration_quad_complete.py).
    #   app_session.census — ops/app_session.py `_census`: reads persisted
    #     launch-handle JSON files only (`_read_handle`/`_list_handles`);
    #     re-validates PID liveness in-memory, never writes.
    #   app_session.launch — ops/app_session.py `_launch`: spawns a process
    #     via `subprocess.Popen` and writes a persisted handle JSON file
    #     (`_write_handle`).
    #   app_session.teardown — ops/app_session.py `_teardown`: signals
    #     (terminates) a spawned process and deletes its persisted handle
    #     file (`_remove_handle`).
    #   fleet.backfill_reference_edges — ops/backfill_reference_edges.py:
    #     stamps a `references:` frontmatter field via `locked_rmw` on every
    #     live handoff/plan match (dry_run defaults True, but `dry_run: false`
    #     performs real writes — the op CAN write).
    #   install.clone_idempotent — install/clone_sibling_repo.py
    #     `clone_idempotent`: `target.parent.mkdir(...)` + `git clone` land a
    #     fresh repo tree on disk when not already present.
    #   install.probe_skill_frontmatter_valid — install/prereq_probe.py
    #     `_check_skill_frontmatter_valid`: reads a skill file and parses its
    #     frontmatter; never opens for write.
    #   install.probe_windows_terminal_presence — install/prereq_probe.py
    #     `_check_windows_terminal_presence`: `shutil.which` + a read-only
    #     `winget list` query; never writes.
    #   install.wrapper_onto_path — install/wrapper_onto_path.py: copies a
    #     wrapper executable into the operator's per-user bin dir
    #     (`shutil.copyfile`) and sets its exec bit.
    #   install.write_shell_rc_guard_block — install/shell_rc_guard.py:
    #     appends a sentinel-delimited block to POSIX shell rc file(s) on disk.
    #   percolate.run_ci_smoke_check — ops/percolate_ci_smoke_check.py
    #     `run_ci_smoke_check`: spawns a consumer-owned CI script and reports
    #     its exit code; the op itself opens nothing for write and persists no
    #     state of its own — same read-only-wrapper shape already affirmed
    #     COMPUTE_ONLY for percolate.run_pre_ci_hooks above.
    #   session.commits — ops/session_commits.py: one read-only `git log`
    #     invocation, parsed and returned; never writes.
    #   session.warm_start — ops/session/warm_start.py `_handler`: spawns a
    #     detached warm-engine server process (`spawn_detached`) and writes a
    #     debounce breadcrumb when the debounce check allows it.
    #   session_baton.mint — ops/session_baton_mint.py: read-modify-writes the
    #     session's lazy baton record file via `session_baton.store.merge_baton`.
    #   session_baton.promote — ops/session_baton_promote.py: scaffolds and
    #     writes a real handoff artifact under state/handoffs/ via the
    #     `coordinator-doc-new` seam, plus a body edit.
    "app_session.census": OpClass.COMPUTE_ONLY,
    "app_session.launch": OpClass.MUTATING,
    "app_session.teardown": OpClass.MUTATING,
    "fleet.backfill_reference_edges": OpClass.MUTATING,
    "install.clone_idempotent": OpClass.MUTATING,
    "install.probe_skill_frontmatter_valid": OpClass.COMPUTE_ONLY,
    "install.probe_windows_terminal_presence": OpClass.COMPUTE_ONLY,
    "install.wrapper_onto_path": OpClass.MUTATING,
    "install.write_shell_rc_guard_block": OpClass.MUTATING,
    "percolate.run_ci_smoke_check": OpClass.COMPUTE_ONLY,
    "session.commits": OpClass.COMPUTE_ONLY,
    "session_baton.mint": OpClass.MUTATING,
    "session_baton.promote": OpClass.MUTATING,
    # op_census.breaches — COMPUTE_ONLY: the budget-breach view over the
    # op-latency sink (ops/op_budget_breaches.py::_op_budget_breaches).
    # DR-208 five-question affirmation:
    #   1. Writes, deletes, or reorders any state file, queue, or git object?  No.
    #      Opens the newest op-latency generation read-only via
    #      _tail_entries's byte-seek tail read (not engine_report.iter_sink_entries --
    #      see op_budget_breaches.py's module docstring for why) and returns a
    #      computed aggregate.
    #   2. Writes into rag's relational store?                                 No.
    #   3. Opens any file for write (including sentinel creation)?             No.
    #   4. Mutates shared mutable state outside its own module?                No.
    #      breach_summary is pure over the parsed rows and leaves them
    #      unmodified (asserted by
    #      telemetry/tests/test_breach_summary.py::
    #      test_breach_summary_does_not_mutate_its_input_rows).
    #   5. Persistent state changes observable across process boundaries?      No.
    #      No subprocess, no network, no cache write -- unlike its
    #      op_census.report sibling, which persists a module index.
    "op_census.breaches": OpClass.COMPUTE_ONLY,
    # hooks.cater_subagent_start — MUTATING: composes the SubagentStart
    # additionalContext catering string and, in doing so, writes to disk.
    # coordinator_core/hooks/cater_subagent_start.py::_resolve_sidecar_leg calls
    # subagent_sandbox.provision_report._provision, which provisions (writes) a
    # run-report sidecar file; and compose_catering's AC9 spill path
    # (_spill_blocks_to_companion) opens a companion blocks file for write under
    # state/subagent-share/<session_id>/ when the composed payload exceeds the
    # additionalContext cap. Both writes land under state/subagent-share/, which
    # is coordinator substrate (claude-klabauter disk-truth), never rag's workstate_store —
    # no dual-write-ban obligation is violated by this classification.
    # DR-208 five-question affirmation:
    #   1. Writes, deletes, or reorders any state file, queue, or git object?   YES.
    #      Sidecar provisioning + companion-file spill, both under state/subagent-share/.
    #   2. Writes into rag's relational store?                                  No.
    #   3. Opens any file for write (including sentinel creation)?              YES.
    #   4. Mutates shared mutable state outside its own module?                 No
    #      beyond the files it writes itself.
    #   5. Persistent state changes observable across process boundaries?       YES.
    #      The sidecar/companion files it writes are read by the dispatched child.
    "hooks.cater_subagent_start": OpClass.MUTATING,
    # plan.tasks.spine_drift_check — COMPUTE_ONLY, affirmed against the handler per
    # DR-208 § Classification correctness discipline. The module's own NEGATIVE-SPEC
    # states plainly: "Does NOT write, anywhere, under any code path. No locked_rmw,
    # no _stamp_rows_in_body, no frontmatter mutation of any kind." — this op exists
    # specifically as the read-only sibling of the mutating close_out_and_stamp
    # oracle (module docstring, DR-263 "report-only by architectural boundary"),
    # reusing close_out_and_stamp's read helpers without ever calling its write path.
    # DR-208 five-question affirmation (all "no"):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?   No.
    #   2. Writes into rag's relational store?                                  No.
    #   3. Opens any file for write (including sentinel creation)?              No.
    #      The module carries no write primitive at all (see NEGATIVE-SPEC above).
    #   4. Mutates shared mutable state outside its own module?                 No.
    #   5. Persistent state changes observable across process boundaries?       No.
    "plan.tasks.spine_drift_check": OpClass.COMPUTE_ONLY,
    # sizing.read_object_fields — COMPUTE_ONLY, affirmed against the handler per
    # DR-208 § Classification correctness discipline. Whole-document YAML parse
    # (yaml.safe_load) of a sizing-object under state/sizings/ or archive/sizings/,
    # projected to the intent/estimate/scout_evidence/appetite quartet; the module's
    # own negative-spec states it "never writes".
    # DR-208 five-question affirmation (all "no"):
    #   1. Writes, deletes, or reorders any state file, queue, or git object?   No.
    #      Reads and parses a sizing-object file; returns a projected dict.
    #   2. Writes into rag's relational store?                                  No.
    #   3. Opens any file for write (including sentinel creation)?              No.
    #      The module carries no write primitive at all.
    #   4. Mutates shared mutable state outside its own module?                 No.
    #   5. Persistent state changes observable across process boundaries?       No.
    "sizing.read_object_fields": OpClass.COMPUTE_ONLY,

    # warm_guard.evaluate — MUTATING: the guard chain it wraps
    # (bash_guards.dispatch.evaluate_payload_json) can write a best-effort advisory-
    # dedupe marker file under the caller's own gitdir as a side effect of a normal
    # evaluation (bash_guards/_advisory_dedupe.py :: mark_advised) — a real disk write,
    # even though the op's PRIMARY job is computing a verdict. Same posture as the
    # other hooks.* ops that write session-scoped bookkeeping (e.g.
    # "hooks.session_heartbeat") rather than the read-only hooks.* entries above.
    "warm_guard.evaluate": OpClass.MUTATING,

    # merge_assemble.apply — MUTATING: `coordinator_core.merge_assemble.
    # ops::_merge_assemble_apply` is a thin adapter over `merge_assemble.apply.
    # apply()`, which recomputes the brief and dispatches its directives[]
    # through a closed CLI table that cuts release tags, mutates branch state,
    # and mints/hands back a Tier-U grant — real, persistent mutation.
    # DR-208 five-question affirmation:
    #   1. Writes, deletes, or reorders any state file, queue, or git object?   Yes.
    #      Cuts tags, mutates branches, writes a Tier-U grant token under
    #      `.git/coordinator-sessions/<sid>/`.
    #   2. Writes into rag's relational store?                                  No.
    #   3. Opens any file for write (including sentinel creation)?              Yes.
    #      The Tier-U grant write/handback.
    #   4. Mutates shared mutable state outside its own module?                 Yes.
    #      Git refs/tags are shared, cross-process state.
    #   5. Persistent state changes observable across process boundaries?       Yes.
    # Spec: docs/plans/2026-08-26-merges-directives-stop-starting-interpreters.md § C6
    "merge_assemble.apply": OpClass.MUTATING,
})


def classify(op_name: str) -> OpClass:
    """Return the OpClass for the named op.

    Raises:
        KeyError: if op_name is not present in OP_CLASSIFICATION — fail-closed
                  by design. At HTTP dispatch the caller MUST treat this KeyError
                  as DENY (never as COMPUTE_ONLY). See DR-208 § Fail-closed runtime
                  semantic and contract.py module docstring.

    Negative-spec:
        Do NOT add a default= fallback — silent treat-as-compute-only is the real
        privilege-escalation path (detect-then-fail-loud, not detect-then-silently-pick).
    """
    # Review: code-reviewer — explicit guard replaces try/except/raise-from-None; single
    # lookup makes the fail-closed intent immediately obvious without exception chain games.
    if op_name not in OP_CLASSIFICATION:
        raise KeyError(
            f"coordinator_core.authz: op {op_name!r} has no classification in "
            "OP_CLASSIFICATION — add an entry before registering the op. "
            "New ops default to MUTATING until a reviewer affirms COMPUTE_ONLY. "
            "See DR-208 § Classification correctness discipline."
        )
    return OP_CLASSIFICATION[op_name]
