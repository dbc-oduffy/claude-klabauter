# Bash-guard override keys and bypass options

This doc is the full content that `coordinator_core.bash_guards._helpers
.operator_override_note()` used to inline on every guard firing (~50 words,
paid by every advisory message in the suite, every time). It now fires a
short pointer to this doc instead — see that function's docstring for the
cut history. This doc carries the detail; the in-message note carries only
the one fact that must survive at decision time (an env var is pre-launch
only).

## Bypass options for a human operator — not for the agent being guarded

None of the routes below are actions a dispatched agent (or the EM acting on
an agent's behalf, mid-session) can take. They are what a human operator,
watching the session, can do.

1. **The `!`-prefixed prompt.** Skips PreToolUse hooks entirely for that one
   turn. Human-only — there is no agent-invocable equivalent.
2. **The blanket-disarm marker**
   (`coordinator_core.bash_guards._blanket_disarm`), a file the operator
   creates via the `!`-prefixed prompt (never by an agent's own Bash call —
   see Security context below). Three scopes:
   - `Scope: time` — machine-wide, bounded, capped at 8 hours.
   - `Scope: session` — one named session, **does not inherit** to that
     session's own dispatched subagents (conservative default).
   - `Scope: machine-total` — standing, no expiry required, but
     **unconditionally EM-only**: never disarms anything for a dispatched
     subagent, regardless of marker content.
   Every scope also names which guard **band(s)** it suppresses via a
   `Bands:` field (`advisory-rewrite`, `platform-conditioned-deny`).
3. **`<ENV_VAR>=1`, set before the harness/hook process launches.** This is
   the one route named inline in every guard message, because it is
   guard-specific (each guard reads its own var) — see the key table below.
   **Pre-launch only.** Nothing reachable from inside a live session sets a
   variable this guard process will see (see Security context, leg 1).
4. **The in-session guard-unlock sentinel**, a one-shot file the operator
   drops for the exact `(session_id, guard_name)` pair that just denied —
   see § In-session unlock below. Unlike routes 1–3, this one is reachable
   from *inside* the session that hit the deny: no relaunch, no waiting for
   a future session to inherit a pre-launch var. It is additive, not a
   replacement — the env-var route above stays fully supported.

**`GuardBand.CONFINEMENT_DENY` is never suppressible by the blanket-disarm
marker, at any scope, with any marker content, full stop.** Its guards
(`--no-verify` bypass, destructive `rm`/`git clean`/`git revert`, worktree
creation, subagent-commit, the reviewer bash allowlist, and siblings) are
platform-independent correctness protection, not advisory noise — a marker
naming `confinement-deny` in its `Bands:` field is rejected in its entirety,
not partially honored. This carve-out is specific to route 2 (the blanket
marker); it does **not** extend to route 4. The in-session unlock is
per-guard, one-shot, and operator-typed at the moment of need, and an
earlier draft that carved the removal-leg guards out of it as
pre-launch-only-forever was rejected outright — see § In-session unlock,
"Coverage" below.

## In-session unlock — no relaunch required

Every override above route 3 required knowing, *before launch*, which guard
would fire. The situation that actually teaches an operator the override
exists — the deny message itself — is inside a session that a pre-launch env
var can no longer reach. This channel is the fix: an operator, from outside
the denied tool call (the `!`-prefixed prompt, same human-only surface as
route 1), drops a small marker file naming the exact guard to clear, and the
engine seam that produced the deny consumes it on the very next attempt.

**What it is.** A per-`(session_id, guard_name)` sentinel file, resolved by
`coordinator_core.session.guard_unlock_sentinel.sentinel_path()` under the
platform temp directory (`tempfile.gettempdir()` — Windows-safe, no
hardcoded `/tmp`):

```
<tempdir>/coordinator-guard-unlock-<session_id>__<guard_name>
```

Both components are slugified before they reach the filename (anything
outside `[a-zA-Z0-9_-]` becomes `_`), so a stray path separator in either
value can never escape the temp directory or collide across the
sanitization boundary into an unintended guard. Its presence is the only
thing that matters — an empty file is sufficient; the operator does not need
to write content into it. The `<guard_name>` and `<session_id>` to use are
whichever values the deny message that just fired names — do not guess
them.

**Scoping (AC2).** Keyed on both the session id *and* the guard module name.
Granting it clears exactly one guard in exactly one session — never a
blanket "all guards off" switch, and never something a peer session can
benefit from. A sentinel for guard A does nothing for guard B; a sentinel
written under one session's id does nothing for another session's.

**One-shot (AC3).** The engine consumes and unlinks the sentinel on the
first hard-deny it clears. The same write attempted again immediately after
is denied again — a fresh sentinel is required per grant. This is
deliberate: the unlock is authorizing *this one blocked action*, not
disabling the guard for the rest of the session.

**Coverage (AC4).** Every hard-deny guard is reachable through this channel,
with no exemptions — including the sentinel-removal / irreversible-harm
cohort (`block_worktree_sentinel_creation`, `block_stash_destruction`, and
siblings). `block_dev_repo_sentinel_removal` was itself flipped to advisory
by the 2026-08-06 guard-class census (its deny leg deleted, not merely
gated) and is no longer part of this hard-deny cohort — dropped from this
example list rather than left to misdescribe a guard this channel no longer
needs to reach. An earlier draft proposed carving
those out as pre-launch-only-forever; the PM rejected that carve-out
outright. The guard itself is not weakened by any of this — it still fires,
still denies by default, and still requires a deliberate operator action to
clear. What changed is reachability, not strictness.

**Fails closed, not open.** If the session id can't be resolved from the
tool-call payload, that is a deny, not a grant. A crash inside the sentinel
resolver is likewise normalized to "no unlock" rather than propagating — the
one direction this mechanism must never fail in is open.

**Relationship to the pre-launch keys (AC5).** Additive only. Every
`COORDINATOR_OVERRIDE_*` key in the table below keeps working exactly as
documented; this channel does not retire, migrate, or shadow any of them.
Pre-launch is no longer the *only* channel for anything, which is the
problem this section exists to close, but it remains a fully valid one —
useful in particular when the same override is needed repeatedly across a
run, where re-typing a one-shot sentinel on every hit would be the wrong
tool for the job.

**Exact deny-message wording.** Both engine seams (`write_guards/engine.py`,
`bash_guards/dispatch.py`) append one line to `permissionDecisionReason` on
every hard-deny they return — the operator never has to go compute the
sentinel path by hand:

```
Bypass options for a human operator, not this agent: create <sentinel path> (in-session, one-shot, clears only this guard for this session) -- full list: docs/reference/guard-override-keys.md
```

`<sentinel path>` is the exact, literal `sentinel_path(session_id,
guard_name)` for the guard that just fired, in the shape documented above.
This line is appended at the two engine seams only — never hand-written
into an individual guard module — so a guard added tomorrow inherits it for
free (C4, docs/plans/2026-08-03-in-session-operator-unlock-for-the-hard-.md).
It is shown FIRST, ahead of any pre-launch-key mention already baked into
that guard's own reason text (route 3 above), since the in-session channel
is the one usable without leaving the denied session. Both lines share the
same "Bypass options for a human operator, not this agent: ..." register —
addressed away from the agent reading it, on purpose (§ Security context) —
and a missing/unresolvable `session_id` (the fail-closed case) simply omits
the in-session line rather than printing a sentinel path keyed to nothing.

**What this channel does not claim.** A file in the temp directory is not
cryptographically operator-only — an agent with shell access could write one.
That is stated here rather than glossed, because a doc claiming a property the
mechanism does not have is worse than one naming the limit. The honest
comparison is against what it replaces, not against perfection: a pre-launch
env var is not agent-proof either, since an agent can set one on a subprocess
it spawns. What the env var actually bought was *deliberateness* — and a
one-shot, per-guard command a human types at the moment of need is equally
deliberate while being reachable, which the env var was not.

So the norm is real even though the enforcement is not: this sentinel exists to
be typed by a human operator. A dispatched agent writing one on its own behalf
is defecting, not clever — the same way the example-doctrine-repo executor that noticed
`block_dev_repo_sentinel_write` had no `Bash` matcher, and declined to route
around it anyway, was not being naive. That restraint is the behaviour this
whole design assumes and depends on.

## Security context — why none of this is reachable by a subagent

Verified, not merely asserted — three independent legs, each with its own
test/mechanism:

1. **Fresh-subprocess-per-event, stdin-only.** The PreToolUse(Bash) hook is
   spawned as a brand-new subprocess for every single tool-call event. It
   reads the tool-call payload on stdin only, and never shell-executes the
   candidate command — every guard does pure string/token analysis on the
   command text. A `VAR=1` prefix, an `export` in a prior Bash call, or an
   `env VAR=1` wrapper *inside* the candidate command string are just
   characters in that string; nothing ever interprets them as shell syntax,
   so they never reach this process's `os.environ`. Pinned by
   `coordinator_core/bash_guards/tests/test_override_unreachability_boundary.py`.

   **This is a side effect of a performance decision, not a designed
   security feature** — fresh-process-per-event exists to avoid a long-lived
   hook process accumulating stale state, at the real cost of a cold
   interpreter start on every Bash call (worst on Windows). A future change
   that pools or reuses the hook process across events would silently delete
   this property: a long-lived process retains whatever environment
   mutations accumulate across calls, and stdin-only delivery stops being the
   only channel in. If that test ever has to be deleted to unblock a pooling
   optimization, the confinement guarantee this section describes needs to be
   re-derived some other way first — it is not optional cleanup.

2. **The blanket-disarm marker excludes dispatched subagents at both
   scopes that could otherwise fail open** — `session` scope does not
   inherit to that session's own dispatched subagents (no-inherit is the
   conservative default), and `machine-total` scope is unconditionally
   EM-only. See `coordinator_core/bash_guards/_blanket_disarm.py`.
