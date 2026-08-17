# Bash-guard override keys and bypass options

A guard message names the guard that fired and nothing else about its
override — no key, no assignment form, no bypass framing. This doc is the
operator's route from a guard name to its override key: the § "Override
keys, by guard" table below is the primary lookup an operator uses, not a
backstop to something guard output already told them. See
`coordinator_core.bash_guards._helpers.operator_override_note()`'s own
docstring for the exact rendered pointer and its cut history.

2026-08-11 (PM-raised, break-class): the in-message note and `annotate_deny`'s
unlock block both used to open with `"Bypass options for a human operator,
not this agent: ..."`. That framing was itself an injection tell, not a
safeguard — the deniability-preserving register an attacker writes to make
an agent feel authorised while covering itself. Two independently-dispatched
agents (a code-reviewer, a review-integrator, no shared context) classified
it as prompt injection and declined to act. Both builders dropped the
disclaimer frame; `annotate_deny`'s unlock block now states its load-bearing
fact plainly instead, and `operator_override_note` went further still — as
of the 2026-08-11 key-removal pass it states no per-key fact at all,
pointing here instead (see this doc's contract note above). See each
builder's own docstring for the full rationale. Every route below remains a
human-only affordance in substance for a dispatched subagent — only the
framing that said so changed. <!-- Review: code-reviewer (slice 4) — P1: this
paragraph's original claim (the EM as a granting role holding a rendered
wiki pointer at deny time) never shipped; item 10 (2026-08-13) removed all
deny-time rendering for every audience, so this is now historical context,
not a current carve-out. --> A PM ruling on 2026-08-13 proposed reversing
this for the EM specifically — the EM would be routed to a wiki pointer
describing these locks, on an agent's behalf, mid-session, and hold that
pointer without handing it to the subagent — but that narrower render
tripped `message_register._rules` rule B8 leg (d), and item 10
(2026-08-13) removed deny-time rendering entirely, for every audience,
rather than carve out an exception. See § "Human-only affordances"
immediately below and § "Exact deny-message wording" for what actually
shipped.

## Human-only affordances — an in-session agent cannot exercise these

None of the routes below are actions a dispatched agent can take — they are
what a human operator, watching the session, can do. <!-- Review:
code-reviewer (slice 4) — P1: dropped the "EM as granting role for a
rendered wiki pointer" claim; no deny-time render exists for any audience,
EM included, post-item-10. --> The PM's 2026-08-13 ruling proposed a narrow
exception for the EM — being routed to the wiki pointer describing these
locks (never the key, path, or command — see AC-10), on a dispatched
agent's behalf mid-session — but that narrower render never shipped: it
tripped `message_register._rules` rule B8 leg (d), and item 10
(2026-08-13) removed deny-time rendering entirely, for every audience,
instead of carving out an EM-only exception. See § "Exact deny-message
wording" below. Every route below otherwise remains exactly what it says: a
human operator, not any agent (EM or subagent), still has to be the one who
actually creates a marker, sets a pre-launch var, or types the `!`-prefixed
prompt.

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
3. **`<ENV_VAR>=1`, set before the harness/hook process launches.**
   Guard-specific — each guard reads its own var. Not named inline in guard
   output; look the guard up by name in the § "Override keys, by guard"
   table below to find its key. **Pre-launch only — unsettable from inside a
   live session.** The var is read once at hook-process spawn; nothing
   reachable from inside a running session sets a variable this guard
   process will see (see Security context, leg 1).
