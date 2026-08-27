"""
coordinator_core.ipc — JSON-RPC 2.0 in-process dispatch + op-registry seam.

Purpose: Op-registry (register_op) and in-process dispatch core (dispatch_message)
for the command-type execution model. Callers pass a pre-parsed JSON-RPC 2.0 dict
directly to dispatch_message; no socket, no connection loop, no service lifecycle.

UDS daemon transport (start_server_async / _handle_connection / socket_path) and
the resident-service model were retired by DR-215.
Backlink: docs/decisions/DR-215-coordinator-core-command-type-execution-model.md

Wire format (Decision D1 — supersedes design spike §2.2 length-prefixed-JSON):
    Request:  {"jsonrpc":"2.0","id":<int>,"method":"<op>","params":{...}}\n
    Response: {"jsonrpc":"2.0","id":<int>,"result":{...}}\n
           or {"jsonrpc":"2.0","id":<int>,"error":{"code":<int>,"message":"..."}}\n

Op-registry seam: register_op(name, handler) stores async/sync callables keyed by
method name. C2–C4 ops self-register by importing this module at load time. The seam
is pinned here so every wave that authors an op can import `register_op` without
depending on the broader package init.

Spec backlink: pln-pcore-03-beachhead-coordinator-core-fecdbb § C1 / C1b
Design spike: docs/research/2026-07-01-resident-service-architecture.md §1.6 §2.1 §2.2 §2.3

Decisions:
    D1 — Wire format: NDJSON + JSON-RPC 2.0 (ratified in pcore-03 handoff; length-prefixed-JSON
         from spike §2.2 is recorded as considered-and-superseded).
    D3 — POSIX-first; Windows named-pipe server is best-effort / documented-conditional.
         Full UDS path on POSIX; TODO(D3/pcore-01) stub on Windows.

Negative-spec (hard-won):
    - The core is read-mostly for coordinator substrate. Handlers MUST NOT write coordinator
      substrate (handoffs, review-trail, commits, state/, archive/). Writers of substrate
      remain the EM/agent's job via bash/git surfaces (design spike §1.6).

      Two populations of hooks.* ops exist; their disk-write permissions differ:

      ADVISORY ops (7, pcore-04) — strictly read-only with respect to disk:
        - nudge_foreground_agent_dispatch: calibration is in-memory via _BG_CAPABLE_SESSIONS
          (pcore-04 Change 1 re-plumb); .harness-bg-capable sentinel write was removed.
        - postuse_advisory_dispatch: all advisory state is in-memory via
          _THROTTLE_LAST_CHECK / _ADVISORY_FIRED / _CRITICAL_FIRED / _RT_BARK_ONCE /
          _COMPACTION_CONSUMED (pcore-04 B-F1 re-plumb); /tmp sentinel touch()/unlink()
          ops and the runtime-tripwire-fire-log.tsv append were all removed.
          External /tmp compaction markers (written by PreCompact hook, not this op) are
          READ but not deleted; consumption is tracked in _COMPACTION_CONSUMED.
        - All other advisory ops: read-only (transcript reads, env reads, file-existence
          checks only). No advisory hook op writes any file — that is the COMPUTE_ONLY
          invariant (OpClass in coordinator_core/authz/classification.py; DR-208), and any
          disk write from an advisory hook handler is a violation.

      BOOKKEEPING ops (4, pcore-08 + receiver-state-sensor) — MAY write under
      .git/coordinator-sessions/ only:
        - session_heartbeat:       writes last_activity field in
                                   .git/coordinator-sessions/<sid>/meta.json
        - agent_completion_log:    appends to .git/coordinator-sessions/logs/agent-audit.jsonl
        - track_dispatched_agents: writes .git/coordinator-sessions/<sid>/dispatched-agents.txt
                                   and .git/coordinator-sessions/.agents/<aid>/em-session-id.txt
        - receiver_state_sensor:   writes .git/coordinator-sessions/<sid>/receiver-state.json
                                   (NEW sibling file, docs/plans/2026-08-14-receiver-state-sensor.md)
        Write confinement (hard): bookkeeping ops MUST NOT write state/, archive/, or any
        path outside .git/coordinator-sessions/. The write target is session-runtime/liveness
        layer — not coordinator substrate (claude-klabauter's disk-truth custody) and not rag's
        workstate_store projection (custody-vs-projection boundary,
        docs/decisions/DR-236-state-is-disk-truth-workstate-store-is-pro.md; the successor
        substrate-write carve-out DR for this confinement rule is DR-241, now authored —
        docs/decisions/DR-241-sovereign-tracker-substrate-write-carveout.md).
        Classification: all five are MUTATING (DR-208, classification.py).

      FLEET archival ops (3, DR-211) — sanctioned MUTATING archival-writer category
        (deliberate DR-211 carve-out, not a silent exception):
        - fleet.archive_completed_plans:    git-mv completed plan docs into archive/
        - fleet.archive_completed_handoffs: git-mv consumed handoffs into archive/
        - fleet.prune_closed_bugs:          git-mv closed bug entries into archive/
        D2 bounds: per-record idempotent, commutative, git-reversible,
        act-time-terminality-re-verifying, reachable only over the ungated UDS.
        **Transport-bound vacated 2026-07-06 by DR-215:** in-process command-type
        satisfies the safety rationale more strongly than ungated-UDS.
        Write confinement (hard): fleet archival ops MAY git-mv into archive/ and issue
        atomic scoped-pathspec commits ONLY; MUST NOT write rag's relational store
        (dual-write ban, docs/decisions/DR-211-fleet-op-substrate-write-boundary.md).
        Classification: all three are MUTATING (DR-208, classification.py).

      FLEET review-trail-findings-reap ops (2 more, DR-218) — sanctioned MUTATING
        delete category, distinct semantic from the DR-211 trio above (git rm, NOT
        git mv into archive/; DR-211 § D5 explicitly excludes state/review-trail/
        from its own carve-out, so this is a separate DR, not an extension of the
        DR-211 count above):
        - fleet.reap_unintegrated_findings: git-rm aged (>14d), marker-absent
                                            sidecars from state/review-trail/findings/
        - fleet.reap_integrated_findings:   git-rm marker-present sidecars (age-
                                            independent) from state/review-trail/findings/
        D2/D2a bounds (delete-adapted from DR-211 § D2, not identical to it): per-record
        idempotent, commutative, cwd-scope-guarded, act-time-terminality-re-verifying,
        fail-closed-to-keep. See classification.py's per-op DR-208 five-question
        affirmation blocks for the full bound-by-bound citation.
        Write confinement (hard): these ops MAY git-rm + scoped-pathspec-commit ONLY
        under state/review-trail/findings/; MUST NOT write rag's relational store
        (dual-write ban, docs/decisions/DR-218-review-trail-aged-unintegrated-reap-boundary.md).
        Classification: both are MUTATING (DR-208, classification.py).

      HANDOFF lifecycle ops (4, DR-212) — sanctioned MUTATING in-place frontmatter-mutation
        category (deliberate DR-212 carve-out; STRONGER crossing than DR-211 — state/handoffs/
        is a more deeply reserved noun than archive/; DR-211 governs git-mv out of state/,
        DR-212 governs in-place writes INTO state/handoffs/ frontmatter; see DR-212 § Context):
        - handoff.transition:  in-place frontmatter mutation of a single state/handoffs/*.md file
        - handoff.stamp:       in-place frontmatter mutation of a single state/handoffs/*.md file
        - handoff.normalize:   batch canonicalize sweep of all state/handoffs/*.md files
                               (D2(ii) batch-normalize exception: N per-file idempotent writes,
                               each atomic, no cross-file state, no archive/ touch; DR-212 D2(ii)
                               amended — see docs/decisions/DR-212-… § D2)
        - handoff.stamp_phase: in-place frontmatter mutation of a single state/handoffs/*.md file
                               (handoff_phase: {continuation|execution} + execution-only
                               execution_authorized_{by,at,sha,note}, sourced from the cited
                               plan's frontmatter; see
                               docs/plans/2026-07-17-claude-klabauter-handoff-phase-execution-emit-leg.md § C4)
        D2 bounds: per-file idempotent, single-target-file-only (handoff.normalize: scan-all
        exception per DR-212 D2(ii) — N per-file idempotent writes; transition/stamp/stamp_phase
        remain single-target-file-only), scope-limited to
        state/handoffs/*.md frontmatter ONLY (no body mutation, no other state/ path, no
        archive/ write, no git commit), reachable only over the ungated UDS (never HTTP).
        **Transport-bound vacated 2026-07-06 by DR-215:** in-process command-type
        satisfies the safety rationale more strongly than ungated-UDS.
        Write confinement (hard): handoff lifecycle ops MAY mutate state/handoffs/*.md
        frontmatter fields ONLY; MUST NOT write any other state/ path, MUST NOT write
        archive/, MUST NOT issue git commits, MUST NOT write rag's relational store
        (dual-write ban, docs/decisions/DR-211-fleet-op-substrate-write-boundary.md
        Invariant-1; DR-212 D4).
        Ownership authority: claude-klabauter owns the authoritative mutation mechanism for handoff
        lifecycle stamps (DR-210 § 1 two-way ownership cut — "authoritative work-state-
        mutation ops"). MUTATING classification is orthogonal to reserved-noun-write
        permission (DR-211 § 1 two-axes framing); DR-212 resolves the latter for this noun.
        Classification: all four are MUTATING (DR-208, classification.py).

      QUEUE write ops (2, DR-213) — sanctioned MUTATING additive-create category
        (deliberate DR-213 carve-out; additive-create semantics, append-only per-entry
        YAML; distinct from DR-211 archival-move and DR-212 in-place frontmatter-mutation):
        - queue.append:   appends a per-entry YAML file to one of four state/ subdirs:
                          state/debt-backlog/, state/bug-backlog/,
                          state/improvement-queue/, or state/lessons/
        - queue.promote:  appends a per-entry YAML file to state/lessons-outbox/
        D2 bounds (derived from additive-create semantics — NOT copied from DR-211's
        archival-move bounds; DR-211's act-time terminality-re-verify does NOT apply
        to additive-create and is explicitly excluded here):
          per-entry idempotent-by-filename-overwrite (same date+slug → os.replace
          overwrites → one file, last-write-wins), commutative, git-reversible,
          reachable only over the ungated UDS.
        **Transport-bound vacated 2026-07-06 by DR-215:** in-process command-type
        satisfies the safety rationale more strongly than ungated-UDS.
        Write confinement (hard): queue write ops MAY write ONLY to the five named
        state/ subdirs above, append-only per-entry YAML; MUST NOT write rag's
        relational store (dual-write ban); MUST NOT write any cross-repo index.
        Classification: both are MUTATING (DR-208, classification.py).

      QUEUE CLOSURE (1, DR-270) — sanctioned MUTATING in-place-mutation-plus-self-commit
        category (deliberate DR-270 carve-out; a third write shape distinct from both
        DR-212's in-place-mutation-no-commit bound and DR-213's additive-create-no-commit
        bound above):
        - queue.close: in-place stamps `status`/`closed_at`/`closed_by` on one named,
                       already-existing state/improvement-queue/*.yaml entry, issues one
                       scoped commit of that single path via
                       coordinator_core.ops.ceremony.git_native.commit_scoped, then
                       delegates the terminal archival move to the unmodified
                       fleet.archive_queue_entry.
        Write confinement (hard): queue.close MAY in-place-mutate ONLY the one named
        state/improvement-queue/*.yaml entry it was called with, and MAY issue ONLY one
        scoped commit over that single path; MUST NOT write rag's relational store
        (dual-write ban); MUST NOT scan state/improvement-queue/ for other entries; archival
        write confinement past this point is DR-211's, via delegation.
        Authorization: DR-272 § 2 ratifies in-place-mutation-plus-self-commit against
        DR-212's "MUST NOT issue git commits" bound (this entry's own open item, now
        resolved — DR-272 is the reconciliation, not a re-derivation of it here).
        Classification: MUTATING (DR-208, classification.py).

      PLAN STATUS TRANSITION (1, DR-272) — sanctioned MUTATING in-place-mutation-plus-
        self-commit category (DR-272 § 2 authorization; same third write shape as QUEUE
        CLOSURE above, extended here to a second reserved noun):
        - plan_status_transition (stamp-implemented verb): in-place stamps the `status:`
          frontmatter field on one named, caller-supplied docs/plans/*.md file to
          `implemented`, then issues one scoped commit of that single path via
          coordinator_core.ops.ceremony.git_native.commit_scoped. Direct-import CLI
          entry point (no @register_op / no JSON-RPC wiring — see the module's own
          Negative-spec), listed here because DR-272's registry-completeness ruling
          (§ 4 Consequences) reaches it regardless of dispatch shape.
        D2 bounds (DR-272 § 2.4): per-record idempotent (terminal/deferred statuses are
        a no-op), git-reversible (single frontmatter field flip), resume-authenticated
        (Bound 1 — a stranded uncommitted flip is re-read and re-validated against its
        own expected terminal state before the stranded commit is issued), reachable
        only via direct in-process import (not a registered op — see above).
        Write confinement (hard): plan_status_transition MAY in-place-mutate ONLY the
        one named docs/plans/*.md entry it was called with (path-contained to the
        resolved git worktree root — C2a), and MAY issue ONLY one scoped commit over
        that single path; MUST NOT write rag's relational store (dual-write ban); MUST
        NOT scan docs/plans/ for other entries.
        Authorization: DR-272 § 2 ratifies this op's writer-side commit ownership,
        superseding the module's own prior concession that DR-211 "does not itself
        admit" it — DR-272, not a widened reading of DR-211 D1, is the admitting
        record (DR-272 § 4 Consequences).
        Classification: MUTATING (DR-208, classification.py).

      MEMO TRANSITION (1, DR-272; commit ownership DR-273) — sanctioned MUTATING
        in-place-mutation-plus-self-commit category (DR-272 § 2 authorization; DR-273
        already established writer-side commit ownership for this op — DR-272 supplies
        the registry block and the DR-212 reconciliation DR-273 did not itself carry):
        - memo.transition: in-place mutates lifecycle frontmatter fields (status plus
          verb-specific fields — picked_up_at/by, decision/decision_note/realized_by,
          actioned_note) on one named, caller-supplied cross-repo/ or state/ memo file
          across four verbs (claim, action, release, resolve), then issues one scoped
          commit of that single path via
          coordinator_core.ops.ceremony.git_native.commit_scoped, using the git root
          the op's own containment check resolves.
        D2 bounds (DR-272 § 2.4): per-record idempotent (each verb carries its own
        no-op short-circuit ahead of the commit helper — see the module's own Verb
        contracts), git-reversible (single-path field writes), resume-authenticated
        (Bound 1 — DR-272's authenticated-resume requirement applies per verb, against
        that verb's own expected terminal state), reachable only via in-process
        command-type dispatch (memo.transition is a @register_op'd JSON-RPC method).
        Write confinement (hard): memo.transition MAY in-place-mutate ONLY the one
        named memo path it was called with, containment-gated to a git-tracked
        cross-repo/ or state/ subtree, and MAY issue ONLY one scoped commit over that
        single path; MUST NOT write rag's relational store (dual-write ban); MUST NOT
        acquire or write .git/coordinator-sessions/memo-claims/ (a separate claim
        surface owned by archive_stamp.py, out of this op's scope).
        Authorization: DR-273 established commit ownership; DR-272 § 2 supplies the
        DR-212 reconciliation DR-273 did not itself carry, and is the record this
        registry block cites for that leg.
        Classification: MUTATING (DR-208, classification.py).

      MEMO send op — RETIRED 2026-08-23 (PM ruling: killed op dies outright, no
        stub). The DR-214-send-class cross-tree write category this registry block
        used to authorize (memo.send: schema-valid memo file into a registry-
        enumerated receiver's cross-repo/inbox/, non-committing) has no live
        handler; `memo.send` is not in the op registry. See
        docs/decisions/DR-214-send-class-cross-tree-write-boundary.md for the
        historical D2 seven-bound this category was admitted under.

      DISTILL-DISPOSAL ops (7, DR-228) — sanctioned MUTATING category split into two
        tiers by write-shape, both crossing ``state/`` (the reserved-noun question
        DR-228 § D6 resolves: ``state/scratch/`` is INSIDE the reserved substrate set,
        not an ephemeral exemption):
        - Disposal tier (3) — create + in-place-stamp + delete + commit, the heaviest
          combination any DR in this lineage has sanctioned:
          - distill.assemble_disposal_manifest: creates the disposal manifest under
                                                 state/scratch/artifact-distillation/<run-id>/
          - distill.stamp_disposal:             in-place all-four-or-none stamp of the
                                                 manifest's disposal_authorized_{by,at,sha,note}
                                                 fields (DR-212-style; DEC-2)
          - distill.apply_disposal:             class-aware git rm/unlink over the
                                                 stamped manifest's eligible set, appends
                                                 state/distillation-log.md rows, issues
                                                 ONE scoped-pathspec commit over the union
                                                 of touched paths
          D2a bounds (delete-adapted from DR-218 § D2/D2a, restated for this noun):
          per-record idempotent, commutative, cwd-scope-guarded, act-time-terminality-
          re-verifying (TOCTOU re-check via delete_guard), fail-closed-to-keep. D2b bounds
          (new to this category): stamped-manifest-gated (refuses any unstamped or
          sha-drifted manifest) and drain-ordering-verified (refuses unless the harvest
          commit is a verified ancestor, via `git merge-base --is-ancestor` PLUS a
          containment check, never a bare `git cat-file -e`).
        - Scratch tier (4, amended 2026-08-06 to add cartography.chunk_table) —
          create-or-full-overwrite, no delete, no commit; strictly lighter than the
          disposal tier:
          - distill.scope:            emits the Workflow INPUT JSON under
                                       state/scratch/artifact-distillation/<run-id>/
          - distill.curation_status:  emits a derived snapshot to the fixed path
                                       state/ceremony/curation-status.json (--emit only;
                                       not run-scoped, since it is a recomputable
                                       point-in-time snapshot, not a per-run artifact)
          - memo.fate_partition:      emits a fate-partition shard under
                                       state/scratch/artifact-distillation/<run-id>/
          - cartography.chunk_table:  emits a reduced source-file chunk table under
                                       <target_root>/state/scratch/cartography-chunk-table/<run-id>/
                                       (--emit only; DoE-claude's cross-tree consumer —
                                       cross-repo/inbox/2026-08-06-doe-claude-em-cartography-
                                       chunk-table-producer-seam.md)
          D6 bounds: write-confined to the op's own named target only; create-or-full-
          rewrite only (never partial in-place mutation); no delete; no commit (landing
          it — if warranted — is the calling EM/session's job via ordinary git surfaces);
          schema_version-pinned on every emitted JSON.
        Write confinement (hard): disposal-tier ops MAY create/stamp under
        state/scratch/artifact-distillation/<run-id>/ and (apply_disposal only) git-rm +
        scoped-pathspec-commit ONLY; scratch-tier ops MAY create-or-overwrite ONLY their
        own named target (state/scratch/artifact-distillation/<run-id>/, the one fixed
        state/ceremony/curation-status.json path, or — cartography.chunk_table only —
        <target_root>/state/scratch/cartography-chunk-table/<run-id>/, target_root being
        that op's own caller-supplied root rather than the dispatching worktree, per the
        cartography.* family's target-resolution model); none of the seven MAY write
        rag's relational store (dual-write ban, docs/decisions/DR-211-fleet-op-substrate-write-boundary.md
        Invariant-1).
        Classification: all seven are MUTATING (DR-208, classification.py), each citing
        DR-228 and its specific tier's bound letters.

      CHANGELOG / COMPLETION / REVIEW-TRAIL write ops (7, DR-216) — sanctioned MUTATING
        mixed-semantics category (deliberate DR-216 carve-out; additive-create AND
        content-additive in-place mutation over a distinct noun population — week-changelog,
        review-trail, and docs/plans/*.md completion sections — not a footnote to DR-212
        (different nouns) or DR-213 (adds in-place semantics DR-213 explicitly excludes)):
        - changelog.append_day:         additive-create (+ in-place append) to
                                        state/week-changelog/
        - changelog.backfill_gaps:      in-place mutation of state/week-changelog/
        - changelog.inject_anchor:      in-place anchor-block append (+ named coverage-anchor
                                        bump exception, D2(iii-b)) to
                                        archive/daily-summaries/<date>-<machine>.md
        - completion.reconcile_commits: in-place mutation of a completion entry's `commits:`
                                        list, confined to archive/completed/**/*.md or the
                                        caller-supplied docs/plans/<plan>.md
        - plan.append_session:          in-place append of a session record inside the
                                        caller-supplied docs/plans/<plan>.md
        - completion.flip_to_released:  in-place flip of release fields on one named
                                        archive/completed/**/*.md entry (admitted into D1 by
                                        DR-216's 2026-08-06 amendment — it had been writing
                                        under this carve-out's bounds without appearing in
                                        any D1 enumeration, so it held no admitting record;
                                        AMENDED by EM under the execution authorization of
                                        docs/plans/2026-08-06-writer-side-commit-ownership-
                                        lock-gap.md — PM counter-signature pending)
        - review_trail.write:           additive-create into state/review-trail/
        D2 bounds: per-record idempotent, git-reversible, content-additive in-place only
        (two named rewrite exceptions: F11's `commits: []` inline-empty-list-to-populated-form
        substitution, and D2(iii-b)'s strict-ancestor-convergent coverage-anchor bump),
        confined to the named nouns, no git commit issued by the handler, single-writer-per-file
        LOCKED via `coordinator_core.locked_write.locked_rmw` at the two multi-writer sites
        (`completion.reconcile_commits`, `plan.append_session`) per DR-216's 2026-08-06
        amendment — the remaining four ops retain the unenforced single-writer posture, asserted
        only because no second writer to their nouns has been demonstrated. In-process
        command-type from birth (DR-215) — no transport clause; reachable only via
        `python -m coordinator_core.invoke`.
        Write confinement (hard): DR-216 ops MAY write ONLY state/week-changelog/,
        state/review-trail/, docs/plans/*.md completion/session sections, and the two
        op-confined archive/ sub-nouns (archive/completed/ for completion.reconcile_commits,
        archive/daily-summaries/ for changelog.inject_anchor); MUST NOT write state/handoffs/
        (DR-212's noun), any other state/ path, any other archive/ path, or rag's relational
        store (dual-write ban, docs/decisions/DR-236-state-is-disk-truth-workstate-store-is-pro.md,
        successor authority to the superseded 2026-07-03 tri-plane DR).
        Classification: all six are MUTATING (DR-208, classification.py).

      SOVEREIGN-TRACKER append-ordered event-store class (library-only, zero ops, DR-241) —
        sanctioned MUTATING append-ordered category ratified in charter ahead of any handler
        code (deliberate DR-241 carve-out; DEC-4: this DR registers zero ops). The
        affirmation obligation is compliance with DR-241's own five D2 bounds below, not a
        DR-208 §5 citation duty (docs/decisions/DR-241-sovereign-tracker-substrate-write-carveout.md
        § Amendment (2026-07-28), "Correction — DR-208 §5 is the wrong citation"). That
        affirmation landed early, at sat-01b, not sat-06 (same § Amendment); the `person_*`
        emitters were separately affirmed at DR-241's § Amendment (2026-08-11), "sat-05
        person-registry event handler affirmation":
        - tracker_store.append_event / tracker_store.read_events: library functions in
          coordinator_core/tracker_store.py with no registered caller as of this entry;
          NOT an op, NOT OP_CLASSIFICATION-entered (DEC-4).
        D2 bounds (constructed fresh for this class — no predecessor's bounds transfer
        wholesale; see DR-241 § 1 semantic-distinctness): idempotent by content-derived
        event id carrying a global-uniqueness guarantee (UUID or explicit machine
        qualification — a bare per-machine `sequence` does NOT satisfy this), commutative
        modulo total order (the (applied_at, observed_at, id) triple resolves a
        deterministic global order at read time regardless of merge direction or shard-glob
        order — never file-append order, never bare `sequence`), git-reversible
        (append-only; no in-place mutation, no deletion), explicitly NO terminality-re-verify
        (categorical inapplicability, not a relaxation), in-process command-type dispatch
        only.
        Write confinement (hard): sanctioned writes MAY target ONLY
        state/sovereign-tracker/ in the CONSUMING repo's own tree — per-repo, never
        fleet-wide and never claude-klabauter's tree on another repo's behalf (DEC-11); MUST NOT
        write any other state/ path, MUST NOT write archive/, MUST NOT write rag's
        relational store (dual-write ban,
        docs/decisions/DR-236-state-is-disk-truth-workstate-store-is-pro.md).
        Classification: a future tracker.* op would be MUTATING (DR-208 §2) — this DR
        does not register one; the guard C3 adds (asserting no coordinator_core/ops/
        module imports tracker_store and no OP_CLASSIFICATION entry references it) holds
        this no-op-registered status honest in code, not merely in prose.

      Backlinks:
        docs/plans/2026-07-04-pcore-04-advisory-hook-ops-claude-klabauter-engine.md § AC4 / C7
        docs/plans/2026-07-04-pcore-08-async-bookkeeping-hooks-engine-vs-mcp.md § D2
        docs/decisions/DR-211-fleet-op-substrate-write-boundary.md § D2 / D3
        docs/decisions/DR-212-handoff-lifecycle-inplace-frontmatter-mutation-carveout.md § D2 / D3
        docs/decisions/DR-213-queue-write-substrate-carveout.md § D2
        docs/decisions/DR-214-send-class-cross-tree-write-boundary.md § D2
        docs/decisions/DR-216-changelog-completion-reviewtrail-write-carveout.md § D2 / D3 / D4
        docs/decisions/DR-228-distill-disposal-substrate-writer-category.md § D1 / D2a / D2b / D6
        docs/decisions/DR-241-sovereign-tracker-substrate-write-carveout.md § D1 / D2

    - Do NOT use thread-per-connection. Single asyncio event loop; request-scoped handlers.
      StreamReader.readline() handles partial-line reassembly natively (AC12).
    - Do NOT persist op-handler state across requests. Handlers receive params + repo_root per call.
    - The UDS daemon transport (start_server_async / _handle_connection / socket_path) was
      REMOVED by C5 per DR-215 command-type execution-model retirement. coordinator_core is
      now an in-process command-type engine; dispatch_message is called directly by callers
      (invoke/__main__.py, test fixtures, ceremony scripts) — no socket, no service loop.
      Backlink: docs/decisions/DR-215-coordinator-core-command-type-execution-model.md
"""
# Review: code-reviewer (slice-A F1) — added DR-211 to Backlinks so the governing
# authority for the FLEET archival block is visible when scanning the Backlinks section.