3. **The `!`-prefixed prompt is a human-only surface.** There is no
   agent-invocable equivalent; it is not a tool call this suite's guards
   ever see.

Net effect: an agent cannot self-grant any of the routes above. This doc's
key table below therefore is not "how a subagent gets around a guard" — a
subagent that reads it gains nothing actionable from it — it is a reference
for the human operator deciding whether to intervene.

## Scope of the table below

This table covers `operator_override_note` call sites in
`coordinator_core/bash_guards/`, `coordinator_core/write_guards/`, and
`coordinator_core/frontmatter/schema_validate.py` — the SSOT consumers this
doc audits. Other packages define their own override keys outside this
registry's audited surface; `coordinator/bin/publish.py`'s
`COORDINATOR_OVERRIDE_DIRTY_TREE` is one such key and its absence from the
table below is this scope boundary, not an omission.

`schema_validate.py` is the third consumer, and joined for a reason rather
than by drift: `_cf_spinoff_roadmap_requires_graph` is a cross-field rule
that hard-denies, so under SC-DR-016 its override key had to be *public* —
minting it without a row here would have failed the very condition that
cleared the deny to ship. The rule imports `operator_override_note` on its
deny leg only (a function-local import), so the audited-SSOT property holds
without putting the guard helper on the hot-path import graph.

