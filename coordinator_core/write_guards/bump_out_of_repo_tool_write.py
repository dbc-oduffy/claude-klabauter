"""coordinator_core.write_guards.bump_out_of_repo_tool_write — the
write-confinement speed bump's tool-surface leg: `Write`/`Edit`/`MultiEdit`/
`NotebookEdit`.

Spec backlink: docs/plans/2026-08-02-write-confinement-guards.md [example-doctrine-repo
repo], chunk C7, "Tool-surface guard — Write/Edit/MultiEdit".

THIS IS A SPEED BUMP, NOT A SECURITY BOUNDARY. Read the plan's "Design
posture -- passable by construction" section before touching this module.
Without this guard the Bash-surface bumps (`bump_foreign_repo_write.py` [C4],
`bump_outside_repo_write.py` [C5]) are trivially sidestepped by an agent that
simply uses `Write` with an absolute path -- not evasion, just the other
tool. This module is the same job, on the tool surface: it does NOT
distinguish "foreign repo" from "no repo at all" the way the two Bash guards
do, because on this surface both collapse to one question -- does the
write's OWN resolved repo (if any) differ from the session's own repo? --
answered once here rather than split across two modules, since there is no
shell parsing seam forcing the Bash-side split in the first place (see
"SIMPLER BY CONSTRUCTION" below).

THIS MODULE REBUILDS, ON THE SAME SURFACE, SOMETHING THIS FLEET ALREADY
TRIED AND REMOVED -- BUT THE COMPARISON'S OUTCOME CHANGED (bug
`2026-08-10-cross-repo-write-boundary-denies-on-bash-b6fd16ed9ab9`,
verified live in-session). `write_guards/engine.py:32-37` (DR-054-adjacent,
2026-07-15) records that the prior `coordinator_core.subagent_sandbox`
tool-surface write-sandbox confinement was removed because it surprised EMs
and dispatched subagents by hard-denying legitimate writes outside a narrow
sandbox. This module's ORIGINAL authoring (chunk C7) read that precedent as
grounds to stay `CLASS = "advisory"` forever, on the theory that the
Bash-surface sibling guards (`bump_foreign_repo_write.py` [C4],
`bump_outside_repo_write.py` [C5]) were ALSO advisory-only, so parity meant
staying soft. That theory was never actually true: `bump_foreign_repo_write.
check_bump_foreign_repo_write` composes its own `_deny()` envelope --
`permissionDecision: "deny"`, a REAL block -- for exactly the payload shape
this module handles, verified by driving both surfaces through the live
seam (example-doctrine-repo repo's `coordinator/hooks/scripts/preuse-write-dispatch.py`
-- abs-path-ok: illustrative prose naming the verification entry point, not
a runtime path reference) with an identical foreign-repo target in the SAME
session. `GuardBand.
ADVISORY_REWRITE` (the band `dispatch.py` registers both Bash bump guards
into) governs SEQUENCING ONLY -- which phase a guard's own crash-handling
and short-circuit precedence run in -- never the SHAPE of the envelope a
successful check() is allowed to return; nothing enforces "this band's
guards may only emit `additionalContext`", and `bump_foreign_repo_write.py`
does not. The asymmetry the bug names (`git checkout` on a peer repo
DENIED; the identical peer-repo `Edit` ALLOWED) is exactly this module
staying soft while its Bash siblings were never actually soft. Three
concrete differences from what was removed by DR-054 still hold, and still
matter -- none of them required staying advisory:

  1. It is PASSABLE BY AN ORDINARY FILE, with no unforgeability machinery --
     see `_write_bump_marker.py` [C3]. No sentinel-creation guard, no paired
     write-guard, no identity gating protects the marker this module reads.
     A `touch` clears it; the removed confinement had no such escape hatch.
  2. It FAILS OPEN ON EVERY UNCERTAIN BRANCH -- an unresolvable anchor, an
     unreadable registry, a missing session record, an unresolvable target
     git root all ALLOW, exactly like C2/C3's own fail-open contracts. The
     removed confinement's defect was hard-denying on exactly this kind of
     ambiguity; this module (like its Bash siblings) never does -- it hard
     DENIES only once every fail-open branch above it has already resolved
     the target with confidence.
  3. It runs through `write_guards.engine`'s hard-deny phase, which carries
     an in-session operator UNLOCK ESCAPE (`coordinator_core.session.
     guard_unlock_sentinel`, appended to every hard-deny's
     `permissionDecisionReason` at that engine's single seam) that the
     removed confinement never had -- a second, independent clear path
     beyond the marker `touch` below.

CLASS/MATCHERS/PRIORITY are pinned explicitly (the Director of Engineering finding 3), not left to
copy a neighbour or default ordering:
  - `CLASS = "hard-deny"` (changed from `"advisory"` by the bug fix cited
    above) -- matches what the Bash-surface siblings ACTUALLY do, not what
    their own `ADVISORY_REWRITE` band name suggests. Do NOT revert this to
    `"advisory"` without first re-verifying (via the live seam, not a code
    read) that `bump_foreign_repo_write.py` has ALSO been downgraded to a
    genuine advisory -- reverting only this module's `CLASS` re-opens the
    exact asymmetry this fix closes.
  - `MATCHERS` covers `Write`, `Edit`, `MultiEdit`, AND `NotebookEdit` --
    `write_guards.engine._VALID_MATCHERS` includes `NotebookEdit`, and this
    guard's job is the full tool surface a well-meaning agent can reach for,
    not a subset with no stated reason for the gap.
  - `PRIORITY = 135` -- hard-deny band, next free slot after 132
    (`block_oss_mirror_memo_delivery.py`); see that package's own sibling
    `PRIORITY = ...` comments (5..132 taken) for the running tally. Hard-deny
    guards run in `PRIORITY` order with first-non-`None`-wins, so this
    module's exact slot only matters relative to other hard-deny guards that
    could ALSO fire on the same payload -- pinned rather than left to the
    `PRIORITY = 100` default so a future sibling addition has a real number
    to sequence against.

TWO UNLOCK MARKERS, TWO SCOPES, BOTH DELIBERATE -- NOT UNIFIED BY THIS FIX.
This module's marker lives at the SESSION'S OWN gitdir (see "MARKER
LOCATION" below); `bump_foreign_repo_write.py`'s marker lives at the
TARGET's own gitdir (narrowed per-(session, target), chunk C3 of
`docs/plans/2026-08-03-narrow-write-confinement-bump.md`). The two do NOT
authorize the same thing and are not made to: this module's marker clears
EVERY foreign target for the Write/Edit/MultiEdit/NotebookEdit surface, for
the rest of the session, in one `touch` -- broad-by-target, narrow-by-
surface. The Bash marker clears ONE target for the Bash surface only --
narrow-by-target, and (because Bash commands can target any repo a `git -C`
or write-sink can reach, with no single "the session's own tool surface" to
scope against) there is no broader location to narrow FROM. An operator (or
an EM clearing on a dispatched subagent's behalf) who wants both surfaces
stood down for one foreign repo therefore still needs two `touch` commands
today -- one per surface -- and this fix does not change that. Unifying the
two into one marker location was considered and rejected for this fix:
doing so would mean either (a) this module's marker also narrows to
per-target, losing the "one clear covers every target" property AC6 of the
governing plan explicitly wanted for the tool surface, or (b) the Bash
guard's marker widens to session-own-gitdir, a change to a sibling module
this fix's declared surface does not include and a sibling session may be
concurrently touching (see `bump_foreign_repo_write.py`'s own "PARITY"
section for a live example of exactly that concurrent-edit hazard). Both
markers remain ordinary, unforgeable-by-design files per `_write_bump_
marker.py`'s own doctrine -- this fix does not add gating to either.

VERIFYING THIS GUARD BY HAND? IT NEEDS A REAL SESSION-START RECORD FIRST.
`check()`'s verdict runs through the SAME `bump_applies`/`resolve_launch_
anchor` gate the Bash siblings use (see "ONE CLEAR, ONE SET OF HATCHES"
below): a hand-typed `session_id` that was never passed through the
SessionStart hook (`session-start-write-bump-anchor.py`, example-doctrine-repo repo)
has no anchor record and no live `CLAUDE_PROJECT_DIR`, so `resolve_launch_
anchor` returns `None` and this guard ALLOWS -- correctly, by the same
fail-open contract every function in `_write_bump_applicability.py`/
`_write_bump_marker.py` documents, and verified identical on the Bash
sibling for the same unanchored `session_id` (2026-08-10 verification
transcript). This is NOT specific to `Write`/`Edit` and is NOT a hole this
module's `CLASS = "hard-deny"` fix needed to close: every REAL session
(EM or dispatched subagent alike) gets its anchor record written
automatically at SessionStart, before any tool call can happen. A manual
probe that skips SessionStart is testing a state a live session can never
actually be in -- write the anchor record first (`_write_bump_session_
start.write_session_start_record(session_id, launch_cwd=cwd)`, exactly as
this package's own test suite does for every non-fail-open test in
`write_guards/tests/test_bump_out_of_repo_tool_write.py`) before drawing
any conclusion from a hand-constructed payload. See that test file's own
`test_em_repro_payload_unanchored_session_fails_open_and_matches_bash_
parity` / `test_em_repro_payload_denies_once_session_has_its_real_anchor_
record` pair for both halves pinned as tests.

ONE CLEAR, ONE SET OF HATCHES, THREE GUARDS. This module consumes the SAME
C2 applicability (`_write_bump_applicability.bump_applies`,
`resolve_launch_anchor`, `target_is_registered_repo`) and the SAME C3 marker
(`_write_bump_marker.resolve_gitdir`, `marker_present`,
`effective_session_id`) that the two Bash guards consume, and renders
through the SAME C6 message (`_write_bump_message.render_bump_message`,
`resolve_agent_class`). Divergence between the Bash surfaces and this one is
the defect this chunk exists to prevent -- do not re-derive any of
applicability, marker, or message logic locally; import and call.

SIMPLER BY CONSTRUCTION. The PreToolUse payload for `Write`/`Edit`/
`MultiEdit`/`NotebookEdit` carries an explicit `tool_input.file_path` (or
`notebook_path`) -- there is no shell command to tokenize and no interpreter
question to classify (contrast C4's `-C`/`cd` shapes and C5's write-sink
enumeration + inline `-c` unwrap). Do NOT import
`_command_tokenizer`/`_sentinel_creation_guard`'s `-c`-flag unwrap here --
there is no command line on this surface for either to operate on.

DESTINATION-CLASS AXIS, NO SECOND MODULE
(docs/plans/2026-08-03-narrow-write-confinement-bump.md, chunk C5). This
surface gains the SAME `destination_class` axis as the two Bash guards --
`DESTINATION_PUBLISH` when the target resolves to a registered
`publish.mirrors.*.path` entry (C1's `target_is_publish_destination`,
gated on `target_gitdir is not None` -- a mirror entry is itself always a
real repo, so a target resolving to no git repo at all can never match
it), `DESTINATION_FOREIGN` otherwise -- without gaining a second
classification module of its own; `check()` calls the same
`_write_bump_applicability` classifier the Bash guards import. Marker
LOCATION is UNCHANGED by this chunk (see "MARKER LOCATION" below) -- only
the message's destination axis is added here.

VERDICT LOGIC -- one question, answered against the session's OWN resolved
git-dir (via C2's `resolve_launch_anchor` + C3's `resolve_gitdir`, NEVER the
live payload `cwd` -- see C2's own docstring for why an in-session `cd`
cannot be trusted as an anchor) and the write's TARGET resolved git-dir (via
`resolve_gitdir` against the target file's containing directory):

  - Session anchor has NO git repo (`own_gitdir is None`): a target that
    ALSO resolves to no git repo never bumps -- mirrors C5's "outside-repo"
    no-bump condition (no gitdir anywhere to site a clearable marker; see
    `anchor_subtree_contains`'s own docstring for why Narrow does not touch
    this cell). A write landing inside a git repo that IS registered in the
    machine-local registry (`target_is_registered_repo`) still bumps
    unconditionally, as it always has -- mirrors C4's cross-repo condition.
    Narrow (PM ruling 2026-08-10, `state/bug-backlog/2026-08-10-a-session-
    anchored-outside-any-git-repo-88ca86c1f8bf.yaml`): an UNREGISTERED
    target now ALSO bumps unless it sits at or under the session's own
    anchor-directory SUBTREE (`anchor_subtree_contains`) -- confining a
    rootless session to its own launch-directory subtree rather than
    leaving every unregistered checkout writable.
  - Session anchor DOES have a git repo: a target resolving to the SAME
    git-dir (resolved + case-folded, both operands -- see "PATH COMPARISON"
    below) never bumps. A target resolving to NO git-dir, or to a
    DIFFERENT one, bumps.

Both branches are gated first by `bump_applies()` (the `~/.claude`
fleet-recovery hatch and the unresolvable-anchor case), exactly as both Bash
guards are.

SETTINGS HOME IS NOT A FOREIGN REPO (example-doctrine-repo finding #2, parity). A target that
resolves to NO git repo AND sits under the settings home
(`trusted_root_guard._settings_home_dir_from_env`) never bumps -- the same
AC9 "always-allowed destination" the Bash-surface `bump_outside_repo_write.py`
[C5] already grants, imported through the SAME helper both `bump_outside_
repo_write.py` and `bump_foreign_repo_write.py` [C4] already use. Before this
fix, this module contained neither that symbol nor a `_settings_home`
concept, so the identical destination -- `$(coordinator-settings-home)/...`
-- bumped on this surface while it was silently exempted on the Bash one.
Same shape as the temp-scratch exemption immediately below: conjunctive
(no-git-repo AND under-the-root), gated by an already-resolved `target_gitdir`
short-circuit so a real checkout that happens to live under the settings
home is still a foreign repo and still bumps. Do NOT hand-roll a parallel
env read here -- import `_settings_home_dir_from_env` exactly as the two
Bash surfaces do; a third independently-derived shape is the failure mode
finding #2 names, not a fix for it.

LESSONS-OUTBOX IS NOT A MISWRITE, EVEN THOUGH IT IS A FOREIGN REPO
(cross-repo write-bump false positive, observed live 2026-08-03).
`coordinator-lesson-promote` (`ops/queue_promote.py`, `queue.promote`) writes
a universal lesson's durable home to `<doe_root>/state/lessons-outbox/
<id>.yaml` BY DESIGN -- example-doctrine-repo is the central lessons repo, not the
session's own repo, and there is no in-repo alternative destination (see
`queue_promote.py`'s own module docstring, "example-doctrine-repo is the central
lessons repo"). Before this fix, every `Edit` to a freshly-promoted
lessons-outbox record tripped this guard and pointed the agent at
`cross-repo-memo` -- advice that is WRONG for this artifact class: a memo
and a lessons-outbox entry are different channels with different sanctioned
CLIs, and `cross-repo-memo` cannot write a lessons-outbox record at all.

`_target_is_lessons_outbox_write` withholds the bump when the resolved,
case-folded target path contains `state/lessons-outbox` as an adjacent
directory-path pair, checked via a simple casefolded path-segment split --
deliberately NOT gated on which repo the segment resolves inside (contrast
`_target_is_bare_temp_scratch`/`_target_is_under_settings_home` immediately
below, both of which fire only when the target has NO git repo). The whole
point of this exemption is that the target IS a foreign repo -- gating it on
"no repo" would exempt nothing real, since `queue.promote` always writes
into an actual example-doctrine-repo checkout.

DO NOT WIDEN THIS TO `cross-repo/inbox/` OR `cross-repo/outbox/`. Those
paths are the memo channel, and this repo's own CLAUDE.md is explicit that
hand-writing a memo into a sibling's tree is forbidden -- the guard's
existing "use cross-repo-memo" message is CORRECT for that class and must
keep firing. The two channels have different sanctioned CLIs and different
"no in-repo alternative" stories; a shared "sanctioned channel" exemption
covering both would blur a distinction the CLAUDE.md draws on purpose. Keep
this predicate narrowly keyed to the literal `state/lessons-outbox` segment,
nothing broader.

PARITY -- BASH SURFACE DOES NOT YET EXEMPT THIS CASE. As of this fix,
`_write_bump_applicability.py` (the shared C2 module both Bash guards
consume) carries no lessons-outbox exemption of its own, so a Bash-surface
`echo >> <doe_root>/state/lessons-outbox/<id>.yaml` still bumps while this
tool-surface guard now stands down for the equivalent `Edit`/`Write`. This
mirrors the SAME shape as the settings-home fix immediately below (example-doctrine-repo
finding #2) BEFORE that fix landed here -- a real, currently-open parity
gap, not a false alarm. Closing it is out of scope for this module: it
belongs in the shared `_write_bump_applicability.py` module, which was
mid-edit by a concurrent session at the time this exemption was added here
and must not be touched from this file.

SYSTEM-TEMP SCRATCH IS NOT A FOREIGN REPO. A target that sits under ANY
recognized temp root AND resolves to NO git repo at all never bumps --
`_target_is_bare_temp_scratch`, a thin wrapper over the SHARED classifier
`_write_bump_applicability.target_is_bare_temp_scratch` (see that module's
own docstring for the full "recognized temp roots" set -- it is deliberately
NOT `tempfile.gettempdir()` alone: on macOS, `TMPDIR`/`gettempdir()` resolves
under `/var/folders/...`, but the harness-designated per-session scratchpad
lives under `/private/tmp`, which `gettempdir()` alone never covers). The
harness designates that scratchpad for ALL temporary files and documents it
as "session-specific, isolated from the user's project"; before the fix that
widened this classifier past `gettempdir()` alone, every memo draft, scratch
script and intermediate file written there still tripped the bump and was
told "cross-repo-memo is the sanctioned channel for repos you don't own" --
advice about a repo, for a path that is not in one. The two write classes
this guard models are a FOREIGN REPO and OUTSIDE ANY REPO; a bare temp path
is neither, because there is no other repo whose ownership the write could
violate, so the message is a category error, not merely noisy. C2's own
docstring settles the direction: "A bump that misfires on legitimate work
gets disabled, and a disabled guard prevents nothing -- so uncertainty here
always resolves toward NOT bumping." A high-rate false positive on the one
directory the harness tells every agent to use is exactly the failure mode
that warning names.

The condition is deliberately CONJUNCTIVE -- under a recognized temp root
AND in no repo. A real checkout that happens to live under the temp root IS
a foreign repo and must still bump; exempting the temp root unconditionally
would open a hole the size of `git clone $TMPDIR/...`. See the shared
classifier's own docstring for the full temp-root set and the realpath +
case-fold treatment of every candidate (this project treats Windows as
first-class, where the root is `%TEMP%`/`%TMP%`; macOS symlinks `/tmp` ->
`/private/tmp`, which the shared classifier's `os.path.realpath("/tmp")`
candidate exists specifically to close).

PATH COMPARISON -- BOTH OPERANDS RESOLVED AND CASE-FOLDED, per the plan's
Anti-scope: `os.path.realpath` on both git-dirs, then
`write_guards._case_fold_path.casefold_path`, before the equality test --
mirrors `commit_tripwires._same_tree` plus the case-fold widening C2 already
applies for the identical reason (this fleet's primary filesystem, APFS, is
case-insensitive). Resolving only one operand would false-bump a session
whose own root is reached through a symlink.

MARKER LOCATION -- SESSION'S OWN GIT-DIR, WITH A NAMED FALLBACK. The marker
this module checks/prints lives at the session's own resolved git-dir
(`own_gitdir`) whenever one exists -- this is what gives AC6 its "one clear
stands down every surface, every target, for the rest of the session"
property: the marker's location does not vary per foreign target, so a
single `touch` covers writes into ANY number of different foreign repos
later in the same session. The one case with no session git-dir to put a
marker in at all is the outside-any-repo-anchor-but-registered-target branch
above (`own_gitdir is None`); there this module falls back to the TARGET's
own resolved git-dir, since a session with no home repo has no other place
for the marker to live, and the alternative (never being clearable in that
one shape) would leave the bump permanently un-passable for that specific
case -- a fail-open choice, not a hardening one.

AGENT-CLASS AND SANDBOX ROOT -- delegated to `_write_bump_message`'s own
`resolve_agent_class()` (the shared `subagent_sandbox.engine.
resolve_effective_types` OR-resolver) for the EM-vs-subagent split. For
subagent-class messages, `sandbox_root` is resolved against the surviving
`state/subagent-share/<session-id>/` convention documented in
`subagent_sandbox/CONTRACT.md` -- DR-058 removed this package's old
`sanctioned_dirs` write-confinement concept entirely, so there is no general
"sandbox root" resolver left to call; the session-keyed report-sidecar home
is the closest still-live, still-named primitive, reusing
`subagent_sandbox.provision_report._sanitize_segment` for the session-id
path segment rather than re-deriving a sanitizer.

OBSERVABILITY (AC18) -- `record_applicability_event` is called exactly once,
immediately before this module returns a firing advisory envelope (never on
an allow path, and never before the marker check -- a cleared bump must not
be logged as having fired). Best-effort per C2's own contract; a failed
append never alters this module's ALLOW/advise decision.

FAIL-OPEN, UNCONDITIONALLY. `check()` is wrapped in a single try/except that
returns `None` on ANY unexpected exception, matching this package's other
advisory guards (`nudge_windows_subprocess_popup.py`). Every helper below
this point already degrades to `None`/`False`/`""` on its own failure paths
(same contract as C2/C3's own modules) -- the outer wrapper is defense in
depth, not the primary fail-open mechanism.

Negative-spec:
  - Does NOT import `_command_tokenizer` or `_sentinel_creation_guard`'s
    inline-`-c` unwrap -- no command line exists on this surface (see
    "SIMPLER BY CONSTRUCTION").
  - Does NOT resolve applicability from the live payload `cwd` or from a
    `cd` target -- see C2's own docstring; this module takes the identical
    anchor-only stance.
  - Does NOT compose a marker or git-dir path as a string join -- every
    git-dir this module uses comes from `_write_bump_marker.resolve_gitdir`
    (`git rev-parse --git-dir`, resolved).
  - Does NOT add a creation guard, a paired write-guard, or identity gating
    around the marker this module reads -- see C3's own docstring; that
    decision is shared, not re-litigated per surface.
  - DOES return `permissionDecision: "deny"` (changed by the bug fix cited
    above, "THIS MODULE REBUILDS...") -- `CLASS = "hard-deny"` now, matching
    what `bump_foreign_repo_write.py` actually does on the Bash surface. Do
    NOT revert this bullet or the envelope shape below it without also
    re-verifying the Bash sibling has not itself changed in the meantime.
  - Does NOT enumerate evasions of this guard itself. Coverage is calibrated
    to the shapes a well-meaning agent reaches for (an absolute or
    repo-relative `file_path` outside its own repo), not to adversarial
    path construction.
  - Does NOT hand-roll a settings-home env read -- imports
    `trusted_root_guard._settings_home_dir_from_env`, the SAME helper both
    Bash-surface bump guards already use, rather than deriving a second,
    parallel notion of "settings home" (see "SETTINGS HOME IS NOT A FOREIGN
    REPO" above -- that drift is finding #2, the defect this exemption
    fixes).
  - Does NOT widen the lessons-outbox exemption to `cross-repo/inbox/` or
    `cross-repo/outbox/` -- see "LESSONS-OUTBOX IS NOT A MISWRITE" above.
    Hand-writing a memo into a sibling's tree stays forbidden, and this
    guard's `cross-repo-memo` message must keep firing for that path shape.
  - Does NOT edit `_write_bump_applicability.py` to close the Bash-surface
    parity gap named above -- that module was mid-edit by a concurrent
    session when this exemption was added; the gap is reported, not papered
    over from this file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from coordinator_core.bash_guards._write_bump_applicability import (
    _is_under,
    anchor_subtree_contains,
    bump_applies,
    is_agent_memory_store_path,
    publish_destination_owner,
    record_applicability_event,
    resolve_launch_anchor,
    target_is_bare_temp_scratch,
    target_is_publish_destination,
    target_is_registered_repo,
    target_is_under_claude_home,
)
from coordinator_core.bash_guards._write_bump_marker import (
    effective_session_id,
    marker_gitdir_is_writable,
    marker_present,
    resolve_gitdir,
)
from coordinator_core.bash_guards._write_bump_sink_shapes import (
    nearest_existing_ancestor,
    resolve_relative,
    translate_msys_path,
)
from coordinator_core.bash_guards._write_bump_message import (
    AGENT_CLASS_SUBAGENT,
    DESTINATION_FOREIGN,
    DESTINATION_PUBLISH,
    SURFACE_TOOL,
    render_bump_message,
    resolve_agent_class,
)
from coordinator_core.subagent_sandbox.engine import resolve_effective_types
from coordinator_core.subagent_sandbox.provision_report import _sanitize_segment
from coordinator_core.trusted_root_guard import _settings_home_dir_from_env
from coordinator_core.write_guards._case_fold_path import casefold_path
from coordinator_core.write_guards._repo_root import resolve_repo_root

CLASS = "hard-deny"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
PRIORITY = 135  # hard-deny band; next free slot after 132 (see block_oss_mirror_memo_delivery.py)

#: Sibling of `_write_bump_marker.resolve_gitdir` -- this module also needs
#: the human-readable repo ROOT (`git rev-parse --show-toplevel`) for the
#: two display strings the message names (`target_repo`/`session_repo`).
#: Deliberately NOT exported from `_write_bump_marker.py` (that module's
#: job is the marker's own git-DIR, worktree-private by design); this is a
#: small, best-effort, display-only resolver, fail-open like every sibling
#: git resolver in this package.
#:
#: AC4 migration note (2026-08-07 no-window-subprocess-primitive, C3b): this
#: resolver now delegates to the shared `write_guards._repo_root` seam (see
#: `_resolve_git_root` below), which owns its own Windows console-popup
#: suppression -- the `_creationflags`/`_CREATIONFLAGS` memoized-flag helper
#: that used to feed this module's own inline spawn was removed as dead code
#: once that spawn was.


def _resolve_git_root(cwd: Optional[str]) -> Optional[str]:
    """``git rev-parse --show-toplevel``. Feeds the ``target_repo``/
    ``session_repo`` message strings AND, via ``target_repo = _resolve_git_
    root(target_dir) or target_dir`` at this module's call site,
    ``target_is_publish_destination``/``publish_destination_owner`` -- so
    this is NOT purely display text. It drives ``destination_class``
    (PUBLISH vs FOREIGN) in the emitted advisory, a content-classification
    branch. It never feeds the bump/no-bump verdict itself, which is
    computed from ``resolve_gitdir`` elsewhere in this module -- that
    boolean is unaffected by anything below.

    AC4 (docs/plans/2026-08-07-no-window-subprocess-primitive.md, chunk C3b):
    delegates to the shared, process-lifetime-memoized
    ``write_guards._repo_root.resolve_repo_root`` instead of hand-rolling its
    own spawn -- same fail-open-to-``None`` contract as the prior inline
    ``subprocess.run``. Unlike the bump/no-bump verdict, the classification
    branch above IS timeout-sensitive: the shared resolver's fixed 2.0s
    timeout (down from this call's prior 10s) only matters on the
    spawn-fallback path (walk found no `.git`, e.g. `target_dir` outside any
    locally-walkable repo) -- exactly the case where a slow/network-drive
    spawn is most likely. A timeout there now fails ~5x sooner than before,
    `target_repo` falls back to the raw `target_dir`, and a target that IS a
    registered publish mirror can misclassify as FOREIGN instead of PUBLISH.

    Judgment call, left as-is deliberately rather than "fixed" here: this
    call site cannot get its own longer timeout without either (a) adding a
    per-call timeout parameter to the shared resolver's public API, which
    touches `coordinator_core/git/repo_root.py` -- out of scope for this
    integration pass (a sibling session owns concurrent edits nearby, and
    the fix is a resolver API change, not a guard-local one) -- or (b)
    reintroducing a second, guard-local spawn just for this call, which is
    the exact duplication AC4 eliminated. The risk window is also narrow: it
    only opens on a target both outside any locally-walkable repo AND slow
    to reach (network drive), and the failure mode is a stricter-than-true
    advisory (FOREIGN read where PUBLISH applies), not a silent under-warn.
    Recommend routing a per-call timeout override through the shared
    resolver as a follow-up if that risk window proves to matter in
    practice.
    """
    return resolve_repo_root(cwd)


def _extract_file_path(payload: Dict[str, Any]) -> str:
    """`file_path`, falling back to `notebook_path` for `NotebookEdit` --
    same extraction shape as this package's other tool-surface guards
    (`block_subagent_archive_write._extract_file_path`,
    `block_subagent_plan_body_write._extract_file_path`)."""
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return ""
    return tool_input.get("file_path") or tool_input.get("notebook_path") or ""


def _normalize_for_compare(path: Optional[str]) -> Optional[str]:
    """`os.path.realpath` -> `casefold_path`, or `None` on any resolution
    failure. Both operands of the "same repo" comparison go through this
    exact helper -- see module docstring, "PATH COMPARISON"."""
    if not path:
        return None
    try:
        resolved = os.path.realpath(path)
    except OSError:
        return None
    return casefold_path(resolved)


def _same_gitdir(a: Optional[Path], b: Optional[Path]) -> bool:
    a_cf = _normalize_for_compare(str(a) if a is not None else None)
    b_cf = _normalize_for_compare(str(b) if b is not None else None)
    if a_cf is None or b_cf is None:
        return False
    return a_cf == b_cf


def _resolve_sandbox_root(git_root: Optional[str], session_id: str) -> str:
    """Best-effort `state/subagent-share/<sanitized-session-id>/` display
    path -- see module docstring, "AGENT-CLASS AND SANDBOX ROOT". Returns
    `""` (fail open -- the message renders with a blank sandbox line rather
    than raising) when either input is unusable."""
    if not git_root or not session_id:
        return ""
    sanitized = _sanitize_segment(session_id)
    if not sanitized:
        return ""
    return str(Path(git_root) / "state" / "subagent-share" / sanitized)


#: Adjacent, case-folded directory-path pair this predicate keys on -- see
#: module docstring, "LESSONS-OUTBOX IS NOT A MISWRITE". Deliberately the
#: two literal segments only, never a broader `cross-repo/` prefix -- see
#: that section's "DO NOT WIDEN" paragraph.
_LESSONS_OUTBOX_SEGMENTS = ("state", "lessons-outbox")


def _target_is_lessons_outbox_write(file_path: str) -> bool:
    """True iff `file_path`'s path components contain `state/lessons-outbox`
    as an adjacent, case-folded directory pair -- see module docstring,
    "LESSONS-OUTBOX IS NOT A MISWRITE, EVEN THOUGH IT IS A FOREIGN REPO".
    Callers treat `True` as "never bump".

    UNLIKE `_target_is_bare_temp_scratch`/`_target_is_under_settings_home`
    immediately below, this predicate is NOT gated on `target_gitdir is
    None` -- the whole point of this exemption is the target IS a foreign
    repo (`coordinator-lesson-promote` always writes into an actual
    example-doctrine-repo checkout), so requiring "no repo" would exempt nothing real.

    Path-shape only, via a simple casefolded split -- matches a subdirectory
    under `state/lessons-outbox/` too (e.g. the `drained/` subdirectory
    `priority_drain.py` adopts), not merely a direct child file. Never
    raises: `casefold_path` and plain `str.split` do not raise on any `str`
    input.

    C4c (docs/plans/2026-08-07-guard-posix-path-rerooting.md): `file_path`
    is now the TRANSLATED path `check()` resolved once via
    `_resolve_translated_file_path` -- an untranslatable candidate (`None`,
    coerced to `""` by the caller) falls through this same `not file_path`
    branch to `False` ("not exempt"), which is safe because `check()`'s own
    `_verdict_bumps` already fails open (no bump) on an unresolved target;
    this predicate merely stops WIDENING an exemption on a path it cannot
    read, never causing a bump on its own.
    """
    if not file_path:
        return False
    normalized = casefold_path(file_path)
    parts = [p for p in normalized.split("/") if p]
    for i in range(len(parts) - 1):
        if (parts[i], parts[i + 1]) == _LESSONS_OUTBOX_SEGMENTS:
            return True
    return False


def _target_is_bare_temp_scratch(file_path: str, target_gitdir: Optional[Path]) -> bool:
    """True iff `file_path` resolves under ANY recognized temp root AND
    resolves to no git repo -- see module docstring, "SYSTEM-TEMP SCRATCH IS
    NOT A FOREIGN REPO". Callers treat `True` as "never bump".

    Delegates to the SHARED classifier,
    `_write_bump_applicability.target_is_bare_temp_scratch` -- this module
    previously derived "system temp" from `tempfile.gettempdir()` alone,
    which does NOT cover the harness-designated per-session scratchpad on
    macOS (`TMPDIR`/`gettempdir()` resolves under `/var/folders/...`, while
    the scratchpad lives under `/private/tmp`; `os.path.realpath("/tmp")`
    is what actually catches it). Two independent, same-shaped classifiers
    that could silently drift is exactly the defect the shared helper exists
    to close -- kept as a thin wrapper, name preserved, so existing
    references to `_target_is_bare_temp_scratch` on this module continue to
    resolve.

    `target_gitdir` is accepted as a short-circuit only: when the caller has
    ALREADY resolved it and found a repo, there is no need to re-resolve via
    the shared helper's own `resolve_gitdir` call.

    C4c: `file_path` is now the TRANSLATED path `check()` resolved once via
    `_resolve_translated_file_path` -- see `_target_is_lessons_outbox_
    write`'s own C4c note for why an untranslatable (`""`/`None`) input
    falling through to "not exempt" here is the correct, fail-open-overall
    behaviour rather than a widening of what bumps.
    """
    if target_gitdir is not None:
        return False
    return target_is_bare_temp_scratch(file_path)


def _target_is_under_settings_home(
    file_path: str, target_gitdir: Optional[Path], env: Optional[dict] = None
) -> bool:
    """True iff `file_path` resolves to no git repo AND sits under the
    settings home -- see module docstring, "SETTINGS HOME IS NOT A FOREIGN
    REPO" (example-doctrine-repo finding #2, parity with the Bash-surface AC9 exemption).

    Same conjunctive shape as `_target_is_bare_temp_scratch` immediately
    above: `target_gitdir` is accepted purely as an already-resolved
    short-circuit, so a real checkout that happens to live under the
    settings home is still classified as a foreign repo and still bumps.
    Resolves the settings home through the SAME shared helper both
    Bash-surface guards already import -- `trusted_root_guard.
    _settings_home_dir_from_env` -- never a hand-rolled env read.

    C4c: `file_path` is now the TRANSLATED path `check()` resolved once via
    `_resolve_translated_file_path` -- same C4c note as
    `_target_is_lessons_outbox_write`/`_target_is_bare_temp_scratch`: a
    `""`/`None` (untranslatable) input falls through
    `_normalize_for_compare`'s own `not path` branch to `None`, so this
    predicate returns `False` ("not exempt") without widening anything,
    relying on `check()`'s own overall fail-open-on-unresolved-target
    contract for the "never bump" guarantee.
    """
    if target_gitdir is not None:
        return False
    env = os.environ if env is None else env
    settings_home = _settings_home_dir_from_env(env)
    if not settings_home:
        return False
    target_cf = _normalize_for_compare(file_path)
    home_cf = _normalize_for_compare(settings_home)
    if target_cf is None or home_cf is None:
        return False
    return _is_under(target_cf, home_cf)


def _resolve_target_gitdir(
    file_path: str, payload_cwd: Optional[str]
) -> Optional[Path]:
    """Resolve the target write's git-dir, walking UP to the nearest
    EXISTING directory ancestor first (example-doctrine-repo finding #1). `resolve_gitdir`
    shells out with the candidate as `cwd`, which requires an existing
    directory -- a `Write`/`Edit` target's containing directory is very
    often not yet created (`Write` to `<own-repo>/newdir/file.txt` where
    `newdir/` does not exist yet). Probing the raw, not-yet-created
    directory directly always fails, which previously resolved to "no repo"
    and bumped even for a write squarely inside the session's OWN repo.

    Shares the SAME helper both Bash surfaces already use for this exact
    problem -- `_write_bump_sink_shapes.nearest_existing_ancestor` -- rather
    than a third, independently-derived ancestor walk.

    `payload_cwd` is threaded through explicitly rather than left ambient
    (example-doctrine-repo finding #3): `nearest_existing_ancestor` calls `os.path.isdir()`,
    which resolves a non-absolute candidate against WHATEVER cwd is live at
    call time. Relying on the coordinator engine process's own cwd for that
    resolution would make a bare relative `file_path` (e.g. `"file.txt"`,
    no dirname) silently resolve against an unrelated, process-cwd-dependent
    location -- a regression on the prior deterministic-fail-to-`None`
    behaviour for that one path shape (see module's finding #3 citation).
    A non-absolute `file_path` is anchored against the payload's own `cwd`
    here; if no payload cwd is available, this returns `None` rather than
    guessing a base -- fail open, matching every other unresolvable-anchor
    branch in this module.

    C4 (docs/plans/2026-08-07-guard-posix-path-rerooting.md): translates
    through `_write_bump_sink_shapes.translate_msys_path`/`resolve_relative`
    BEFORE any `os.path.isabs`/`os.path.join` call touches the candidate --
    same invariant C2 applies to the two Bash-surface bump guards, and the
    same shared helpers, not a third independently-derived copy. Without
    this, a POSIX-absolute MSYS path (`/x/claude-klabauter/scratch/t.txt`) gets
    anchored onto the process's current drive on Windows (`os.path.isabs`
    True pre-3.13 -> `realpath` anchors it; False on 3.13+ -> `os.path.join`
    re-roots it onto `payload_cwd`'s drive), producing a nonexistent path and
    bumping a write that is actually inside the session's own repo. An
    untranslatable shape (`translate_msys_path`/`resolve_relative` returning
    `None`) takes this same fail-open `None` branch -- never a bump, never
    reaching `nearest_existing_ancestor`/`resolve_gitdir` (AC6).

    C4b (same plan): the translation step itself now lives in
    `_resolve_target_dir` below -- this function is a thin wrapper
    (`_resolve_target_dir` -> `_target_gitdir_from_dir`) kept for existing
    callers/tests. `check()` calls the two halves directly so the
    translation runs exactly ONCE per PreToolUse payload and the resolved
    `target_dir` is threaded to every other site that used to recompute
    `os.path.dirname(file_path) or file_path` raw (`_verdict_bumps`, and
    `check()`'s own `target_repo` resolution) -- see C4b's "same module
    resolves the target twice more" framing.
    """
    target_dir = _resolve_target_dir(file_path, payload_cwd)
    return _target_gitdir_from_dir(target_dir)


def _resolve_target_dir(file_path: str, payload_cwd: Optional[str]) -> Optional[str]:
    """Translate `os.path.dirname(file_path) or file_path` through the
    shared MSYS/POSIX translation helpers (`translate_msys_path` /
    `resolve_relative`), returning an absolute, native-form directory path
    -- or `None` when the candidate is untranslatable or (for a relative
    translated path) no `payload_cwd` is available to anchor it against.

    C4b: extracted out of `_resolve_target_gitdir` so the translation runs
    ONCE per `check()` call and the result can be threaded to
    `_verdict_bumps` and to `check()`'s own `target_repo` resolution,
    instead of each site recomputing `os.path.dirname(file_path) or
    file_path` RAW (untranslated) as they did before this chunk. Identity
    on POSIX and a no-op on an already-native drive-absolute input, same as
    `translate_msys_path` itself -- correct inputs resolve byte-identically
    to before this chunk.
    """
    target_dir = os.path.dirname(file_path) or file_path
    translated = translate_msys_path(target_dir)
    if translated is None:
        return None
    if os.path.isabs(translated):
        return translated
    if not payload_cwd:
        return None
    return resolve_relative(payload_cwd, translated)


def _resolve_translated_file_path(
    file_path: str, payload_cwd: Optional[str]
) -> Optional[str]:
    """C4c (docs/plans/2026-08-07-guard-posix-path-rerooting.md): the FILE
    (not directory) counterpart of `_resolve_target_dir` -- same
    `translate_msys_path` -> (absolute short-circuit | `resolve_relative`
    against `payload_cwd`) shape, but over the raw `file_path` itself, never
    `os.path.dirname(file_path)`.

    Fixes the exemption-predicate gap this chunk exists for: C4/C4b already
    translate the DIRECTORY used for the bump/no-bump VERDICT
    (`_resolve_target_dir` -> `target_dir`/`target_gitdir`), but `check()`
    was still handing the four exemption predicates
    (`_target_is_bare_temp_scratch`, `_target_is_under_settings_home` via
    `_normalize_for_compare`, `_target_is_lessons_outbox_write`,
    `is_agent_memory_store_path`) the RAW, untranslated `file_path`. An
    MSYS-spelled write to the harness scratchpad
    (`/x/AppData/Local/Temp/claude/.../scratch/foo.txt` on Windows) then
    matched no recognized native temp root, so the temp-scratch exemption
    never fired -- the write fell through to `_verdict_bumps`, where the
    correctly-translated `target_gitdir` was already `None` (no repo) and
    `own_gitdir` was not, so it bumped. That is a `check()`-only fix
    (this function plus its call site), not a change to any predicate's OWN
    classification logic.

    Returns `None` when `file_path` is untranslatable, or (for a relative
    translated result) when no `payload_cwd` is available to anchor it --
    the SAME two fail-open shapes `_resolve_target_dir` already has. Callers
    treat `None` as "cannot evaluate this predicate meaningfully" and fall
    through to "not exempt", relying on `check()`'s own overall
    fail-open-on-unresolved-target contract (`_verdict_bumps` already
    returns `False`/no-bump whenever `target_dir` -- resolved via the exact
    same translation -- is `None`) for the "never bump on an unresolved
    path" guarantee. Identity on POSIX and a no-op on an already-native
    drive-absolute input, same as `translate_msys_path` itself -- correct
    inputs resolve byte-identically to before this chunk.
    """
    translated = translate_msys_path(file_path)
    if translated is None:
        return None
    if os.path.isabs(translated):
        return translated
    if not payload_cwd:
        return None
    return resolve_relative(payload_cwd, translated)


def _target_gitdir_from_dir(target_dir: Optional[str]) -> Optional[Path]:
    """Ancestor-walk + `resolve_gitdir` half of the old
    `_resolve_target_gitdir` -- takes an ALREADY-TRANSLATED `target_dir`
    (see `_resolve_target_dir`) rather than a raw `file_path`. `None` in ->
    `None` out, fail-open, never reaching `nearest_existing_ancestor` on an
    unresolved candidate."""
    if target_dir is None:
        return None
    probe_dir = nearest_existing_ancestor(target_dir)
    if probe_dir is None:
        return None
    return resolve_gitdir(probe_dir)


def _verdict_bumps(
    session_id: str,
    cwd: Optional[str],
    anchor: str,
    own_gitdir: Optional[Path],
    target_gitdir: Optional[Path],
    target_dir: Optional[str],
) -> bool:
    """The one-question verdict described in the module docstring, "VERDICT
    LOGIC" -- does NOT itself consult the marker or applicability; those are
    the caller's job (see `check()`).

    C4b: `target_dir` is the SAME translated value `check()` resolved once
    via `_resolve_target_dir` -- this is a bump DECISION (not a display
    string), so it must never be taken against an untranslated MSYS-form
    path (AC6: "a deny is only ever emitted for a path this guard has fully
    and natively resolved"). `target_dir is None` (untranslatable) takes the
    same fail-open `return False` branch as `target_gitdir is None` --
    never a bump on a path this guard could not resolve.
    """
    if own_gitdir is None:
        # Session anchor is in no git repo -- mirrors C5's outside-repo
        # no-bump condition when the target ALSO has no repo (never bumps;
        # no gitdir anywhere to site a clearable marker). A target that DOES
        # resolve to a repo mirrors C4's cross-repo condition, narrowed by
        # Narrow (PM ruling 2026-08-10): a REGISTERED target still bumps
        # unconditionally, as it always has; an UNREGISTERED target now
        # ALSO bumps unless it sits at or under the session's own anchor
        # SUBTREE -- see `anchor_subtree_contains`'s own docstring.
        if target_gitdir is None:
            return False
        if target_dir is None:
            return False
        if target_is_registered_repo(target_dir):
            return True
        return not anchor_subtree_contains(anchor, target_dir)
    return not _same_gitdir(own_gitdir, target_gitdir)


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the tool-surface out-of-repo bump against a PreToolUse
    payload. Returns `None` (allow) or a `permissionDecision: "deny"` /
    `permissionDecisionReason` envelope (Review: coordinator:code-reviewer
    -- this docstring still described the pre-hard-deny advisory envelope
    shape after CLASS flipped to hard-deny, finding P3). Fails open,
    unconditionally -- see module docstring."""
    try:
        tool_name = payload.get("tool_name") or ""
        if tool_name not in MATCHERS:
            return None

        file_path = _extract_file_path(payload)
        if not file_path:
            return None

        session_id = payload.get("session_id") or ""
        payload_cwd = payload.get("cwd")

        if not bump_applies(session_id, cwd=payload_cwd):
            return None

        anchor = resolve_launch_anchor(session_id, cwd=payload_cwd)
        if not anchor:
            return None

        own_gitdir = resolve_gitdir(anchor)
        # C4b: translate ONCE here and thread `target_dir` to every other
        # site that used to recompute `os.path.dirname(file_path) or
        # file_path` raw (`_verdict_bumps` below, and this function's own
        # `target_repo` resolution further down) -- see module docstring
        # note on `_resolve_target_dir`/`_target_gitdir_from_dir`.
        target_dir = _resolve_target_dir(file_path, payload_cwd)
        target_gitdir = _target_gitdir_from_dir(target_dir)
        # C4c: translated ONCE here (same MSYS/POSIX translation C4b already
        # applies to `target_dir`) and threaded to every exemption predicate
        # below -- see `_resolve_translated_file_path`'s own docstring for
        # the defect this closes. `translated_file_path` may be `None`
        # (untranslatable candidate); every predicate below already
        # fails open ("not exempt") on a falsy input, and `_verdict_bumps`
        # independently fails open (no bump) whenever `target_dir` -- the
        # SAME translation, over the dirname -- is `None`.
        translated_file_path = _resolve_translated_file_path(file_path, payload_cwd)

        if _target_is_bare_temp_scratch(translated_file_path or "", target_gitdir):
            return None

        if _target_is_lessons_outbox_write(translated_file_path or ""):
            return None

        # Agent memory store -- Claude Code's own per-project persistent
        # memory (`<home>/.claude/projects/<slug>/memory/**`), never a
        # sibling repo or a cross-repo delivery even though `~/.claude` is
        # itself a git checkout on this fleet -- see the shared
        # `is_agent_memory_store_path` classifier's own docstring for the
        # false positive this closes. Checked unconditionally (not gated on
        # `target_gitdir`, unlike the temp-scratch/settings-home exemptions
        # below): the whole point is the target IS a foreign repo on this
        # fleet, mirroring the lessons-outbox exemption immediately above.
        if is_agent_memory_store_path(translated_file_path or ""):
            return None

        # ~/.claude carve-out (docs/plans/2026-08-10-carve-claude-out-and-
        # close-the-backslash-bypass.md, C1, AC1/AC3). Checked unconditionally
        # (not gated on `target_gitdir`), same reasoning as the agent-memory
        # exemption immediately above: `~/.claude` IS a real git checkout on
        # this fleet, so a check gated on `target_gitdir is not None` (the
        # `_target_is_under_settings_home` shape) would never fire for it --
        # see `target_is_under_claude_home`'s own docstring.
        if target_is_under_claude_home(translated_file_path or ""):
            return None

        # NOTE: correctness here depends on `target_gitdir` already
        # reflecting the ancestor-walked resolution above (example-doctrine-repo finding #1) --
        # this conjunctive exemption and that resolution order are described
        # as independent chunks in the governing plan but share this one
        # call's `target_gitdir` value; an isolated future edit to either
        # could silently break the other's assumption (example-doctrine-repo finding #4).
        if _target_is_under_settings_home(translated_file_path or "", target_gitdir):
            return None

        if not _verdict_bumps(
            session_id, payload_cwd, anchor, own_gitdir, target_gitdir, target_dir
        ):
            return None

        # Marker location -- session's own gitdir when one exists, else the
        # target's (see module docstring, "MARKER LOCATION").
        marker_gitdir = own_gitdir if own_gitdir is not None else target_gitdir

        own_git_root = _resolve_git_root(anchor)
        raw_agent_id = payload.get("agent_id") or ""
        canonical_agent_id, _agent_type, _subagent_type = resolve_effective_types(
            payload, own_git_root
        )

        effective_sid = effective_session_id(
            session_id, own_git_root, canonical_agent_id or raw_agent_id
        )
        if effective_sid and marker_present(marker_gitdir, effective_sid):
            return None

        agent_class = resolve_agent_class(payload, own_git_root)

        # C4b: `target_dir` is the SAME translated value resolved once
        # above -- previously this line recomputed
        # `os.path.dirname(file_path) or file_path` RAW, so an MSYS-form
        # `file_path` fed an untranslated string into `_resolve_git_root`
        # (and, via `target_repo`, into `destination_class`/
        # `_resolve_sandbox_root` below -- behavioural, not display-only).
        # `target_dir is None` (untranslatable) falls back to `file_path`,
        # matching this line's pre-C4b fallback-to-raw-string shape for the
        # one case with nothing translated to fall back to.
        target_repo = (
            (_resolve_git_root(target_dir) if target_dir is not None else None)
            or target_dir
            or file_path
        )
        session_repo = own_git_root or anchor

        sandbox_root = ""
        if agent_class == AGENT_CLASS_SUBAGENT:
            sandbox_root = _resolve_sandbox_root(own_git_root or target_repo, session_id)

        if marker_gitdir is None or not marker_gitdir_is_writable(marker_gitdir):
            # Nothing to compose a clear line against, OR the marker
            # location exists but is not writable/readable (STAFF-ENG
            # F0/AC5, mirrored from `bump_foreign_repo_write.
            # _evaluate_foreign_repo_candidate`) -- fail open (allow) in
            # BOTH cases rather than advertise a `touch` that can never
            # succeed. Under the pre-hard-deny `advisory` CLASS this was an
            # unsatisfiable suggestion; under `hard-deny` it would be an
            # unclearable wall.
            return None

        # C1 -- classify the target as a registered PUBLISH destination or
        # an ordinary FOREIGN source repo, the SAME closed-set membership
        # test the Bash-surface guards use (C4/C5). Only reachable when
        # `target_gitdir is not None` -- a target resolving to no git repo
        # at all can never match a `publish.mirrors.*.path` entry, since a
        # mirror entry is itself always a real repo.
        destination_class = DESTINATION_FOREIGN
        destination_owner = ""
        if target_gitdir is not None and target_is_publish_destination(target_repo):
            destination_class = DESTINATION_PUBLISH
            destination_owner = publish_destination_owner(target_repo)

        # R1 (docs/plans/2026-08-08-the-bump-message-never-showed-the-
        # operat.md): `file_path` is the RAW, pre-translation token this
        # payload carried -- captured straight off `tool_input` by
        # `_extract_file_path` above, never reconstructed from
        # `target_repo` (AC2). `target_repo` here is a repo ROOT, not the
        # file path itself, so the two are expected to differ in the
        # ordinary case; suppressed only in the (rare) case they are
        # byte-identical, per `_target_phrase`'s "never print the same
        # string twice" contract.
        message = render_bump_message(
            agent_class=agent_class,
            target_repo=target_repo,
            session_repo=session_repo,
            gitdir=marker_gitdir,
            session_id=effective_sid or session_id,
            sandbox_root=sandbox_root,
            destination_class=destination_class,
            destination_owner=destination_owner,
            raw_target=file_path if file_path != target_repo else "",
            surface=SURFACE_TOOL,
        )

        record_applicability_event(
            session_id,
            repo=session_repo,
            target=target_repo,
            agent_class=agent_class,
            cwd=anchor,
        )

        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": message,
            }
        }
    except Exception:
        # Fail-open, unconditionally -- see module docstring.
        return None
