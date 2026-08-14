"""
coordinator_core.bash_guards._write_bump_session_start — SessionStart anchor record.

Purpose: the write-confinement speed bump (docs/plans/2026-08-02-write-confinement-guards.md
[DoE-claude repo], chunk C0) needs to know, later in a session and from an arbitrary Bash or
tool-surface call, "what repo did this session actually launch in" — a value an ordinary `cd`
must NOT be able to move (the Director of Engineering F1: the harness Bash tool contract states "Working directory
persists between calls", so the live payload `cwd` drifts under an agent with no adversarial
intent at all). This module writes that value ONCE, at SessionStart, keyed on session id, and
lets it be read back later regardless of how many `cd`s happened in between.

Anchor status — settled by a live liveness probe, not assumed (see § Liveness probe below):
this record is, as far as this probe can establish, the PRIMARY anchor for the write-confinement
bump, not mere corroboration of `CLAUDE_PROJECT_DIR`. `CLAUDE_PROJECT_DIR` was UNVERIFIED as
present in a real PreToolUse(Bash) hook subprocess's environment before this probe — the only two
call sites elsewhere in this codebase
(coordinator/bin/workday-start-inbox-blitz-assemble.py, coordinator_core/ops/check_em_environment.py,
coordinator_core/orient_assemble/readers_clean_ops.py) are all NON-hook call sites, and all three
fall back to `os.environ.get("PWD")` / `os.getcwd()` when it is absent — meaning nobody in this
codebase actually depends on it being live. This module's own probe (run from a live confined
Bash-tool subprocess, the closest first-party evidence obtainable without harness introspection
tooling this dispatch does not have) found `CLAUDE_PROJECT_DIR` ABSENT from the process
environment. See :data:`CLAUDE_PROJECT_DIR_LIVE_IN_HOOK_ENV` and
``coordinator_core/bash_guards/tests/test_write_bump_session_start.py::test_liveness_probe_verdict_is_pinned``
for the pinned fixture — chunk C2's applicability module MUST read this constant (or the pinned
test fixture) and describe itself accordingly, rather than presenting two anchors where the
harness in fact only gives it one live one.

FAIL OPEN — the opposite of this package's fail-closed neighbours (deliberate, per the plan's
§ Design posture: this is a passable speed bump, not a security boundary). Every branch here that
cannot write or read the record — an unresolved git root, a permission error, a missing
directory, a stray/unparseable file — returns a falsy/None result. No exception ever escapes this
module's public functions. Absence of the record is not an error condition anywhere in this
plan's design: C2 simply falls back to whichever anchor the liveness probe found live. Do NOT
"fix" this to raise on a write/read failure — that would silently invert the one property this
module exists to have.

Storage location: TWO records are written, per
`docs/plans/2026-08-03-write-bump-anchor-outside-the-guarded-repo.md` chunk C1 (the "write-both,
read-settings-home-first" call in that plan's Problem section):

  1. The existing per-session state hub this fleet already keeps
     (:func:`coordinator_core.session.core.sessions_dir` — `<git-common-dir>/coordinator-sessions/`,
     resolved via `git rev-parse --git-common-dir`, worktree-safe). A plain single-value text file
     (`write_bump_launch_cwd`), matching the existing `started_at` / `head_at_start` convention in
     `coordinator_core.session.core.init()`. This hub is REPO-RELATIVE — it moves when `cwd`
     crosses a repo boundary, which is exactly the defect the settings-home hub below fixes.
  2. A settings-home hub, `$(coordinator-settings-home)/claude-klabauter/write-bump-anchor/
     <session_id>/write_bump_launch_cwd`, resolved via
     :func:`coordinator_core.trusted_root_guard._settings_home_dir_from_env` — cwd-independent by
     construction (no git involved), so it survives a `cd` across a repo boundary, a linked
     worktree, a bare checkout, or a `cd` outside any git repo. This is claude-klabauter's first out-of-repo
     durable-data-plane write governed by `docs/reference/boundary-and-data-planes.md`
     § Durable-data plane's forward-binding rule — the path is under
     `$(coordinator-settings-home)/claude-klabauter/`, never the settings-home root or an ad-hoc
     `~/` path. Lifecycle: this record is session ephemera, not durable data, and does NOT survive
     by uninstall's blanket-with-provenance default — it is deleted on session end by
     :func:`delete_settings_home_session_record`, called from
     `coordinator_core.session.scope.archive()` (the live seam DoE's real `SessionEnd` hook
     actually calls), by an EXACT `session_id` match — no prefix matching, so it shares no
     collision hazard with `_write_bump_marker.py`'s prefix-match bug (C6's fix, a different
     record entirely).

Both writes are best-effort and independent: a settings-home write failure never affects this
module's return-value semantics (see :func:`write_session_start_record`'s own docstring) and
never masks or is masked by the in-repo write's own success/failure.

No unforgeability machinery (this is not the marker — see `_write_bump_marker.py` / § Design
posture — but the same posture applies): an agent that later edits this record has no incentive
to, since it exists to help the bump fire correctly, not to gate anything the agent wants.

Negative-spec:
    - Do NOT import from `coordinator_core.hooks` (the mcp_tool op package) — it eagerly imports
      13 hook modules for their `register_op()` side effects on every Bash tool call, and
      `coordinator_core/tests/test_bash_guards_avoid_hooks_package.py` pins the bash-guard
      chain's import graph away from it. This module needs none of that machinery: it is a plain
      read/write pair, invoked directly (not via the mcp_tool/ipc envelope), by whatever
      SessionStart entry point wires it — see the install-surface note below.
    - Do NOT add a bounded-age expiry to the read path. The record is valid for the life of the
      session, exactly like the marker in `_write_bump_marker.py` § Marker liveness; a crashed
      session's stale record is later hygiene (if any), never a load-bearing correctness gate.
    - Do NOT raise on any write/read failure. See "FAIL OPEN" above.
    - Do NOT delete the settings-home record by prefix match. `delete_settings_home_session_record`
      matches `session_id` EXACTLY — the settings-home hub is keyed one-record-per-`session_id`
      under its own directory, so there is no prefix-collision hazard to guard against in the
      first place (unlike `_write_bump_marker.py`'s `sweep_stale_markers`, C6's surface).
    - Do NOT let a settings-home write/read/delete failure change this module's return-value
      semantics for the in-repo record. The settings-home hub is additive; see each function's
      own docstring for exactly what its return value reflects.

Install-surface note (2026-08-03, C0 dispatch; wiring landed 2026-08-03, C8 dispatch): the plan's
chunk body named `coordinator_core/install/gen_settings_hooks.py` as the file to "register" this
hook in. That generator is data-driven entirely off `coordinator/hooks/hooks.json` [DoE-claude
repo] — it has no per-hook registration list of its own to edit (confirmed by reading the whole
module: the only per-hook-shaped logic is the generic CPR-rewrite/portability-assert pipeline,
which applies uniformly to every `hooks.json` entry). C8 landed the actual wiring: a
`type=="command"` `SessionStart` entry (matcher `startup|resume|clear|compact|fork`) in
`coordinator/hooks/hooks.json` [DoE-claude repo], plus a thin shim,
`coordinator/hooks/scripts/session-start-write-bump-anchor.py` [DoE-claude repo], that imports
this module directly (not via `coordinator_core.hooks`/`coordinator_core.ipc`, per this module's
own "Negative-spec" above) and calls :func:`write_session_start_record` in-process. Both are
DoE-claude-resident and were out of C0's file scope, hence this breadcrumb; it now points at the
landed wiring rather than describing a gap.

Spec backlink: DoE-claude:pln-write-confinement-guards-cross-996567 § Anchor applicability
    on something an agent cannot move; chunk C0.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

from coordinator_core.session.core import sessions_dir
from coordinator_core.trusted_root_guard import _settings_home_dir_from_env

#: `$(coordinator-settings-home)/claude-klabauter/write-bump-anchor/` — the durable-data-plane
#: prefix this module's settings-home anchor hub lives under, per
#: `docs/reference/boundary-and-data-planes.md` § Durable-data plane's forward-binding rule.
#: NOT the settings-home root -- see the module docstring's "Storage location" § item 2.
_SETTINGS_HOME_ANCHOR_SUBDIR = ("claude-klabauter", "write-bump-anchor")

# Generator-provenance declaration (generator_provenance.py).
# write_session_start_record writes write_bump_launch_cwd under
# sessions_dir()/<session_id>/ (.git/coordinator-sessions/, untracked) and
# under the settings-home write-bump-anchor hub -- neither is a tracked
# repo artifact.
GENERATES = []

#: Liveness-probe verdict for `CLAUDE_PROJECT_DIR` inside a real, live confined Bash-tool
#: subprocess (the closest first-party evidence available to this dispatch — see the module
#: docstring's "Anchor status" paragraph). `False` means ABSENT: this session-start record is the
#: PRIMARY anchor, not corroboration. Pinned as a fixture by
#: `test_write_bump_session_start.py::test_liveness_probe_verdict_is_pinned` so a later change in
#: harness behaviour shows up as a failing test rather than silent doctrine drift. If a future
#: probe on a live PreToolUse(Bash) hook subprocess (not merely a Bash-tool subprocess, which is
#: the nearest available proxy) finds it present and live, flip this constant AND update the
#: fixture AND C2's docstring in the same change — the three must never disagree.
CLAUDE_PROJECT_DIR_LIVE_IN_HOOK_ENV = False

#: Single-value record file name, written once per session under
#: `<git-common-dir>/coordinator-sessions/<session_id>/` AND under the settings-home hub's
#: `<session_id>/` directory -- same filename, same single-scalar content, two locations.
_RECORD_FILENAME = "write_bump_launch_cwd"


def _is_safe_settings_home_session_id(session_id: str) -> bool:
    """`True` iff `session_id` resolves to a direct child of the settings-home anchor hub.

    Review: code-reviewer (`e2a586f9`) — this is the first `session_id`-keyed write to land
    outside the repo tree, under a settings-home directory this fleet also uses for other
    consumers' durable data, so a `session_id` containing a path separator or a `.`/`..`
    segment could escape `write-bump-anchor/<session_id>/` into sibling settings-home
    content. Deliberately NOT a full `_SESSION_ID_FORMAT_RE`-shaped format check (that regex
    lives in `subagent_sandbox.engine` and is out of scope here) -- this only rejects the
    traversal shape, so short test session ids like `abc` remain valid.
    """
    return (
        bool(session_id)
        and "/" not in session_id
        and "\\" not in session_id
        and session_id not in (".", "..")
    )


def _settings_home_anchor_dir(env: Optional[dict] = None) -> str:
    """`$(coordinator-settings-home)/claude-klabauter/write-bump-anchor`, resolved via the
    injected `env` mapping (defaulting to `os.environ` when `env` is `None` -- the same
    optional-`env` contract every function in this module honours, per this chunk's
    cross-repo-writer constraint: DoE's `session-start-write-bump-anchor.py` calls
    `write_session_start_record` with no `env`, so a required parameter would break that
    call site).

    Returns `""` (fail open) when the settings home itself cannot be resolved -- callers
    treat an empty string exactly like "no settings-home hub available" and skip the
    settings-home leg of whatever they're doing, never raising.
    """
    resolved_env = os.environ if env is None else env
    home = _settings_home_dir_from_env(resolved_env)
    if not home:
        return ""
    return os.path.join(home, *_SETTINGS_HOME_ANCHOR_SUBDIR)


def write_session_start_record(
    session_id: str, launch_cwd: Optional[str] = None, env: Optional[dict] = None
) -> bool:
    """Write the launch cwd for `session_id`, keyed on session id, ONCE at SessionStart.

    `launch_cwd` defaults to `os.getcwd()` — the natural choice at the moment a SessionStart
    hook actually fires, before anything has had a chance to `cd`. The same value is used both
    as the content recorded AND as the `cwd` from which the IN-REPO session hub is resolved
    (`sessions_dir(launch_cwd)`) — at SessionStart these are definitionally the same directory,
    and resolving that hub from anywhere else would be resolving the wrong session's tree.

    Writes BOTH records (see module docstring "Storage location"):
      1. The in-repo `sessions_dir(launch_cwd)/<session_id>/write_bump_launch_cwd` record —
         unchanged from this module's original behaviour.
      2. The settings-home `<session_id>/write_bump_launch_cwd` record, resolved via
         `_settings_home_anchor_dir(env)` — cwd-independent, keyed only on `session_id`.
         A machine-wide `session_id` collision (astronomically unlikely) does not have an
         unconditional safety guarantee: it either has no effect (the two sessions' recorded
         values happen to agree) or produces a wrong-but-still-anchored verdict for one of
         them. Review: code-reviewer (`e2a586f9`) — a wrong-but-anchored verdict is far more
         likely to bump spuriously (the overwritten value rarely matches this session's live
         cwd) than to suppress a needed bump (which requires the overwritten value to
         coincidentally equal this session's live cwd too) — but neither absolute claim holds.

    A `session_id` that would escape the settings-home hub (a path separator, or `.`/`..`)
    is rejected by `_is_safe_settings_home_session_id` for the settings-home leg only —
    fail-open, matching every other guard in this module: the settings-home write is
    silently skipped, never raised, and the in-repo write and this function's return value
    are unaffected.

    `env` is optional, defaulting to `None` (meaning: resolve the settings home from
    `os.environ`) — see `_settings_home_anchor_dir`'s own docstring for why this MUST stay
    optional: DoE's `coordinator/hooks/scripts/session-start-write-bump-anchor.py` calls this
    function with no `env` argument, and a required parameter would break that cross-repo
    call site.

    RETURN-VALUE SEMANTICS ARE UNCHANGED from this module's original behaviour: the return
    value reflects ONLY the in-repo write's own success (`True` iff it wrote; `False` on any
    resolution/write failure — no git root, permission error, read-only filesystem, ...). The
    settings-home write in step 2 is a fully independent, best-effort, fail-open side effect:
    its failure (an unresolvable or unwritable settings home) is swallowed unconditionally and
    never turns a successful in-repo write into a `False` return, exactly as its own failure
    never turns an ALREADY-failed in-repo write into a `True` one. Never raises, from either
    write. Idempotent — a later call with the same `session_id` overwrites the same file(s)
    with the same content; this module does not attempt to detect or reject an already-written
    record, since a repeat SessionStart fire (double-fire, a compact) recording the same cwd is
    harmless by construction.
    """
    if not session_id:
        return False
    resolved_cwd = launch_cwd if launch_cwd else os.getcwd()

    in_repo_ok = False
    try:
        base = sessions_dir(resolved_cwd)
        if base:
            sdir = Path(base) / session_id
            sdir.mkdir(parents=True, exist_ok=True)
            (sdir / _RECORD_FILENAME).write_text(resolved_cwd, encoding="utf-8")
            in_repo_ok = True
    except Exception:
        # Fail open, unconditionally -- see module docstring "FAIL OPEN". A record that
        # cannot be written is a missed bump for this session, never a raised error.
        in_repo_ok = False

    try:
        hub = _settings_home_anchor_dir(env)
        if hub and _is_safe_settings_home_session_id(session_id):
            hdir = Path(hub) / session_id
            hdir.mkdir(parents=True, exist_ok=True)
            (hdir / _RECORD_FILENAME).write_text(resolved_cwd, encoding="utf-8")
    except Exception:
        # Fail open, unconditionally, and independently of the in-repo write above -- see
        # this function's own "RETURN-VALUE SEMANTICS" paragraph.
        pass

    return in_repo_ok


def _read_settings_home_record(session_id: str, env: Optional[dict]) -> Optional[str]:
    """Read the settings-home hub's record for `session_id`, or `None` if absent/unreadable.
    Fail-open helper backing `read_session_start_record`'s settings-home-first read order --
    see that function's own docstring. A traversal-shaped `session_id` (rejected by
    `_is_safe_settings_home_session_id`) is treated exactly like an absent record."""
    try:
        if not _is_safe_settings_home_session_id(session_id):
            return None
        hub = _settings_home_anchor_dir(env)
        if not hub:
            return None
        record_path = Path(hub) / session_id / _RECORD_FILENAME
        if not record_path.is_file():
            return None
        text = record_path.read_text(encoding="utf-8").strip()
        return text or None
    except Exception:
        return None


def read_session_start_record(
    session_id: str, cwd: Optional[str] = None, env: Optional[dict] = None
) -> Optional[str]:
    """Read back the launch cwd recorded for `session_id`, or None if absent/unreadable.

    Read order (settings-home-first — the headline fix this chunk's plan makes):
      1. The settings-home hub (`_settings_home_anchor_dir(env)`), cwd-INDEPENDENT by
         construction. Tried first because it is the only one of the two records that
         survives `cwd` drifting across a repo boundary — see item 2 below for why the other
         record cannot be relied on for that case.
      2. The in-repo `sessions_dir(cwd)/<session_id>/write_bump_launch_cwd` record, tried only
         when the settings-home read comes back empty (absent settings home, absent record, or
         any read failure).

    `cwd` is where to resolve the IN-REPO session hub FROM (typically the live payload cwd at
    read time, which may have drifted from the launch cwd via an intervening `cd`). Docstring
    correction (this record previously claimed passing the live `cwd` here was safe because it
    resolved "the SAME hub regardless of which subdirectory of the repo it's resolved from" —
    that claim is TRUE for subdirectories of one repo, but SILENTLY FALSE once `cwd` crosses a
    repo boundary: `sessions_dir` then resolves an entirely different repo's hub, where this
    session's record was never written, and this function's in-repo leg alone would return
    `None`. That drift is exactly the defect
    `docs/plans/2026-08-03-write-bump-anchor-outside-the-guarded-repo.md` fixes — which is why
    the settings-home hub above, not this in-repo record, is now the PRIMARY read for the
    cross-repo case; this in-repo record remains a same-repo (or settings-home-unavailable)
    fallback, still correct for the case it was always correct for.

    `env` is optional, defaulting to `None` (meaning: resolve the settings home from
    `os.environ`) — same optional-`env` contract as `write_session_start_record` and
    `_settings_home_anchor_dir`, so a caller resolving other settings-home-anchored facts
    (e.g. `_write_bump_applicability`'s `~/.claude` fleet-recovery hatch) from an injected
    `env` mapping can thread the SAME mapping in here, rather than one leg of a single verdict
    resolving from injected `env` and the other from live `os.environ`.

    FAIL OPEN: returns None on any resolution/read failure at either tier (no git root, no
    settings home, missing record, permission error, undecodable content, an
    unexpectedly-empty file). Never raises.
    """
    if not session_id:
        return None
    hub_value = _read_settings_home_record(session_id, env)
    if hub_value is not None:
        return hub_value
    try:
        base = sessions_dir(cwd)
        if not base:
            return None
        record_path = Path(base) / session_id / _RECORD_FILENAME
        if not record_path.is_file():
            return None
        text = record_path.read_text(encoding="utf-8").strip()
        return text or None
    except Exception:
        # Fail open, unconditionally -- see module docstring "FAIL OPEN". An unreadable or
        # unparseable record is treated exactly like an absent one.
        return None


def delete_settings_home_session_record(session_id: str, env: Optional[dict] = None) -> bool:
    """Delete the settings-home anchor record for `session_id`, by EXACT match only.

    Called by `coordinator_core.session.scope.archive()` at session end (see module docstring
    "Storage location" item 2, and AC12 of the write-bump-anchor plan) — the settings-home
    anchor is session ephemera, not durable data, so it must not survive its own session by
    riding uninstall's blanket-with-provenance default. This is a plain exact-`session_id`
    delete: the settings-home hub is keyed one-record-per-`session_id` under its own directory,
    so there is no prefix-collision hazard here at all, unlike `_write_bump_marker.py`'s
    `sweep_stale_markers` (C6's surface, a different record, fixed separately).

    FAIL OPEN, matching every other function in this module: returns `True` on a successful
    delete OR when the record was already absent (idempotent — "already archived or never
    existed" is success, not failure). Returns `False` only when a delete was actually
    attempted and failed (e.g. a permission error). Never raises. A traversal-shaped
    `session_id` (rejected by `_is_safe_settings_home_session_id`) is treated as "nothing to
    clean up" — returns `True`, no delete attempted.
    """
    if not session_id:
        return False
    try:
        if not _is_safe_settings_home_session_id(session_id):
            # Review: code-reviewer (`e2a586f9`) -- a traversal-shaped session_id resolves to
            # nothing this hub owns; treat it like an already-absent record (idempotent
            # success), never attempt the delete.
            return True
        hub = _settings_home_anchor_dir(env)
        if not hub:
            return True  # no settings-home hub resolvable -- nothing to clean up, not a failure
        sdir = Path(hub) / session_id
        if not sdir.exists():
            return True  # idempotent -- already gone or never written
        shutil.rmtree(sdir)
        return True
    except Exception:
        return False
