# Memo tool conformance — contract-invariant vs. CLI-ergonomic

> Spec backlink: `pln-memo-tool-rebuild-claude-klabauter-owns--bd5745` § C8 (A7)
> Spec backlink: `docs/decisions/DR-210-claude-klabauter-native-tooling-ownership-strangler.md` § 2(a)/2(b)

C8's mandate: "Name which of the coordinator-claude CLI's tests are contract-invariant vs.
CLI-ergonomic so contract coverage isn't silently thinned at cutover." This is that
naming pass, run against the coordinator-claude clone on 2026-07-21. It is a classification, not a
byte-compare — claude-klabauter's native ops deliberately diverge from the coordinator-claude CLI's bytes on
all five footguns this tool's ownership move exists to fix (see plan § Problem).

**Contract** = DR-061 delivery model + `cross-repo-communication.md` lifecycle/doctrine
+ the frontmatter schema + the `kind` enum (`ask | consult | fyi | proposal`) — coordinator-claude
keeps this per the DR-210 Amendment 2026-07-21. **Ergonomics** = the bash CLI's own
argparse surface, editor invocation, list formatting, folder-scan fallback — claude-klabauter is
not obligated to replicate these, and several are the exact footguns being removed.

## Bucket 1 — Contract-invariant (must keep passing; gates drift at cutover)

These assert the WIRE CONTRACT, independent of which tool produces the memo. Gated by
`test_doe_round_trip_conformance.py` in this directory (live off coordinator-claude-HEAD, never
vendored, per `doe_drift.resolve_doe_clone()`).