from __future__ import annotations

import contextvars
import importlib
import os
import sys as _sys
import threading
import types as _types
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from coordinator_core.lifecycle import (
    git_common_dir,
)


# Deferred-import accessor (Windows hot-path import diet — docs/plans/
# 2026-08-06-windows-hot-path-less-work-per-interpreter.md § C9c).
#
# See coordinator_core.lifecycle's `_log()` for the full rationale (same
# ~8.1ms cold `logging` import cost, same "only getLogger + level calls used"
# shape, same identity-preservation argument for why deferring the *import*
# itself — not just the getLogger call — is safe here).
_logger = None


def _log():
    global _logger
    if _logger is None:
        import logging
        _logger = logging.getLogger(__name__)
    return _logger

# ---------------------------------------------------------------------------
# JSON-RPC 2.0 standard error codes (spec §5.1)
# ---------------------------------------------------------------------------
PARSE_ERROR = -32700       # Malformed JSON — not a valid JSON value
INVALID_REQUEST = -32600   # Valid JSON but not a valid Request object
METHOD_NOT_FOUND = -32601  # The method does not exist / is not available
INVALID_PARAMS = -32602    # Invalid method parameter(s)
INTERNAL_ERROR = -32603    # Internal JSON-RPC error

# ---------------------------------------------------------------------------
# App-defined error code — JSON-RPC 2.0 §5.1 reserves -32000..-32099 for
# implementation-defined "Server error" codes.
# ---------------------------------------------------------------------------
STRUCTURAL_PIN_ERROR = -32001
"""An op handler raised an exception carrying ``structurally_wedged = True`` (e.g.
coordinator_core.ops.emit.validate.ContractPinError). Distinct from INTERNAL_ERROR: a
structural-pin failure is not transient — it recurs on every invocation until the
underlying pin (contract version vs. vendored bundle) is remediated, so a caller that
retries or degrades-to-skip on INTERNAL_ERROR must NOT do the same here. See
dispatch_message's exception handling below and coordinator_core.invoke.__main__'s
distinct exit code (2) for this class."""

UNSTAMPED_ENGINE_ROOT_ERROR = -32005
"""`dispatch_message` refused because the `coordinator_core` package THIS PROCESS has
imported carries no engine build stamp -- see `_refuse_unstamped_dispatch`'s own
docstring for the ruling this enforces and `warm.skew.UnstampedEngineRootError`
(-32005 is the next free app-code slot after WARM_DISPATCH_INDETERMINATE, -32004,
`coordinator_core.warm.client`) for the sibling check on the warm axis this one
extends to dispatch generally."""

OP_SUSPENDED_ERROR = -32006
"""`dispatch_message` refused because the op is TURNED OFF for blowing the 2s max bar
-- see `coordinator_core.op_budget_suspension` for the ruling, the measured evidence
per op, and the cold-measurement bar an op must clear to come back. Distinct from
METHOD_NOT_FOUND: the op exists and would work, which is the problem. Distinct from
a timeout: nothing ran, so nothing was paid for. A caller that degrades-to-skip on
METHOD_NOT_FOUND should do the same here; a caller that RETRIES on either is wrong,
since this recurs identically until the op is made fast or deleted."""


# ---------------------------------------------------------------------------
# Dispatch-axis stamp gate (state/handoffs/2026-08-21_103635_reaching-the-
# warm-engine.md; PM ruling verbatim: "for any live ops there should be no
# fallback to claude-klabauter. none whatsoever ... I want that shit to fail hard every
# time if it can't go via Klabauter").
#
# Four caller-side resolvers already existed on this box before this row
# (coordinator-invoke.py's require_dispatch_engine_on_path, cc_invoke's
# delegation to coordinator_engine_root_with_class, the 745 settings-home
# forwarders' exec_cli, and the warm axis's own UnstampedEngineRootError) --
# each correctly refuses an unstamped published root WHEN CONSULTED. None of
# them is consulted by a caller that skips straight to `import coordinator_core`
# or `python -m coordinator_core.invoke`, which is served instead by this
# box's machine-wide editable install (unconditionally pointing at the live,
# unstamped claude-klabauter tree). This gate closes that gap at the one seam
# every dispatch -- cold or warm, CLI or hook, however it got here -- actually
# passes through: `dispatch_message` itself, "the SOLE process-level dispatch
# chokepoint" per its own docstring above.
# ---------------------------------------------------------------------------

#: Process-local, explicit opt-in -- NEVER an environment variable (PM
#: constraint: "explicit, deliberate, never ambient, never inherited
#: silently from a parent process"; an env var is inherited by every child,
#: grandchild, and detached spawn this box runs, which is exactly the
#: silent-off-in-processes-nobody-intended failure mode this gate exists to
#: remove). A plain module-global bool cannot cross a `Popen`/`subprocess`
#: boundary at all -- a child process that itself dispatches gets the
#: enforced behaviour regardless of what its parent set, which is correct:
#: that child IS a live dispatch.
_unstamped_dispatch_allowed = False

#: ARMED. PM ruling, verbatim: "for any live ops there should be no fallback to
#: claude-klabauter. none whatsoever ... I want that shit to fail hard every time if it
#: can't go via Klabauter." This flag is the single seam that decides whether
#: the gate below REFUSES or merely stands ready, and it refuses.
#:
#: History, so nobody re-derives it. The gate first landed in `30ac82322260`
#: and was reverted by `d179bfb39` on the reading that the 233 recorded
#: live-tree cold dispatches were `hooks.postuse_advisory_dispatch` and
#: `hooks.track_touched_files`, which would make an armed gate refuse every
#: tool call fleet-wide. That reading does not reproduce: both of those ops go
#: through `DoE-claude/coordinator/hooks/scripts/postuse-advisory-dispatch.py`,
#: which resolves via `_engine_root.resolve_claude_klabauter_root()` to the STAMPED
#: klabauter mirror, and setuptools' `_EditableFinder` sits after `PathFinder`
#: in `sys.meta_path`, so that script's `sys.path.insert(0, root)` wins over the
#: machine-wide editable pin rather than losing to it.
#:
#: The population that DOES resolve unstamped is a different family: the git
#: hooks (`prepare-commit-msg`, `post-commit`, `pre-commit`, `pre-push`,
#: `post-checkout`) exec through the settings-home forwarder, whose
#: `_resolve_claude_klabauter.resolve_claude_klabauter_root_with_class()` returns
#: `(<live tree>, RESOLUTION_LIVE_WORKING_TREE)` by design. Those import the
#: engine unstamped -- which is why they contribute `client-cold.jsonl` rows,
#: `record_client_cold_fallback` keying `svc_dir` on `_engine_clone_root()` --
#: but they do not dispatch ops: none of the CLIs they exec references
#: `dispatch_ops_from_hook` or `ipc.dispatch`. A row in that file is evidence of
#: an unstamped IMPORT, never of a gated DISPATCH, and conflating the two is
#: what produced the original count.
#:
#: The remaining sanctioned unstamped dispatcher is this repo's own test suite,
#: which imports and dispatches against the live tree by construction and opts
#: out explicitly in `conftest.py::pytest_configure` -- see
#: `allow_unstamped_dispatch` for that carve-out and the CLI flag beside it.
#:
#: DISARMING IT: flip to False. Do that only to stop an actual observed
#: fleet-wide refusal, and say so in the commit -- not to quiet a single
#: caller, which should instead route through `coordinator-invoke`.
_STAMP_GATE_ARMED = True


def allow_unstamped_dispatch() -> None:
    """Explicit, process-local opt-out of the stamp gate below.

    THE TWO SANCTIONED CALLERS, per the PM ruling: (1) `coordinator_core`'s
    own `conftest.py`, so the test suite -- which imports and dispatches
    against the live tree by design -- keeps working; (2) an explicit CLI
    flag on the dispatch entrypoint (`coordinator_core.invoke.__main__`'s
    `--allow-unstamped-dispatch`), typed per invocation, for deliberate
    manual testing against a live engine build. Neither is ambient: a
    conftest fixture runs only inside that process's own pytest collection,
    and a CLI flag is visible in the command that set it.

    Not a toggle callers are expected to pair with a matching "off" call --
    tests that need the ENFORCED behaviour for one case (asserting the
    refusal itself) flip the underlying `_unstamped_dispatch_allowed` flag
    off via `monkeypatch.setattr`, which reverts automatically at that
    test's teardown -- see `test_dispatch_message.py`'s own gate tests. A
    bespoke reset function here would be a second, competing revert
    mechanism for the same state.
    """
    global _unstamped_dispatch_allowed
    _unstamped_dispatch_allowed = True


def is_unstamped_dispatch_allowed() -> bool:
    """Public reader for `_unstamped_dispatch_allowed` -- the single opt-in
    this module exposes, read by `invoke.__main__` to decide whether a
    warm-unavailable dispatch may fall through to cold at all (see
    `_dispatch_argv_body`'s own "fail hard, not just fail closed" block):
    the same explicit, deliberate, per-invocation carve-out covers both the
    stamp gate above and the no-cold-fallback policy, rather than growing a
    second, independently-toggled opt-in for what is the same "this is a
    deliberate manual/test invocation" declaration."""
    return _unstamped_dispatch_allowed


#: This process's own resolved engine clone root, `ipc.py`-anchored:
#: `coordinator_core/ipc.py`'s parent is `coordinator_core/`, whose parent is
#: the repo root -- byte-identical derivation to
#: `warm.engine_root.current_engine_clone()` (`Path(__file__).resolve().
#: parents[2]` from `coordinator_core/warm/engine_root.py`, two directories
#: deeper), NOT imported from there (see `_is_dispatch_engine_stamped`'s own
#: docstring for why). Computed once at import time -- this process's own
#: `coordinator_core` cannot be re-rooted after the fact.
_DISPATCH_ENGINE_ROOT = Path(__file__).resolve().parent.parent

#: Repo-relative parts to this process's own engine build stamp -- mirrors
#: `warm.skew.ENGINE_STAMP_FILENAME` / `_engine_stamp_path` byte-for-byte in
#: shape. This is one of SIX independent copies of this value on this box
#: (see `coordinator_core/tests/test_engine_stamp_predicate_pin.py`'s module
#: docstring for the full list and why each cannot simply import the
#: canonical one) -- keep all six in sync by hand if the stamp filename or
#: location ever changes; that test is the drift guard.
_ENGINE_STAMP_RELATIVE_PARTS = ("coordinator_core", "_engine_stamp")

#: Cached verdict: is `_DISPATCH_ENGINE_ROOT` a stamped engine build? `None`
#: means "not yet computed". Memoized, not re-stat'd per dispatch (staff EM
#: review, this row): the root of an ALREADY-IMPORTED package cannot change
#: for the life of the process, so the cost of proving it is paid once, not
#: per request on a warm server serving many dispatches/sec (the brightline
#: is measured in process time -- see CLAUDE.md § brightline).
_engine_stamped_verdict: Optional[bool] = None


def _reset_engine_stamped_verdict_for_test() -> None:
    """Test-only seam: clear the memoized stamp verdict.

    Needed because a test may monkeypatch `_DISPATCH_ENGINE_ROOT` (or the
    stamp file itself) to simulate a differently-rooted process; without a
    reset the FIRST test to compute the verdict would pin it for every
    later test in the same pytest process.
    """
    global _engine_stamped_verdict
    _engine_stamped_verdict = None


def _is_dispatch_engine_stamped() -> bool:
    """True iff `_DISPATCH_ENGINE_ROOT` carries a valid engine build stamp.

    DELIBERATELY NOT `warm.engine_root.is_engine_root` -- that predicate is
    `dispatch_message`'s natural single-implementation source of truth, and
    importing it was the first cut of this row. Measured (this box,
    `coordinator_core/benchmarks/process_time.py::batched_process_time_ms`,
    K=20): importing `coordinator_core.warm.engine_root` (which pulls
    `warm.skew` -> `coordinator_core.engine_version` +
    `coordinator_core.lifecycle`) costs ~21ms of PROCESS time per cold CLI
    invocation, on a chokepoint every single dispatch on this box now pays
    once. That is the exact defect class `warm.client`'s own module
    docstring fights at length (`spawn_detached`'s lazy import, W11's
    `is_warm_enabled()`-before-`warm.client`-import ordering) -- and the
    same tradeoff `coordinator/lib/resolve-claude-klabauter/_resolve_claude_klabauter.py`
    already made for the identical reason (see that module's own
    `_is_stamped_engine_root` docstring: "cannot import
    coordinator_core... its entire job is to FIND claude-klabauter, so it cannot
    presuppose claude-klabauter is already importable"). This is that module's
    pattern applied one layer down: a THIRD standalone twin of the same
    two-line predicate, not a fourth import edge onto a hot chokepoint.
    Never raises.
    """
    global _engine_stamped_verdict
    if _engine_stamped_verdict is None:
        stamp_path = _DISPATCH_ENGINE_ROOT.joinpath(*_ENGINE_STAMP_RELATIVE_PARTS)
        try:
            _engine_stamped_verdict = len(stamp_path.read_bytes()) > 0
        except OSError:
            _engine_stamped_verdict = False
    return _engine_stamped_verdict


def _unstamped_dispatch_refusal(request_id) -> dict:
    """JSON-RPC 2.0 error envelope for a dispatch refused by the stamp gate.

    Register (docs/wiki/guard-messaging.md § Register): one fact, one
    runnable alternative, no self-legitimacy, no apology. Names
    `coordinator-invoke` (which resolves the published engine through the
    existing, already-stamp-gated resolver chain) rather than a slash
    command -- this can fire from a bare CLI invocation before any Claude
    Code session exists.
    """
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": UNSTAMPED_ENGINE_ROOT_ERROR,
            "message": (
                f"dispatch refused: engine root {_DISPATCH_ENGINE_ROOT} has no build stamp -- "
                "not a published engine. Dispatch via coordinator-invoke, or "
                "pass --allow-unstamped-dispatch for deliberate manual testing."
            ),
        },
    }


