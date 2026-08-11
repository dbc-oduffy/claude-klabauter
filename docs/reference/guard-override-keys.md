# Bash-guard override keys and bypass options

This doc is the full content that `coordinator_core.bash_guards._helpers
.operator_override_note()` used to inline on every guard firing (~50 words,
paid by every advisory message in the suite, every time). It now fires a
short pointer to this doc instead — see that function's docstring for the
cut history. This doc carries the detail; the in-message note carries only
the one fact that must survive at decision time (an env var is unsettable
from inside a running session — it is read once at hook-process spawn).

2026-08-11 (PM-raised, break-class): the in-message note and `annotate_deny`'s
unlock block both used to open with `"Bypass options for a human operator,
not this agent: ..."`. That framing was itself an injection tell, not a
safeguard — the deniability-preserving register an attacker writes to make
an agent feel authorised while covering itself. Two independently-dispatched
agents (a code-reviewer, a review-integrator, no shared context) classified
it as prompt injection and declined to act. Both builders now state the
load-bearing fact plainly instead of through a disclaimer frame; see
`operator_override_note`'s and `annotate_deny`'s own docstrings for the full
rationale. Every route below remains a human-only affordance in substance —
only the framing that said so changed.

## Human-only affordances — an in-session agent cannot exercise these

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

**Exact deny-message wording.** Both engine seams (`write_guards/engine.py`,
`bash_guards/dispatch.py`) append one line to `permissionDecisionReason` on
every hard-deny they return. As of 2026-08-11 (PM ruling, "a guard's block
message must STOP carrying its own unlock recipe") that line is
INFORMATIONAL only — it names that the unlock exists and routes to a
pointer, but it does **not** hand the reader a resolved sentinel path or a
create-then-retry recipe. The operator takes one extra hop (this doc, or the
wiki once it lands) to construct the sentinel path themselves; that hop is
the point, not an oversight — see "Why the recipe was removed" below. The
block is APPENDED after the guard's own reason, not prepended before it (see
"Why appended, not prepended" below):