4. **The in-session guard-unlock sentinel**, a one-shot file the operator
   drops for the exact `(session_id, guard_name)` pair that just denied —
   see § In-session unlock below. Unlike routes 1–3, this one is reachable
   from *inside* the session that hit the deny: no relaunch, no waiting for
   a future session to inherit a pre-launch var. It is additive, not a
   replacement — the env-var route above stays fully supported.
   **Reversed for the EM specifically, on a small named allowlist (PM
   ruling, 2026-08-13; `DR-298`, superseding `DR-260`'s human-only framing
   for route 4 only — DR-260's forgery-leg reasoning is unchanged): the EM
   may itself mint this sentinel, without a PM utterance at the moment of
   use, for the guards in `_GRANTABLE_GUARDS` — currently
   `bump-foreign-repo-write` and `bump-outside-repo-write`.** No other guard
   is EM-grantable; every guard outside that allowlist keeps this route
   exactly as human-only as stated above, and a dispatched subagent remains
   barred from acquiring any grant regardless of guard. This is not a
   re-litigation of AC4 above — which guards a human operator may reach via
   this route stays universal and unexempted — it is a separate, narrower
   axis: which guards the EM may self-grant. See `DR-298` for the full
   record, including the failure-mode discriminator that keeps
   IRREVERSIBLE-tier guards off this allowlist at any scope.

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
<tempdir>/coordinator-guard-unlock-<session_id>.<guard_name>
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

**Exact deny-message wording — corrected 2026-08-13, break-class.** This
section previously documented a rendered unlock block, including a passage
instructing future editors not to collapse an inlined filename-shape/
identifier rendering back out. **That rendering no longer exists at all,
and the "do not collapse" instruction is now false** — verified against
`coordinator_core/session/guard_unlock_sentinel.py::annotate_deny`, whose
body is `return out` unmodified on every call (item 10, 2026-08-13,
staff-eng review). Neither engine seam (`write_guards/engine.py`,
`bash_guards/dispatch.py`) appends anything to `permissionDecisionReason`
for the in-session unlock any more; a hard-deny's message is exactly the
firing guard's own reason, with no unlock-block suffix of any kind.

The history, briefly, so a reader is not left guessing why the shape moved
this much: item 3 (2026-08-11, PM ruling) first stripped a resolved
sentinel path and create-then-retry recipe out of an appended block,
leaving a human-only-affordance sentence plus doc/wiki pointers and the
bare `session_id`/`guard_name` values as data. Item 6 (2026-08-12)
regressed that — re-inlining the filename shape, drop-location
description, and both identifiers as live parameters — which is the state
this section used to document, including its "future edit must not
collapse this back" instruction. **Item 7 (2026-08-13, C3) reverted item
6**, restoring the doc/wiki-pointers-only form. **Item 9 (2026-08-13, C4d)
went further still**: even that pointers-only sentence tripped
`message_register._rules` rule B8 leg (d) — B8 treats any pointer into the
override-key/unlock doc surface as a gate-referent, and no narrower
rendered form between "the full disclosure paragraph" and "nothing" grades
clean. `annotate_deny` was changed to return its input unmodified on every
call. Item 10 then removed the ~20 lines of identity-resolution logic item
9 had kept live-but-unbranching as speculative insurance, since running a
resolver for zero effect on every deny made AC-6-style "is this seam
reachable" lints vacuous.

**What this means for an operator today.** A hard-deny's message names the
guard and the reason it fired, and nothing about the unlock channel at
all — not a resolved path, not a filename shape, not even the fact that an
in-session unlock exists. This doc (and, for the EM specifically on the
allowlisted route-4 guards, `_GRANTABLE_GUARDS`) is the sole discoverability
surface for the mechanism described in this section; nothing in a deny
message points here any more. `guard_unlock_sentinel.py`'s own docstring
carries a companion negative-spec: "Do NOT re-inline the sentinel's
filename shape, its drop location, or the per-firing identifiers into any
rendered deny/advisory text — this was tried (item 6) and reverted (item
7)." A future edit reintroducing any rendered form must re-check it
against `message_register._rules` B8 first, since that is what foreclosed
the narrower form in item 9, not a stylistic preference.

`operator_override_note`'s own per-guard pointer (the pre-launch env-var
key table's rendering) is a **separate** mechanism from the in-session
unlock block described above, and is unaffected by items 7-10: it still
renders its one-line pointer to this doc on the routes that use it. Do not
conflate the two — this section's history is about the in-session
guard-unlock sentinel's block only.

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
is defecting, not clever — the same way the DoE executor that noticed
`block_dev_repo_sentinel_write` had no `Bash` matcher, and declined to route
around it anyway, was not being naive. That restraint is the behaviour this
whole design assumes and depends on.

## Cross-repo write markers — same filename, two different repos

Writing into a sibling repo is gated on both the Bash and the Write/Edit surface,
and each surface reads a marker of the same name from a **different** repo. That
is the detail a reader gets wrong, so it is stated here rather than left to be
inferred from a deny message with no room to explain it.

| Surface | Marker read from | Meaning |
|---|---|---|
| Bash (`git -C <peer> ...`, any shell write) | the **target** repo's `.git/allow-xrepo-write-<session-id>` | clears writes into that one peer, for that one session |
| Write/Edit/MultiEdit/NotebookEdit | the **session's own** repo's `.git/allow-xrepo-write-<session-id>` | clears cross-repo writes for that session, whatever the target |

Both `allow-xrepo-write-<session-id>` markers are per-session, and **there is no
creation guard for either siting today.** Grep finds no creation-guard or write-guard
registration for `MARKER_PREFIX`/`allow-xrepo-write-` on either surface;
`_write_bump_marker`'s own docstring states this is deliberate ("NO CREATION GUARD, NO
PAIRED WRITE GUARD, NO IDENTITY GATING"). The marker is an ordinary file, agent-creatable
by construction, held by norm rather than mechanism — see `state/bug-backlog/2026-08-10-nothing-stops-an-agent-creating-its-own-5631418073ca.yaml`
for the filed defect. Do not read this as a protection in force. The deny message that
fires names the exact path to create; prefer that over reconstructing one from this
table, since it resolves the session id for you.

Distinct from both, and not interchangeable with either:
`.coordinator-doctrine-edit-approved` is read at the **hook process's own cwd repo
root** — `guard_doctrine_surface_edits._sentinel_state()` is fed by `_git_root()`,
which calls `resolve_repo_root()` with no argument, and that resolver's own docstring
resolves against the process's current cwd. It is not sited "at a target repo's root";
it is same-repo-only relative to wherever the hook process is running, and the
protected-path set (`_protected_paths()`) is composed from that same cwd-resolved root.
It is PM-created, time-boxed, and covers a multi-edit change inside its window. It does
**not** clear a cross-repo write, and it is orthogonal to — never additive with — the
`allow-xrepo-write-*` markers above: because the protected set is composed from the
session's own root, this guard does not even fire on a peer repo's
`coordinator.local.md`, so there is no scenario in which a session needs both markers
together.

An unanchored session — one with no SessionStart record — fails **open** on both
surfaces, identically. That is deliberate, not a hole: it is the state a hand-constructed
payload is in, never a live session, and failing closed there would wedge every write on a
resolution failure.

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

Net effect, bounded rather than unqualified as of 2026-08-13: an agent
cannot self-grant any of routes 1-3, and cannot self-grant route 4 either
**except** that the EM specifically may self-grant route 4's sentinel for
the guards named in `_GRANTABLE_GUARDS` (see route 4's own entry above and
`DR-298`) — a dispatched subagent still cannot, for any guard, on any
route. This doc's key table below therefore is not "how a subagent gets
around a guard" — a subagent that reads it gains nothing actionable from
it — it is a reference for the human operator, and, for the allowlisted
route-4 guards only, the EM, deciding whether to intervene.

## Scope of the table below

This doc names every `COORDINATOR_OVERRIDE_*` key that exists in non-test
code across the repo — not only the `operator_override_note` call sites in
`coordinator_core/bash_guards/` and `coordinator_core/write_guards/`. The
first table below (Bash-guard / write-guard suite, plus the
`schema_validate.py` cross-field rule) is generated from those SSOT call
sites, matching the original scope of this doc. The second table
(`## Override keys outside the bash-guard/write-guard suite`) covers every
other `COORDINATOR_OVERRIDE_*` key found by the same sweep — precommit-hook
installers, publish/percolate gates, and ops/ceremony checks — each with its
own reading, since none of those route through `operator_override_note`.
`coordinator/bin/publish.py`'s `COORDINATOR_OVERRIDE_DIRTY_TREE` and the
pre-commit-gate-registry overrides named just below remain intentionally
excluded from BOTH tables, for the reasons stated in this section.

Also out of this scope by the same boundary: `coordinator_core.ops.detect_staged_rollback`'s
`COORDINATOR_OVERRIDE_PRECOMMIT_STAGED_ROLLBACK` (pre-existing) and
`COORDINATOR_OVERRIDE_PRECOMMIT_MASS_DELETION` (2026-08-10) — both are
pre-commit-gate-registry overrides (`coordinator_core.ops.install_claude_klabauter_precommit_hook
._GATE_REGISTRY`), the same class as `publish.py`'s `COORDINATOR_OVERRIDE_DIRTY_TREE`
above, self-documented in `detect_staged_rollback.py`'s own module docstring and
`--help` output rather than routed through `operator_override_note`.

`schema_validate.py` is the third consumer, and joined for a reason rather
than by drift: `_cf_spinoff_roadmap_requires_graph` is a cross-field rule
that hard-denies, so under SC-DR-016 its override key had to be *public* —
minting it without a row here would have failed the very condition that
cleared the deny to ship. As of `b1e1119f9` (ruling D5,
`tasks/guard-messages-keys/DECISIONS.md`) the rule's deny leg no longer calls
`operator_override_note` at all — the key is public and registered in this
table, but its name is not surfaced in the deny text itself.

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
| `COORDINATOR_ALLOW_GIT_COMMIT_AMEND` | `git-commit-safe-commit-advise` amend-ownership gate (`check_git_commit_safe_commit_advise`) | `bash_guards/dispatch_checks.py` |
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
| `COORDINATOR_OVERRIDE_CONFINED_AGENT_WRITE` | `block-confined-agent-write` | `write_guards/block_confined_agent_write.py` |
| `COORDINATOR_OVERRIDE_CONSUMED_HANDOFF_EDIT` | `block-consumed-handoff-edit` | `write_guards/block_consumed_handoff_edit.py` |
| `COORDINATOR_OVERRIDE_WIKI_MIRROR` | `block-dev-side-mirror-wiki` | `write_guards/block_dev_side_mirror_wiki.py` |
| `COORDINATOR_OVERRIDE_REVIEW_INTEGRATION_PENDING` | `block-em-hand-edit-pending-review-integration` | `write_guards/block_em_hand_edit_pending_review_integration.py` |
| `COORDINATOR_OVERRIDE_ILLEGAL_FILENAME` (Write/Edit leg) | `block-illegal-filename` | `write_guards/block_illegal_filename.py` |
| `COORDINATOR_OVERRIDE_MEMO_STATUS_HAND_EDIT` | `block-memo-status-hand-edit` | `write_guards/block_memo_status_hand_edit.py` |
| `COORDINATOR_OVERRIDE_PRIORITY_LEDGER_EDIT` | `block-priority-ledger-edit` | `write_guards/block_priority_ledger_edit.py` |
| `COORDINATOR_OVERRIDE_SUBAGENT_ARCHIVE` | `block-subagent-archive-write` | `write_guards/block_subagent_archive_write.py` |
| `COORDINATOR_OVERRIDE_SUBAGENT_PLAN_BODY` (Write/Edit leg) | `block-subagent-plan-body-write` | `write_guards/block_subagent_plan_body_write.py` |
| `COORDINATOR_OVERRIDE_CLAUDE_MD_WRITE` | `block-unauthorized-claude-md-write` | `write_guards/block_unauthorized_claude_md_write.py` |
| `COORDINATOR_OVERRIDE_CUTOVER_PHASE_HAND_EDIT` | `block-cutover-phase-hand-edit` | `write_guards/block_cutover_phase_hand_edit.py` |
| `COORDINATOR_OVERRIDE_OSS_MIRROR_MEMO_GUARD` | `block-oss-mirror-memo-delivery` | `write_guards/block_oss_mirror_memo_delivery.py` |
| `COORDINATOR_OVERRIDE_PROSE_QUEUE_CREATION` | `nudge-prose-queue-creation` | `write_guards/nudge_prose_queue_creation.py` |
| `COORDINATOR_OVERRIDE_OWN_INBOX` | `validate-frontmatter-schema` (`_own_inbox_deny_message`) | `write_guards/validate_frontmatter_schema_deny.py` |
| `COORDINATOR_OVERRIDE_ROADMAP_GRAPH_FIELDS` | roadmap-baton required-graph-fields (`_cf_spinoff_roadmap_requires_graph`) | `frontmatter/schema_validate.py` |
| `COORDINATOR_OVERRIDE_AGENT_MODEL_PIN` | `enforce-agent-model-pin` (`check`, composed into `PreToolUse(Agent)` via `block_unenumerated_agent_type.check()`) | `hooks/enforce_agent_model_pin.py` |

## Override keys outside the bash-guard/write-guard suite

Found by the same `COORDINATOR_OVERRIDE_*` sweep as the table above, but
none of these route through `operator_override_note` — each reads its own
env var directly, with its own message shape. Rostered here per the same
"an operator cannot reach an override they cannot discover" rationale as
the table above, including entries that are internal plumbing rather than
operator-facing (marked as such).

| Env var | Guard(s) / check function(s) | Module | What it relaxes |
|---|---|---|---|
| `COORDINATOR_OVERRIDE_PRECOMMIT_EXEC_BIT` | exec-bit gate CANNOT-RUN wrapper | `ops/install_meta_repo_precommit_hook.py` (also read in the generated hook body from `ops/install_publish_repo_precommit_hook.py`) | Skips the exec-bit pre-commit gate when its script/interpreter can't run, instead of blocking the commit |
| `COORDINATOR_OVERRIDE_PRECOMMIT_ILLEGAL_PATHS` | illegal-path gate CANNOT-RUN wrapper | `ops/install_meta_repo_precommit_hook.py` (also `ops/install_publish_repo_precommit_hook.py`) | Skips the illegal-path pre-commit gate when its script/interpreter can't run |
| `COORDINATOR_OVERRIDE_PRECOMMIT_PLATFORM_PATHS` | foreign-platform-path gate CANNOT-RUN wrapper | `ops/install_meta_repo_precommit_hook.py` | Skips the foreign-platform-path pre-commit gate when its script/interpreter can't run |
| `COORDINATOR_OVERRIDE_PRECOMMIT_SETTINGS_TRACKING` | settings-tracking gate CANNOT-RUN wrapper | `ops/install_meta_repo_precommit_hook.py` | Skips the settings-tracking pre-commit gate when its script/interpreter can't run |
| `COORDINATOR_OVERRIDE_PRECOMMIT_BASH_MISSING` | bash-kind gate group CANNOT-RUN wrapper | `ops/install_meta_repo_precommit_hook.py` (also `ops/install_publish_repo_precommit_hook.py`) | Lets the pre-commit hook proceed when no `bash` interpreter is found on PATH, instead of blocking the commit |
| `COORDINATOR_OVERRIDE_POSTSYNC_MARKER_RESYNC` | marker-resync gate CANNOT-RUN wrapper | `ops/install_meta_repo_precommit_hook.py` | Skips the postsync-marker-resync pre-commit gate when its script/interpreter can't run |
| `COORDINATOR_OVERRIDE_PRECOMMIT_NODE_MISSING` | exec-bit gate (publish-repo hook variant) | `ops/install_publish_repo_precommit_hook.py` | Lets the exec-bit gate be skipped when no `node` interpreter is found on PATH, instead of blocking the commit |
| `COORDINATOR_OVERRIDE_PUBLISH_DEST_HOME` | `check_live_install_clobber` | `coordinator/bin/publish.py` | Allows publishing to a DEST at/under the live-install root, normally banned by the 2026-05-20 clobber doctrine |
| `COORDINATOR_OVERRIDE_VERSION_REGRESSION` | marketplace version-regression gate | `coordinator/bin/publish.py` | Allows a publish that would downgrade `marketplace.json`'s version (source version < target version) |
| `COORDINATOR_OVERRIDE_VERSION_CONSISTENCY` | version-consistency gate | `coordinator/bin/publish.py` | Lets publish proceed, unchecked, when the version-consistency gate script can't be found on disk |
| `COORDINATOR_OVERRIDE_EMPTY_SOURCE_PRUNE` | empty-source-directory prune preflight | `coordinator/lib/percolate/publish_sync.py` | Confirms an empty-source directory prune is deliberate (accepts `1` for blanket allow, or a comma-separated directory-name list for a scoped allow) instead of aborting on a suspected accidental full wipe |
| `COORDINATOR_OVERRIDE_ORPHAN_SWEEP` | orphan top-level-directory sweep preflight | `coordinator/lib/percolate/publish_sync.py` | Confirms a deliberate removal of orphaned top-level publish-target directories instead of aborting |
| `COORDINATOR_OVERRIDE_COVERAGE_GATE` | REMOVED (state/kill-ledger.md K-005, 2026-08-16) | n/a | Was the `coverage-gate` warn-and-continue fence; the whole `coverage.gate` op/subcommand it read is deleted |
| `COORDINATOR_OVERRIDE_BRIGHTLINE` | REMOVED (state/kill-ledger.md K-004, 2026-08-16, Verdict A) | n/a | Was the tier=A declared-but-unwalked-repo hard-stop bypass; the tier branch it read is deleted (measured tier=A zero across 151 records) |
| `COORDINATOR_OVERRIDE_BRANCH` | branch-mutation force path | `coordinator_core/consolidate_assemble/apply.py` (`force = os.environ.get(...) == name`); also set (not read) into a subprocess environment by several ops scripts (`ops/workday_start_step0_reconcile.py`, `coordinator/bin/merge-recovery-and-tag-cut.py`, `coordinator/lib/session_ensure_branch.py`, `coordinator/bin/workday-start-step0.py`, `ops/merge_branch_into_workstream.py`, `ops/migrate_branch_canonical_case.py`, `ops/agent_worktree_sweep.py`) whose ultimate reader was not found in this repo's non-test Python — likely a non-Python (git-hook) consumer | In `consolidate_assemble/apply.py`, forces a branch mutation when the var's value names that exact branch. **Negative-spec note:** `bash_guards/block_noncanonical_branch_creation.py` and `bash_guards/guard_longlived_branch_naming.py` explicitly do NOT read this var (PM ruling R4) — do not assume it bypasses those two guards |
| `COORDINATOR_OVERRIDE_BRANCH_REASON` | paired with `COORDINATOR_OVERRIDE_BRANCH` | same set of ops/lib scripts as `COORDINATOR_OVERRIDE_BRANCH` above | Internal plumbing, not independently operator-facing: carries a free-text reason string alongside `COORDINATOR_OVERRIDE_BRANCH`; no `os.environ.get` read site for it was found in this repo's non-test Python, so its consumer is presumed to be a non-Python (git-hook) surface outside this doc's audited scope |
| `COORDINATOR_OVERRIDE_REVERSE_DRIFT` | reverse-drift gate | `coordinator_core/ops/workweek_reverse_drift_gate.py` (also `coordinator/bin/workweek-complete-reverse-drift-gate.py`) | Downgrades a reverse-drift gate failure (misconfigured reader, reader error, or a failed `reverse_drift_cmd`) from a blocking exit-1 to a reported-but-non-blocking warning |
| `COORDINATOR_OVERRIDE_LEGACY_MONOLITH` | legacy-monolith-completion-append tripwire | `coordinator_core/ops/check_no_monolith_completion_append.py` | Exempts a matched line from the no-monolith-append tripwire scan — the literal string is an in-file exemption marker, not an env var the check reads at runtime |
| `COORDINATOR_OVERRIDE_MEMO_ACTION_CLAIM` | memo action-claim liveness guard | `coordinator_core/archive_stamp.py` | Lets an agent action a memo held by a DIFFERENT live session's claim, instead of refusing |
| `COORDINATOR_OVERRIDE_MEMO_ADDRESSEE` | memo addressee gate | `coordinator_core/pickup_assemble/__init__.py` | Cross-seat override: proceeds past an addressee-gate mismatch (memo's `to:` doesn't match the resolved addressee) instead of refusing |
| `COORDINATOR_OVERRIDE_GOALS_LOG_WRITE` | `block-goals-log-hand-write` | `coordinator_core/write_guards/block_goals_log_hand_write.py` | Recovery-only escape hatch permitting a hand-edit of a `goals-log.<machine>.jsonl` file that this guard otherwise blocks |
| `COORDINATOR_OVERRIDE_DERIVED_GLOBAL_DOCTRINE_WRITE` | `block-derived-global-doctrine-write` | `coordinator_core/write_guards/block_derived_global_doctrine_write.py` | Rare-use escape hatch permitting a direct Write/Edit/MultiEdit/NotebookEdit to a derived global-doctrine surface that this guard otherwise redirects to the authoring surface |
| `COORDINATOR_OVERRIDE_SUBAGENT_GRANT_RECORD_WRITE` | `block-subagent-grant-record-write` | `coordinator_core/write_guards/block_subagent_grant_record_write.py` | Rare-use escape hatch permitting a direct Write/Edit/MultiEdit/NotebookEdit to a subagent grant-record file that this guard otherwise blocks |
| `COORDINATOR_OVERRIDE_POWERSHELL_VIA_BASH_GUARD` | `powershell-via-bash` | `coordinator_core/bash_guards/guard_powershell_via_bash.py` | Bypasses the guard entirely for a Bash call invoking a `powershell`/`pwsh` binary |
| `COORDINATOR_OVERRIDE_FSIZE_CAP` | shell-init `ulimit -f` cap emitter | `bin/shell-init-guard.py` | Not a guard bypass but a sizing knob: sets the shell's file-size ulimit cap (GiB, or `unlimited`) instead of the 8 GiB default — DR-047 cross-repo seam with DoE's `~/.bashrc` |

Excluded from the table above (found by the sweep, deliberately not rostered):

- `COORDINATOR_OVERRIDE_CARRY_GATE` — named only in a historical docstring in
  `coordinator_core/ops/handoff_carry_gate.py` ("the `COORDINATOR_OVERRIDE_CARRY_GATE`
  escape hatch that existed only to override that refusal are all gone," PM
  ruling 2026-08-06). No `os.environ` read site for it exists anywhere in
  this repo's non-test Python — it is dead, not merely undocumented.
- `COORDINATOR_OVERRIDE_PRECOMMIT_STAGED_ROLLBACK` and
  `COORDINATOR_OVERRIDE_PRECOMMIT_MASS_DELETION` — already covered by the
  "Also out of this scope" paragraph above (pre-commit-gate-registry
  overrides, self-documented via `--help`, not routed through
  `operator_override_note`).
- `COORDINATOR_OVERRIDE_DIRTY_TREE` — already covered by the same paragraph.

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
  (a key that takes a one-sentence reason, not a bare flag). All four
  modules call `operator_override_note(env_var,
  reason_placeholder="<one-sentence reason>")` (2026-07-30 P1 fix, origin of
  the shape; `nudge_prose_queue_append.py` and
  `nudge_new_sh_file_naked_python.py` follow the same shape) to mark
  themselves reason-shaped rather than flag-shaped. That distinction still
  exists in the call-site argument and in `operator_override_note`'s own
  branching — it simply has no reader in guard output any more, since the
  2026-08-11 key-removal pass stopped rendering the key (or its
  parenthetical shape) inline at all; the guard message points to this doc
  instead (see `operator_override_note`'s own docstring for the current
  render) — the four key names above are how an operator finds them, since
  none of the four appears in either table (this bullet is why). Before the
  2026-07-30 fix, the helper's hardcoded `%s=1` render was not merely "not
  literally accurate" for `COORDINATOR_QUEUE_PUNT`/`COORDINATOR_BATON_BODY_
  PUNT` — it was actively wrong: both vars' own `_is_trivial_reason`
  denylists the literal string `"1"`, so the printed remediation would have
  been refused by the very guard printing it. The open reachability
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

## Hyphenated prompt-marker overrides — a different kind of artifact

The two tables above are `COORDINATOR_OVERRIDE_*`/`COORDINATOR_ALLOW_*`/
`COORDINATOR_DISABLE_*` **environment variables**, read at hook-process
spawn, `_FAMILY_RE`-matched by `coordinator_core.message_register._override_keys`
(underscore-only pattern:
`COORDINATOR_(?:OVERRIDE|ALLOW|DISABLE)_[A-Z0-9_]+`). The table below is a
different kind of artifact entirely: **hyphenated prompt markers and
sentinel filenames**, read from a dispatch prompt's own text or from an
agent-writable file on disk, not from `os.environ`. Because they are
hyphenated they fall outside `_FAMILY_RE` and are structurally invisible to
registry-derived enumeration — this table is what makes the family
doc-known so it can be enumerated at all, and **this table IS the registry
for the hyphenated family**: adding a marker to a guard without adding a row
here is the defect the register lint (B8/AC-5) catches.

| Marker | Guard | What it does |
|---|---|---|
| `COORDINATOR-OVERRIDE-UNENUMERATED-AGENT-TYPE` | `hooks/block_unenumerated_agent_type.py` | A `COORDINATOR-OVERRIDE-UNENUMERATED-AGENT-TYPE: <reason>` line, own line, non-empty reason, read off the dispatch prompt (`tool_input`, never `dispatched-agents.txt`) — short-circuits the unenumerated-`subagent_type` hard deny before the roster lookup runs. Not disclosed in the rendered deny text (2026-08-13): this marker is agent-writable bypass text the denied dispatcher pastes into its own next prompt, so announcing it in the deny message would hand the bypass to the agent in the same breath the guard refused it. |
| `COORDINATOR-OVERRIDE-DISPATCH-SUITE-GUARD` | `coordinator/hooks/scripts/block-dispatch-suite-invocation.py` (DoE-claude, peer repo — not present under `coordinator_core/` in this repo) | Same pattern, named in `block_unenumerated_agent_type.py`'s own docstring as the precedent this module's marker reuses ("no new pattern per PM constraint"): a `COORDINATOR-OVERRIDE-DISPATCH-SUITE-GUARD: <reason>` line short-circuits that guard's own dispatch-suite-invocation deny. |
| `.COORDINATOR-OVERRIDE-WORKTREE-GUARD` | `coordinator_core/write_guards/block_worktree_sentinel_write.py` (case-folded basename match; also referenced by `coordinator_core/bash_guards/block_worktree_sentinel_creation.py`'s sibling Bash-leg guard) | An ordinary dotfile sentinel — `_SENTINEL_NAME = ".coordinator-override-worktree-guard"` — whose presence (case-insensitively matched by basename, Windows-safe) short-circuits the guard that otherwise blocks a Write/Edit into the worktree-sentinel path. Same `XREPO_MARKER_IS_ORDINARY_FILE` posture as the cross-repo write markers above: an ordinary file, no identity gating, no expiry — this table adds it to the registry, it does not harden the mechanism. |

Members are resolved by grepping `coordinator_core/` for the literal marker
text, not guessed from a guard's name — see this table's own spec backlink
below for the sweep that produced it. A member whose guard does not exist in
this tree yet (or has moved) is a defect in this table, not a defect to
paper over silently.

Spec backlink: `pln-guard-messages-stop-handing-ag-549b61`
§ C9 (this table is C9's precondition of C6/AC-5 — C6 widens
`message_register._override_keys._FAMILY_RE` to match the hyphenated shape
and enumerates FROM this table, not from a hand-kept list).

## Dotfile state-marker sentinels — a third artifact shape

Distinct from both tables above: not an `os.environ` key, and not a
hyphenated `COORDINATOR-OVERRIDE-*`/`.COORDINATOR-OVERRIDE-*` bypass token
read off a dispatch prompt or matched by `_FAMILY_RE`. These are lowercase,
hyphen-separated **dotfile sentinels on disk** whose mere presence (not
content, for the two that don't parse one) flips machine- or repo-wide
guard/hook behavior. `coordinator_core.message_register._marker_filenames.
real_marker_filenames()` scans for exactly this shape (a module-level
`SENTINEL`/`MARKER` constant whose value matches the dotfile/hyphenated-
marker pattern) and `doc_registered_gap()` reported these three present in
code but absent from this doc (chunk C6, live finding) — closed here rather
than left invisible to registry-derived enumeration, the same B8/AC-5
property the hyphenated table above exists for. They get their own table,
not a row in the hyphenated table above, because that table's own heading
scopes it to the uppercase `COORDINATOR-OVERRIDE-*` family specifically
(`_FAMILY_RE`-adjacent); these three fall outside that family's shape
entirely, not merely outside its case-folding.

Per `XREPO_MARKER_IS_ORDINARY_FILE`: every marker below is an ordinary file,
bare `touch`-able, with no identity gating and no expiry mechanism (except
where its own guard's docstring, linked below, documents an expiry the
marker's *content* — not this table — enforces). This table adds them to the
registry; it does not harden any of the three mechanisms.

| Marker | Owning guard/module | What it does |
|---|---|---|
| `.coordinator-bash-guards-disarmed` | `coordinator_core/bash_guards/_blanket_disarm.py` (`MARKER_BASENAME`); creation protected by `coordinator_core/bash_guards/block_disarm_marker_sentinel_creation.py` | The blanket-disarm marker itself — see § "Human-only affordances" route 2 above for its full `Scope:`/`Bands:` content contract. Machine-scoped (resolved under the settings home, not a repo root), and this is the one basename in this table already described narratively elsewhere in this doc; it is rostered here by literal basename so registry-derived enumeration finds it. |
| `.coordinator-dev-repo` | `coordinator_core/bash_guards/block_dev_repo_sentinel_removal.py` (Bash-leg, advisory since the 2026-08-06 guard-class census) and `coordinator_core/write_guards/block_dev_repo_sentinel_write.py` (Write/Edit leg); consumed by `coordinator_core/claude_md_budget.py` (`DEV_REPO_SENTINEL`) and `coordinator_core/resolve_coordinator_clone.py`; written at install time by `coordinator_core/install/maximalist.py` | Repo-root discriminant: its mere presence tells the dev doctrine repo apart from an OSS install. Not a bypass token — removing or relocating it silently breaks that discriminant fleet-wide, which is why the two guards above exist. |
| `.coordinator-hooks-disabled` | `coordinator_core/ops/session/guard_settings_integrity.py` (`_KILL_SWITCH_MARKER_NAME`, owns the marker's own `Since:`/`Expires:`/`Reason:` content format); consumed by `coordinator_core/write_guards/guard_settings_json_write.py` (`_HOOKS_DISABLED_MARKER`) and detected by `coordinator_core/ops/doctor.py` | A machine-wide kill-switch: its presence (armed, per its own parsed content) suppresses hook-delivered guard enforcement outright — the widest-blast-radius marker in this table, which is why `guard_settings_integrity.py` parses and validates its content rather than treating bare presence as sufficient. |