class CallerFacingValidationError(ValueError):
    """Raised by an op handler for a caller-supplied-PARAMETER validation failure whose
    message is deliberately written to be read by the caller — e.g. a closed-enum
    rejection that already names its own legal value set (``review_trail_write._validate``'s
    ``reviewer``/``scope``/``verdict``/``scope_kind`` checks).

    ``caller_facing_validation = True`` is a duck-type marker consumed by
    ``_handler_exception_error``, mirroring ``ContractPinError``'s ``structurally_wedged``
    marker above: an op handler that raises an exception carrying this attribute gets
    ``INVALID_PARAMS`` (-32602) with the exception's OWN message preserved (length-bounded),
    instead of the blanket ``INTERNAL_ERROR`` / class-name-only shape every other,
    unclassified exception still gets. That blanket shape is the RIGHT default for an
    arbitrary exception (its message is not assumed safe to surface), but it actively
    defeats a validator that already did the work of naming its own remediation — a caller
    reduced to ``-32603 Internal error: ValueError`` has to guess the legal enum values one
    at a time against the allow-list in the module's own source, exactly what happened here:
    cross-repo/inbox/2026-08-15-example-retrieval-repo-em-wsc-review-trail-skips-silently.md.

    Subclasses ``ValueError`` (not a bare ``Exception``), so every existing
    ``pytest.raises(ValueError, ...)`` call site against a function that now raises this
    stays passing unchanged — this is a narrowing of WHICH ``ValueError`` an op handler
    raises for validation, never a widening of the exception hierarchy those tests assert
    against. A caller invoking the underlying Python function directly (not through the IPC
    boundary) sees no behavior change at all; only ``dispatch_message``'s error-shaping reads
    this marker.

    Any future op-handler validator that already composes a caller-facing message (names its
    own legal values, points at a concrete remediation) should raise this same class rather
    than inventing a parallel signal — mirrors ``ContractPinError``'s own "any future sibling
    ... should set this same attribute" guidance."""

    caller_facing_validation = True


#: Bound on the preserved message length for a `CallerFacingValidationError` — long enough for
#: every enum-rejection message this module composes today (the longest, an ALL-invalid-enum-
#: fields aggregate from `review_trail_write._validate`, runs well under 1000 chars), short
#: enough to cap a pathological caller-constructed message from ballooning the JSON-RPC error
#: payload. Never applies to the STRUCTURAL_PIN_ERROR branch above, which has its own message
#: shape and is not subject to this bound.
_CALLER_FACING_MESSAGE_MAX_LEN = 2000

# ---------------------------------------------------------------------------
# Per-invocation dispatch timeout (C3 / AC-3 / Gap-1)
#
# Per-invocation hung-op guard: cancels a runaway or hanging op handler after
# DISPATCH_TIMEOUT_SECS, preventing the caller from blocking indefinitely.
# (In the command-type model, one op runs per process — the "other partitions"
# framing of the multiplex era no longer applies; the guard is now a simple
# per-invocation safety net.)
#
# Overridable via env var, resolved PER-REQUEST by `_resolve_dispatch_timeout_secs()`
# below (C11) rather than once at import — a warm, long-lived engine process would
# otherwise have this knob permanently pinned to whatever `COORDINATOR_
# DISPATCH_TIMEOUT_SECS` happened to read at process start, with no way to retune
# it without a restart. `DISPATCH_TIMEOUT_SECS` below remains a real module
# attribute (computed once, at import, from the env as seen then) and is still
# the value `_resolve_dispatch_timeout_secs()` falls back to when the env var is
# absent at request time — this is deliberate, not a residual of the old
# import-time-only design: it lets a caller (production or test) set the knob
# either way — via the env var (picked up live, per request) or by assigning
# `ipc.DISPATCH_TIMEOUT_SECS` directly (the fallback default) — and both take
# effect on the very next dispatch, never requiring a fresh process. Integration
# tests use either form to exercise the timeout path without slowing the suite.
#
# Note: the timeout is only effective if the handler is either:
#   (a) an async handler that yields control (e.g. awaits I/O or asyncio.sleep), OR
#   (b) a sync handler offloaded via asyncio.to_thread — asyncio.wait_for cancels the
#       AWAIT, not the thread. Python has no thread-cancellation primitive: the worker
#       thread keeps running inside the handler after the timeout fires and the caller
#       unblocks. (2026-08-07 ceremony-lock-leak audit,
#       state/audits/2026-08-07-ceremony-lock-leak-root-cause.md — this comment
#       previously claimed the thread WAS cancelled, which is false and was directly
#       contradicted by invoke/__main__.py's own comments on the same path.)
#
#       The cross-process lock this paragraph used to name was ceremony_lock, deleted
#       2026-08-08 by fa88f327b (docs/plans/2026-08-07-excise-the-ceremony-lock.md).
#       The surviving lock surface is coordinator_core/locked_write, which is
#       kernel-backed (fcntl.flock / msvcrt.locking) and therefore released by the OS
#       on process death by ANY means — os._exit included. os._exit is load-bearing
#       for termination LATENCY, not for lock reclaim: measured 1.22s vs 20.1s for
#       sys.exit, which blocks joining the executor thread.
#       Negative spec: do NOT infer from this that an abandoned handler thread is
#       harmless in a long-lived process. It is harmless only because the process
#       exits. Verified 2026-08-15:
#       docs/research/spike-verdicts/2026-08-15-warm-engine-os-exit-and-lock-reclaim.md
# Blocking-sync handlers NOT wrapped in asyncio.to_thread cannot be interrupted by
# asyncio.wait_for — the event loop stalls for the full blocking duration.
# C3 wraps all sync handlers via asyncio.to_thread to ensure the AWAIT (not the
# thread) can time out, so the caller unblocks even though the thread runs on.
#
# DEC-2 (docs/plans/2026-07-22-wsc-tail-sub-2s-invoke-budget.md): this guard is a
# RUNAWAY guard, not a performance budget — it exists to unblock a caller stuck
# behind a genuinely hung op, not to enforce a target latency. The <2s figure for
# ceremony.wsc_tail (and its wsc_commit/wsc_resolve predecessors) is enforced as a
# regression-tested performance property by KPI test
# (test_wsc_tail_parity.py::test_kpi_wsc_tail_blocking_path_under_2s), not by
# tightening this timeout — collapsing the guard down toward the target would
# convert every transient disk stall into an abandoned-mid-mutation event (see the
# negative-spec below). This guard now applies uniformly to every op, including the
# three ceremony.* ops that formerly carried a widened 120s per-op override — see
# `_timeout_for` below; the override table that used to hold those rows is retired.
#
# Negative-spec (hard-won, relocated verbatim from the retired per-op override
# table's comment block — the Staff Engineer F11: this is MORE true after the override retirement,
# not less, so it must survive the table edit rather than vanish with it): a
# CLIENT-side transport timeout does NOT abort server-side op execution. The engine
# keeps running to completion after the caller has given up — mutating ops (e.g.
# ceremony.wsc_tail, ceremony.wsc_commit) actually commit, so their mutations
# (including git commits) can and do land AFTER the client already treated the call
# as failed. Observed, not theoretical: in the 2026-07-22 sibling dogfood run, two
# commits (fleet.archive_completed_plans, fleet.archive_completed_handoffs) landed
# after the caller's client timed out at its floor. A caller that times out MUST
# reconcile against actual repo state before retrying — never assume a transport
# timeout means "nothing happened" and blind-retry, which re-runs the mutation. Any
# op that also commits inherits this hazard by construction — say so if it does.
#
# Spec backlink: docs/decisions/DR-215-coordinator-core-command-type-execution-model.md
# ---------------------------------------------------------------------------
# NARROW-ONLY ENV KNOB (2026-08-21, PM ruling). This constant is the built-in
# default AND the ceiling `COORDINATOR_DISPATCH_TIMEOUT_SECS` is clamped against
# in `_resolve_dispatch_timeout_secs()`. It is a bare literal on purpose: it used
# to be `float(os.environ.get("COORDINATOR_DISPATCH_TIMEOUT_SECS", "30"))`, which
# made the env var its own ceiling — `COORDINATOR_DISPATCH_TIMEOUT_SECS=420` set
# the module constant to 420 at import, and a clamp against it would have been a
# clamp against nothing. Reading the env here is therefore the bypass, not a
# convenience, and the read is gone.
#
# What the knob can still do: LOWER the guard, live, per request, with no restart
# (`COORDINATOR_DISPATCH_TIMEOUT_SECS=0.5` for a fast-fail run resolves to 0.5).
# What it can no longer do: raise it. An op that does not fit inside its guard is
# an op with a defect; the remedy is to make the op cheaper — fewer spawns,
# batched git, a warm path — never a larger number here or in the environment.
# Re-running a failing op with more time is the behaviour this clamp exists to
# make impossible, and it is impossible from any sibling repo too: the env is the
# only surface a caller outside this process has.
#
# Assigning `ipc.DISPATCH_TIMEOUT_SECS` directly still moves both the default and
# the ceiling together — that is the in-process test seam (several suites raise it
# to exercise the timeout path), and it is not reachable by an operator, who has
# the environment and nothing else.
DISPATCH_TIMEOUT_SECS: float = 30.0


def _warn_on_near_miss_timeout_env(environ: Optional[Dict[str, str]] = None) -> None:
    """Design-as-offers near-miss hint for the timeout env-var knob.

    A caller who sets a `COORDINATOR_*TIMEOUT*`-shaped env var that ISN'T the
    real `COORDINATOR_DISPATCH_TIMEOUT_SECS` knob gets silent no-op today — the
    typo'd var is simply ignored and the caller has no idea why their timeout
    override never took effect. This scans the environment at import time and
    warns on any near-miss so the caller is pointed at the real knob instead of
    debugging a silent no-op.

    Advisory only — this function never clamps anything, and it never offers the
    knob as a way to buy an op more time: the real knob narrows and cannot widen
    (see `DISPATCH_TIMEOUT_SECS`). Naming a var that "did not take effect" must
    not read as "set the right one and your op gets its time back", because it
    would not.

    Narrowed match (Review: code-reviewer F1) — a key only counts as a
    near-miss if it CONTAINS "COORDINATOR" AND ENDS WITH "_TIMEOUT" or
    "_TIMEOUT_SECS" (mirroring the real knob's naming shape), excluding the
    exact real-knob name. A bare "COORDINATOR" + "TIMEOUT" substring-anywhere
    test would also fire on legitimate future vars that merely mention timeout
    in passing (e.g. `COORDINATOR_SUBAGENT_TIMEOUT_LOG_PATH`), turning the
    "did you mean?" hint into a nag — the opposite of design-as-offers intent.

    `environ` is injectable (defaults to `os.environ`) so tests can exercise
    the scan without mutating the real process environment.
    """
    env = os.environ if environ is None else environ
    for key in env:
        upper = key.upper()
        looks_like_timeout_knob = upper.endswith("_TIMEOUT") or upper.endswith("_TIMEOUT_SECS")
        if "COORDINATOR" in upper and looks_like_timeout_knob and key != "COORDINATOR_DISPATCH_TIMEOUT_SECS":
            _log().warning(
                "coordinator_core.ipc: env var %r is set but not a recognized engine "
                "timeout knob. The only one is COORDINATOR_DISPATCH_TIMEOUT_SECS, and "
                "it narrows the dispatch guard only — it cannot widen it.",
                key,
            )


# Review: code-reviewer F2 — import-time side effect must never break
# `import coordinator_core.ipc` for production dispatch, even if a future
# refactor makes the scan capable of raising.
try:
    _warn_on_near_miss_timeout_env()
except Exception:
    _log().debug("coordinator_core.ipc: near-miss timeout env scan failed at import", exc_info=True)

# ---------------------------------------------------------------------------
# Per-op dispatch timeout overrides — RETIRED (DEC-2, docs/plans/
# 2026-07-22-wsc-tail-sub-2s-invoke-budget.md)
#
# This table formerly widened the dispatch cap to 120s for ceremony.wsc_commit,
# ceremony.wsc_resolve, and ceremony.wsc_tail, on the premise that their cold-run
# tail genuinely exceeded the global 30s default. DEC-2 retires all three rows: a
# dispatch timeout is a runaway guard, not a performance budget, and a widened
# per-op cap only masked what should instead be a measured, regression-tested
# latency property. All ops — including these three — now fall to the single
# global DISPATCH_TIMEOUT_SECS runaway guard above, whose comment block carries
# the relocated reconcile-before-retry negative-spec (the Staff Engineer F11) and the DEC-2
# rationale in full. The sub-2s performance target for ceremony.wsc_tail is
# enforced by KPI test (test_wsc_tail_parity.py::test_kpi_wsc_tail_blocking_path_under_2s),
# not by any per-op timeout knob.
#
# Historical spec backlink (why these three ops were widened in the first place):
# state/improvement-queue/2026-07-13-ceremony-wsc-commit-reliably-times-out-o-62330efd3dd4.yaml
# cross-repo/archive/2026-07-13-example-retrieval-repo-ue-addon-em-wsc-commit-30s-timeout-friction.md
#
# Kept as an empty table (not deleted outright) so `_timeout_for`, `--dump-op-timeouts`,
# and the `OP_TIMEOUT_OVERRIDES` cross-repo parity re-export keep their existing shape —
# a future op with a genuinely justified widened cap has a table to land in without
# reintroducing this one's now-retired rationale.
#
# coverage.gate's row was removed here (K-001, state/kill-ledger.md, 2026-08-16): the
# close path no longer invokes the op that needed this widened cap — see the kill
# ledger for the measured cost and disposition. `coverage.gate` may still be reached
# off the close path (mint-only plumbing behind `cmd_brightline_gate`, itself
# removed 2026-08-19 per state/kill-ledger.md K-007); that caller
# now resolves via the ordinary global runaway-guard timeout like any unlisted op.
#
# ceremony.scoped_git_commit carried a 150.0s row here from 2026-08-15, sized at ~3.7x
# headroom over a measured 40.9s worst sample for a ~2100-path publish commit. REVOKED
# 2026-08-21 by the ceremony budget below. The measurement was honest; the conclusion
# was not. That trial recorded 53 `git` subprocess spawns for one commit -- the spawn
# count WAS the defect, and a cap sized to accommodate it is how the cost stayed
# unexamined for six days. A budget is a choice, not a measurement: read against its
# caller's end-to-end target (the close ceremony's 500ms, DR-344), a 150s per-op cap is
# 300x the ceremony that invokes it, which makes the cap itself the finding. Sizing a
# number against the load norm is what `docs/wiki/machine-load-norm.md` forbids outright
# -- a loaded box raises the bar and never relaxes one.
#
# The reconcile-before-retry negative-spec above is UNAFFECTED and more load-bearing
# now, not less: a tighter budget means callers reach it more often, and a client-side
# timeout still never aborts server-side execution. Never blind-retry a timed-out
# ceremony; reconcile against real repo state first.
# ---------------------------------------------------------------------------
#
# percolate.build_token_index (2026-08-26, docs/plans/2026-08-26-payload-parity-asks-
# an-index-not-the-payload.md chunk C3) resolves via the global runaway-guard default
# below rather than a row here, deliberately: it already bounds its own process time
# internally at DR-344's 500ms brightline
# (`coordinator_core.ops.percolate_build_token_index :: _BUDGET_SECS`, derived from
# `coordinator_core.op_census.timing.PROCESS_TIME_BAR_MS`, not a private literal) via
# a deadline stamped at entry with each slice sized from the remainder (DR-349 §
# "Decision" point 4). A per-op row here would only widen its ceiling, which nothing
# about this op needs — the internal deadline is what makes a breach here evidence of
# a real defect (a hung stat/read on a bad filesystem) rather than a tight-cap
# artifact, and a table row would just be a second, looser number to keep in sync
# with the first.
_OP_TIMEOUT_OVERRIDES: Dict[str, float] = {}


# ---------------------------------------------------------------------------
# The ceremony budget -- a ratchet, not a tunable (2026-08-21, PM ruling)
#
# EVERY `ceremony.*` op is bounded at CEREMONY_BUDGET_SECS end-to-end. No per-op
# exception, no env override, no future widening. The number may be LOWERED, never
# raised: `coordinator_core/tests/test_ceremony_budget_ratchet.py` is the enforcement
# and fails on any edit that lifts the constant, admits a widening override row for a
# ceremony op, or lets the env knob out-resolve the ceiling.
#
# Why a ceiling rather than a table of per-op rows: a new ceremony op must be BORN
# inside the budget, not admitted to it. A table binds only the ops someone remembered
# to list, and "the next EM remembers to add a row" is not an artifact (CLAUDE.md
# § North star). Prefix matching makes the correct path the default and leaves no shape
# for an exception to take -- there is no row to add, so there is nothing to negotiate.
#
# Why 2s when the brightline is 500ms: 500ms is the end-to-end target the close ceremony
# is held to (DR-344). This is the RUNAWAY guard around a single op -- already 4x that
# target, deliberately generous so a breach is unambiguous evidence of a real defect
# rather than a tight-cap artifact. It is a ceiling, never a target. An op that needs
# all of it is an op with a problem.
#
# What this breaks, on purpose: a ceremony op whose real cost exceeds 2s now FAILS
# instead of quietly occupying the box for the ~50 peers queued behind it. That failure
# is the signal, and the remedy is always to make the op cheaper -- fewer spawns,
# batched git, a warm path -- never to come back here and raise the number. A request
# to widen it is denied by construction, not by review.
#
# Read docs/decisions/DR-348-the-ceremony-budget-is-a-ratchet.md before touching this
# number.
# ---------------------------------------------------------------------------
CEREMONY_BUDGET_SECS: float = 2.0

#: Method-name prefix every ceremony op shares. Naming is the FIRST membership
#: signal, deliberately: an allow-list would let an op dodge the budget by omission,
#: and there is no row to forget to add. If it is called ceremony, it is budgeted as
#: ceremony, registered or not.
_CEREMONY_METHOD_PREFIX = "ceremony."

#: Package path every ceremony op's implementation lives under. The SECOND membership
#: signal, and the one that closes the rename bypass: the prefix test alone is dodged
#: by `git mv`-free renaming — call `ceremony.scoped_git_commit` something else and the
#: 2s ceiling silently becomes 30s, with no diff a reviewer would read as a budget
#: change. Moving the implementation OUT of the ceremony package is a diff nobody
#: mistakes for a rename.
#:
#: Not hypothetical: `review.snapshot_diff_and_head` and `commit.exec_bit_change`
#: already live in this package under non-ceremony names and escaped the budget on
#: name alone until this signal landed.
_CEREMONY_PACKAGE_PREFIX = "coordinator_core.ops.ceremony."


#: Ops implemented in the ceremony package but NAMED outside the `ceremony.` namespace.
#: Each row exists because the op's name disagrees with where it lives, and the budget
#: follows the implementation rather than the label — otherwise renaming an op is a
#: working bypass of a rule whose whole premise is that it has none.
#:
#: This is a table, not a policy: the honest long-term fix is to rename these ops into
#: the namespace they belong to, which nobody has done because an op name is a wire
#: contract with every caller. Until then the row carries the budget.
#:
#: `test_ops_implemented_in_the_ceremony_package_are_ceremony_ops` is the drift guard —
#: it walks the ops package live and fails when a ceremony-package op appears with
#: neither a `ceremony.` name nor a row here. Adding the op to the package and
#: forgetting this table is a red suite, not a silent escape.
_CEREMONY_PACKAGE_ALIASES = frozenset({
    "commit.exec_bit_change",          # coordinator_core.ops.ceremony.commit_exec_bit
    "review.snapshot_diff_and_head",   # coordinator_core.ops.ceremony.snapshot_diff_and_head
})


def _owning_module_is_ceremony(method: str) -> bool:
    """True when `method`'s implementation lives in the ceremony package.

    Both lookups are strictly free — a dict `get` against tables already resident in
    this process — and neither is allowed to trigger an import. `coordinator_core.ops`
    costs ~420ms and 350 submodules to import; paying that inside a timeout resolver
    on the dispatch hot path would breach the brightline outright
    (`docs/decisions/DR-344-the-brightline-process-budget-for-claude-klabauter.md`), so
    `_registry_map` is consulted only when something else already loaded it.

    Two independent sources, because each covers the other's blind spot:
      - `_REGISTRY` carries the resolved handler's own `__module__`. On the dispatch
        path this is always populated by the time `_timeout_for` runs — handler
        resolution happens upstream in `dispatch_message` — so a renamed ceremony op
        cannot reach `asyncio.wait_for` with the wrong budget.
      - `OP_MODULE_MAP` is the static method->module table, which knows an op's owning
        module without the op being registered. It makes the `--dump-op-timeouts`
        projection exact for a renamed ceremony op in any process that has the ops
        package loaded.

    Returns False rather than raising if neither source is available; the caller falls
    back to the name prefix, which is the pre-existing behaviour.
    """
    handler = _REGISTRY.get(method)
    if handler is not None:
        module = getattr(handler, "__module__", "")
        if isinstance(module, str) and module.startswith(_CEREMONY_PACKAGE_PREFIX):
            return True
    registry_map = _sys.modules.get("coordinator_core.ops._registry_map")
    op_module_map = getattr(registry_map, "OP_MODULE_MAP", None)
    if isinstance(op_module_map, dict):
        module = op_module_map.get(method)
        if isinstance(module, str) and module.startswith(_CEREMONY_PACKAGE_PREFIX):
            return True
    return False


