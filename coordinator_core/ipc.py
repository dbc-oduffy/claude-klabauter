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

Spec backlink: docs/plans/2026-07-02-pcore-03-beachhead-coordinator-core.md § C1 / C1b
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

      BOOKKEEPING ops (4, pcore-08) — MAY write under .git/coordinator-sessions/ only:
        - track_touched_files:     writes .git/coordinator-sessions/<sid>/touched.txt
                                   and .git/coordinator-sessions/.agents/<aid>/touched.txt
        - session_heartbeat:       writes last_activity field in
                                   .git/coordinator-sessions/<sid>/meta.json
        - agent_completion_log:    appends to .git/coordinator-sessions/logs/agent-audit.jsonl
        - track_dispatched_agents: writes .git/coordinator-sessions/<sid>/dispatched-agents.txt
                                   and .git/coordinator-sessions/.agents/<aid>/em-session-id.txt
        Write confinement (hard): bookkeeping ops MUST NOT write state/, archive/, or any
        path outside .git/coordinator-sessions/. The write target is session-runtime/liveness
        layer — not coordinator substrate (claude-klabauter's disk-truth custody) and not rag's
        workstate_store projection (custody-vs-projection boundary,
        docs/decisions/DR-236-state-is-disk-truth-workstate-store-is-pro.md; the successor
        substrate-write carve-out DR for this confinement rule is DR-241, now authored —
        docs/decisions/DR-241-sovereign-tracker-substrate-write-carveout.md).
        Classification: all four are MUTATING (DR-208, classification.py).

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

      MEMO send op (1, DR-214-send-class) — sanctioned MUTATING cross-tree write category
        (deliberate DR-214-send-class carve-out; strongest crossing in the strang-0x sequence
        — cross-repo write, not merely cross-tree-within-own-repo):
        - memo.send: writes one schema-valid memo file (non-committing dirty file) into
                     the cross-repo/inbox/ of a registry-enumerated receiver repo.
        D2 seven-bound (send-class, all must hold — docs/decisions/DR-214-send-class-…):
          single-file per invocation; registry-enumerated receiver (never wire-derived);
          non-committing dirty file; fail-loud on collision (read-before-write → refuse);
          in-process command-type dispatch only (HTTP/UDS transport and gate 6 vacated
          by DR-215; MUTATING gating is now inherent to command-type serial execution);
          schema-valid emission; B3 gitignore delivery guard.
        Write confinement (hard): memo.send MAY write ONLY to the cross-repo/inbox/ of a
        registry-enumerated receiver; MUST NOT commit into the sibling tree; MUST NOT write
        the sibling's state/, .git/, or rag's relational store (dual-write ban); MUST NOT
        traverse outside the resolved inbox path.
        Q-c HARD (AC4): refuses when the claude-klabauter engine is unreachable — no legacy
        direct-write fallback. UDS-only negative-spec: NOT reachable over HTTP.
        Classification: MUTATING (DR-208, classification.py).

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
                                       (--emit only; example-doctrine-repo's cross-tree consumer —
                                       cross-repo/inbox/2026-08-06-example-doctrine-repo-em-cartography-
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
        code (deliberate DR-241 carve-out; DEC-4: this DR registers zero ops — the first
        tracker.* op to call append_event/read_events in anger, and its per-op DR-208 §5
        five-question affirmation against real handler code, land at sat-06, a separate
        downstream stub):
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
        this unaffirmed-until-sat-06 status honest in code, not merely in prose.

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

import importlib
import inspect
import json
import os
import types as _types
from pathlib import Path
from typing import Any, Callable, Dict, Optional

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

# ---------------------------------------------------------------------------
# Per-invocation dispatch timeout (C3 / AC-3 / Gap-1)
#
# Per-invocation hung-op guard: cancels a runaway or hanging op handler after
# DISPATCH_TIMEOUT_SECS, preventing the caller from blocking indefinitely.
# (In the command-type model, one op runs per process — the "other partitions"
# framing of the multiplex era no longer applies; the guard is now a simple
# per-invocation safety net.)
#
# Overridable at process startup via env var — integration tests set a shorter
# value (e.g. 0.1 s) to exercise the timeout path without slowing the suite.
# Override must be set BEFORE import (module-level constant evaluated at import).
#
# Note: the timeout is only effective if the handler is either:
#   (a) an async handler that yields control (e.g. awaits I/O or asyncio.sleep), OR
#   (b) a sync handler offloaded via asyncio.to_thread — asyncio.wait_for cancels the
#       AWAIT, not the thread. Python has no thread-cancellation primitive: the worker
#       thread keeps running inside the handler after the timeout fires and the caller
#       unblocks. (2026-08-07 ceremony-lock-leak audit,
#       state/audits/2026-08-07-ceremony-lock-leak-root-cause.md — this comment
#       previously claimed the thread WAS cancelled, which is false and was directly
#       contradicted by invoke/__main__.py's own comments on the same path; a handler
#       thread holding a cross-process lock across this timeout keeps holding it until
#       invoke/__main__.py's own os._exit() tears down the whole process.)
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
DISPATCH_TIMEOUT_SECS: float = float(
    os.environ.get("COORDINATOR_DISPATCH_TIMEOUT_SECS", "30")
)


def _warn_on_near_miss_timeout_env(environ: Optional[Dict[str, str]] = None) -> None:
    """Design-as-offers near-miss hint for the timeout env-var knob.

    A caller who sets a `COORDINATOR_*TIMEOUT*`-shaped env var that ISN'T the
    real `COORDINATOR_DISPATCH_TIMEOUT_SECS` knob gets silent no-op today — the
    typo'd var is simply ignored and the caller has no idea why their timeout
    override never took effect. This scans the environment at import time and
    warns on any near-miss so the caller sees "did you mean
    COORDINATOR_DISPATCH_TIMEOUT_SECS?" instead of debugging a silent no-op.

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
                "timeout knob — did you mean COORDINATOR_DISPATCH_TIMEOUT_SECS?",
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
# ---------------------------------------------------------------------------
_OP_TIMEOUT_OVERRIDES: Dict[str, float] = {}


def _timeout_for(method: str) -> float:
    """Per-op dispatch timeout: _OP_TIMEOUT_OVERRIDES[method] if listed, else the global DISPATCH_TIMEOUT_SECS runaway-guard."""
    return _OP_TIMEOUT_OVERRIDES.get(method, DISPATCH_TIMEOUT_SECS)


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
# Spec backlink: docs/plans/2026-07-04-coordinator-core-global-multiplex-migration.md § C1a
# ---------------------------------------------------------------------------
_ORIGIN_WORKTREE_FIELD = "_origin_worktree"

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
# Spec backlink: docs/plans/2026-07-04-coordinator-core-global-multiplex-migration.md § C1c
# DR:            docs/decisions/2026-07-04-coordinator-core-global-multiplex-topology.md § AC-1b
# Amendment:     docs/plans/2026-07-07-per-repo-emission-cutover.md § C3
# Split:         cross-repo/inbox/2026-07-21-claude-central-em-python-bin-cold-invocation-minutes-per-call.md
# ---------------------------------------------------------------------------
from coordinator_core.op_scopes import (  # noqa: E402,F401
    OP_KEY_SCOPE,
    WORKTREE_SCOPED_OPS,
    _OP_KEY_SCOPE,
)

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

    Spec backlink: docs/plans/2026-07-04-coordinator-core-global-multiplex-migration.md § C1c
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

    Spec backlink: docs/plans/2026-07-04-coordinator-core-global-multiplex-migration.md § C1b
    """
    raw = msg.get(_ORIGIN_WORKTREE_FIELD)
    if not raw or not isinstance(raw, str):
        return None
    return Path(raw).resolve()


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
# example-doctrine-repo central-root writes (genuinely outside the caller's own
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
# writes into the example-doctrine-repo central root therefore stay unclaimed orphans
# at that sink. This is the CORRECT outcome, not a regression — no claim is
# always safer than a WRONG claim, and a wrong claim here actively harms a
# sibling repo claude-klabauter does not own. Do not "fix" this back to declared-path
# anchoring; the example-doctrine-repo side has its own adoption path
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

# Cap on the number of paths a single declaration may carry (2026-08-04 F4
# fix). Each declared path costs ~9-10ms warm (24ms cold) on this repo —
# one `session.scope.touch()` call, itself a `git ls-files --full-name`
# normalization plus a `touched.txt` append — on the dispatch hot path
# every op and CLI passes through, budgeted at 300ms for the MUTATING
# classification (`coordinator_core/benchmarks/budget-manifest.json`,
# `defaults.MUTATING.target_ms`, itself marked `_provisional`). 16 paths at
# the measured warm cost is ~160ms — under half the budget, leaving
# headroom for the handler's own work and the cold-start case. A handler
# legitimately writing more than 16 files in one call is off the shape this
# contract was designed for (a single-write-primitive self-report, per the
# module contract above) and should be revisited rather than raising the
# cap. Excess entries are dropped (log-and-truncate), never silently.
_MAX_DECLARED_TOUCH_PATHS = 16


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
    `session.scope.touch()`'s own `cwd` param so the claim lands in the
    caller's own `.git/coordinator-sessions/<sid>/touched.txt` — the one a
    same-repo `scoped_git_commit` call will actually read.

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
                path_repo_root, abs_path = resolved
                _scope.touch(sid, abs_path, path_repo_root)
            except Exception as exc:  # fail-open — never let one bad path abort the rest
                _log().debug(
                    "coordinator_core.ipc: self-reported touch failed for %r: %s",
                    raw_path, exc,
                )
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


def get_op_handler(name: str) -> Optional[Callable]:
    """Return the registered handler callable for *name*, or None if not registered.

    Callers that need to invoke a fleet op by its public key string (e.g.
    ``"fleet.archive_completed_plans"``) can call this directly — a registry MISS
    triggers the same lazy-import + eager-fallback resolution as the IPC dispatch
    path (`_lazy_import_and_lookup`) before returning None, so it works correctly
    under `COORDINATOR_CORE_LAZY_OPS=1` without the caller having to pre-import the
    op's owning module itself. Returns None only when the op is genuinely
    unregistered — callers should treat None as a not-found condition.

    2026-07-25 break-class fix: this used to be a bare `_REGISTRY.get(name)`, which
    returned None for any sibling op not yet imported under lazy ops — breaking
    every caller that resolves a sibling op by key (e.g. `cutover.advance`
    resolving `cutover.gate`) whenever it ran as anything other than the directly
    dispatched op. See
    cross-repo/inbox/2026-07-25-example-doctrine-repo-em-cutover-advance-cannot-resolve-gate-op.md.

    Review: code-reviewer F11 — added to allow fleet-op callers to resolve handlers
    via the public op key rather than accessing the op module's private handler
    function name directly.
    """
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
         hooks.* ops — see that map's header comment), so under
         COORDINATOR_CORE_LAZY_OPS/`sys._coordinator_core_lazy_ops` the step-1
         import of "coordinator_core.hooks" is a lazy-gated no-op that never
         registers anything and would otherwise always fall through to step 3.
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
         importlib.import_module("coordinator_core.ops")) is deliberate: when
         lazy op registration is active (armed by the CLI dispatcher), the
         coordinator_core.ops package is already in sys.modules with its
         eager-import block SKIPPED — a bare re-import of the package is a
         cached no-op and would NOT trigger the other op modules. Calling the
         function directly forces every op module to import regardless of the
         package's cached lazy-init state (each already-imported submodule
         import is itself a cheap no-op, so this is safe to call unconditionally).

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

    _eager_import_all()
    return _REGISTRY.get(method)


