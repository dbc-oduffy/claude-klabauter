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
TRIED AND REMOVED. `write_guards/engine.py:32-37` (DR-054-adjacent,
2026-07-15) records that the prior `coordinator_core.subagent_sandbox`
tool-surface write-sandbox confinement was removed from this engine because
it surprised EMs and dispatched subagents by HARD-DENYING legitimate writes
outside a narrow sandbox. A reader who finds that note after this guard
lands, without this citation, will read this module as an unreviewed
re-introduction and remove it again -- it is not. Three concrete
differences from what was removed:

  1. It BUMPS rather than hard-denies -- `CLASS = "advisory"`, never
     `"hard-deny"`. A passable, deny-then-clear bump is the whole point of
     this plan; `"hard-deny"` would silently invert the design on the one
     line that decides it (mirrors the Bash-side `fail_closed=False` choice
     in C4/C5 -- same posture, expressed in this engine's own vocabulary).
  2. It is PASSABLE BY AN ORDINARY FILE, with no unforgeability machinery --
     see `_write_bump_marker.py` [C3]. No sentinel-creation guard, no paired
     write-guard, no identity gating protects the marker this module reads.
  3. It FAILS OPEN ON EVERY UNCERTAIN BRANCH -- an unresolvable anchor, an
     unreadable registry, a missing session record, an unresolvable target
     git root all ALLOW, exactly like C2/C3's own fail-open contracts. The
     removed confinement's defect was hard-denying on exactly this kind of
     ambiguity; this module never does.

CLASS/MATCHERS/PRIORITY are pinned explicitly (the Director of Engineering finding 3), not left to
copy a neighbour or default ordering:
  - `CLASS = "advisory"` -- see point 1 above. Do NOT "fix" this to
    `"hard-deny"`; that is a straight regression on this plan's own stated
    goal, not a hardening.
  - `MATCHERS` covers `Write`, `Edit`, `MultiEdit`, AND `NotebookEdit` --
    `write_guards.engine._VALID_MATCHERS` includes `NotebookEdit`, and this
    guard's job is the full tool surface a well-meaning agent can reach for,
    not a subset with no stated reason for the gap.
  - `PRIORITY = 180` -- next free slot in the advisory/deny-offer band after
    170 (`nudge_prose_queue_append.py`), a numbering convention this
    package's advisory guards already follow (100..170 taken; see sibling
    modules' own `PRIORITY = ...` comments for the running tally). Advisory
    guards short-circuit on first non-`None` winner in `PRIORITY` order, so
    this module's exact slot only matters relative to other advisories that
    could ALSO fire on the same payload -- none currently do, but the slot
    is pinned rather than left to the `PRIORITY = 100` default so a future
    sibling addition has a real number to sequence against.

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

  - Session anchor has NO git repo (`own_gitdir is None`): mirrors C5's
    "outside-repo" no-bump condition -- a write into an unregistered, freshly
    scaffolded tree never bumps. A write landing inside a git repo that IS
    registered in the machine-local registry (`target_is_registered_repo`)
    still bumps -- mirrors C4's cross-repo condition, applied to the same
    anchor-outside-any-repo case C2 documents under "Where the bump does not
    fire".
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
  - Does NOT return `permissionDecision: "deny"` -- `CLASS = "advisory"`
    means the ONLY envelope shape this module ever returns is
    `additionalContext`, matching this package's other advisory guards.
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
    bump_applies,
    is_agent_memory_store_path,
    publish_destination_owner,
    record_applicability_event,
    resolve_launch_anchor,
    target_is_bare_temp_scratch,
    target_is_publish_destination,
    target_is_registered_repo,
)
from coordinator_core.bash_guards._write_bump_marker import (
    effective_session_id,
    marker_present,
    resolve_gitdir,
)
from coordinator_core.bash_guards._write_bump_sink_shapes import (
    nearest_existing_ancestor,
)
from coordinator_core.bash_guards._write_bump_message import (
    AGENT_CLASS_SUBAGENT,
    DESTINATION_FOREIGN,
    DESTINATION_PUBLISH,
    render_bump_message,
    resolve_agent_class,
)
from coordinator_core.subagent_sandbox.engine import resolve_effective_types
from coordinator_core.subagent_sandbox.provision_report import _sanitize_segment
from coordinator_core.trusted_root_guard import _settings_home_dir_from_env
from coordinator_core.write_guards._case_fold_path import casefold_path

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
PRIORITY = 180  # advisory/deny-offer band; next slot after 170 (see nudge_prose_queue_append.py)