def is_ceremony_method(method: str) -> bool:
    """True when `method` falls under the ceremony budget.

    Single source of truth for ceremony membership: `_timeout_for`, the
    `--dump-op-timeouts` projection, and the ratchet test all route through this
    rather than re-spelling the membership test and drifting apart.

    Membership is a UNION of three signals, never an intersection. Any one of them
    makes an op ceremony, so the budget is escaped only by an op that is neither named
    ceremony, nor listed below, nor resolvable to the ceremony package — at which point
    it is not a rename, it is a different op.

    The frozenset is what makes this correct in a COLD process, and it is not
    redundant with `_owning_module_is_ceremony`. That helper reads two tables that must
    already be resident; it never imports, because importing
    `coordinator_core.ops._registry_map` costs a measured 470ms and pulls 351 ops
    submodules, which breaches DR-344's 500ms end-to-end brightline on its own. So in a
    fresh interpreter — a `--dump-op-timeouts` spawn, a cold CLI invocation — both of
    its sources miss and it returns False. Relying on it alone made membership
    load-order dependent: the same op was budgeted or not depending on what else the
    process happened to have imported, which is the silent escape this budget exists to
    make impossible. The explicit table is O(1), import-free, and true everywhere.
    `_owning_module_is_ceremony` is kept as a belt-and-braces catch for a
    ceremony-package op that lands with no row here, on the warm dispatch path where
    `_REGISTRY` is populated — but it is the backstop, not the guarantee.
    """
    if not isinstance(method, str):
        return False
    return (
        method.startswith(_CEREMONY_METHOD_PREFIX)
        or method in _CEREMONY_PACKAGE_ALIASES
        or _owning_module_is_ceremony(method)
    )


def _resolve_dispatch_timeout_secs() -> float:
    """Live, per-request read of the global runaway-guard timeout, clamped narrow-only.

    Re-reads `COORDINATOR_DISPATCH_TIMEOUT_SECS` from `os.environ` on every
    call rather than trusting the import-time snapshot baked into the
    `DISPATCH_TIMEOUT_SECS` module constant — in a warm, long-lived engine
    process that constant is otherwise pinned for the process's entire life,
    making the knob un-retunable without a restart. Falls back to the
    `DISPATCH_TIMEOUT_SECS` module attribute (not a private closed-over copy)
    when the env var is absent or unparsable, so a caller that sets
    `ipc.DISPATCH_TIMEOUT_SECS` directly (as several tests do) keeps working
    identically to before.

    The env-derived value is then `min()`-ed against `DISPATCH_TIMEOUT_SECS` —
    the same one-directional shape the ceremony budget uses one level down. This
    is THE seam, not one of several: `os.environ` is read here and nowhere else,
    so every consumer downstream (`_timeout_for`, `warm.lifecycle`'s drain
    ceiling, `--dump-op-timeouts`) inherits the ceiling without re-stating it,
    and there is no path by which an unclamped env value reaches a caller.
    Clamping one level up in `_timeout_for` instead would have left this
    function as a public, unbounded second reading of the same knob.

    Narrowing stays fully live: 0.5 resolves to 0.5. Widening is inert, not an
    error — 420 resolves to the default, silently and identically to it, because
    an operator who wanted more time is not entitled to it and a raised
    exception here would land on the dispatch hot path.
    """
    raw = os.environ.get("COORDINATOR_DISPATCH_TIMEOUT_SECS")
    if raw is None:
        return DISPATCH_TIMEOUT_SECS
    try:
        requested = float(raw)
    except (TypeError, ValueError):
        return DISPATCH_TIMEOUT_SECS
    return min(requested, DISPATCH_TIMEOUT_SECS)


def _timeout_for(method: str, msg: Any = None) -> float:
    """Per-op dispatch timeout, with the ceremony budget applied as a hard ceiling.

    THE PUBLISH-LANE CARVE-OUT comes first, and is the one resolution below that can
    exceed `CEREMONY_BUDGET_SECS`. `coordinator_core.publish_lane.budget_for` returns a
    number ONLY for a named op in `PUBLISH_LANE_OPS` inside a declared percolate/publish
    round, and None — no opinion — for every other (op, caller) pair in the tree. So the
    clamp below is untouched for every caller that is not a publish round, including
    every close ceremony, and no environment variable can put a caller into the lane for
    an op the closed list does not name. PM ruling 2026-08-21; DR-350.

    `msg` is the JSON-RPC request envelope when one is in hand, and optional because the
    lane's other signal is the environment. The warm SERVER cannot read the caller's env
    (its own reflects whoever spawned it), so the envelope field is how a lane crosses
    the pipe; the cold path and the `--dump-op-timeouts` probe inherit the env directly
    and need pass nothing. Defaulting to None keeps every existing call site — and the
    ratchet tests, which call this with a bare method name to prove the 2s clamp — byte
    for byte unchanged in behaviour.

    Resolution order: `_OP_TIMEOUT_OVERRIDES[method]` if listed, else the global
    runaway-guard timeout re-resolved per request (C11) via
    `_resolve_dispatch_timeout_secs()` rather than the import-time constant. That
    result is then CLAMPED to `CEREMONY_BUDGET_SECS` for any `ceremony.*` method.

    The clamp is one-directional by construction — `min`, never `max`. A ceremony op
    can resolve BELOW the budget (a future narrower row, or an operator dialling
    `COORDINATOR_DISPATCH_TIMEOUT_SECS` down for a fast-fail run) but can never
    resolve above it, no matter what an override row or the env knob says. That is
    the whole point: the knob that would otherwise be the escape hatch is the one
    thing the clamp is positioned after.

    NON-ceremony ops are bounded too, one level up: `_resolve_dispatch_timeout_secs()`
    clamps the env knob to `DISPATCH_TIMEOUT_SECS`, so an unlisted op resolves to at
    most the built-in default no matter what the environment asks for. The two
    ceilings are separate rules with separate rationales (DR-348 for the ceremony
    budget, the narrow-only knob for the global guard) and neither subsumes the other:
    a ceremony op is capped at 2s even where the global default is 30s, and lowering
    the global default is a distinct change that does not touch this function.

    `_OP_TIMEOUT_OVERRIDES` is empty (DEC-2 retired its three rows; `coverage.gate`
    removed by K-001, state/kill-ledger.md; `ceremony.scoped_git_commit`'s 150s row
    revoked 2026-08-21 by the budget). It is kept as a live table so a genuinely
    justified NON-ceremony widening has somewhere to land.
    """
    lane_budget = publish_lane.budget_for(method, msg)
    if lane_budget is not None:
        return lane_budget
    if method in _OP_TIMEOUT_OVERRIDES:
        resolved = _OP_TIMEOUT_OVERRIDES[method]
    else:
        resolved = _resolve_dispatch_timeout_secs()
    if is_ceremony_method(method):
        return min(resolved, CEREMONY_BUDGET_SECS)
    return resolved


# ---------------------------------------------------------------------------
# Repo-key transport field (C1a seam — AC-1 / AC-1c)
#
# Mandatory JSON-RPC envelope field for all working-tree-scoped ops.
# Missing or empty on a working-tree-scoped op → structured fail-loud error (C1c).
# Emit ops (artifact.emit, backlog.record, goal.append) are common_dir-scoped
# (per-repo emission, 2026-07-07) and REQUIRE this field — no longer central.
# Reading and routing this field into per-request partition resolution lands in C1b/C1c.
#
# Wire shape:  { "_origin_worktree": "/abs/path/to/worktree", ... }
#
# Spec backlink: pln-coordinator-core-global-multip-9ddcf7 § C1a
# ---------------------------------------------------------------------------
_ORIGIN_WORKTREE_FIELD = "_origin_worktree"

# Telemetry-only companion field to _ORIGIN_WORKTREE_FIELD (C7,
# 2026-08-20-a-refusal-cannot-exit-zero) — the CALLER's actual process cwd,
# stamped unconditionally by coordinator_core.invoke.__main__.main for every
# op, including "none"-scoped ops that never get _origin_worktree. Read ONLY
# by op-latency telemetry recording below, as a fallback when
# resolve_request_repo(msg) is None, so a warm-served none-scoped op still
# attributes its row to the caller's repo instead of the server's own cwd
# (coordinator_core.telemetry.op_latency._write_entry's Path.cwd() fallback,
# which in a warm pool worker is the SERVER's cwd). Never used for authz or
# repo-scope resolution — that stays _ORIGIN_WORKTREE_FIELD's job.
_CALLER_CWD_FIELD = "_caller_cwd"

# ---------------------------------------------------------------------------
# AC-1b op-keying table (C1c) — moved to coordinator_core.op_scopes (2026-07-21)
# to break the asyncio-on-import chain: `import coordinator_core` re-exports
# OP_KEY_SCOPE / WORKTREE_SCOPED_OPS as a cross-repo parity surface, and this
# module (ipc.py) does `import asyncio` at top level — so the old inline
# definition here dragged asyncio into every stdlib-only op's import path.
# Re-exported below for backward compatibility: existing callers using
# `from coordinator_core.ipc import OP_KEY_SCOPE, WORKTREE_SCOPED_OPS,
# _OP_KEY_SCOPE` (including test fixtures that mutate `ipc._OP_KEY_SCOPE` in
# place) keep working unchanged — same dict/frozenset objects, just defined
# in op_scopes.py now.
#
# Spec backlink: pln-coordinator-core-global-multip-9ddcf7 § C1c
# DR:            docs/decisions/2026-07-04-coordinator-core-global-multiplex-topology.md § AC-1b
# Amendment:     docs/plans/2026-07-07-per-repo-emission-cutover.md § C3
# Split:         cross-repo/inbox/2026-07-21-claude-central-em-python-bin-cold-invocation-minutes-per-call.md
# ---------------------------------------------------------------------------
from coordinator_core.op_scopes import (  # noqa: E402,F401
    OP_KEY_SCOPE,
    WORKTREE_SCOPED_OPS,
    _OP_KEY_SCOPE,
)

# Module-scope rather than deferred into the dispatch hot path: this module imports
# nothing but `typing`, so it costs no measurable import time, and the suspension
# check runs on EVERY dispatch — a per-call import lookup would be the more expensive
# of the two shapes.
from coordinator_core import op_budget_suspension  # noqa: E402

# Same shape and same reasoning as the suspension import above: stdlib-only, imports
# nothing, and both of its consumers (`_timeout_for`, the two suspension doors) run on
# every dispatch, so a per-call import lookup would cost more than the module does.
from coordinator_core import publish_lane  # noqa: E402

OP_TIMEOUT_OVERRIDES = _types.MappingProxyType(dict(_OP_TIMEOUT_OVERRIDES))


def resolve_op_repo_key(method: str, request_repo: Optional[Path]) -> Optional[Path]:
    """Resolve the effective repo key for this op per the AC-1b keying table.

    Reads _OP_KEY_SCOPE to determine how the per-request repo key is derived:
    - "none":       returns None (no per-request key needed).
    - "common_dir": returns git_common_dir(request_repo) — the shared
                    .git directory, correct for linked-worktree setups.
                    Emit ops (artifact.emit, backlog.record, goal.append) are
                    common_dir-scoped since the 2026-07-07 per-repo-emission cutover.
    - "show_top":   returns request_repo directly (per-worktree key).

    NOTE: "central" scope has been retired (2026-07-07). No op is currently
    classified "central". The sentinel check below handles any legacy "central"
    value as equivalent to "none" for backward compatibility with persisted data,
    but new ops must NOT use "central".

    AC-1c — fail-loud on unresolvable key:
        If the op scope requires a key but _origin_worktree was absent or
        unresolvable, raises ValueError with a diagnostic message.  The caller
        (dispatch_message) converts this to an INVALID_PARAMS (-32602) error —
        never a silent fallback to a default repo.

    Returns:
        Path  — the canonical repo key for partition lookup (already resolved).
        None  — for "none" scope ops (and legacy "central" if any remain).

    Raises:
        ValueError — if scope requires a key but request_repo is None or the
                     path cannot be resolved to a valid git common directory.

    Spec backlink: pln-coordinator-core-global-multip-9ddcf7 § C1c
    Amendment:     docs/plans/2026-07-07-per-repo-emission-cutover.md § C3
    """
    scope = _OP_KEY_SCOPE.get(method, "none")  # unclassified ops → no key (see table note)
    if scope in ("none", "central"):  # "central" retired 2026-07-07; treated as "none" if seen
        return None
    if request_repo is None:
        raise ValueError(
            f"op {method!r} (scope={scope!r}) requires _origin_worktree but it was absent "
            f"or not a valid string.  Pass the originating worktree path as the "
            f"'_origin_worktree' JSON-RPC envelope field."
        )
    if scope == "common_dir":
        try:
            return git_common_dir(request_repo)
        except (RuntimeError, OSError) as exc:
            raise ValueError(
                f"op {method!r} routing key unresolvable: {exc}.  "
                f"Ensure _origin_worktree ({request_repo}) is a valid path inside a git repository."
            ) from exc
    # scope == "show_top": use the resolved worktree path directly
    return request_repo


# ---------------------------------------------------------------------------
# Per-request repo resolution (C1b-ii seam)
# ---------------------------------------------------------------------------

def resolve_request_repo(msg: dict) -> Optional[Path]:
    """Extract and resolve the _origin_worktree from a JSON-RPC message envelope.

    Reads the _ORIGIN_WORKTREE_FIELD top-level envelope field (C1a transport seam) and
    returns the canonical Path (Path.resolve()) if present and non-empty, else None.

    Does NOT validate that the path exists on disk — that is the op handler's job.
    Does NOT enforce that the field is present — enforcement (fail-loud for working-tree
    ops with a missing key) lands in C1c / resolve_op_repo_key.

    Returns:
        Optional[Path] — canonical resolved Path if _origin_worktree is a non-empty string;
                         None if the field is absent, empty, or not a string.

    Spec backlink: pln-coordinator-core-global-multip-9ddcf7 § C1b
    """
    raw = msg.get(_ORIGIN_WORKTREE_FIELD)
    if not raw or not isinstance(raw, str):
        return None
    return Path(raw).resolve()


def resolve_caller_cwd(msg: dict) -> Optional[Path]:
    """Extract the caller's process cwd from a JSON-RPC message envelope (C7).

    Reads the _CALLER_CWD_FIELD companion field (telemetry-only, never
    authz/repo-scope) and returns the canonical Path if present and
    non-empty, else None. Deliberately does NOT ``.resolve()`` against the
    CURRENT process — the whole point is that this path may not exist
    relative to whichever process (warm pool worker or cold CLI) happens to
    read it; ``Path(raw)`` is already absolute (``os.getcwd()`` always is).

    Returns:
        Optional[Path] — the caller's cwd if _caller_cwd is a non-empty
                         string; None if the field is absent, empty, or not
                         a string (e.g. a pre-C7 client that never stamped it).

    Spec backlink: docs/plans/2026-08-20-a-refusal-cannot-exit-zero.md § C7
    """
    raw = msg.get(_CALLER_CWD_FIELD)
    if not raw or not isinstance(raw, str):
        return None
    return Path(raw)


# ---------------------------------------------------------------------------
# Self-report scope-touch contract (design (b) — EM ruling, 2026-08-04)
#
# Purpose: a sanctioned-mutating handler (e.g. an engine op invoked via a
# `coordinator/bin/` CLI, never routed through the PreToolUse Edit/Write hot
# path that fires `track_touched_files`) writes files that otherwise carry
# NO session claim — `session.scope.compute_scope` sees them only via the
# `mtime_dirty_since_started_at` fallback, with no `touched.txt` entry to
# attribute them to a session, so they land in `orphans` and
# `scoped_git_commit` refuses them. Known defect:
# state/improvement-queue/2026-08-03-sanctioned-mutating-clis-record-no-sessi-dedd1f017d02.yaml.
#
# Contract: a handler MAY set `result[_SCOPE_TOUCH_PATHS_KEY]` to a list of
# repo-relative or absolute paths it ACTUALLY WROTE this call — never an
# intended/declared surface. This is a hard requirement, not a style
# preference: state/lessons/2026-08-03-a-commit-pathspec-must-come-from-the-exe-a035adecbc88.yaml
# is a landed lesson that a commit pathspec must come from the REAL write
# set, never a `surface:`-shaped declaration, because the two legitimately
# diverge (a handler that intended to write N paths but only wrote M of them,
# or wrote a different path than planned on a fallback branch). A handler
# that declares nothing behaves exactly as before this contract existed — no
# claim, no change (safe failure direction: under-coverage, never a false
# claim; a rushed/incorrect declaration can only WITHHOLD a path from other
# live sessions — see `session.scope.compute_scope` Step 3 — never falsely
# grant one, since `_record_self_reported_touches` below writes ONLY into
# the resolved session's OWN `touched.txt`, and every downstream reader of
# that file already treats an entry there as a claim to be weighed against
# peer liveness, not as ground truth).
#
# `dispatch_message` (the sole process-level dispatch chokepoint every
# CLI-routed and hook-routed op passes through — see that function's own
# docstring) reads and STRIPS this key BEFORE building the wire envelope —
# the key must NEVER reach a caller. The FLEET envelope shape is documented
# elsewhere as frozen/non-extensible without a bilateral cross-repo memo;
# this key is engine-internal plumbing, not a new envelope field.
#
# Recording reuses `session.scope.touch(sid, path, cwd)` verbatim — the
# existing, already-tested, already-fail-open, already-mtime-disciplined
# primitive every other writer of `touched.txt` shares (see that function's
# own docstring for why a second dialect must never be inlined at a new call
# site). This module adds NO second recorder — only identity resolution
# (`session.core.resolve_session_id`, the substrate's canonical 4-tier
# resolver — no new identity source) and pre-touch validation of the
# declared paths (containment + on-disk existence), since a self-reported
# path is untrusted handler input, not a hook-verified tool-call path.
#
# Fail-open, unconditionally: a failure anywhere in this recording path
# (unresolvable identity, an unreadable/unwritable session dir, a malformed
# declared path) must never fail the op that already succeeded — see
# `_record_self_reported_touches`'s own try/except shape.
#
# Cross-repo containment (2026-08-04 F1 fix, staff-eng REQUIRES_CHANGES):
# a declared path is recorded ONLY when it resolves inside the CALLER's own
# `_origin_worktree` repo — never the declared path's own containing repo.
# An earlier version anchored containment on the declared path itself so
# that `queue.append`'s central `queue_scope` and `queue.promote`'s
# DoE-claude central-root writes (genuinely outside the caller's own
# worktree) could still be recorded. That was unsound and was reproduced
# live: a session id is a REPO-LOCAL namespace key, not a portable identity,
# and `session.scope.touch()` lazily calls `session.core.init()`, so
# recording into a foreign repo MATERIALIZES a real session dir there under
# the caller's sid, with a fresh `last_activity` that reads as a live peer
# for the next 30 minutes (Layer-2 liveness). That phantom peer then
# subtracts against the TARGET repo's own sessions in `compute_scope` Step
# 3 — reproduced stealing a live native session's own claimed file — and
# independently perturbs the target repo's tier-4 sentinel resolution
# (`live_count >= 2` -> ambiguous), which can leave a sibling repo's own
# session unable to resolve its own id.
# shell-doc-ok: the backticked comparison above is a Python boolean
# expression quoted from this module's own sentinel logic, not a shell
# version constraint.
# See
# `_resolve_declared_touch_root_and_path`'s docstring for the containment
# check itself.
#
# Consequence, deliberate: a declared path outside the caller's own repo is
# SKIPPED — never recorded, never written cross-repo. `queue.promote`'s
# writes into the DoE-claude central root therefore stay unclaimed orphans
# at that sink. This is the CORRECT outcome, not a regression — no claim is
# always safer than a WRONG claim, and a wrong claim here actively harms a
# sibling repo claude-klabauter does not own. Do not "fix" this back to declared-path
# anchoring; the DoE-claude side has its own adoption path
# (`--include-orphans`) for exactly this residual.
#
# Every skip is logged (never silent — see `_record_self_reported_touches`),
# since a silently-dropped declaration is indistinguishable from "nothing
# was ever wrong" to an operator debugging a missing claim.
#
# Known residual — linked-worktree containment mismatch (2026-08-05 review,
# not yet fixed): `caller_repo_root` above is derived from `git_root(sid_cwd)`
# — the caller's OWN worktree root, whichever worktree that is. But
# `common_dir`-scoped handlers (`memo.send`/`memo.compose`/`memo.draft`, and
# any sibling following the same precedent) derive the paths they declare
# via `main_worktree_root(git_common_dir(...))` — the repo's MAIN worktree
# root, per Key Decision 5. In a linked-worktree setup where the dispatching
# `_origin_worktree` is a linked worktree (not the main worktree root), these
# two roots differ, so every path such a handler declares fails containment
# here and is silently skipped (logged, not recorded) — the declaration is
# dead on arrival. Fails safe (under-declaration, this contract's designed
# direction), not a correctness bug, but if you're debugging "why wasn't my
# memo-outbox write claimed?" from a linked-worktree checkout, this is why.
_SCOPE_TOUCH_PATHS_KEY = "_scope_touch_paths"