## Override keys, by guard

Generated from the actual `operator_override_note(...)` call sites across
BOTH consumers of the SSOT — `coordinator_core/bash_guards/` and
`coordinator_core/write_guards/` (grep + constant resolution, not hand-typed
from memory) — re-run that sweep if this table and the source drift.
`write_guards` imports `operator_override_note` directly from
`bash_guards._helpers` (cross-package import, matching the pre-existing
`csn_check`/`emit_kind_resolution_failure_signal` precedent) rather than
forking a second copy — see that function's own docstring for the SSOT
rationale.

| Env var | Guard(s) / check function(s) | Module |
|---|---|---|
| `COORDINATOR_OVERRIDE_NO_VERIFY` | `no-verify` (`check_no_verify`) | `bash_guards/dispatch_checks.py` |
| `COORDINATOR_ALLOW_ORPHAN` | `destructive-git-orphan` (`check_destructive_git_orphan`) | `bash_guards/dispatch_checks.py` |
| `COORDINATOR_OVERRIDE_GIT_CLEAN` | `destructive-git-clean` (`check_destructive_git_clean`) | `bash_guards/dispatch_checks.py` |
| `COORDINATOR_OVERRIDE_GIT_REVERT` | `destructive-git-revert` (`check_destructive_git_revert`) | `bash_guards/dispatch_checks.py` |
| `COORDINATOR_OVERRIDE_BLANKET_ADD` | `blanket-git-add` (`check_blanket_git_add`) | `bash_guards/dispatch_checks.py` |
| `COORDINATOR_OVERRIDE_RAW_PID_LIVENESS` | `check-raw-pid-liveness` | `bash_guards/check_raw_pid_liveness.py` |
| `COORDINATOR_OVERRIDE_ILLEGAL_FILENAME` (Bash leg) | `block-illegal-filename` | `bash_guards/block_illegal_filename.py` |
| `COORDINATOR_OVERRIDE_PATHSPEC_DIVERGENCE` | `check_staged_pathspec_divergence` | `bash_guards/commit_tripwires.py` |
| `COORDINATOR_ALLOW_CD_PREFIX` | `offer-git-c` (`check_offer_git_c`) | `bash_guards/guard_offer_git_c.py` |
| `COORDINATOR_OVERRIDE_CLAUDEMD_BUDGET` | `validate-commit` (`check_validate_commit`) | `bash_guards/dispatch_checks.py` |
| `COORDINATOR_OVERRIDE_REGISTRATION_QUAD` | `validate-commit` (`check_validate_commit`) | `bash_guards/dispatch_checks.py` |
| `COORDINATOR_DISABLE_INPROCESS_SEARCH` | `inprocess-search` | `bash_guards/guard_inprocess_search.py` |
| `COORDINATOR_PROBE_NUDGE_OFF` | `probe-spray` (`check_probe_spray`) | `bash_guards/dispatch_checks.py` |
| `COORDINATOR_ALLOW_FIND_EXEC` | `find-exec-rewrite` (`check_find_exec_rewrite`) | `bash_guards/dispatch_checks.py` |
| `COORDINATOR_ALLOW_GREP_VIA_BASH` | `grep-via-bash-rewrite` (`check_grep_via_bash_rewrite`) | `bash_guards/dispatch_checks.py` |
| `COORDINATOR_ALLOW_SED_RANGE` | `sed-range-read-advise` (`check_sed_range_read_advise`) | `bash_guards/dispatch_checks.py` |
| `COORDINATOR_ALLOW_CAT_HEREDOC` | `cat-heredoc-write-advise` (`check_cat_heredoc_write_advise`) | `bash_guards/dispatch_checks.py` |
| `COORDINATOR_ALLOW_GIT_COMMIT_BARE` | `git-commit-safe-commit-advise` (`check_git_commit_safe_commit_advise`) | `bash_guards/dispatch_checks.py` |
| `COORDINATOR_ALLOW_MULTIPROBE_BANNER` | `multiprobe-banner-rewrite` (`check_multiprobe_banner_rewrite`) | `bash_guards/dispatch_checks.py` |
| `COORDINATOR_ALLOW_HEAD_TAIL_PLUMBING` | `head-tail-plumbing-rewrite` (`check_head_tail_plumbing_rewrite`) | `bash_guards/guard_head_tail_rewrite.py` |
| `COORDINATOR_ALLOW_INVOKE_ARGV_PARAMS` | `offer-invoke-params-stdin` (`check_offer_invoke_params_stdin`) | `bash_guards/guard_offer_invoke_params_stdin.py` |
| `COORDINATOR_OVERRIDE_MULTIPROBE_BANNER` | `multiprobe-banner` (`_check_multiprobe_banner`) | `bash_guards/guard_multiprobe_banner.py` |
| `COORDINATOR_OVERRIDE_PLUMBING_AND_LOOPS` | `plumbing-and-loops` (`_check_plumbing_and_loops`) | `bash_guards/guard_plumbing_and_loops.py` |
| `COORDINATOR_OVERRIDE_GREP_VIA_BASH_GUARD` | `grep-via-bash` (advisory-only leg, `check`) | `bash_guards/guard_grep_via_bash.py` |
| `COORDINATOR_OVERRIDE_TEST_SUITE_INVOCATION` | `check-test-suite-invocation` | `bash_guards/check_test_suite_invocation.py` |
| `COORDINATOR_OVERRIDE_DEV_REPO_SENTINEL` (Bash leg) | `block-dev-repo-sentinel-removal` | `bash_guards/block_dev_repo_sentinel_removal.py` |
| `COORDINATOR_OVERRIDE_DEV_REPO_SENTINEL` (Write/Edit leg) | `block-dev-repo-sentinel-write` | `write_guards/block_dev_repo_sentinel_write.py` |
| `COORDINATOR_OVERRIDE_SUBAGENT_PLAN_BODY` (Bash leg) | `block-subagent-plan-body-write` | `bash_guards/block_subagent_plan_body_bash_write.py` |
| `COORDINATOR_OVERRIDE_COMPLETION_MONOLITH` | `block-completion-monolith-write` | `write_guards/block_completion_monolith_write.py` |
| `COORDINATOR_OVERRIDE_CONSUMED_HANDOFF_EDIT` | `block-consumed-handoff-edit` | `write_guards/block_consumed_handoff_edit.py` |
| `COORDINATOR_OVERRIDE_WIKI_MIRROR` | `block-dev-side-mirror-wiki` | `write_guards/block_dev_side_mirror_wiki.py` |
| `COORDINATOR_OVERRIDE_REVIEW_INTEGRATION_PENDING` | `block-em-hand-edit-pending-review-integration` | `write_guards/block_em_hand_edit_pending_review_integration.py` |
| `COORDINATOR_OVERRIDE_ILLEGAL_FILENAME` (Write/Edit leg) | `block-illegal-filename` | `write_guards/block_illegal_filename.py` |
| `COORDINATOR_OVERRIDE_MEMO_STATUS_HAND_EDIT` | `block-memo-status-hand-edit` | `write_guards/block_memo_status_hand_edit.py` |
| `COORDINATOR_OVERRIDE_PRIORITY_LEDGER_EDIT` | `block-priority-ledger-edit` | `write_guards/block_priority_ledger_edit.py` |
| `COORDINATOR_OVERRIDE_SUBAGENT_ARCHIVE` | `block-subagent-archive-write` | `write_guards/block_subagent_archive_write.py` |
| `COORDINATOR_OVERRIDE_SUBAGENT_PLAN_BODY` (Write/Edit leg) | `block-subagent-plan-body-write` | `write_guards/block_subagent_plan_body_write.py` |
| `COORDINATOR_OVERRIDE_TRACKER_EDIT` | `block-tracker-edit` | `write_guards/block_tracker_edit.py` |
| `COORDINATOR_OVERRIDE_CLAUDE_MD_WRITE` | `block-unauthorized-claude-md-write` | `write_guards/block_unauthorized_claude_md_write.py` |
| `COORDINATOR_OVERRIDE_CUTOVER_PHASE_HAND_EDIT` | `block-cutover-phase-hand-edit` | `write_guards/block_cutover_phase_hand_edit.py` |
| `COORDINATOR_OVERRIDE_OSS_MIRROR_MEMO_GUARD` | `block-oss-mirror-memo-delivery` | `write_guards/block_oss_mirror_memo_delivery.py` |
| `COORDINATOR_OVERRIDE_PROSE_QUEUE_CREATION` | `nudge-prose-queue-creation` | `write_guards/nudge_prose_queue_creation.py` |
| `COORDINATOR_OVERRIDE_OWN_INBOX` | `validate-frontmatter-schema` (`_own_inbox_deny_message`) | `write_guards/validate_frontmatter_schema_deny.py` |
| `COORDINATOR_OVERRIDE_ROADMAP_GRAPH_FIELDS` | roadmap-baton required-graph-fields (`_cf_spinoff_roadmap_requires_graph`) | `frontmatter/schema_validate.py` |

