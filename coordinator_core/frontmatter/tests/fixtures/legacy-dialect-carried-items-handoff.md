---
title: "The mirror is published and private — the install test needs a human, and going public is the unfired gate"
created: 2026-08-07
branch: "work/machine-b/2026-07-21to26"
status: open
predecessor: "state/handoffs/2026-08-05-2026-08-05_215136_2026-08-04_235721_2026-08-03_155105_claude-klabauter-oss-release.md"
kind: session-handoff
handoff_phase: continuation
deployment_state: shipped
advanced_by: dlv-claude-klabauter-oss-release-engine-mirr-a0952e
advanced_at: '2026-10-21T12:55:11Z'
category: infra
summary: "Published: ec6f152, 3247 files, 7/7 rows, 0 warnings, still PRIVATE. Remaining work is a human install test and the public-gate ask."
pickup_ready: false
deliverable_id: "dlv-claude-klabauter-oss-release-engine-mirr-a0952e"
plan_blitz_hold_reason: "PM RULED 2026-09-12: the claude-klabauter mirror goes public (\"yes it always does\"). The question is closed. Still held from plan-blitz because nothing left on this baton is planning work: the install checklist needs a human, the DoE cross-repo memo is one CLI call, and the go-public flip is irreversible and externally visible - a deliberate act by whoever picks this baton up, not something a wave's XS executor should do on the way past. Five consecutive sizings already agree it is XS. Clear the hold by picking the baton up, not by re-sizing it."
plan_blitz_hold_cite: "state/plan-blitz/20260911T100110Z/wave-1.landing.json"
initiative: null  # FK to state/initiatives/<id>.yaml; null when no named initiative
predecessor_id: "hnd-payload-proven-clean-tree-and-65adaa"
authoring_session: "a0df95e6-7369-4211-9ebd-c1eaa8466848"
distill_fate: commitment
carried_items:
- carry_id: cf-all-families-residual-invariant-3a91c4
  description: No invariant asserts a row's transform output is free of EVERY codename family — only
    the glued content-engine compound is pinned.
  carry_count: 3
  disposition: carried
  disposition_detail: 'Third carry, still needing an actual-content oracle rather than synthetic tokens.
    This session ran an INDEPENDENT full-tree scan of the published payload as a substitute (1 codename
    hit across 3247 files, the ratified self-exclusion; 5 persona-slug files, all ratified or keepset)
    — that is evidence the gap is not currently leaking, not the invariant itself. The invariant is
    still worth building; the publish no longer blocks on it.'
- carry_id: cf-vendored-schema-drift-major-8c7e21
  description: 4 of 15 vendored schemas diverge from DoE HEAD; review-findings.schema.json is we-are-behind
    at bump-class major.
  carry_count: 3
  disposition: carried
  disposition_detail: 'Untouched this session — the publish did not depend on it. Re-vendor via bin/claude-klabauter-revendor-schema.py,
    never by hand: the probe compares against DoE HEAD while the gating tamper-check compares against
    a pinned SHA, so a hand copy turns the probe green while breaking check_schema_drift.'