# DR-276: the same declaration expressed as a call rather than a result key, so
# one handler-side API serves both invocation paths. `declared_writes` owns only
# the context-local list; this module remains the sole recorder. See
# coordinator_core/session/declared_writes.py and coordinator_core/cli_entry.py.
from coordinator_core.session.declared_writes import (  # noqa: E402
    _ACTIVE as _declared_writes_var,
)
from coordinator_core.locked_write import LockTimeout, held_lock  # noqa: E402

# Cap on the number of paths a single declaration may carry (2026-08-04 F4
# fix). Re-measured 2026-08-14 (C4, docs/plans/2026-08-14-cli-authored-writes-
# get-claimed.md), AFTER C1 (touch() forwards `root`, engaging
# normalize_touch_path's zero-spawn arm) and C2 (one held_lock acquire per
# batch, not per path) landed — both prior figures in this comment's history
# (a ~9-10ms/24ms warm/cold estimate, and a since-superseded ~402ms/path
# pre-C1 measurement) were wrong at the time they were written and are
# superseded by this one; do not average across them.
#
# Method: 20 end-to-end trials of `_record_self_reported_touches` (this
# module's own recorder — includes the one `held_lock` batch acquire, NOT
# just `normalize_touch_path` in isolation) against 16 real tracked files in
# this repo. Measured on this machine with ~130-135 concurrent
# claude.exe/node.exe processes live (`tasklist`-counted immediately before
# and after the run) — consistent with the documented load norm
# (docs/wiki/machine-load-norm.md: 50-70 concurrent LLMs average, floor two
# dozen), not an idle-box number.
#
# Result: mean 56.8ms, median 49.9ms, p90 73.2ms, max 148.9ms for the full
# 16-path batch (per-path mean ~3.6ms). Against the 300ms MUTATING target
# (`coordinator_core/benchmarks/budget-manifest.json`,
# `defaults.MUTATING.target_ms` — marked `_provisional`, DR-276: the manifest
# is not a runtime ceiling, so treat 300ms itself as not yet settled), even
# the observed max leaves ~150ms of headroom for the handler's own work and
# a colder start than any of these 20 trials hit. 16 stays: it costs well
# under half the (provisional) budget at the measured tail, and a handler
# legitimately writing more than 16 files in one call is off the shape this
# contract was designed for (a single-write-primitive self-report, per the
# module contract above) and should be revisited rather than raising the
# cap. Excess entries are dropped (log-and-truncate), never silently.
_MAX_DECLARED_TOUCH_PATHS = 16

#: Sub-second, bounded acquire timeout for the ONE `touched.txt` batch lock
#: `_record_self_reported_touches` takes per dispatch (C2,
#: docs/plans/2026-08-14-cli-authored-writes-get-claimed.md) — see that
#: function's own comment for why one acquire covers the whole
#: `_MAX_DECLARED_TOUCH_PATHS`-bounded batch rather than one per path.
#: Matches `session.scope._TOUCH_LOCK_TIMEOUT_SECS`'s per-call default; kept
#: as a separate constant because the two call sites (batch vs. single-path)
#: are independent tuning knobs even though they share a starting value
#: today.
_TOUCH_BATCH_LOCK_TIMEOUT_SECS = 0.2


def _resolve_declared_touch_root_and_path(
    raw_path: object, sid_cwd: Optional[str], caller_repo_root: Optional[str]
) -> tuple:
    """Validate one `_SCOPE_TOUCH_PATHS_KEY` entry.

    Returns `(resolved, skip_reason)`: `resolved` is `(repo_root, abs_path)`
    when the declaration is accepted (never `None` in that case);
    `skip_reason` is a short human-readable string when the declaration is
    rejected (and `resolved` is `None`), else `None`. The caller
    (`_record_self_reported_touches`) logs every skip — see this module's
    `_SCOPE_TOUCH_PATHS_KEY` contract comment for why a silent skip is not
    acceptable here even though skipping is the SAFE failure direction.

    A declared path is untrusted handler input (the contract's own
    ACTUALLY-WROTE requirement is unenforceable here — this function cannot
    know what a handler really wrote, only what it claims) — treat it
    accordingly: skip (never raise) on anything that is not a non-empty
    string, resolves outside the CALLER's own repo, is not an existing
    regular file, or the caller's own repo root is itself unresolvable.

    Repo containment is resolved from the CALLER's OWN `_origin_worktree`
    (`caller_repo_root`, precomputed ONCE per dispatch by the caller from
    `sid_cwd` — never re-derived per path, both for cost (F4) and so every
    declared path in one call is judged against the same anchor), NOT from
    the declared path's own containing repo. See this module's
    `_SCOPE_TOUCH_PATHS_KEY` contract comment (F1 fix, 2026-08-04) for why
    anchoring on the declared path itself was unsound and reproduced live —
    in short, a session id is a repo-local namespace key, and recording a
    claim in a repo the caller has no standing in can steal a live native
    session's own file and corrupt that repo's session-liveness resolution.
    The returned `repo_root` (== `caller_repo_root`) is threaded through to
    `session.scope.touch()`'s `cwd` param so the claim lands in the
    caller's own `.git/coordinator-sessions/<sid>/touched.txt` — the one a
    same-repo `scoped_git_commit` call will actually read. `_record_self_
    reported_touches` ALSO forwards it as `touch()`'s `root` param: it was
    resolved via `core.git_root` and realpath'd above, satisfying
    `normalize_touch_path`'s "MUST be the worktree root itself"
    precondition, so this call site can engage the zero-spawn fast arm
    instead of re-deriving the root itself per declared path.

    Only a FILE may be declared, never a directory (F2 fix, 2026-08-04): a
    directory reaches `commit_pipeline.explicit_stage`'s `git add -- <dir>`
    pathspec and stages the entire subtree, while `compute_offer`
    simultaneously (and correctly) lists the files under it as orphans,
    since only the directory entry — not its contents — was ever recorded
    as touched. Reproduced by staff-eng review.

    `os.path.realpath` on the candidate so a macOS `/var` -> `/private/var`
    symlink prefix mismatch cannot produce a false "outside the repo"
    verdict (same discipline as `session.scope.normalize_touch_path`);
    `caller_repo_root` is realpath'd once by the caller.
    """
    if not isinstance(raw_path, str) or not raw_path:
        return None, "not a non-empty string"
    if not caller_repo_root:
        return None, "caller repo root unresolvable"

    if os.path.isabs(raw_path):
        anchor = raw_path
    elif sid_cwd:
        anchor = os.path.join(sid_cwd, raw_path)
    else:
        anchor = raw_path

    abs_path = os.path.realpath(anchor)
    if abs_path != caller_repo_root and not abs_path.startswith(caller_repo_root + os.sep):
        return None, f"outside the caller's own repo ({caller_repo_root})"
    if not os.path.isfile(abs_path):
        return (
            None,
            "not an existing regular file (directories are rejected; a "
            "path that was never actually written cannot be verified)",
        )
    return (caller_repo_root, abs_path), None


def _record_self_reported_touches(result: object, sid_cwd: Optional[str]) -> object:
    """Strip `_SCOPE_TOUCH_PATHS_KEY` from `result` (if present) and record a
    `session.scope.touch()` claim for each validated declared path.

    MUST be called exactly once, on every dispatch path, before the wire
    envelope is built — see the module-level contract comment above this
    function. The key is popped FIRST, unconditionally, before any recording
    is attempted, so a failure anywhere below can never leak it onto the
    wire (fail-open covers recording, never the strip).

    `sid_cwd` (the dispatching request's `_origin_worktree`, when present)
    anchors BOTH session-identity resolution (`session.core.resolve_session_id`'s
    tier-4 sentinel-file fallback reads THIS session's own repo) AND, as of
    the F1 fix (2026-08-04), per-path repo containment — every declared path
    is judged against the CALLER's own repo, never the declared path's own
    containing repo; see `_resolve_declared_touch_root_and_path`'s docstring
    and this module's `_SCOPE_TOUCH_PATHS_KEY` contract comment for why.

    Every declaration this function skips (cap overflow, containment
    failure, not-a-file, ...) is logged — never silently dropped — so an
    operator debugging a missing claim (or auditing a cross-repo attempt)
    has something to grep for.

    Returns `result` unchanged (same object, mutated in place when the key
    was present) — never raises; never fails the op.
    """
    if not isinstance(result, dict) or _SCOPE_TOUCH_PATHS_KEY not in result:
        return result

    declared = result.pop(_SCOPE_TOUCH_PATHS_KEY, None)
    try:
        if not isinstance(declared, list):
            return result

        from coordinator_core.session import core as _session_core
        from coordinator_core.session import scope as _scope

        sid = _session_core.resolve_session_id(sid_cwd)
        if not sid:
            return result  # no resolvable session -> no claim, op still succeeds

        if len(declared) > _MAX_DECLARED_TOUCH_PATHS:
            _log().warning(
                "coordinator_core.ipc: session %r declared %d touch paths, "
                "exceeding the cap of %d — recording only the first %d, "
                "dropping the remaining %d",
                sid, len(declared), _MAX_DECLARED_TOUCH_PATHS,
                _MAX_DECLARED_TOUCH_PATHS, len(declared) - _MAX_DECLARED_TOUCH_PATHS,
            )
            declared = declared[:_MAX_DECLARED_TOUCH_PATHS]

        caller_repo_root: Optional[str] = None
        if sid_cwd:
            root = _session_core.git_root(sid_cwd)
            if root:
                caller_repo_root = os.path.realpath(root)

        # Resolve every declared path FIRST (unchanged per-path fail-open
        # try/except), before any locking decision — a resolution failure
        # for one path must never affect the lock scope covering the rest.
        resolved_paths = []
        for raw_path in declared:
            try:
                resolved, skip_reason = _resolve_declared_touch_root_and_path(
                    raw_path, sid_cwd, caller_repo_root
                )
                if resolved is None:
                    _log().info(
                        "coordinator_core.ipc: skipped self-reported touch "
                        "%r for session %r: %s",
                        raw_path, sid, skip_reason,
                    )
                    continue
                resolved_paths.append(resolved)
            except Exception as exc:  # fail-open — never let one bad path abort the rest
                _log().debug(
                    "coordinator_core.ipc: self-reported touch failed to "
                    "resolve %r: %s",
                    raw_path, exc,
                )

        def _record_touches() -> None:
            for path_repo_root, abs_path in resolved_paths:
                try:
                    _scope.touch(
                        sid, abs_path, path_repo_root, root=path_repo_root,
                    )
                except Exception as exc:  # fail-open — one bad path must not abort the rest
                    _log().debug(
                        "coordinator_core.ipc: self-reported touch failed for "
                        "%r: %s",
                        abs_path, exc,
                    )

        if not resolved_paths:
            return result

        # C2 (docs/plans/2026-08-14-cli-authored-writes-get-claimed.md):
        # acquire the batch lock ONCE for the whole declared-path batch,
        # here at the recorder, rather than once per path inside
        # `session.scope.touch()` — to bound worst-case latency (one
        # acquire instead of up to `_MAX_DECLARED_TOUCH_PATHS`). This lock
        # exists ONLY to bound that latency, never to avoid a nested
        # acquire: `scope.touch` takes no lock of its own to re-enter (its
        # vestigial `lock=` parameter was traced by AC11 and deleted
        # 2026-08-27 — C4/AC17 had already removed the dedup-scan region it
        # once serialized, and `touch_record.append_event`'s single atomic
        # append needs no app-level lock). Every resolved path in
        # one call shares the same `caller_repo_root`
        # (`_resolve_declared_touch_root_and_path` enforces single-repo
        # containment against the caller's own repo — see its docstring),
        # so they also share one record-sink target and one lock, keyed on
        # the SAME `touch-record.jsonl` seam `scope.touch`/`touch_record`
        # use, not the retired `touched.txt` dialect.
        locked = False
        touched_path: Optional[Path] = None
        anchor_repo_root = resolved_paths[0][0]
        try:
            sdir = _session_core.session_dir(sid, anchor_repo_root)
            if sdir:
                touched_path = Path(sdir) / _scope._TOUCH_RECORD_FILENAME
        except Exception as exc:
            _log().debug(
                "coordinator_core.ipc: could not resolve session dir for "
                "batch touch lock: %s", exc,
            )
        if touched_path is not None:
            try:
                with held_lock(
                    Path(os.path.abspath(str(touched_path))),
                    anchor_root=Path(os.path.realpath(anchor_repo_root)),
                    timeout=_TOUCH_BATCH_LOCK_TIMEOUT_SECS,
                ):
                    _record_touches()
                    # Mark the batch done as soon as the body completes, not
                    # after the `with` statement exits — `held_lock`'s
                    # `finally` (`_plat_unlock`/`os.close`) can raise
                    # `OSError` at RELEASE time, AFTER `_record_touches`
                    # already ran the whole batch. Setting `locked = True`
                    # here (inside the `with`) means a release-time OSError
                    # is still caught below, but `locked` is already `True`
                    # by then, so `if not locked:` does NOT re-run
                    # `_record_touches` — over EVERY resolved path — a
                    # second time. Mirrors `scope.py::touch()`'s equivalent
                    # fix. Review: EM addendum (2026-08-15) to code-reviewer
                    # P1/P2.
                    locked = True
            except (LockTimeout, RuntimeError, ValueError, OSError) as exc:
                # LockTimeout: contended past the bound. RuntimeError: no
                # lock backend on this platform. ValueError: held_lock's own
                # absolute-path precondition (defensive; both paths above
                # are realpath'd/abspath'd). OSError: held_lock's acquire
                # path (lock_dir.mkdir, os.open of the sidecar fd) or its
                # release path (_plat_unlock, os.close in the `finally`) can
                # both raise a plain OSError — this site's own outer
                # `except Exception` (below) would also catch it, but this
                # tuple is the one asserting the intended degrade-not-abort
                # contract explicitly, same as `scope.py::touch()`'s
                # matching tuple. Review: code-reviewer P1/P2 (2026-08-14).
                # All four fail open: degrade to recording the batch
                # WITHOUT the latency bound this lock buys — the appends
                # themselves are atomic either way — never abort the whole
                # batch.
                _log().debug(
                    "coordinator_core.ipc: batch touch lock unavailable, "
                    "degrading to per-path locking: %s", exc,
                )
        if not locked:
            _record_touches()
    except Exception as exc:  # fail-open — recording must never fail the op
        _log().debug(
            "coordinator_core.ipc: self-reported touch recording failed: %s", exc
        )
    return result


# ---------------------------------------------------------------------------
# Op-registry — method-name → callable[(params: dict, repo_root: Optional[Path]) -> Any]
#
# Populated by register_op() at import time (op modules call it as a decorator).
# ---------------------------------------------------------------------------
_REGISTRY: Dict[str, Callable] = {}


def get_op_handler(name: str, msg: Any = None) -> Optional[Callable]:
    """Return the registered handler callable for *name*, or None if not registered.

    Callers that need to invoke a fleet op by its public key string (e.g.
    ``"fleet.archive_completed_plans"``) can call this directly — a registry MISS
    triggers the same lazy-import + eager-fallback resolution as the IPC dispatch
    path (`_lazy_import_and_lookup`) before returning None, so it works correctly
    without the caller having to pre-import the op's owning module itself (lazy
    registration is now unconditional — see coordinator_core/ops/__init__.py).
    Returns None only when the op is genuinely unregistered — callers should
    treat None as a not-found condition.

    **TWO refusal shapes, and None is only one of them.** A SUSPENDED or KILLED
    op does not return None: it raises ``op_budget_suspension.OpSuspendedError``
    from the roster check below. The return annotation says ``Optional[Callable]``
    and cannot say otherwise, so this paragraph is the only place the second
    shape is written down. A caller that guards ``if handler is None`` and stops
    there does not refuse a killed op — it crashes on one.

    Handle BOTH, and route them to the SAME answer:

        try:
            handler = get_op_handler(key)
        except OpSuspendedError:
            handler = None          # or the caller's own fail-closed branch
        if handler is None:
            ...                     # the not-found path, unchanged

    Folding the raise into the existing not-found branch is deliberate, not
    laziness: a killed op should reach the fail-closed answer the unregistered
    case already produced, never a second divergent path a caller has to reason
    about separately.

    Why this paragraph exists (2026-08-27): the kill sweep ``d20d56893`` added the
    raise and left this docstring describing None as the only outcome. Thirteen
    non-test call sites implement the contract it taught, all guarding ``is None``
    with no ``try``; one of them raised out of ``post_commit_stamp_and_ship``
    AFTER the ceremony commit had already landed, which is the worst available
    shape — an operator distrusting a commit that actually succeeded. Fixing call
    sites while leaving this text in place would reproduce the defect in the next
    caller written against it.

    2026-07-25 break-class fix: this used to be a bare `_REGISTRY.get(name)`, which
    returned None for any sibling op not yet imported under lazy ops — breaking
    every caller that resolves a sibling op by key (e.g. `cutover.advance`
    resolving `cutover.gate`) whenever it ran as anything other than the directly
    dispatched op. See
    cross-repo/inbox/2026-07-25-doe-claude-em-cutover-advance-cannot-resolve-gate-op.md.

    Review: code-reviewer F11 — added to allow fleet-op callers to resolve handlers
    via the public op key rather than accessing the op module's private handler
    function name directly.
    """
    # The suspension roster, less the publish-lane carve-out (DR-350). `budget_for`
    # returns None — refuse as before — for every op the lane's closed list does not
    # name and for every caller that is not a declared percolate/publish round.
    #
    # `msg` is threaded for the SAME reason `_timeout_for` takes it, and omitting it
    # here was a live defect: `_dispatch_message_impl` yields to the lane at its own
    # suspension check (which reads the envelope) and then calls THIS function to
    # resolve the handler. On the warm path this process is the server, whose
    # `os.environ` is its spawner's — so an env-only check refused a lane request that
    # had just been admitted one step earlier, and the envelope field that exists
    # precisely to carry the lane across the pipe was ignored at the second door. The
    # cold path masked it (env is inherited there), which is why it survived a live
    # 9-row publish round.
    #
    # Defaulting to None keeps the in-process "path 3" callers (`tail_ops.py`)
    # reading the environment alone, which is correct for them: those resolve in
    # the CALLER's own process, where the env IS the caller's.
    #
    # `safe_commit_offer.py` was named here until 2026-08-27 and no longer
    # belongs: it is now the `session.safe_commit_offer` op, so it resolves in
    # THIS process, the server, whose env is its spawner's. Its handler takes
    # identity from the caller's `cwd` wire param through
    # `resolve_session_id(cwd)` for exactly that reason. A module moving from
    # this list to the registry has to move its identity read with it — an
    # env-only read left behind commits under the engine's session, not the
    # caller's.
    if op_budget_suspension.is_suspended(name) and publish_lane.budget_for(name, msg) is None:
        raise op_budget_suspension.OpSuspendedError(
            op_budget_suspension.refusal_message(name)
        )
    handler = _REGISTRY.get(name)
    if handler is None:
        handler = _lazy_import_and_lookup(name)
    return handler