Not in this table, by design:

- `COORDINATOR_OVERRIDE_HOME_MEMO_GUARD`
  (`write_guards/block_home_dir_memo_delivery.py`) — deliberately
  UNADVERTISED. It is never named in that guard's deny text at all (see that
  module's own docstring for why an advertised override on that specific
  scope would turn a hard block into a speed bump for the exact caller it
  exists to stop), so it never renders through `operator_override_note` and
  has nothing to reconcile against this table.
- `COORDINATOR_OVERRIDE_MEMO_REDIRECT` — read in a code condition in both
  `validate_frontmatter_schema_deny.py` and its advisory sibling, but never
  interpolated into any rendered deny/advisory message text in either module
  — there is no `operator_override_note` call site for it to list here.
- `COORDINATOR_SCHEMA_STRICT` — an internal MODE flag, not a bypass/escape
  route. Since the 2026-08-06 guard-class census (C15,
  `validate_frontmatter_schema_deny.py`) it no longer upgrades anything to a
  hard deny — every strict-gated deny leg in that module was converted to
  advisory. What it does now: selects which of the two split modules
  (`validate_frontmatter_schema_deny.py` vs its advisory sibling
  `validate_frontmatter_schema_advisory.py`) renders the (identical) warning
  for a given violation — `_is_strict()` gates which sibling stands down
  (`if _is_strict(): return None`), not whether the write is blocked. It
  still makes the checked-for condition MORE visible, the opposite direction
  from every entry above; it does not belong in this bypass-options table and
  does not carry `operator_override_note`.