#: Sibling of `_write_bump_marker.resolve_gitdir` -- this module also needs
#: the human-readable repo ROOT (`git rev-parse --show-toplevel`) for the
#: two display strings the message names (`target_repo`/`session_repo`).
#: Deliberately NOT exported from `_write_bump_marker.py` (that module's
#: job is the marker's own git-DIR, worktree-private by design); this is a
#: small, best-effort, display-only resolver, fail-open like every sibling
#: git resolver in this package.
_CREATIONFLAGS: Optional[int] = None


def _creationflags(subprocess_module) -> int:
    """Memoized `CREATE_NO_WINDOW` flag (0 off-Windows) -- computed once on
    first call, not per invocation. Deliberately not module-scope (that would
    force an eager `import subprocess`, undoing the deferral this file's
    diff exists to buy); memoized here instead so the one-time cost survives
    the deferred import. Review: code-reviewer (P3) -- restores the
    hoisted-constant behaviour without re-adding a module-level import."""
    global _CREATIONFLAGS
    if _CREATIONFLAGS is None:
        _CREATIONFLAGS = getattr(subprocess_module, "CREATE_NO_WINDOW", 0)
    return _CREATIONFLAGS


def _resolve_git_root(cwd: Optional[str]) -> Optional[str]:
    import subprocess
    creationflags = _creationflags(subprocess)
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    root = (result.stdout or "").strip()
    return root or None


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
    """
    target_dir = os.path.dirname(file_path) or file_path
    if not os.path.isabs(target_dir):
        if not payload_cwd:
            return None
        target_dir = os.path.join(payload_cwd, target_dir)
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
    file_path: str,
) -> bool:
    """The one-question verdict described in the module docstring, "VERDICT
    LOGIC" -- does NOT itself consult the marker or applicability; those are
    the caller's job (see `check()`)."""
    if own_gitdir is None:
        # Session anchor is in no git repo -- mirrors C5's outside-repo
        # no-bump condition, except a REGISTERED target still bumps
        # (mirrors C4's cross-repo condition applied to this anchor shape).
        if target_gitdir is None:
            return False
        target_dir = os.path.dirname(file_path) or file_path
        return target_is_registered_repo(target_dir)
    return not _same_gitdir(own_gitdir, target_gitdir)


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the tool-surface out-of-repo bump against a PreToolUse
    payload. Returns `None` (allow) or the advisory `additionalContext`
    envelope. Fails open, unconditionally -- see module docstring."""
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
        target_gitdir = _resolve_target_gitdir(file_path, payload_cwd)

        if _target_is_bare_temp_scratch(file_path, target_gitdir):
            return None

        if _target_is_lessons_outbox_write(file_path):
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
        if is_agent_memory_store_path(file_path):
            return None

        # NOTE: correctness here depends on `target_gitdir` already
        # reflecting the ancestor-walked resolution above (example-doctrine-repo finding #1) --
        # this conjunctive exemption and that resolution order are described
        # as independent chunks in the governing plan but share this one
        # call's `target_gitdir` value; an isolated future edit to either
        # could silently break the other's assumption (example-doctrine-repo finding #4).
        if _target_is_under_settings_home(file_path, target_gitdir):
            return None

        if not _verdict_bumps(
            session_id, payload_cwd, anchor, own_gitdir, target_gitdir, file_path
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

        target_dir = os.path.dirname(file_path) or file_path
        target_repo = _resolve_git_root(target_dir) or target_dir
        session_repo = own_git_root or anchor

        sandbox_root = ""
        if agent_class == AGENT_CLASS_SUBAGENT:
            sandbox_root = _resolve_sandbox_root(own_git_root or target_repo, session_id)

        if marker_gitdir is None:
            # Nothing to compose a clear line against -- fail open (allow)
            # rather than advise with a marker path that cannot exist.
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

        message = render_bump_message(
            agent_class=agent_class,
            target_repo=target_repo,
            session_repo=session_repo,
            gitdir=marker_gitdir,
            session_id=effective_sid or session_id,
            sandbox_root=sandbox_root,
            destination_class=destination_class,
            destination_owner=destination_owner,
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
                "additionalContext": message,
            }
        }
    except Exception:
        # Fail-open, unconditionally -- see module docstring.
        return None