def register_op(name: str, handler: Optional[Callable] = None) -> Callable:
    """Register a JSON-RPC method handler in the op-registry.

    Supports two calling forms:

    Parametrized decorator (preferred — used by op modules):
        @register_op("ping")
        async def _ping(params: dict, repo_root: Optional[Path] = None) -> dict: ...

    Direct call (for programmatic registration):
        register_op("ping", _ping)

    Handler signature: (params: dict, repo_root: Optional[Path]) -> Any
        params:     the "params" field from the JSON-RPC request (dict; empty dict if absent).
        repo_root:  the per-request resolved repo root (Path or None).  Derived from the
                    _origin_worktree envelope field (C1b-ii seam).  None for "none"-keyed
                    ops (ping, advisory hooks). Non-None (common_dir) for emit ops
                    (artifact.emit, backlog.record, goal.append) — per-repo emission
                    (reclassified from "central" 2026-07-07).
        return:     serialised as the "result" field of the JSON-RPC response.

    Handlers may be async (preferred) or sync.  Async handlers run on the event loop
    and are wrapped with asyncio.wait_for for per-request timeout (AC-3).  Sync handlers
    are offloaded to asyncio.to_thread (thread-pool executor) so the event loop stays
    live for other partitions while sync work runs — do NOT add blocking I/O to async
    handlers without wrapping it in asyncio.to_thread at the call site (AC-3 Gap-3).
    """
    def _decorator(fn: Callable) -> Callable:
        _REGISTRY[name] = fn
        _log().debug("coordinator_core.ipc: registered op %r", name)
        return fn

    if handler is not None:
        # Direct call: register_op("ping", _ping)
        return _decorator(handler)
    # Decorator factory: @register_op("ping")
    return _decorator


def _record_registry_fallback(method: str, stage: str, mapped: bool) -> None:
    """Count one escalation past the targeted-import fast path — never raise.

    Deliberately swallowing: an op must not fail to resolve because its
    telemetry could not be written (registry_fallback_counter's
    CANNOT-BREAK-DISPATCH CONTRACT). Imports are function-local and the write
    is unconditional-cost only on a path already paying for a 233-module
    sweep, so this adds nothing measurable to the fast path it is absent from.
    """
    try:
        from coordinator_core import registry_fallback_counter as _fbc
        from coordinator_core.ops.session_context import resolve_current_session_id

        _fbc.record_registry_fallback(
            method, stage, resolve_current_session_id() or "", mapped=mapped
        )
    except Exception:  # noqa: BLE001 -- see docstring; telemetry never breaks dispatch
        pass


def _lazy_import_and_lookup(method: str) -> Optional[Callable]:
    """Import the owning module for *method* on a registry MISS, then re-check.

    Lazy op registration (F6 / claude-klabauter-windows-portability § C4): imports fire each
    op module's register_op(...) side-effect. This targets ONLY the missing op's
    module instead of the whole coordinator_core.ops package (~55 modules).

    Resolution order:
      1. OP_MODULE_MAP[method] — if present, import that module and re-check the
         registry. Handles the common case in one targeted import.
      2. HOOKS-SCOPED FALLBACK (C2, docs/plans/2026-08-06-windows-hot-path-less-
         work-per-interpreter.md) — if *method* is a `hooks.*` op and step 1
         didn't register it, force coordinator_core.hooks's own full-load
         routine (`coordinator_core.hooks._eager_import_all()`) and re-check.
         Every `hooks.*` key in OP_MODULE_MAP maps to the shared
         "coordinator_core.hooks" package value (one import registers all 15
         hooks.* ops — see that map's header comment), so — lazy registration
         now being unconditional — the step-1 import of "coordinator_core.hooks"
         is always a no-op that never registers anything and would otherwise
         always fall through to step 3.
         This stage is ordered AHEAD of step 3 specifically so a hooks.* miss
         is served by importing only the coordinator_core.hooks package (~15
         modules) rather than escalating to all ~160 op modules (~562
         sys.modules entries) — strictly cheaper, and it is what makes the C1
         lazy-hooks channel a net win rather than a regression.
      3. SAFE FALLBACK — if the map has no entry for *method*, OR the targeted
         import in step 1 (and, for hooks.* ops, step 2) didn't actually
         register it (stale/drifted map entry), force a full import of every
         op module (coordinator_core.ops._eager_import_all()) — today's eager
         behavior — and re-check once more. This is the never-worse-than-today
         guarantee: an incomplete map degrades to today's correctness, not to
         a broken dispatch.

         Calling _eager_import_all() directly (rather than
         importlib.import_module("coordinator_core.ops")) is deliberate: lazy
         registration is unconditional now, so the coordinator_core.ops
         package is always already in sys.modules with its eager-import
         skipped — a bare re-import of the package is a cached no-op and would
         NOT trigger the other op modules. Calling the function directly
         forces every op module to import regardless of the package's cached
         init state (each already-imported submodule import is itself a cheap
         no-op, so this is safe to call unconditionally).

    Returns:
        The registered handler callable if found after import, else None.
    """
    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    module_path = OP_MODULE_MAP.get(method)
    if module_path is not None:
        try:
            importlib.import_module(module_path)
        except ImportError as exc:
            _log().debug(
                "coordinator_core.ipc: lazy import of %r for op %r failed: %s "
                "— falling back to full coordinator_core.ops import",
                module_path, method, exc,
            )
        else:
            handler = _REGISTRY.get(method)
            if handler is not None:
                return handler
            _log().debug(
                "coordinator_core.ipc: lazy import of %r did not register op %r "
                "(stale OP_MODULE_MAP entry?) — falling back to full "
                "coordinator_core.ops import",
                module_path, method,
            )

    # HOOKS-SCOPED FALLBACK (C2): a hooks.* miss escalates to the hooks
    # package's own full-load routine BEFORE the ops-wide SAFE FALLBACK below,
    # so it never pays for a 562-module ops import to serve one hook.
    if method.startswith("hooks."):
        from coordinator_core.hooks import _eager_import_all as _hooks_eager_import_all

        # DELIBERATELY NOT COUNTED HERE. Every `hooks.*` key in OP_MODULE_MAP
        # maps to the shared "coordinator_core.hooks" package value, so step 1
        # is ALWAYS a no-op for them and this stage is the designed resolution
        # path, not a miss — measured firing on 100% of hooks.* dispatches.
        # Counting it would put a record on every tool call and bury the
        # ops-wide cliff this telemetry exists to surface. A hooks.* op that
        # this stage fails to resolve still falls through to the safe fallback
        # below, which does count it.
        _hooks_eager_import_all()
        handler = _REGISTRY.get(method)
        if handler is not None:
            return handler
        _log().debug(
            "coordinator_core.ipc: hooks-scoped full import did not register "
            "op %r — falling back to full coordinator_core.ops import",
            method,
        )

    # SAFE FALLBACK: unmapped op or a map entry that didn't pan out — force a
    # full import of every op module (bypasses the lazy-skip, see docstring
    # above) and retry. Never-worse invariant.
    from coordinator_core.ops import _eager_import_all

    _record_registry_fallback(
        method, "safe-fallback", mapped=module_path is not None
    )
    _eager_import_all()
    return _REGISTRY.get(method)


# ---------------------------------------------------------------------------
# Shared dispatch core — steps 2-7 on a pre-parsed message dict
# ---------------------------------------------------------------------------

def _handler_exception_error(exc: BaseException) -> dict:
    """Build the JSON-RPC error object for an exception that escaped an op handler.

    Selects STRUCTURAL_PIN_ERROR (preserving the exception's own message, which already
    states the remediation — see ContractPinError) when ``exc`` carries the
    ``structurally_wedged`` duck-type marker; selects INVALID_PARAMS (preserving the
    exception's own message, length-bounded — see CallerFacingValidationError) when ``exc``
    carries the ``caller_facing_validation`` duck-type marker; otherwise falls back to the
    generic INTERNAL_ERROR shape, which now carries both the exception class name and its
    own ``str(exc)`` text — mirroring the STRUCTURAL_PIN_ERROR shape above — rather than
    discarding the message text as the prior class-name-only reduction did.
    """
    if getattr(exc, "structurally_wedged", False):
        return {"code": STRUCTURAL_PIN_ERROR, "message": f"{type(exc).__name__}: {exc}"}
    if getattr(exc, "caller_facing_validation", False):
        message = str(exc)
        if len(message) > _CALLER_FACING_MESSAGE_MAX_LEN:
            message = message[:_CALLER_FACING_MESSAGE_MAX_LEN] + "...(truncated)"
        return {"code": INVALID_PARAMS, "message": message}
    return {"code": INTERNAL_ERROR, "message": f"Internal error: {type(exc).__name__}: {exc}"}


async def _dispatch_message_impl(msg: dict) -> dict:
    """Validate a pre-parsed JSON-RPC 2.0 message dict and invoke the registered handler.

    Purpose: shared core for the dispatch pipeline — takes an ALREADY-PARSED dict
    (step 1, JSON parsing, is handled by the caller) and performs steps 2-7, returning
    a JSON-RPC 2.0 result-or-error dict (NOT bytes).

    Validation precedence (spec-pinned — do NOT reorder):
        jsonrpc version → params type → method string → op suspension →
        registry lookup → handler invoke

    Spec backlink: pln-pcore-03-beachhead-coordinator-core-fecdbb § C1 / C1b

    Returns:
        On success: {"jsonrpc": "2.0", "id": id_, "result": <handler return value>}
        On error:   {"jsonrpc": "2.0", "id": id_, "error": {"code": <int>, "message": <str>}}
    """
    # asyncio deferred to first use here (not module scope) — this is the only function
    # in the module that touches the asyncio namespace at runtime (the `async def` keyword
    # itself needs no import); a module-scope `import asyncio` dragged asyncio.base_events
    # (~9ms) into every stdlib-only op's import path, including the read-only /pickup brief
    # path that never calls dispatch_message. Spec: docs/plans/2026-07-24-canonical-resolution-engine.md task W0-1.
    import asyncio

    id_ = msg.get("id")          # echoed on both result and error (AC12)

    # Step 2: Validate jsonrpc version field (JSON-RPC 2.0 §4).
    jsonrpc = msg.get("jsonrpc")
    if jsonrpc != "2.0":
        return {
            "jsonrpc": "2.0",
            "id": id_,
            "error": {
                "code": INVALID_REQUEST,
                "message": f"Invalid Request: 'jsonrpc' must be '2.0', got {jsonrpc!r}",
            },
        }

    method = msg.get("method")

    # Step 3: Validate params type (JSON-RPC 2.0 §4: params MUST be Object when present).
    # Array (positional) params are not supported — reject with -32602.
    params_raw = msg.get("params")
    if params_raw is None:
        params = {}
    elif isinstance(params_raw, dict):
        params = params_raw
    else:
        return {
            "jsonrpc": "2.0",
            "id": id_,
            "error": {
                "code": INVALID_PARAMS,
                "message": (
                    f"Invalid params: named params (Object) required; "
                    f"got {type(params_raw).__name__}"
                ),
            },
        }

    # Step 4: Validate method field
    if not isinstance(method, str) or not method:
        return {
            "jsonrpc": "2.0",
            "id": id_,
            "error": {
                "code": INVALID_REQUEST,
                "message": "Invalid Request: 'method' must be a non-empty string",
            },
        }

    # Step 4b: Refuse a SUSPENDED op (PM ruling 2026-08-21 — over 2s max is off).
    #
    # Positioned deliberately BEFORE the registry lookup below, not after: the lookup
    # lazily imports the op's owning module, and for a suspended op that import buys
    # nothing — the call is going to be refused either way. Refusing here makes a
    # breach cost one dict lookup instead of a module import on a box running ~50
    # concurrent sessions.
    #
    # Consequence, stated rather than discovered: this fires for EVERY caller,
    # including the CLIs and hooks that wrap these ops. That is the ruling's intent —
    # the op stops firing, and the failure is what surfaces who actually needed it.
    #
    # The publish-lane carve-out (DR-350) is the one caller this refusal yields to, and
    # it is read from `msg` as well as the environment: on the warm path THIS process is
    # the server, whose `os.environ` reflects whoever spawned it rather than the caller
    # of this request, so the envelope field is the only honest signal available here.
    if op_budget_suspension.is_suspended(method) and publish_lane.budget_for(method, msg) is None:
        _log().debug("coordinator_core.ipc: refusing suspended op %r", method)
        return {
            "jsonrpc": "2.0",
            "id": id_,
            "error": {
                "code": OP_SUSPENDED_ERROR,
                "message": op_budget_suspension.refusal_message(method),
            },
        }

    # Step 5: Look up handler (AC12: unknown method → -32601)
    #
    # Lazy op registration (F6 / claude-klabauter-windows-portability § C4): the op-registry
    # is populated by each op module's register_op(...) side-effect at IMPORT time.
    # Rather than eagerly importing all ~55 op modules at process startup (Windows
    # cold-compile tax, no __pycache__ warm-up), a registry MISS here triggers a
    # targeted import of ONLY the missing op's owning module (via OP_MODULE_MAP),
    # then retries the lookup once. If the map has no entry for this op, or the
    # targeted import still didn't register it (map drift), fall back to importing
    # the whole coordinator_core.ops package — today's eager behavior — and retry
    # once more. This makes the map a pure performance optimization: a stale or
    # incomplete map degrades to today's correctness, never to a broken dispatch.
    handler = get_op_handler(method, msg)
    if handler is None:
        # 2026-07-21 break-class fix: a registry MISS that survives both the
        # targeted lazy import AND the SAFE FALLBACK full eager import (see
        # _lazy_import_and_lookup) is ambiguous on its face — it could mean
        # "no such op" OR "the op's owning module failed to import". Those
        # are very different failures and must not look the same to a
        # caller. Disambiguate via coordinator_core.ops._POISONED_MODULES
        # (populated by _eager_import_all(), which the SAFE FALLBACK above
        # just ran): if *method*'s owning module (per OP_MODULE_MAP) is
        # recorded there, surface the REAL import exception instead of a
        # generic "Method not found" — see coordinator_core/ops/__init__.py's
        # module docstring "Negative-spec" for why silently reporting
        # "unknown op" here would be strictly worse than today's collapse.
        from coordinator_core.ops import get_poisoned_modules
        from coordinator_core.ops._registry_map import OP_MODULE_MAP

        owning_module = OP_MODULE_MAP.get(method)
        poisoned = get_poisoned_modules()
        if owning_module is not None and owning_module in poisoned:
            cause = poisoned[owning_module]
            _log().debug(
                "coordinator_core.ipc: method %r not found because its "
                "owning module %r failed to import: %s",
                method, owning_module, cause,
            )
            return {
                "jsonrpc": "2.0",
                "id": id_,
                "error": {
                    "code": INTERNAL_ERROR,
                    "message": (
                        f"Op {method!r} is unavailable: its owning module "
                        f"{owning_module!r} failed to import "
                        f"({type(cause).__name__}: {cause})"
                    ),
                },
            }
        _log().debug("coordinator_core.ipc: method not found: %r", method)
        return {
            "jsonrpc": "2.0",
            "id": id_,
            "error": {
                "code": METHOD_NOT_FOUND,
                "message": f"Method not found: {method!r}",
            },
        }

    # C1b-ii: extract the per-request repo_root from the _origin_worktree envelope field.
    request_repo = resolve_request_repo(msg)

    # C1c / AC-1b: resolve the effective repo key for this op per the keying table.
    # Fail-loud (AC-1c) if the op requires a key but _origin_worktree was absent or
    # unresolvable — never fall back to a silent default repo.
    try:
        op_repo_key = resolve_op_repo_key(method, request_repo)
    except ValueError as exc:
        _log().debug("coordinator_core.ipc: routing key error for %r: %s", method, exc)
        return {
            "jsonrpc": "2.0",
            "id": id_,
            "error": {
                "code": INVALID_PARAMS,
                "message": f"Missing required routing key: {exc}",
            },
        }

    # Step 6: Invoke handler — pass (params, repo_root=op_repo_key) so handlers receive the
    # canonical per-request repo key (git_common_dir or show-toplevel per AC-1b table).
    # For central/"none"-scoped ops op_repo_key is None.
    #
    # C3 / AC-3 fault containment:
    #   - asyncio.wait_for provides a per-request timeout (DISPATCH_TIMEOUT_SECS) that
    #     bounds the poison-request class for BOTH async and sync handlers.
    #   - Async handlers: wait_for can cancel the coroutine if it yields (e.g. awaits I/O).
    #     Blocking I/O inside async handlers MUST be wrapped in asyncio.to_thread at the
    #     call site (see AC-3 Gap-3 / async-handler-discipline grep gate).
    #   - Sync handlers: offloaded via asyncio.to_thread so the event loop is not stalled
    #     while sync work runs in a thread-pool executor.  This makes the per-invocation
    #     timeout effective: abandoning the thread future unblocks the caller.
    #   - CancelledError from asyncio shutdown MUST propagate (re-raised, never swallowed).
    #   - BaseException subclasses (SystemExit, KeyboardInterrupt, MemoryError) that escape
    #     an op handler are logged and converted to INTERNAL_ERROR, preventing unexpected
    #     process-level side-effects from propagating to the caller.
    #
    # Review: code-reviewer — F5: inspect.iscoroutinefunction preferred over
    # asyncio.iscoroutinefunction (deprecated Python 3.12+); no behavior change.
    #
    # Spec backlink: pln-coordinator-core-global-multip-9ddcf7 § C3
    #
    # DR-215 command-type retirement: drain/in-flight machinery removed — no concurrent
    # requests in the command-type model (one op per process). _handle_connection and
    # start_server_async removed by C5. is_draining/in_flight_increment/in_flight_decrement
    # no longer called from dispatch_message.
    # Backlink: docs/decisions/DR-215-coordinator-core-command-type-execution-model.md
    # `msg` is threaded through so a warm-served request carries its own publish-lane
    # declaration (DR-350): this process's environment is the SERVER's, not the
    # caller's, so the envelope is the only place the lane can be read from here.
    op_timeout = _timeout_for(method, msg)
    # DR-276: open a declare-write collection around the handler so an op may use
    # `session.declared_writes.declare_write()` and have it work identically here
    # and on the in-process path (`coordinator_core.cli_entry.run_op_main`). The
    # list object is bound BEFORE `asyncio.to_thread` copies the context — that
    # copy makes a rebind invisible to us, but appends to the same list are not.
    # Setting `result[_SCOPE_TOUCH_PATHS_KEY]` directly remains fully supported;
    # the two are merged below, so an op may use either or both.
    #
    # Deliberate: a declaration made before the handler RAISES or TIMES OUT is
    # dropped, because those branches return an error envelope without reaching
    # the merge. That is this contract's designed direction (under-declaration,
    # never a false claim) and matches the pre-DR-276 behaviour exactly.
    #
    # C11: bound via `ContextVar.set()`'s returned Token, reset in `finally` below
    # (mirroring `session.declared_writes.collecting()`'s own shape, and the
    # Token/reset pattern `contract.apply_base.session_identity()` uses for the
    # same overlapping-dispatch problem). The prior comment here argued nothing
    # leaks because "every dispatch REBINDS the var as its first act" — TRUE
    # serially, FALSE under overlapping requests: two interleaved `to_thread`-
    # offloaded dispatches share the same `ContextVar` slot in the absence of a
    # per-dispatch Token/reset pair, so a later dispatch's bare `set()` can stomp
    # a still-in-flight sibling's list, and unwinding via `reset()` is the only
    # way to hand the slot back to whatever the caller's context held before this
    # dispatch (default `None`, or an outer `collecting()` block) rather than
    # leaving this dispatch's list bound after it returns. The failure direction
    # is a MISATTRIBUTED write claim — exactly the direction the prior comment
    # said could not happen.
    _declared_writes: list = []
    _declared_writes_token = _declared_writes_var.set(_declared_writes)
    # `inspect` is imported HERE, not at module scope: it costs 13 modules on the
    # engine's cold-start path (ast/dis/tokenize/opcode/annotationlib/weakref and
    # friends) for one predicate call. This module is measured against a
    # module-count ceiling — `coordinator_core/benchmarks/import-budget-manifest.json`
    # `/entrypoints/coordinator_core.ipc`. After the first dispatch it is a
    # `sys.modules` hit, so the per-call cost is a dict lookup. Do NOT hoist it
    # back to module scope to tidy the import block.
    import inspect
    try:
        if inspect.iscoroutinefunction(handler):
            # Async handler: wrap with per-request timeout.
            # The timeout is only interruptible if the handler actually yields (await).
            # Blocking I/O inside the handler must be wrapped in asyncio.to_thread
            # at the handler's call site (AC-3 Gap-3 — enforced by CI grep gate).
            try:
                result = await asyncio.wait_for(
                    handler(params, repo_root=op_repo_key),
                    timeout=op_timeout,
                )
            except asyncio.TimeoutError:
                _log().error(
                    "coordinator_core.ipc: op %r timed out after %ss", method, op_timeout
                )
                return _timeout_error_envelope(method, op_timeout, id_)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                _log().error(
                    # %s, not %r, on exc: repr() re-escapes an already-quoted
                    # message, so a Windows path in an op's own error text reaches
                    # the operator with doubled separators
                    # ("X:\\repo\\docs\\..."). The exception TYPE is already
                    # carried by the preceding %s, so nothing is lost.
                    "coordinator_core.ipc: op %r raised %s: %s", method, type(exc).__name__, exc,
                    exc_info=True,
                )
                return {
                    "jsonrpc": "2.0",
                    "id": id_,
                    "error": _handler_exception_error(exc),
                }
        else:
            # Sync handler: offload to thread-pool executor so the event loop is not
            # stalled while the sync work runs; makes the per-invocation timeout effective.
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(handler, params, repo_root=op_repo_key),
                    timeout=op_timeout,
                )
            except asyncio.TimeoutError:
                _log().error(
                    "coordinator_core.ipc: op %r timed out after %ss", method, op_timeout
                )
                return _timeout_error_envelope(method, op_timeout, id_)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                _log().error(
                    # %s, not %r, on exc: repr() re-escapes an already-quoted
                    # message, so a Windows path in an op's own error text reaches
                    # the operator with doubled separators
                    # ("X:\\repo\\docs\\..."). The exception TYPE is already
                    # carried by the preceding %s, so nothing is lost.
                    "coordinator_core.ipc: op %r raised %s: %s", method, type(exc).__name__, exc,
                    exc_info=True,
                )
                return {
                    "jsonrpc": "2.0",
                    "id": id_,
                    "error": _handler_exception_error(exc),
                }
        # _mark_partition_active removed by DR-215 strip (2026-07-06) — no-op call retired.
        #
        # Self-report scope-touch contract (design (b) — see the module-level comment
        # above _SCOPE_TOUCH_PATHS_KEY): strip + record BEFORE the wire envelope is
        # built, on every dispatch path (both the async and sync handler branches
        # above converge into this one `result` var). Fail-open by construction —
        # never raises, never mutates `result`'s shape beyond popping the key.
        #
        # DR-276: fold anything the handler declared via `declare_write()` into the
        # same key, so the two declaration styles converge before the single
        # recorder runs. Order is preserved and duplicates are dropped here rather
        # than in the recorder, which caps at _MAX_DECLARED_TOUCH_PATHS and would
        # otherwise spend cap budget on repeats.
        if _declared_writes and isinstance(result, dict):
            merged = list(result.get(_SCOPE_TOUCH_PATHS_KEY) or []) + list(_declared_writes)
            deduped = list(dict.fromkeys(merged))
            result[_SCOPE_TOUCH_PATHS_KEY] = deduped
        result = _record_self_reported_touches(result, str(request_repo) if request_repo else None)
        return {"jsonrpc": "2.0", "id": id_, "result": result}
    finally:
        _declared_writes_var.reset(_declared_writes_token)