- `COORDINATOR_QUEUE_PUNT` / `COORDINATOR_BATON_BODY_PUNT` /
  `COORDINATOR_PROSE_QUEUE_APPEND_PUNT` / `COORDINATOR_NEW_SH_PUNT`
  (`write_guards/nudge_improvement_queue_write.py`,
  `write_guards/nudge_baton_body_bar.py`,
  `write_guards/nudge_prose_queue_append.py`,
  `write_guards/nudge_new_sh_file_naked_python.py`) — a different shape entirely
  (`VAR="<non-trivial reason>"`, not `VAR=1`). All four modules render via
  `operator_override_note(env_var, reason_placeholder="<one-sentence
  reason>")` (2026-07-30 P1 fix, origin of the shape; `nudge_prose_queue_
  append.py` and `nudge_new_sh_file_naked_python.py` follow the same shape),
  which prints `VAR="<one-sentence reason>" (pre-launch only)` instead of
  the helper's default flag-shaped `VAR=1`. Before this fix the helper's
  hardcoded `%s=1` render was not merely "not literally accurate" for
  `COORDINATOR_QUEUE_PUNT`/`COORDINATOR_BATON_BODY_PUNT` — it was actively
  wrong: both vars' own `_is_trivial_reason` denylists the literal string
  `"1"`, so the printed remediation `COORDINATOR_QUEUE_PUNT=1 (pre-launch
  only)` was refused by the very guard printing it. The open reachability
  question the 2026-07-30 dud-offer-remediation-sweep run-report raised for
  these two is resolved for `nudge_improvement_queue_write.py` a second way:
  as of the 2026-07-30 escape-mechanism rework it is no longer the
  advertised route at all — a queue entry carrying its own non-trivial
  `justification:` line is checked against the write payload directly and
  needs no env var, no relaunch, and no operator. `COORDINATOR_QUEUE_PUNT`,
  `COORDINATOR_BATON_BODY_PUNT`, `COORDINATOR_PROSE_QUEUE_APPEND_PUNT`, and
  `COORDINATOR_NEW_SH_PUNT` all remain honored as operator-only,
  pre-launch-only knobs (`COORDINATOR_BATON_BODY_PUNT`,
  `COORDINATOR_PROSE_QUEUE_APPEND_PUNT`, and `COORDINATOR_NEW_SH_PUNT` only
  silence an advisory on future writes, since none of those three guards
  ever blocks) — none of the four is a route a dispatched agent can take
  mid-session.

Spec backlink: `coordinator_core/bash_guards/_helpers.py::operator_override_note`
(the SSOT this doc is the reference half of), and the PM ruling (2026-07-30,
same session as H12) that moved the full bypass-options content here in
place of a per-firing inline note.