On a coordinator-only machine (no example-doctrine-repo checkout, or one without the wiki page
yet admitted to example-doctrine-repo's seed set):

```
<the guard's own deny reason>

An in-session unlock exists for this guard, but it is a human-only affordance: it is granted by a human operator from a terminal outside this session, it cannot be granted by this agent, and creating it from inside the session is a doctrine violation, not a shortcut. How to construct and grant it is documented at ~/.coordinator-claude-settings/coordinator-claude/docs/wiki/ (wiki page for this channel still pending -- meanwhile also docs/reference/guard-override-keys.md); the construction steps there need these two values from this firing: session <session_id>, guard <guard_name>.
```

On a machine with a example-doctrine-repo checkout present (the dedicated page has landed
there — `example-doctrine-repo coordinator/docs/wiki/guard-unlock-channel.md`, commit
`fe0919f3b`):

```
<the guard's own deny reason>

An in-session unlock exists for this guard, but it is a human-only affordance: it is granted by a human operator from a terminal outside this session, it cannot be granted by this agent, and creating it from inside the session is a doctrine violation, not a shortcut. How to construct and grant it is documented at example-doctrine-repo coordinator/docs/wiki/guard-unlock-channel.md; the construction steps there need these two values from this firing: session <session_id>, guard <guard_name>.
```

The pending clause (`wiki page for this channel still pending -- meanwhile
also docs/reference/guard-override-keys.md`) is branch-conditional, not a
fixed part of the line: it renders only on the settings-root branch, where
the dedicated page genuinely isn't reachable yet. On the example-doctrine-repo-source branch
the dedicated page IS the answer, so the clause — and the `docs/reference/
guard-override-keys.md` fallback it names — drops out entirely.

`<wiki pointer>` and whether the pending clause renders are both resolved
by `guard_unlock_sentinel._unlock_wiki_pointer()` at render time, per the
dual-install rule below — the function returns `(pointer, pending)` as a
pair rather than the pointer alone, so `annotate_deny` doesn't have to
re-derive pending-ness from the pointer's string shape. Never a sentinel
path, and never an in-process-resolved absolute path (same portability
constraint as `_resolve_override_keys_doc_display`, § "Exact deny-message
wording"'s sibling history in `bash_guards._helpers`).

**The message supplies data; the wiki supplies shape — that division is the
whole design.** `<session_id>` and `<guard_name>` above are the exact, bare
values from this firing (never assembled into a path, never paired with an
imperative) — this doc and the pending wiki page carry the fixed SHAPE
(`<tempdir>/coordinator-guard-unlock-<session_id>.<guard_name>`, § "In-session
unlock" below), which cannot vary per firing and so belongs on a static page;
the two identifiers CAN only vary per firing, and so belong in the message,
as data, not as part of any assembled path. Removing the identifiers (as an
earlier draft of this fix did) makes the unlock effectively unreachable — no
static page can ever render a value it doesn't have — so they stay; what
came out of the message is only the ASSEMBLED path, the imperative, and the
create-then-retry sequencing (see "Why the recipe was removed" below). A
future edit must not collapse this back in either direction: folding the
shape into the per-firing message re-creates the recipe; dropping the
identifiers from the message re-creates the unreachability this paragraph
exists to prevent.

**Dual-install resolution.** A coordinator-only install has the wiki only
under the settings root; a example-doctrine-repo user has both, and the example-doctrine-repo source tree
is the fresher, editable copy. `_unlock_wiki_pointer()` prefers the example-doctrine-repo
source-tree form when a example-doctrine-repo checkout is present and its
`coordinator/docs/wiki/` directory actually exists on disk; otherwise it
names the settings-root form. As of 2026-08-11 the two forms are no longer
symmetric in what they point at:

- **example-doctrine-repo-source branch** — `example-doctrine-repo coordinator/docs/wiki/guard-unlock-
  channel.md`, the dedicated page directly (commit `fe0919f3b`). Safe to
  name because this branch only resolves when a example-doctrine-repo checkout is present,
  and on any such machine the file is on disk the moment that commit is
  pulled — there is no separate install step in between for this branch.
  `pending` is `False` here.
- **Settings-root branch** — `~/.coordinator-claude-settings/coordinator-
  claude/docs/wiki/`, the directory, NOT the page (portable — never expanded
  to an absolute path). The dedicated page exists in example-doctrine-repo's tree but is not
  yet admitted to example-doctrine-repo's seed set — that allowlist doubles as their public
  OSS publish allowlist, so admission is a PM decision on example-doctrine-repo's side with no
  committed date — so naming the page here would hand a 404 to precisely
  the operator with the fewest other ways to find the answer. `pending` is
  `True` here, which is what makes `annotate_deny` append the "meanwhile
  also `docs/reference/guard-override-keys.md`" fallback clause.

The check is a single registry read plus one `Path.is_dir()` stat, cached
for the process lifetime, and never raises — it degrades to the
settings-root form (`pending=True`) on any doubt (unresolved registry key,
missing directory, or any exception along the way), since a crash here
would fail the hard-deny guard OPEN.

Constructing the actual sentinel path is now left to the operator, following
§ "In-session unlock" above (`<tempdir>/coordinator-guard-unlock-
<session_id>.<guard_name>`) using the `session_id`/`guard_name` the deny
message and envelope already name elsewhere — this doc (or the wiki page,
once ratified) is where that shape is documented, since the deny message no
longer hands it over pre-assembled.

This block is appended at the two engine seams only — never hand-written
into an individual guard module — so a guard added tomorrow inherits it for
free (C4, docs/plans/2026-08-03-in-session-operator-unlock-for-the-hard-.md).

**Why appended, not prepended (2026-08-11).** The original design put this
block FIRST, ahead of the guard's own reason, on the theory that the
in-session channel is the one usable without leaving the denied session.
That theory held, but the execution didn't: four independently-dispatched
agents in a sibling repo hit the (then-prepended) line and three classified
it as prompt injection
(`cross-repo/inbox/2026-08-11-example-doctrine-repo-em-guard-unlock-banner-still-reads-
as-agent-instruction.md`). An agent under a hard-deny reads top-to-bottom
and acts on the first actionable thing it meets — and the old single-
sentence form fused the disclaimer with a fully-resolved path, a bare
imperative ("create ... FIRST"), and success-sequencing advice, all of
which were actionable regardless of the disclaimer leading it. The fix: the
guard's own "you are blocked, and why" text now leads unconditionally, and
the unlock block trails it, separated by a blank line. Within the block the
human-only-affordance statement still leads the bypass-naming sentence, and
the block is de-imperativized and made explicitly self-limiting (states
outright that this sentinel is created from a terminal outside the session
and that an agent creating it is a doctrine violation) — the same
self-limiting property `operator_override_note`'s own payload already has
by construction (an in-session agent cannot act on a key that is unsettable
from inside a running session either way, so its leading position never
posed this problem).

**Why the recipe was removed (2026-08-11, PM ruling).** Reordering the block
(above) was not the whole fix — the appended block still handed the reader a
working, fully-parameterized bypass recipe: the literal, ASSEMBLED sentinel
path, plus the create-then-retry ordering fact framed as "how to make a
combined call succeed." In the PM's own words, that is "here, take a bite
out of this lovely apple," not an informational pointer. The unlock stays
discoverable — it is documented in full here, and will be on the wiki once
that page lands — but the deny message itself now only names that it
exists and where to look, via an extra hop, never a ready-to-paste path.
The two bare `session_id`/`guard_name` values were briefly removed along
with the recipe in an earlier pass of this same fix, then reinstated as
DATA (not recipe) once it became clear no static page could substitute for
them — see the paragraph above.

Both this block and `operator_override_note`'s line state the same
human-only-affordance fact — plainly, not through a disclaimer register (the
2026-08-11 reshape retired the old "Bypass options for a human operator, not
this agent: ..." phrasing from both builders, since that framing was itself
an injection tell, not a safeguard; see the note above § "Human-only
affordances") — and a missing/unresolvable `session_id` (the fail-closed
case) simply omits the in-session block rather than printing a sentinel path
keyed to nothing.

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

Net effect: an agent cannot self-grant any of the routes above. This doc's
key table below therefore is not "how a subagent gets around a guard" — a
subagent that reads it gains nothing actionable from it — it is a reference
for the human operator deciding whether to intervene.

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
| `COORDINATOR_OVERRIDE_COVERAGE_GATE` | `coverage-gate` warn-and-continue fence | `coordinator_core/ops/coverage_gate.py` (mirrored in `coordinator_core/coverage.py`, `coordinator/bin/merge-gate-and-pr.py`, `coordinator/bin/wsc-coverage-gate-runner.py`) | Downgrades a coverage-gate `UNCOVERED` verdict from a hard exit-1 to a warn-and-continue |
| `COORDINATOR_OVERRIDE_BRIGHTLINE` | tier=A declared-but-unwalked-repo halt | `coordinator/bin/wsc-coverage-gate-runner.py` | Bypasses the hard stop for a deferred:false code-bearing plan row declaring a repo the chain walk saw zero commits in — requires the `/autonomous` sentinel to also be present, or the override is refused outright |
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
| `COORDINATOR_OVERRIDE_FSIZE_CAP` | shell-init `ulimit -f` cap emitter | `bin/shell-init-guard.py` | Not a guard bypass but a sizing knob: sets the shell's file-size ulimit cap (GiB, or `unlimited`) instead of the 8 GiB default — DR-047 cross-repo seam with example-doctrine-repo's `~/.bashrc` |

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
  modules render via `operator_override_note(env_var,
  reason_placeholder="<one-sentence reason>")` (2026-07-30 P1 fix, origin of
  the shape; `nudge_prose_queue_append.py` and
  `nudge_new_sh_file_naked_python.py` follow the same shape), which prints
  `Override key (reason), unsettable from inside this session -- ...` instead
  of the helper's default `Override key (flag), ...` parenthetical
  (2026-08-11 reshape: neither shape renders `VAR=1`/`VAR="..."` anymore —
  see `operator_override_note`'s own docstring, NEGATIVE SPEC 4). Before the
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