def _timeout_error_envelope(method: str, op_timeout: float, id_: Any) -> dict:
    """The error envelope for a dispatch that exceeded its budget.

    WHY A TIMED-OUT MUTATION IS NOT A FAILURE (F1, 2026-08-27). `asyncio.
    wait_for` cancels the AWAIT, never the THREAD -- this module's own AC-3
    comment above says so. So when a sync handler offloaded via `asyncio.
    to_thread` breaches its budget, the handler keeps running to completion in
    its thread: the commit lands, the handler computes the correct answer, and
    that answer is discarded because nobody is waiting for it any more. The
    caller then sees a bare `INTERNAL_ERROR` reading "op timed out", which is
    indistinguishable from "the op did not run" -- and re-invokes.

    That re-invocation is the actual defect. It has been observed six times on
    `ceremony.scoped_git_commit`, proven by a Commit-Token mismatch between the
    landed commit and the token its own retry searched for, and once it left a
    duplicate empty commit in shared history. See
    `state/bug-backlog/2026-08-26-run-commit-pipeline-reports-failure-af-
    3f84418091fd.yaml`.

    So: for an op that MAY MUTATE, report indeterminacy rather than failure,
    reusing the envelope `warm/client.py` already landed one seam out --
    `WARM_DISPATCH_INDETERMINATE` and `_MUTATION_INDETERMINATE_MESSAGE`,
    classified by that module's fail-closed `_op_may_mutate` (an unknown op
    answers True, because one honest error costs less than a mutation executed
    twice). A COMPUTE_ONLY op keeps the plain `INTERNAL_ERROR`: re-running a
    read is free, and nothing is gained by making its caller reconcile.

    Negative-spec:
      - Does NOT stop the abandoned thread. Nothing short of a cancellable
        handler protocol does. A genuinely slow commit still occupies the box
        for its full duration while its caller has moved on. This removes the
        DOUBLE EXECUTION, which is the disease; the misreport was the symptom.
      - Does NOT widen any budget. `CEREMONY_BUDGET_SECS` is untouched and must
        stay untouched -- the ratchet's own rule is that the remedy is to make
        the op cheaper, never to raise the number. What this changes is that a
        breach is now a CLEAN failure, which is what that ratchet already
        assumed it was.
      - Imports `warm.client` LAZILY, inside the function. `warm.client`
        imports this module (`_mutation_deadline_for` reads `_timeout_for`), so
        a module-scope import here is a cycle; this module is also measured
        against an import-budget ceiling and this path runs only on a timeout.
      - An import that fails falls back to the historical `INTERNAL_ERROR`
        rather than inventing a local copy of the constant -- a second copy of
        a shared code is how the two spellings drift apart.
    """
    try:
        from coordinator_core.warm.client import (  # noqa: PLC0415 - see docstring
            WARM_DISPATCH_INDETERMINATE,
            _MUTATION_INDETERMINATE_MESSAGE,
            _op_may_mutate,
        )
    except Exception:  # pragma: no cover - defensive, see negative-spec
        return {
            "jsonrpc": "2.0",
            "id": id_,
            "error": {
                "code": INTERNAL_ERROR,
                "message": f"op timed out after {op_timeout}s",
            },
        }

    if _op_may_mutate(method):
        return {
            "jsonrpc": "2.0",
            "id": id_,
            "error": {
                "code": WARM_DISPATCH_INDETERMINATE,
                "message": (
                    f"{_MUTATION_INDETERMINATE_MESSAGE} "
                    f"(op {method!r} timed out after {op_timeout}s)"
                ),
            },
        }

    return {
        "jsonrpc": "2.0",
        "id": id_,
        "error": {
            "code": INTERNAL_ERROR,
            "message": f"op timed out after {op_timeout}s",
        },
    }



async def dispatch_message(msg: dict, *, caller: Optional[str] = None) -> dict:
    """Validate + dispatch ``msg``, recording a durable per-op wall-clock sample.

    Thin timing wrapper around ``_dispatch_message_impl`` (which retains this
    function's original name in every doc comment/backlink above and does the
    actual JSON-RPC validation/dispatch work unchanged). This is the SOLE
    process-level dispatch chokepoint every CLI-routed
    (coordinator_core.invoke.__main__) and hook-routed (dispatch_from_hook)
    call passes through — wrapping here captures every invocation exactly
    once, with no risk of double-counting a call that goes through both a
    CLI wrapper and this function.

    Records via coordinator_core.telemetry.op_latency.record_op_latency —
    see that module's own docstring for the sink, concurrency, kill-switch,
    and fail-open guarantees. A telemetry failure here can never fail the
    op: the record call is unconditionally wrapped to swallow every
    exception, and this wrapper itself never raises anything the impl
    function did not already raise (asyncio.CancelledError re-raises through
    unchanged — recording best-effort completes first via `finally`).

    Also appends a "started" row (op_latency.record_op_started) BEFORE
    `_dispatch_message_impl` is awaited, wrapped in the same unconditional
    swallow-everything contract. A process killed after that point (e.g. a
    caller-side `subprocess.run(timeout=)` in cc_invoke, which kills the
    child before this `finally` block ever runs) still leaves the started
    row on disk — the vanished-invocation case op_latency's module docstring
    now documents. The started and completion rows share a `corr_id` minted
    once at entry via `op_latency.new_correlation_id()`.

    ``caller`` (C15, 2026-08-25-reconcile-open-comes-back-under-the-bar):
    the invoking module/entry point, DECLARED by the caller rather than
    recovered by a best-effort stack walk. This function is the SOLE
    process-level dispatch chokepoint (above), so it is the one seam every
    entry point already passes through — threading the identity through this
    parameter makes attribution 100% by construction: every one of this
    function's own call sites now passes an explicit, hand-written string
    naming itself (see each call site's own comment). A caller that omits
    ``caller`` falls back to `op_latency.caller_module()`'s best-effort frame
    walk, kept ONLY for a caller that cannot declare itself — as of C15 every
    known call site declares, so the walk is dead code on the measured
    population, not a silent 95%-style out. `caller_module()` itself is
    unchanged: still honest, still returns `None` rather than guessing.

    outcome classification (measurement only — see module's own
    DISPATCH_TIMEOUT_SECS negative-spec above for what "timeout" does and
    does NOT mean):
        "timeout" — the impl's own per-invocation asyncio.wait_for fired
                    (detected via the error message's "op timed out after"
                    prefix — that string is authored a few lines above in
                    this same module, not translated/localized, so matching
                    it here is stable). Means the CALLER gave up; the
                    handler thread may still be running to completion.
        "error"   — any other JSON-RPC error response, or an exception that
                    escapes the impl call entirely.
        "ok"      — a success response (no "error" key).

    Spec backlink: state/handoffs/2026-08-08-engine-fails-the-load-norm.md
                   docs/wiki/machine-load-norm.md

    STAMP GATE (checked FIRST, before anything else in this function): a
    refused dispatch never ran, so it gets no `record_op_started`/
    `record_op_latency` row -- those measure real invocations, and a
    refusal is not one. See `_is_dispatch_engine_stamped`'s own docstring
    for what this enforces and `allow_unstamped_dispatch` for the two
    sanctioned ways past it.

    THE GATE IS ARMED (`_STAMP_GATE_ARMED = True`) -- it refuses for real.
    Read that flag's own comment for the ruling it enforces and for which
    caller families resolve stamped vs unstamped on this box.
    """
    if _STAMP_GATE_ARMED and not _unstamped_dispatch_allowed and not _is_dispatch_engine_stamped():
        request_id = msg.get("id") if isinstance(msg, dict) else None
        return _unstamped_dispatch_refusal(request_id)

    import time as _time

    method = msg.get("method") if isinstance(msg, dict) else None
    request_repo = resolve_request_repo(msg) if isinstance(msg, dict) else None
    # C7 fallback: a "none"-scoped op never carries _origin_worktree (see
    # WORKTREE_SCOPED_OPS gating in invoke.__main__.main), so request_repo is
    # routinely None for it even on a live client. Prefer the caller's
    # stamped process cwd over letting op_latency._write_entry fall back to
    # THIS process's own cwd — which, warm-served, is the server's, not the
    # caller's.
    telemetry_repo_root = request_repo
    if telemetry_repo_root is None and isinstance(msg, dict):
        telemetry_repo_root = resolve_caller_cwd(msg)

    t_start = _time.time()
    perf_start = _time.perf_counter()
    # The brightline's own unit, taken at the same seam as the wall-clock pair
    # above so process-time coverage equals `started` coverage by construction
    # rather than by remembering to wire up each new entry point
    # (MEASUREMENT_SCOPE_PER_OP_HANDLER's comment has the population this fixed).
    process_start = _time.process_time()
    spawn_start = _spawn_count_or_none()
    _ancestors = _ANCESTOR_RECORDS.get()
    _record = _enter_dispatch(_ancestors)
    _ancestor_token = _ANCESTOR_RECORDS.set(_ancestors + (_record,))
    # Push this frame's child-CPU accumulator; the parent's, if any, is restored
    # in `finally` via the token so a raise cannot strand a frame on the stack.
    _nested_parent = _NESTED_DISPATCH_CPU_MS.get()
    _nested_token = _NESTED_DISPATCH_CPU_MS.set([])
    outcome = "ok"

    corr_id = None
    # Review: code-reviewer (Finding 2, P2) — sid is resolved once here, in the
    # entry block, not independently re-resolved in the `finally` block below.
    # If the entry block raises after this point but before `sid` is assigned
    # (or `record_op_started` itself raises), the completion row inherits
    # whatever `sid` value survived rather than getting its own resolve-at-exit
    # attempt — deliberate coupling, safe today only because
    # `resolve_session_id()` never raises (coordinator_core/session/core.py,
    # no exception paths per its own docstring). If `resolve_session_id` ever
    # grows a raising branch, the completion row's `sid` degrades silently
    # wherever that raise wasn't already caught upstream of this function.
    sid = None
    # C1 (2026-08-25-reconcile-open-comes-back-under-the-bar), superseded by
    # C15 (same plan): `caller` is now the function's own explicit parameter
    # (declared by the caller at the seam), not a stack walk resolved here.
    # Still resolved once, in the entry block, and reused for both the
    # started and complete rows -- same coupling as `sid` above and for the
    # same reason: the caller of THIS dispatch does not change between the
    # started and complete rows of the same corr_id. A caller that passed no
    # `caller` argument falls back to `caller_module()`'s best-effort walk
    # (kept for a caller that genuinely cannot declare itself -- see this
    # function's own docstring).
    if caller is None:
        try:
            from coordinator_core.telemetry.op_latency import caller_module

            caller = caller_module()
        except Exception:
            caller = None
    # The JSON-RPC `error.code` off the response, so the `outcome == "error"`
    # population on disk is READABLE without probing a live registry. See
    # `record_op_latency`'s own `error_code` note for the incident.
    error_code = None
    # The failure's identity. `error_code` alone is not readable: `-32603
    # Internal error` is the modal value and names nothing, so a population
    # thousands of rows deep still has to be reproduced by hand against a live
    # engine to learn what it is. See `record_op_latency`'s `error_kind` note.
    error_kind = None
    try:
        from coordinator_core.telemetry.op_latency import (
            new_correlation_id,
            record_op_started,
        )
        from coordinator_core.session.core import resolve_session_id

        corr_id = new_correlation_id()
        sid = resolve_session_id() or None
        record_op_started(
            op=method if isinstance(method, str) else "<unknown>",
            t_start=t_start,
            corr_id=corr_id,
            repo_root=telemetry_repo_root,
            sid=sid,
            caller=caller,
        )
    except Exception:
        _log().debug(
            "coordinator_core.ipc: op-started recording failed for %r", method,
            exc_info=True,
        )

    try:
        response = await _dispatch_message_impl(msg)
        error = response.get("error") if isinstance(response, dict) else None
        if error is not None:
            message = error.get("message", "") if isinstance(error, dict) else ""
            code = error.get("code") if isinstance(error, dict) else None
            error_code = code if isinstance(code, int) else None
            error_kind = message if isinstance(message, str) else None
            outcome = "timeout" if isinstance(message, str) and message.startswith("op timed out after") else "error"
        return response
    except BaseException as exc:
        # An exception that escapes dispatch is not converted to a JSON-RPC
        # error response, so `error_code` stays None and the row on disk used
        # to carry no identity at all -- the single largest un-diagnosable
        # failure population on the box entered it through here.
        outcome = "error"
        error_kind = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        elapsed_ms = (_time.perf_counter() - perf_start) * 1000.0
        span_ms = (_time.process_time() - process_start) * 1000.0
        uncontaminated = _exit_dispatch(_record)
        _ANCESTOR_RECORDS.reset(_ancestor_token)
        try:
            children_ms = sum(_NESTED_DISPATCH_CPU_MS.get() or ())
        except Exception:
            children_ms = 0.0
        finally:
            _NESTED_DISPATCH_CPU_MS.reset(_nested_token)
        # This op's own handler CPU: the span minus what nested dispatches
        # inside it consumed. Clamped at zero -- `process_time()` is monotonic
        # so the subtraction cannot legitimately go negative, and a negative
        # figure on disk would read as a measurement nobody can interpret.
        own_ms = span_ms - children_ms
        # A negative delta is not always clock noise. Children dispatched
        # CONCURRENTLY each charge their whole span to this frame, and their
        # spans overlap on one process-wide clock, so the sum can legitimately
        # exceed it. Review (slice-a Finding 3): clamping in silence discards
        # that signal. Clamp, and say the figure is no longer this op's own.
        clamped = own_ms < 0.0
        if clamped:
            own_ms = 0.0
        # Charge the WHOLE span to the parent, not `own_ms`: the parent must
        # exclude everything its child's span consumed, children included.
        if _nested_parent is not None:
            try:
                _nested_parent.append(span_ms)
            except Exception:
                pass
        _spawn_end = _spawn_count_or_none()
        _spawns = (
            _spawn_end - spawn_start
            if _spawn_end is not None and spawn_start is not None
            else None
        )
        try:
            record_op_process_time(
                op=method if isinstance(method, str) else "<unknown>",
                process_ms=own_ms,
                # Derived from what was observed across the WHOLE span, never
                # from two snapshots of it -- see `_enter_dispatch`'s own
                # comment for the sibling this used to miss.
                measurement_scope=(
                    MEASUREMENT_SCOPE_PER_OP_HANDLER
                    if (uncontaminated and not clamped)
                    else MEASUREMENT_SCOPE_PROCESS_WIDE
                ),
                source_path="dispatch_chokepoint",
                t_start=t_start,
                sid=sid,
                corr_id=corr_id,
                caller=caller,
                repo_root=telemetry_repo_root,
                spawns=_spawns,
            )
        except Exception:
            _log().debug(
                "coordinator_core.ipc: chokepoint process-time recording failed for %r",
                method, exc_info=True,
            )
        try:
            from coordinator_core.telemetry.op_latency import record_op_latency

            record_op_latency(
                op=method if isinstance(method, str) else "<unknown>",
                t_start=t_start,
                elapsed_ms=elapsed_ms,
                outcome=outcome,
                repo_root=telemetry_repo_root,
                sid=sid,
                corr_id=corr_id,
                caller=caller,
                error_code=error_code,
                error_kind=error_kind,
            )
        except Exception:
            _log().debug(
                "coordinator_core.ipc: op-latency recording failed for %r", method,
                exc_info=True,
            )


#: Discriminator values for `record_op_process_time`'s `measurement_scope` field
#: (C9, state/dispatch-briefs/2026-08-21-the-cli-bootstrap-tax-dies-at-the-
#: interpreter-floor/C9.md). `elapsed_ms` (op_latency.record_op_latency) stays
#: wall clock, unchanged unit, unchanged consumers -- see that field's own
#: module docstring. This is a SEPARATE key ("process_ms", row `kind`
#: "process_time") for CPU time, never a redefinition of `elapsed_ms` in place:
#: 187,074 existing wall-clock rows carry no schema version and no
#: discriminator, and `telemetry/cost_census.py` + `telemetry/engine_report.py`
#: (two sites) aggregate them as homogeneous -- silently mixing units under one
#: name is the exact defect this row exists to remove, not reintroduce one
#: level down.
#:
#: PER_OP_PROCESS: `time.process_time()` delta taken entirely inside a single
#: process running exactly one op at a time for the delta's duration --
#: uncontaminated by any peer op. True of the pool-worker path
#: (`warm.server._pool_dispatch_worker`, one `ProcessPoolExecutor` task at a
#: time per worker process) and the one-shot CLI path (`dispatch_from_hook`,
#: `coordinator_core.invoke.__main__`'s cold path -- a whole process per op).
#:
#: PROCESS_WIDE: `time.process_time()` delta taken on an accept-process
#: connection thread (`warm.server._run_dispatch`), where `WORKER_POOL_SIZE`
#: threads share ONE interpreter and ONE `process_time()` clock -- the delta
#: can include CPU spent on OTHER concurrently-dispatched ops on sibling
#: threads during the same wall-clock span, so it is process-wide, not this
#: op's own CPU. Recording it under the same name as PER_OP_PROCESS would be
#: the identical unit-mixing hazard the wall-clock/process-time split exists
#: to stop, one level down -- so the two never share a value under one
#: unlabelled key; `measurement_scope` is the required discriminator.
#: PER_OP_HANDLER: `time.process_time()` delta taken at the `dispatch_message`
#: chokepoint, with the CPU of any NESTED dispatch subtracted out, while this
#: dispatch was the only one in flight in the process. It is this op's own
#: handler CPU and nothing else -- narrower than PER_OP_PROCESS, which is a
#: whole one-shot process and therefore also carries interpreter start, envelope
#: parse and response serialization.
#:
#: Why it exists (the-meter-02 AC-6, 2026-08-27): the other three scopes are
#: recorded at four OUTER entry points (`invoke.__main__`, `dispatch_from_hook`,
#: `dispatch_ops_from_hook`, `warm.server`), so an op reaching `dispatch_message`
#: by any other path -- one op composing another, a module `_main`, an in-process
#: caller -- recorded `started`/`complete` and NO process time at all. Measured
#: 2026-08-27: 17 of 64 observed ops had zero `process_time` rows, among them
#: eight live kill-ledger CANDIDATES (K-018/019/020/029/030/042/043), which is
#: precisely the population the brightline must be able to convict on. Recording
#: at the chokepoint closes that by construction: coverage now equals `started`
#: coverage, because it is the same seam.
#:
#: This NEVER replaces a PER_OP_PROCESS row and never shares its name. A
#: CLI-routed op now writes two process-time rows -- the outer whole-process one
#: and this inner handler one -- which is two honest measurements of different
#: spans, not a double count. A reader that averages them together commits the
#: unit-mixing this discriminator exists to prevent: select ONE scope, state
#: which (`op_census.meter` does, in its Population).
MEASUREMENT_SCOPE_PER_OP_HANDLER = "per_op_handler"
MEASUREMENT_SCOPE_PER_OP_PROCESS = "per_op_process"
MEASUREMENT_SCOPE_PROCESS_WIDE = "process_wide"
_MEASUREMENT_SCOPES = frozenset({
    MEASUREMENT_SCOPE_PER_OP_HANDLER,
    MEASUREMENT_SCOPE_PER_OP_PROCESS,
    MEASUREMENT_SCOPE_PROCESS_WIDE,
})