# ---------------------------------------------------------------------------
# Shared dispatch core — steps 2-7 on a pre-parsed message dict
# ---------------------------------------------------------------------------

def _handler_exception_error(exc: BaseException) -> dict:
    """Build the JSON-RPC error object for an exception that escaped an op handler.

    Selects STRUCTURAL_PIN_ERROR (preserving the exception's own message, which already
    states the remediation — see ContractPinError) when ``exc`` carries the
    ``structurally_wedged`` duck-type marker; otherwise falls back to the pre-existing
    generic INTERNAL_ERROR shape (class-name-only message — the original message text is
    not assumed safe to surface for an arbitrary, unclassified exception).
    """
    if getattr(exc, "structurally_wedged", False):
        return {"code": STRUCTURAL_PIN_ERROR, "message": f"{type(exc).__name__}: {exc}"}
    return {"code": INTERNAL_ERROR, "message": f"Internal error: {type(exc).__name__}"}


async def _dispatch_message_impl(msg: dict) -> dict:
    """Validate a pre-parsed JSON-RPC 2.0 message dict and invoke the registered handler.

    Purpose: shared core for the dispatch pipeline — takes an ALREADY-PARSED dict
    (step 1, JSON parsing, is handled by the caller) and performs steps 2-7, returning
    a JSON-RPC 2.0 result-or-error dict (NOT bytes).

    Validation precedence (spec-pinned — do NOT reorder):
        jsonrpc version → params type → method string → registry lookup → handler invoke

    Spec backlink: docs/plans/2026-07-02-pcore-03-beachhead-coordinator-core.md § C1 / C1b

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
    handler = get_op_handler(method)
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
    # Spec backlink: docs/plans/2026-07-04-coordinator-core-global-multiplex-migration.md § C3
    #
    # DR-215 command-type retirement: drain/in-flight machinery removed — no concurrent
    # requests in the command-type model (one op per process). _handle_connection and
    # start_server_async removed by C5. is_draining/in_flight_increment/in_flight_decrement
    # no longer called from dispatch_message.
    # Backlink: docs/decisions/DR-215-coordinator-core-command-type-execution-model.md
    op_timeout = _timeout_for(method)
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
    # Bound with a bare `set()` — no `with` block and no reset token — so the
    # handler-invocation branches below keep their existing indentation. This is
    # the dispatch hot path, and a 60-line re-indent to gain a context manager is
    # churn a reviewer would have to diff line-by-line for no behavioural gain.
    # Nothing leaks: every dispatch REBINDS the var as its first act, so an error
    # branch that returns without unbinding cannot carry a stale list into the
    # next dispatch — the next call's `set` shadows it before any handler can
    # append. The failure direction if one ever did survive is a DISCARDED
    # declaration (under-coverage), never a misattributed one.
    _declared_writes: list = []
    _declared_writes_var.set(_declared_writes)
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
            return {
                "jsonrpc": "2.0",
                "id": id_,
                "error": {
                    "code": INTERNAL_ERROR,
                    "message": f"op timed out after {op_timeout}s",
                },
            }
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
            return {
                "jsonrpc": "2.0",
                "id": id_,
                "error": {
                    "code": INTERNAL_ERROR,
                    "message": f"op timed out after {op_timeout}s",
                },
            }
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


async def dispatch_message(msg: dict) -> dict:
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
    """
    import time as _time

    method = msg.get("method") if isinstance(msg, dict) else None
    request_repo = resolve_request_repo(msg) if isinstance(msg, dict) else None

    t_start = _time.time()
    perf_start = _time.perf_counter()
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
    try:
        from coordinator_core.telemetry.op_latency import new_correlation_id, record_op_started
        from coordinator_core.session.core import resolve_session_id

        corr_id = new_correlation_id()
        sid = resolve_session_id() or None
        record_op_started(
            op=method if isinstance(method, str) else "<unknown>",
            t_start=t_start,
            corr_id=corr_id,
            repo_root=request_repo,
            sid=sid,
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
            outcome = "timeout" if isinstance(message, str) and message.startswith("op timed out after") else "error"
        return response
    except BaseException:
        outcome = "error"
        raise
    finally:
        elapsed_ms = (_time.perf_counter() - perf_start) * 1000.0
        try:
            from coordinator_core.telemetry.op_latency import record_op_latency

            record_op_latency(
                op=method if isinstance(method, str) else "<unknown>",
                t_start=t_start,
                elapsed_ms=elapsed_ms,
                outcome=outcome,
                repo_root=request_repo,
                sid=sid,
                corr_id=corr_id,
            )
        except Exception:
            _log().debug(
                "coordinator_core.ipc: op-latency recording failed for %r", method,
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

    The single engine-side home for the post-import dispatch body that seven example-doctrine-repo hook
    shims currently hand-roll identically (build envelope -> asyncio.run(dispatch_message)
    -> unwrap result). DR-118 rules that a example-doctrine-repo-resident hook shim is a pointer only —
    resolve the engine root, hand over the raw payload, translate the verdict into the
    harness channel, and degrade. Transport policy (the envelope shape, the dispatch call,
    error surfacing) is engine-resident, not the shim's to reimplement.

    Spec backlink: cross-repo/archive/2026-07-31-example-doctrine-repo-em-dr116-seam-contents-and-ipc-hook-dispatch.md
                   (example-doctrine-repo's DR-118 — the memo's filename says dr116 because example-doctrine-repo renumbered
                   the ruling at execute time; DR-116 in the example-doctrine-repo tree is an unrelated
                   record, per 2026-07-31-example-doctrine-repo-em-dr116-is-now-dr118-citation-correction.md)

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

    msg: dict = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": op_name,
        "params": params,
    }
    if origin_worktree:
        msg[_ORIGIN_WORKTREE_FIELD] = origin_worktree

    response = asyncio.run(dispatch_message(msg))

    error = response.get("error")
    if error is not None:
        raise HookDispatchError(
            op_name,
            error.get("code", INTERNAL_ERROR),
            error.get("message", ""),
        )

    return response.get("result", {})


# _handle_connection removed by C5 (DR-215): UDS server transport retired.
# coordinator_core is now a command-type engine; dispatch_message is called directly.
# Backlink: docs/decisions/DR-215-coordinator-core-command-type-execution-model.md

# UDS socket_path() removed by C5 (DR-215): coordinator_core is now command-type; no socket.

# _guard_socket_stomp() removed by C5 (DR-215): UDS transport retired.

# start_server_async removed by C5 (DR-215): UDS server transport retired.