- carry_id: cf-handoff-id-missing-blocks-successor-6a013d
  description: 21 live state/handoffs/*.md carry no handoff_id, and a handoff without one cannot have
    a successor written by baton-assemble.
  carry_count: 2
  disposition: closed
  disposition_detail: 'Closed by completion, not by decision. Real count was 25, not the ~20 estimated.
    21 backfilled at 3462ff60f via per-file calls to the sanctioned script''s own backfill_one(), never
    a directory sweep; 2 more at 8426d3e2b after quoting their bare YAML titles; 2 deliberately skipped
    as live-peer-claimed and left for their owners. Verified end-to-end: baton-assemble now resolves
    predecessor_id for backfilled files with no fallback error.'
- carry_id: cf-backfill-script-quoted-title-only-7c31d9
  description: coordinator-content-repo's backfill-handoff-id.py silently skips any handoff whose `title:` is a bare
    YAML scalar rather than double-quoted, reporting `skipped-no-title`.
  carry_count: 1
  disposition: carried
  disposition_detail: 'New this session, found by hitting it on two handoffs. Worked around locally by
    quoting the two titles, but the script is sibling-owned and will keep silently skipping any future
    bare-title handoff. Belongs in a cross-repo memo to coordinator-content-repo, not a local patch — the failure is
    silent (a skip, not an error), which is what makes it worth reporting rather than absorbing.'
- carry_id: cf-blank-machine-install-test-unrun-4e21b7
  description: The blank-machine install checklist is authored and committed but has never been run by
    an actual human on an actual clean machine.
  carry_count: 1
  disposition: blocked
  disposition_detail: 'Blocked on a human with a clean machine — a genuine external dependency no dispatch
    can discharge. The checklist exists at docs/install/blank-machine-install-test.md and is keyed on
    observable behaviour rather than installer banners, per AGENTS.md''s pattern. Unblocks the moment a
    tester runs it; until then the install chain is unvalidated on anything but this machine.'
shipped_in: 2416ef44
shipped_in_kind: ship-commit
---

## What Was Accomplished

**The mirror is published.** `dbc-oduffy/claude-klabauter` carries commit `ec6f152` — 3247 files —
and `origin/main` now exists where the repo had no default branch at all. The publish run itself was
clean: exit 0, 7/7 rows, 3229 files synced, **0 warnings**. It is still **PRIVATE**, verified live
against the remote rather than inherited from the predecessor.

**The dirty-skip warning the PM ratified is landed** (`5f3854bb3`). `check_dirty_tree`'s real-run leg
now warns — naming every uncommitted path individually — and publishes from the committed ref,
instead of silently dropping the row. It earned its keep immediately: on the release run itself,
`coordinator/bin` was dirty with a live peer's in-flight file, so under the old behaviour that entire
row would have dropped out of the release without failing anything. 76/76 tests green.

**A false first-impression was caught before it shipped.** The mirror's `INSTALL.md` still carried a
"current payload note" telling first-time users that `scripts/`, `bin/`, `lib/` and `coordinator/bin/`
were not published, and directing them to a degraded interim path that skips registration wiring
entirely. All 7 rows ship those directories — `scripts/setup.py` is 1592 lines in the payload. The
note would have been the first thing every reader hit. Removed at `1f0a7fbc8`.

**Two things reconciled as already-done since the predecessor:** the `*check-persona*` over-broad glob
was already fixed on this branch at `084dbad7f` (a doc line still citing the dead mechanism was
corrected), and the handoff_id backfill closed at 25/25 rather than the estimated ~20.

## Current State

**Nothing about the release is in flight.** The payload is published, committed, and pushed; the
working tree carries no publish-related residue. The remaining work is not more publishing.

**Going public has never been asked.** It is an unfired PM gate and was deliberately left untouched —
the predecessor named it out of scope and nothing this session did changes that. The repo's visibility
is PRIVATE and correct.

**`COORDINATOR_OVERRIDE_DIRTY_TREE` is now a provable no-op** on the leg the ratified change touched:
override-set and override-unset both warn and proceed identically. It was left wired exactly as-is,
because the PM ruled on the warning change only and this is a documented key. Retiring it is now a
pure doc/message cleanup with no behaviour change — which should make the sibling repo's separate
retirement ask cheap to land, but it still needs its own ask.

**PM ruling this session — the two-pass convergence proof leaves the routine loop.** The ~39-minute
figure the predecessor recorded is the cost of *two* complete publishes run back-to-back to prove
byte-identical convergence. That was one-time scaffolding to establish the payload is deterministic;
it is now proven and shipped. A single publish pass is ~10 minutes and is what the routine loop
should use. Re-run the two-pass proof only if the transform logic itself changes — not per release.

**Concurrency note for the picker-up:** at least one live peer session (`96a20bd5`) was working in
`coordinator_core/workstream_complete/` and `coordinator/bin/wsc-coverage-gate-runner.py` throughout.
Its files were deliberately left uncommitted and untouched. Do not adopt them.

**One inert residue:** a session-scoped write sentinel was left in the mirror clone's `.git/`
(`allow-xrepo-write-a0df95e6-…`). A destructive-rm guard correctly refused to delete anything inside
`.git/`, and it was not worth overriding. It is inert and keyed to a session that has ended.

## Next Steps

1. **Get the blank-machine install checklist actually run by a human on a clean machine.** This is the
   single highest-value remaining item and no dispatch can do it. The checklist is written and keyed
   on observable behaviour, never an installer success banner. Until someone runs it, the install
   chain is validated on exactly one machine — the one that authored it.

2. **Ask the PM whether the mirror goes public.** It is an external, effectively irreversible action
   and has never been put to them as a question. Do not infer authorization from the publish having
   happened — private publish and public release were separated deliberately.

3. **Send coordinator-content-repo a memo about the bare-title backfill skip.** Their script silently skips rather
   than failing, so the defect is invisible at the call site. Route it through `cross-repo-memo`, never
   a hand-written file in their tree.

4. **Raise the `COORDINATOR_OVERRIDE_DIRTY_TREE` retirement as its own ask** if and when it is worth
   the PM's attention — it is now doc-only, and bundling it into something else on inference is
   exactly what the predecessor warned against.

**Out of scope for next session:** re-running any convergence proof (the PM ruled it out of the
routine loop); making the mirror public without a fresh explicit ask; re-litigating the joint rename;
the peer-owned `coordinator_core/workstream_complete/` work; the `coordinator-claude` republish half of
the tracker row, which is a distinct workstream from the klabauter mirror and has its own open items.

## Session Ledger

<!-- Phase 2 LoE accumulator. Each session appends one line. -->
<!-- Format: YYYY-MM-DD | <sid6> | <tshirt> | <Nd / No> | <one-line summary> -->
2026-08-05 | 93efc3 | XL | 13d / 14o | Payload clean at 7/7 rows and zero findings; four lying instruments identified as the real cause of the stall; glued-compound leak closed on all 7 rows; stage-verify-swap ordering; guard messages made portable; DoE unblocked twice.
2026-08-06 | c24214 | M | 3d / 2o | Convergence proven twice, second time with no dirty override at all; DoE unblocked by committing their three orphaned files; their roster-leak premise refuted against 3123 published files; PM ruled for the dirty-skip nudge; handoff_id gap found by hitting it.
2026-08-06 | a0df95 | L | 4d / 0o | Mirror PUBLISHED at ec6f152 (3247 files, 7/7, 0 warnings, still private); ratified dirty-skip warning landed and immediately saved a row from silently dropping; stale INSTALL.md interim note caught before it became the release's first impression; handoff_id backfill closed 25/25.