#: CPU consumed by dispatches nested inside the currently-running one, so a
#: parent reports its OWN handler CPU rather than its children's as well.
#: `process_time()` is process-wide and monotonic, so a naive parent delta
#: contains every child's CPU -- an op that composes three others would read as
#: the cost of all four, and the brightline would convict the wrong one.
#: A ContextVar, not a plain global: each asyncio task gets its own copy, so
#: sibling tasks cannot pop each other's frames.
_NESTED_DISPATCH_CPU_MS: "contextvars.ContextVar[Optional[List[float]]]" = (
    contextvars.ContextVar("coordinator_core.ipc.nested_dispatch_cpu_ms", default=None)
)

#: Contamination is observed CONTINUOUSLY, not sampled at two instants.
#:
#: The first cut of this compared a global in-flight count against this
#: dispatch's nesting depth at entry and at exit. Review (2026-08-27, slice-a
#: Finding 1) showed that misses a sibling that both starts and finishes
#: strictly INSIDE this dispatch's span: neither sample point sees it, yet its
#: CPU is in this span's `process_time()` delta. The row would then claim
#: `per_op_handler` -- the narrowest, most-trusted scope, the one the brightline
#: is read in -- while carrying another op's CPU. A discriminator that is wrong
#: in the direction of overclaiming precision is worse than no discriminator.
#:
#: So each dispatch owns a record, and an entering dispatch marks every active
#: record that is not one of its own ancestors -- and is marked by them in turn.
#: Contamination is therefore recorded the moment the overlap exists, whenever
#: within the span it happens, rather than inferred from two snapshots.
#:
#: An ancestor is never contamination: a parent awaiting its child burns no CPU
#: during the child's span, and treating it as a sibling would label every
#: composed op PROCESS_WIDE -- pessimising exactly the ops most worth measuring.
#: Ancestry is carried explicitly on `_ANCESTOR_RECORDS` rather than inferred
#: from a count, because a count cannot tell an ancestor from a stranger.


class _DispatchRecord:
    """One in-flight dispatch, and whether a non-ancestor overlapped its span."""

    __slots__ = ("contaminated",)

    def __init__(self) -> None:
        self.contaminated = False


_ACTIVE_DISPATCHES: "List[_DispatchRecord]" = []
_ACTIVE_DISPATCH_LOCK = threading.Lock()

#: This dispatch's ancestor records, innermost last. A ContextVar so each
#: asyncio task carries its own chain rather than reading a sibling's.
_ANCESTOR_RECORDS: "contextvars.ContextVar[tuple]" = contextvars.ContextVar(
    "coordinator_core.ipc.ancestor_dispatch_records", default=()
)


def _enter_dispatch(ancestors: tuple) -> "_DispatchRecord":
    """Register a dispatch as in flight, cross-marking it against every active
    non-ancestor. The list is short (in-flight dispatches, not history), so the
    scan is a handful of identity comparisons under a lock already held for a
    list append."""
    rec = _DispatchRecord()
    with _ACTIVE_DISPATCH_LOCK:
        for other in _ACTIVE_DISPATCHES:
            if not any(other is a for a in ancestors):
                other.contaminated = True
                rec.contaminated = True
        _ACTIVE_DISPATCHES.append(rec)
    return rec


def _exit_dispatch(rec: "_DispatchRecord") -> bool:
    """Deregister a dispatch. True if no non-ancestor ever overlapped its span."""
    with _ACTIVE_DISPATCH_LOCK:
        for i, other in enumerate(_ACTIVE_DISPATCHES):
            if other is rec:
                del _ACTIVE_DISPATCHES[i]
                break
        return not rec.contaminated


def _telemetry_sid() -> Optional[str]:
    """Session id for a process-time row, or None.

    Every `process_time`-kind row in the sink carried `sid: null` until
    2026-08-25 -- 2,108 of 2,108 for `hooks.track_touched_files` alone -- because
    the four `record_op_process_time` call sites never passed one, while the
    wall-clock `started`/`complete` rows beside them did. A CPU sample that
    cannot be joined to a session cannot be ranked within one, so the sink could
    not answer "what does fire #1 of a session cost" in process time at all, and
    an audit needing that had to fall back to wall clock.

    `resolve_session_id` is a ContextVar read plus up to three env reads with no
    I/O and no spawn, so this is free on the hot path. Never raises: a telemetry
    row with a null sid is strictly better than a broken dispatch.
    """
    try:
        from coordinator_core.session.core import resolve_session_id

        return resolve_session_id() or None
    except Exception:
        return None


def _spawn_count_or_none() -> Optional[int]:
    """Process-local spawn count, or ``None`` if the counter is unavailable.

    Function-local AND `try/except`-wrapped, like this module's two other
    `coordinator_core.telemetry` imports and for the same reason: `ipc.py` carries
    a documented negative spec that telemetry never appears in its top-level
    import closure (pinned by
    `coordinator/tests/test_publish_payload_import_closure.py::test_ipc_telemetry_negative_spec_holds_under_new_discriminator`),
    because the publish payload must not drag telemetry into every consumer.

    Returns ``None`` rather than ``0`` on failure. Zero is the substantive claim
    that an op spawned nothing; absence is "not counted here". Collapsing the two
    is how an unmeasured op comes to read as a cheap one.
    """
    try:
        from coordinator_core.telemetry.spawn_counter import spawn_count

        return spawn_count()
    except Exception:
        return None


def _spawn_delta(start: Optional[int], end: Optional[int]) -> Optional[int]:
    """Spawns between two readings, or ``None`` if either end is unavailable.

    Returns a plain ``int``, trusting -- without re-asserting -- that
    `spawn_counter`'s own negative spec holds ("not reset between ops,
    ever"). If that global were ever reset mid-measurement this could return
    a negative delta; not a bug given that contract holds today, but this
    function depends on it rather than defending against its violation.
    """
    if start is None or end is None:
        return None
    return end - start


def record_op_process_time(
    *,
    op: str,
    process_ms: float,
    measurement_scope: str,
    source_path: str,
    t_start: float,
    repo_root: Optional[Path] = None,
    sid: Optional[str] = None,
    corr_id: Optional[str] = None,
    spawns: Optional[int] = None,
    caller: Optional[str] = None,
) -> None:
    """Append one durable process-time sample, alongside (never replacing) the
    wall-clock `elapsed_ms` row `dispatch_message` already records.

    Reuses `coordinator_core.telemetry.op_latency._write_entry` -- the
    module's own single append discipline (kill switch, route stamping,
    repo-key resolution, one atomic `atomic_append.append_line` call, never
    raises) -- rather than re-deriving any of that here. Writes to the SAME
    sink (`op-latency.jsonl`) under a DISTINCT row `kind`, `"process_time"`,
    so the existing `kind != "complete"` skip already present at both
    `cost_census.py::run_census` and `engine_report.py`'s four aggregation
    sites (verified against source before this function was written, per
    this row's own instruction) excludes these rows from every wall-clock
    percentile automatically -- no consumer edit required, and no existing
    percentile is diluted by a CPU-time sample entering its `elapsed_ms`
    population.

    `measurement_scope` MUST be one of `MEASUREMENT_SCOPE_PER_OP_PROCESS` /
    `MEASUREMENT_SCOPE_PROCESS_WIDE` above -- an unrecognised value is
    coerced to `"unknown"` rather than raising, matching this function's own
    never-breaks-dispatch contract (an invalid label costs one
    lower-confidence row, never a peer's op).

    Never raises -- same fail-open contract as every other telemetry call on
    this hot path (`record_op_started` / `record_op_latency` above).
    """
    try:
        from coordinator_core.telemetry.op_latency import _write_entry
        import os as _os

        scope = measurement_scope if measurement_scope in _MEASUREMENT_SCOPES else "unknown"
        entry = {
            "op": op,
            "t_start": t_start,
            "process_ms": process_ms,
            "measurement_scope": scope,
            "source_path": source_path,
            "pid": _os.getpid(),
            "sid": sid,
            "kind": "process_time",
            "corr_id": corr_id,
            "caller": caller,
        }
        # The brightline's second axis, omitted rather than zero-filled when the
        # caller did not measure it: a missing `spawns` key means "not counted
        # here", while `0` is the substantive claim that this op spawned nothing.
        # A reader that cannot tell those apart re-runs the 2026-08-23 sweep's
        # own mistake of reading an absent figure as a measured one.
        if spawns is not None:
            entry["spawns"] = spawns
        _write_entry(entry, repo_root)
    except Exception:
        _log().debug(
            "coordinator_core.ipc: process-time recording failed for %r", op,
            exc_info=True,
        )


class HookDispatchError(Exception):
    """A JSON-RPC error response surfaced by ``dispatch_from_hook``.

    Attributes:
        code:    the JSON-RPC error code (e.g. METHOD_NOT_FOUND, INTERNAL_ERROR).
        message: the raw JSON-RPC error message (not including the op-name prefix
                 that ``str(exc)`` carries — see the constructor).
    """

    def __init__(self, op_name: str, code: int, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{op_name}: {message} (code={code})")


def dispatch_from_hook(
    op_name: str,
    params: dict,
    *,
    origin_worktree: Optional[str] = None,
) -> dict:
    """Build a JSON-RPC 2.0 envelope, dispatch it in-process, and return the op's result.

    The single engine-side home for the post-import dispatch body that seven DoE hook
    shims currently hand-roll identically (build envelope -> asyncio.run(dispatch_message)
    -> unwrap result). DR-118 rules that a DoE-resident hook shim is a pointer only —
    resolve the engine root, hand over the raw payload, translate the verdict into the
    harness channel, and degrade. Transport policy (the envelope shape, the dispatch call,
    error surfacing) is engine-resident, not the shim's to reimplement.

    Spec backlink: cross-repo/archive/2026-07-31-doe-claude-em-dr116-seam-contents-and-ipc-hook-dispatch.md
                   (DoE's DR-118 — the memo's filename says dr116 because DoE renumbered
                   the ruling at execute time; DR-116 in the DoE tree is an unrelated
                   record, per 2026-07-31-doe-claude-em-dr116-is-now-dr118-citation-correction.md)

    Args:
        op_name:         the JSON-RPC "method" to dispatch.
        params:          the JSON-RPC "params" dict.
        origin_worktree: stamped into the envelope's "_origin_worktree" field ONLY when
                          it is a non-empty string. A None or empty-string value is
                          OMITTED from the envelope entirely rather than carried as ""  —
                          resolve_request_repo() already treats an empty/absent field
                          identically (``if not raw or not isinstance(raw, str): return
                          None``), so this omission is behavior-preserving, not a change
                          in effective resolution.

    Returns:
        The handler's JSON-RPC "result" value. If the response carries no "result" key
        (defensive — should not happen for a success response), returns ``{}`` rather
        than raising KeyError.

    Raises:
        HookDispatchError: if the JSON-RPC response carries an "error" object (unknown
            op, handler exception, timeout, etc.) — carries ``.code`` and ``.message``
            attributes from the JSON-RPC error object; the op name is included in the
            exception's string form.

    Negative spec (this function's non-responsibilities — stay with the calling shim):
        - Does NOT catch ImportError. This function lives inside the module that might
          be unimportable in the first place, so it cannot hold the "engine unimportable
          -> exit 0" fail-open guard — that guard remains each shim's own responsibility,
          wrapping its own `import coordinator_core...` line, not a try/except inside here.
        - Does NOT retry, gate on environment, or decide whether/what to speak on failure.
          The harness contract (stderr + exit 2) belongs to the shim exclusively; this
          function only raises or returns — it never touches stderr, sys.exit, or any
          harness-facing surface.
        - Does NOT swallow exceptions from dispatch_message beyond the JSON-RPC error
          envelope dispatch_message itself already converts handler exceptions into
          (STRUCTURAL_PIN_ERROR / INTERNAL_ERROR) — an error response always surfaces as
          HookDispatchError, never as a bare traceback escaping this function un-wrapped.
    """
    # asyncio import deferred to first use (not module scope) for the same ~9ms
    # import-cost reason documented in dispatch_message's own deferred import above.
    import asyncio
    import time as _time

    envelope = _hook_envelope(op_name, params, origin_worktree)

    # Per-op process time (C9): this function is a whole process per op (a
    # DoE hook shim's one-shot cold spawn) -- the same uncontaminated-CPU
    # argument as the pool-worker path applies trivially here. Measured
    # around the SAME asyncio.run(dispatch_message(...)) call the docstring
    # above already describes as this function's dispatch body, never inside
    # dispatch_message itself (the async wrapper the C9 row's own dispatch
    # site is deliberately NOT).
    t_start = _time.time()
    process_start = _time.process_time()
    spawn_start = _spawn_count_or_none()
    _caller = "coordinator_core.ipc.dispatch_from_hook"
    try:
        # C16 (following up C15's honest PARTIAL): this function IS one of
        # `dispatch_message`'s own call sites, and now declares itself
        # explicitly rather than relying on the stack-walk fallback -- the
        # out-of-scope test doubles that previously pinned a fixed `(msg)`
        # signature (`coordinator_core.ops.tests.test_ipc_dispatch_from_hook`)
        # are in THIS row's `writes:` and have been widened to accept
        # `caller=`.
        response = asyncio.run(dispatch_message(envelope, caller=_caller))
    finally:
        process_ms = (_time.process_time() - process_start) * 1000.0
        request_repo = resolve_request_repo(envelope) or resolve_caller_cwd(envelope)
        record_op_process_time(
            op=op_name,
            process_ms=process_ms,
            measurement_scope=MEASUREMENT_SCOPE_PER_OP_PROCESS,
            source_path="one_shot_cli",
            t_start=t_start,
            repo_root=request_repo,
            sid=_telemetry_sid(),
            spawns=_spawn_delta(spawn_start, _spawn_count_or_none()),
            caller=_caller,
        )
    return _unwrap_hook_response(op_name, response)


def _hook_envelope(
    op_name: str,
    params: dict,
    origin_worktree: Optional[str],
) -> dict:
    """Build the JSON-RPC 2.0 envelope both hook entry points dispatch.

    Shared by dispatch_from_hook and dispatch_ops_from_hook so the two cannot
    drift on envelope shape or on the omit-empty _origin_worktree rule
    documented in dispatch_from_hook's Args section.
    """
    msg: dict = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": op_name,
        "params": params,
    }
    if origin_worktree:
        msg[_ORIGIN_WORKTREE_FIELD] = origin_worktree
    return msg


def _unwrap_hook_response(op_name: str, response: dict) -> dict:
    """Return the "result" value, or raise HookDispatchError for an error envelope."""
    error = response.get("error")
    if error is not None:
        raise HookDispatchError(
            op_name,
            error.get("code", INTERNAL_ERROR),
            error.get("message", ""),
        )
    return response.get("result", {})


def dispatch_ops_from_hook(
    ops: Sequence[tuple],
    *,
    origin_worktree: Optional[str] = None,
) -> list:
    """Dispatch several ops from one hook, sequentially, under a single event loop.

    The multi-op sibling of dispatch_from_hook, for a hook shim that carries more
    than one independent concern in a single invocation (DoE's
    postuse-advisory-dispatch.py: an advisory dispatch plus a bookkeeping
    dispatch, where a failure in either must not suppress the other).

    Spec backlink: cross-repo/inbox/2026-08-19-doe-claude-em-widen-the-seam-dispatch-ops-from-hook.md

    Ops run in the order given, each awaited to completion before the next
    starts. Sequential is the contract, not an implementation detail:
    dispatch_message's safety under two in-flight calls in one loop is
    unverified, and concurrent scheduling has already been tried and rejected
    on the DoE side for breaking the nudge-firing tests.

    Args:
        ops:             (op_name, params) pairs, dispatched in sequence.
        origin_worktree: stamped into every envelope under the same omit-empty
                         rule dispatch_from_hook documents.

    Returns:
        One entry per input op, positionally aligned: the handler's "result"
        value on success, or a HookDispatchError instance (RETURNED, not raised)
        on an error envelope. Callers discriminate with isinstance. An empty
        ``ops`` returns [] without opening an event loop.

    Negative spec:
        - Does NOT raise HookDispatchError. Raising would let the first failing
          concern suppress every later one, which is the exact isolation this
          entry point exists to provide.
        - Does NOT catch ImportError, retry, gate on environment, or speak to
          the harness — identical to dispatch_from_hook's negative spec, and for
          the same reason: this code lives inside the module that may itself be
          unimportable, so the fail-open guard stays with the calling shim.
        - Does NOT add a broad except around dispatch_message. Handler
          exceptions and timeouts already arrive as JSON-RPC error envelopes and
          become returned HookDispatchError entries; anything escaping that
          contract is an engine-level fault, not a per-op concern to swallow.
        - Does NOT schedule concurrently. See above.
    """
    import asyncio
    import time as _time

    op_list = list(ops)
    if not op_list:
        return []

    async def _run_all() -> list:
        results: list = []
        for op_name, params in op_list:
            envelope = _hook_envelope(op_name, params, origin_worktree)
            # Per-op process time, same contract as dispatch_from_hook's own
            # sample. This loop is sequential and single-threaded inside one
            # process -- it awaits each dispatch to completion before starting
            # the next and never schedules concurrently (see this function's
            # negative spec) -- so a `process_time()` delta taken around one
            # iteration contains that op's CPU and no sibling's. That is the
            # PER_OP_PROCESS case, not the accept-thread PROCESS_WIDE one,
            # where `WORKER_POOL_SIZE` threads share a clock.
            t_start = _time.time()
            process_start = _time.process_time()
            spawn_start = _spawn_count_or_none()
            _caller = "coordinator_core.ipc.dispatch_ops_from_hook"
            try:
                # C16 (following up C15's honest PARTIAL): declares itself
                # explicitly now that this function's own test doubles
                # (in this row's `writes:`) accept `caller=`.
                response = await dispatch_message(envelope, caller=_caller)
            finally:
                record_op_process_time(
                    op=op_name,
                    process_ms=(_time.process_time() - process_start) * 1000.0,
                    measurement_scope=MEASUREMENT_SCOPE_PER_OP_PROCESS,
                    source_path="hook_batch",
                    t_start=t_start,
                    sid=_telemetry_sid(),
                    repo_root=(
                        resolve_request_repo(envelope) or resolve_caller_cwd(envelope)
                    ),
                    spawns=_spawn_delta(spawn_start, _spawn_count_or_none()),
                    caller=_caller,
                )
            try:
                results.append(_unwrap_hook_response(op_name, response))
            except HookDispatchError as exc:
                results.append(exc)
        return results

    return asyncio.run(_run_all())


# _handle_connection removed by C5 (DR-215): UDS server transport retired.
# coordinator_core is now a command-type engine; dispatch_message is called directly.
# Backlink: docs/decisions/DR-215-coordinator-core-command-type-execution-model.md

# UDS socket_path() removed by C5 (DR-215): coordinator_core is now command-type; no socket.

# _guard_socket_stomp() removed by C5 (DR-215): UDS transport retired.

# start_server_async removed by C5 (DR-215): UDS server transport retired.