| Fixture / test | File (coordinator-claude clone) | Why contract-invariant |
|---|---|---|
| Site 1 — CLI write target | `coordinator/bin/cross-repo-memo-roundtrip.test.py` | Asserts the memo lands at `cross-repo/inbox/<date>-<from>-<topic>.md` via the REAL `memo.send` op — the DR-026 filename lockstep site. |
| Site 2 — schema `applies_to` | same | Frontmatter schema-validity is the contract; claude-klabauter's emitted memo must satisfy coordinator-claude's schema regardless of who wrote it. |
| Site 3a/3b — own-inbox guard regex | same | The guard fires on `cross-repo/inbox/` writes matching the sender's own repo, never on `cross-repo/archive/` — a cross-repo safety invariant, not CLI UX. |
| Site 5 — archival sweep | same | `cs_sweep_actioned_memos` must still git-mv a `status:actioned` memo written by the native op — proves the native writer's on-disk shape is indistinguishable from the legacy writer's, from the sweep's perspective. |
| Collision 1 / 1b — cross-sender same-day-same-topic | same | DR-026 sender-namespacing: both memos must survive distinctly. Collision 1b dispatches the REAL `memo.send` op — this is claude-klabauter's own collision contract, not the CLI's. |
| Collision 2 — same-sender same-day-same-topic | same | O_EXCL fail-loud, no clobber — DR-214 D2 criterion 4, already re-affirmed for `memo.send` (strang-03 AC5). |
| AC9 — op-refusal regression | same | A real collision refusal must exit non-zero, retain the outbox, commit nothing — pins the op-level (not CLI-level) refusal contract. |
| `test_cross_repo_memo_collision.py` (`coordinator/tests/`) | coordinator-claude's own `_write_file` O_EXCL guard | **Reference oracle, not re-run here** — it tests the coordinator-claude CLI's OWN write helper, not claude-klabauter's op. Claude-Klabauter's equivalent collision semantics are covered locally by `coordinator_core/ops/fleet/tests/test_memo_send.py`; this file is the doctrine source those local tests must keep tracking, not a second gate to wire. |
| `kind` enum round-trip (`test_kind_{ask,consult,fyi,proposal}_round_trips`, `test_kind_invalid_value_rejected`, `test_send_side_kind_validation_*`) | `cross-repo-memo.test.py` | The `kind` enum is coordinator-claude contract per the DR-210 Amendment. Claude-Klabauter's `memo_send.py` already mirrors `_VALID_KINDS` natively (`("ask", "consult", "fyi", "proposal")`, enforced in `_validate_kind_and_summary`) with local coverage in `test_memo_send.py`/`test_memo_draft.py`. No separate cross-repo gate needed — the enum literal itself is the contract surface; a value drift on either side would need a schema-visible change, not a silent one. |
| `test_summary_derived_from_body_when_omitted` / `test_summary_truncated_when_over_120_chars` (semantics, not the CLI's specific truncation UX) | `cross-repo-memo.test.py` | `summary:` presence + a bounded length is schema-relevant; claude-klabauter's *prose-first* derivation (footgun #4 fix) deliberately changes the ALGORITHM, not the schema requirement that a `summary:` field exists and is bounded. Covered natively in `test_memo_compose.py`/`test_memo_send.py`. |
| `test_topic_path_traversal_rejected`, `test_write_file_traversal_guard`, `test_no_orphan_dir_on_gitignore_block` | `cross-repo-memo.test.py` | Cross-repo write-safety invariants (no path traversal, no orphan dirs on a blocked write) apply to ANY writer, not CLI-specific UX. Claude-Klabauter's `_containment_check`/`_write_memo_file` already enforce the equivalent locally. |

## Bucket 2 — CLI-ergonomic (coordinator-claude CLI's own UX; not re-tested here, not thinned)

These test the **bash CLI's own surface** — argparse flags, editor invocation, receiver
listing formatting, the registry-read fallback ladder the CLI's `--allow-folder-scan`
knob controlled. Claude-Klabauter's native ops are not obligated to replicate them; several are
the footguns this plan removes outright (PM decision 2026-07-21: no folder-scan
fallback at all — Bucket 2's fallback-ladder tests describe behavior claude-klabauter explicitly
does NOT reproduce).

| Test group | File | Why CLI-ergonomic |
|---|---|---|
| `test_central_receiver_alias_*`, `test_central_receiver_canonical`, `test_central_receiver_case_whitespace_normalization` | `cross-repo-memo.test.py` | `central`/`central-em`/`coordinator-claude-em` aliasing is the CLI's own `--to` convenience layer. Claude-Klabauter's resolver (`_memo_resolver.py`) does its own exact-normalized-name match (A3) — a deliberately narrower, fail-loud posture, not a port of the alias table. |
| `test_publish_target_*` (7 tests) | `cross-repo-memo.test.py` | Publish-target rejection is a CLI-side guard against a specific historical misuse (`coordinator-claude-em` as a `--to`); not part of the wire schema. |
| `test_home_redirect_*` (5 tests) | `cross-repo-memo.test.py` | Machine-specific home-redirect precedence is a CLI config-resolution detail, not a memo-shape contract. |
| `test_fallback_*` (9 tests: `resolves_single_verified_match`, `resolve_receiver_path_disabled_by_default`, `exact_match_not_prefix`, `verify_failure_treated_as_no_match`, `zero_matches_hard_fails`, `ambiguous_matches_hard_fail_with_candidates`, `does_not_fire_on_clean_key_absence`, `does_not_fire_on_happy_path`, `resolves_alias_divergent_receiver`) | `cross-repo-memo.test.py` | This is the CLI's **folder-scan fallback ladder itself** — footgun #3, the exact behavior the PM ruled OUT (A3: "the `--allow-folder-scan` opt-in is removed, not gated"). These tests describe a fallback path claude-klabauter's resolver does not have and must not grow. |
| `test_dry_run_*` (4 tests) | `cross-repo-memo.test.py` | The CLI's `--dry-run` UX (filename labeling, non-git-cwd handling). Claude-Klabauter's dry-run resolution (A2) reuses `memo_send.py`'s `build_dry_run_result`, covered natively in `test_memo_list.py`. |
| `test_list_receivers_*` (5 tests) | `cross-repo-memo.test.py` | Receiver-listing display formatting (mirror-owner labels, OSS-mirror labeling). Covered by claude-klabauter's own `memo_list.py` output shape, tested in `test_memo_list.py` — not obligated to match the CLI's column layout. |
| `test_premise_check_advisory_*` (6 tests) | `cross-repo-memo.test.py` | Exercises a coordinator-claude-side PreToolUse advisory hook (`memo-pre-dispatch-guard`) reacting to the CLI's own dispatch path in coordinator-claude's repo. Hooks are discovery-resolved surface claude-klabauter does not own (see claude-klabauter CLAUDE.md § Project Overview — plugin/hook surfaces stay in coordinator-claude/coordinator-claude). |
| `test_send_time_registry_error_*`, `test_machine_local_repos_keys_*`, `test_classify_receiver_registry_error_not_unknown`, `test_draft_registry_error_not_conflated_with_unknown` | `cross-repo-memo.test.py` | CLI-side error-message-shape/diagnostics tests for the registry read. Claude-Klabauter's resolver (`_memo_resolver.py`) fails loud on registry-read failure with its own message shape (A3); the *outcome* (fail loud, no folder-scan) is the contract-invariant part, already covered above — the specific diagnostic wording is CLI-ergonomic. |
| `test_body_file_dash_reads_stdin`, `test_missing_send_args_points_at_list_receivers` | `cross-repo-memo.test.py` | argparse/stdin-plumbing convenience, not schema. |
| `test_memo_filename_unit`, `test_memo_filename_different_senders_no_clobber`, `test_memo_filename_same_sender_clobber_guard` | `cross-repo-memo.test.py` | Unit-level filename-builder tests for the CLI's own `_memo_filename`. The FILENAME SHAPE these assert (DR-026 sender-namespacing) is contract-invariant and is already gated by Bucket 1's Collision 1/1b; the unit-level CLI helper itself is not re-tested here. |
| `test_check_addressee_*` | `cross-repo-memo.test.py` | CLI-internal helper unit tests. |
| Everything in `cross-repo-memo-draft.test.py` (18 tests: `test_draft_*`, `test_send_consumes_outbox`, `test_send_missing_topic_hint_list`, `test_send_malformed_outbox_leaves_in_place`, `test_send_receiver_unresolvable_leaves_in_place`, `test_send_preserves_supersedes`, `test_list_*`, `test_discard_*`, `test_compose_*`) | `cross-repo-memo-draft.test.py` | This is the coordinator-claude CLI's OWN draft/compose/list/discard verb test suite (the CLI's `_cmd_draft`/`_cmd_compose`/`_cmd_list`/`_cmd_discard`). Claude-Klabauter's native `memo.draft`/`memo.compose`/`memo.list` ops (C2, C5) have their own equivalent coverage in `test_memo_draft.py`/`test_memo_compose.py`/`test_memo_list.py` — this file describes the BASH CLI's UX (its own outbox dir, its own `$EDITOR` invocation in `test_compose_open_without_editor`), not the wire schema those ops emit into. `test_send_preserves_supersedes` is the one item worth flagging: it verifies the CLI's outbox→send path preserves a `supersedes:` field already staged in a draft. Claude-Klabauter's `memo.send` handles `supersedes:` directly as a param (A6/C6) rather than via an outbox-staged draft, so this is a CLI-shape test, not a schema gap — the schema-level behavior (does a sent memo with `supersedes:` set correctly reference the prior file) is covered by `test_memo_send.py`'s `supersedes` tests. |
| `cross-repo-memo-c6.test.py` — `test_c6b_*` (`actioned_ask_validates_green`, `actioned_fyi_validates_green`, `status_consumed_rejected`, `status_active_rejected`, `combined_handoff_mutation_rejected`) | `cross-repo-memo-c6.test.py` | Exercises coordinator-claude's own frontmatter-schema-validator CLI/hook path (`validate-frontmatter-schema.js`) directly — a coordinator-claude-owned validation tool, not the memo-producer contract. Claude-Klabauter's ops emit schema-valid frontmatter (self-validated, `_self_validate_frontmatter_fields`); whether coordinator-claude's OWN validator hook correctly rejects a malformed instance is coordinator-claude's tooling to test. |
| `test_c6a_kind_roundtrip_schema_and_band` | `cross-repo-memo-c6.test.py` | Same — exercises coordinator-claude's schema-band tooling, not the emitted memo's shape (already covered by Bucket 1's kind-enum row). |

## Coverage accounting

Coordinator-claude's own memo-test corpus (`cross-repo-memo.test.py` 98 tests + `cross-repo-memo-draft.test.py`
18 + `cross-repo-memo-c6.test.py` 6 + `test_cross_repo_memo_collision.py` 7 +
`cross-repo-memo-roundtrip.test.py` 10 = **139 tests**) splits as:

  <!-- Review: code-reviewer — the prior "~17"/"~122" bucket-count split against the
       139-test total was not independently checkable from this table (rows mix
       single tests, "N tests," and unenumerated groups); dropped the specific
       figures rather than assert an unverifiable arithmetic. -->
- **Bucket 1 (contract-invariant)** — the rows in the table above marked
  contract-invariant, gated end-to-end by the 10-case round-trip fixture wired above
  (which itself subsumes several: kind-enum validity, summary presence/bounds, and
  write-safety are all exercised inside claude-klabauter's own `memo_send.py`/`memo_compose.py`
  local suites, cross-referenced above rather than re-run against the coordinator-claude clone a
  second time).
- **Bucket 2 (CLI-ergonomic)** — the remaining rows above, covering the CLI's own
  argparse/UX/fallback-ladder/hook-adjacent surface. None of these describe behavior
  claude-klabauter's native ops are contractually bound to reproduce; several
  (`test_fallback_*`, `test_dry_run_*`) test footguns this plan's PM-ratified
  decisions explicitly remove.

No contract-relevant behavior is left untested at cutover: every Bucket 1 row has either
a live cross-repo assertion (round-trip fixture) or an already-shipped native-local test
cited above.

## Known fixture defect (coordinator-claude-owned, relayed not patched)

`test_site4_surface_glob` in `cross-repo-memo-roundtrip.test.py` invokes coordinator-claude's own
`workday-start-cross-repo-memo-surface.py` (shebang `#!/usr/bin/env python3`) via
`subprocess.run(["bash", surface_script], ...)` — bash cannot execute Python source and
the site fails at the first non-shell syntax token. Verified live against the coordinator-claude clone
2026-07-21. This is a bug in coordinator-claude's OWN fixture harness, not a claude-klabauter regression — claude-klabauter
does not own `coordinator-claude/coordinator/bin/`. Tracked explicitly in
`test_doe_round_trip_conformance.py`'s `_KNOWN_DOE_SIDE_FAILURES` (a NEW failure past
this one entry still fails the gate) and relayed via the C8 cutover memo rather than
patched here.
