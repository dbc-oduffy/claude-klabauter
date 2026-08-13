"""coordinator_core.bash_guards._write_bump_marker -- the write-confinement
speed bump's clear-once marker, shared by all three bump guards (Bash
cross-repo, Bash outside-repo, and the `Write`/`Edit`/`MultiEdit` tool
surface) so the clear-once property holds identically across every surface a
session can write through.

Spec backlink: docs/plans/2026-08-02-write-confinement-guards.md, chunk C3,
"Marker -- session-scoped, ordinary file, gitdir resolved not composed".

THIS IS A SPEED BUMP, NOT A SECURITY BOUNDARY. Read the plan's "Design
posture -- passable by construction" section before touching this module.
The short version: a bump that misfires on legitimate work gets disabled,
and a disabled guard prevents nothing -- so every uncertain branch in this
module ALLOWS (treats the bump as cleared, or falls through to "no verdict
either way" for the caller to re-bump on). This is the OPPOSITE fail
direction from this package's fail-closed neighbours (`_blanket_disarm.py`,
`_sentinel_creation_guard.py`) -- do not "fix" it back toward fail-closed;
that would undo the one property this module exists to have.

WHAT THIS MODULE IS: an ordinary file, at a session-scoped path, that a
caller can create to stand this bump down for the rest of a session. NO
creation guard, NO paired write-guard, NO identity gating protects it -- a
human clearing it via a `!`-prefixed prompt and an agent creating it after
its PM has assented in conversation are EQUALLY VALID clears. An agent that
creates this marker to route around an explicit "check with your PM first"
message is not the well-meaning, mid-task agent this bump targets, and
building unforgeability machinery to chase that agent is the iron-law
posture the PM explicitly rejected for this plan. This decision is also
registered in `coordinator/docs/wiki/coordinator-tripwires.md` (coordinator-claude,
landed by chunk C1) -- restated here because this module's own docstring is
where the next maintainer actually looks before "helpfully" hardening it.

MARKER SHAPE -- `<gitdir>/allow-xrepo-write-<session-id>`. The session id
lives in the marker's PATH, never its body, and is composed by the calling
guard at deny time (this module supplies `clear_line()`/`marker_path()` to
do that composition) -- never stamped into the file's contents. Reason: the
advertised clear command is a bare `touch <path>`, which creates a
zero-byte file. A design that stamped the session id into the body could
never actually be satisfied by the command the guard tells the caller to
run, which would make the advertised clear line a lie the first time
anyone tried it (see the plan's "What clears the bump" section, "the Director of Engineering
F4-mechanism").

GITDIR -- RESOLVED, NEVER COMPOSED. `resolve_gitdir()` shells out to `git
rev-parse --git-dir`, exactly as this package's other git-dir resolvers do
(see `coordinator_core.git.commit_trailers._resolve_git_dir` and
`coordinator_core.write_guards.block_subagent_plan_body_write._resolve_git_dir`,
whose join-relative-against-cwd shape this mirrors). It is NEVER a literal
`<repo-root>/.git` string join: in a linked worktree or a submodule, `.git`
at the checkout root is a plain FILE (a `gitdir: <pointer>` line), not a
directory, and joining a path under it produces a location that can be
neither written nor read. `--git-dir` deliberately resolves to the
PER-WORKTREE private gitdir, not the shared common dir (`--git-common-dir`)
-- each worktree checkout is this bump's unit of "which repo", matching
what a session actually sees when it runs `git` from that checkout.

BASENAME MATCHING ON READ IS BY PREFIX, NOT EXACT MATCH. `marker_present()`
scans the resolved gitdir's entries and treats the marker as present the
moment ANY entry's basename STARTS WITH `allow-xrepo-write-<session-id>` --
not only an entry whose name equals that string exactly. State this
explicitly because it sits next to plenty of exact-match marker readers
elsewhere in this package (e.g. `_blanket_disarm.marker_path().exists()`)
and would otherwise read as a bug next to that precedent. The looseness is
deliberate and in the fail-open direction: this predicate only ever widens
what counts as "cleared", never narrows it, and a spurious clear is a
nuisance while a spurious re-bump is the guard remaining exactly as
annoying as it already is -- the asymmetry this whole plan is built on.

NO CREATION GUARD, NO PAIRED WRITE GUARD, NO IDENTITY GATING -- restated
from above because it is the single most important sentence in this
module. Do not add a `SentinelCreationDetector` registration for this
basename, do not gate `marker_present()` on which caller created the file,
and do not add a nonce/signature scheme "just in case". Every one of those
would make this bump harder to clear, which is a straight regression on
the plan's own stated goal.

MARKER SCOPE -- NARROWED TO ONE TARGET, THE MECHANISM UNCHANGED (chunk C3,
docs/plans/2026-08-03-narrow-write-confinement-bump.md). The marker now
lives at the TARGET's own gitdir, not the session's anchor gitdir, whenever
a target git repo exists (the FOREIGN_SOURCE and PUBLISH_DESTINATION
classes) -- generalizing the fallback the no-repo-anchor branch of
`check_bump_foreign_repo_write` already used (`marker_probe = probe_dir`).
The one destination class with no target gitdir to relocate into
(OUTSIDE_ANY_REPO) keeps today's anchor-gitdir marker, unchanged -- that
guard's own defining predicate is that the target resolves to no git repo
at all, so there is no target gitdir to narrow into and per-target scoping
is a structural no-op for that class. The forbidden hardenings all attack
the clear MECHANISM. Narrowing the marker's SCOPE (per-target rather than
per-session) is permitted because the clear act remains a bare `touch` of
an ordinary file, forgeable by anyone, with no identity check and no
expiry -- that mechanism, not the breadth of what one clear covers, is what
XREPO_MARKER_IS_ORDINARY_FILE protects.

ALL THREE SURFACES NARROW ON THE SAME RULE (parity pass, 2026-08-10). The
paragraph above described the two Bash legs when C3 landed; the tool-surface
leg (`write_guards/bump_out_of_repo_tool_write.py`) kept an anchor-sited
marker for a further week on the mistaken ground that the PARENT plan's AC6
("with the marker present, none of the three surfaces bump for the rest of
the session") demanded one clear cover every target. It does not -- that
criterion is load-bearing on the TIME axis (see that plan's own § "Marker
liveness -- session-scoped, not time-scoped"), and the successor plan's AC4
had already superseded its breadth reading on the target axis. The tool leg
now sites its marker per-(session, target) too, so `marker_path()`/
`clear_line()` mean the same thing on every surface. Note "AC6" is ambiguous
across at least three plans in this area -- cite the plan filename with it.

MIGRATION IS THE CALLER'S JOB, NOT THIS MODULE'S. Nothing here is versioned
or dual-located: `marker_present()` answers about ONE gitdir. A guard that
relocates its marker while sessions holding the old one are live must call
`marker_present()` against both locations itself and advertise only the new
one -- see `bump_out_of_repo_tool_write._marker_locations` for the shape.
Do not add a "check the old place too" fallback to this module; which
locations are in play is a per-surface question this module cannot see.

UNWRITABLE/UNREADABLE TARGET GITDIR -- SAME DISPOSITION AS UNRESOLVABLE:
ALLOW, never an unclearable deny (STAFF-ENG F0, AC5). This extends
`check_bump_foreign_repo_write`'s own existing precedent -- `if
marker_gitdir is None: continue`, commented "cannot compose a clear line
without a gitdir -- fail open rather than print a message the reader can
never satisfy" -- which ALLOWS the write. Stated on the WRITE axis,
deliberately avoiding the bare word "fail-open" here because that word has
previously been misread in this exact spot as its own opposite: an
unresolvable gitdir means the guard STANDS DOWN, not that it fires harder.
A target repo whose `.git` is read-only (a mirror synced under another
uid) must be PERMITTED, not handed a permanent unclearable deny advertising
a `touch` it cannot execute -- see `marker_gitdir_is_writable()` below,
which callers consult for exactly this case, alongside the existing
`gitdir is None` check, before composing a deny.

NO BOUNDED-AGE EXPIRY IN THE READ PATH. A marker matching the current
session id is honoured for the WHOLE session, unconditionally -- there is
no read-path wall-clock check here, and none should be added. This
supersedes an earlier design that borrowed
`write_guards.guard_doctrine_surface_edits`'s `APPROVAL_WINDOW_SECONDS`
freshness-window idiom; that idiom is calibrated for a FAIL-CLOSED security
grant, where a marker outliving its intended window is a standing breach
worth clamping. Here the marker's basename IS the identity -- there is no
mismatch case to expire away, only presence-or-absence for THIS session --
and a marker that outlives its session is a missed bump for a session that
no longer exists, not a standing grant to anyone else. Cleanup of that
residue is chunk C8's session-end sweep, which is pure best-effort hygiene,
never load-bearing for correctness: this module's own read path must keep
working exactly the same whether or not that sweep ever runs, because
SessionEnd is a known-unreliable trigger in this fleet's own roadmap.

RESIDUAL UNREADABLE/UNPARSEABLE-MARKER CASE. Because the marker's identity
lives entirely in its basename (there is no body to parse), the classic
"unreadable file" hazard mostly dissolves here -- `marker_present()` never
opens the file at all, only lists the directory and compares names. The one
surviving failure mode is an unreadable or unlistable GITDIR itself
(permissions, a race where it vanished, a stray non-directory at that
path): treated as ABSENT, so the bump RE-FIRES rather than raising or
dead-ending. Re-bumping is this module's fail-open direction for THIS
decision specifically -- a spurious bump is a nuisance message; a spurious
stand-down is the guard not existing at all.

ABSENCE NEVER HARD-DENIES. Every function in this module that reports "no
marker" is reporting exactly that and nothing more -- it is the CALLING
guard's job to decide what a missing marker means (bump), never this
module's. Nothing here raises on a missing/unresolvable git context; the
worst any function does is return `None`/`False`/`""`.

SUBAGENTS -- EM SESSION ID VIA THE EXISTING BACK-POINTER, NOT REIMPLEMENTED.
`resolve_em_session_id()` ports (does not reimplement) the first leg of
`coordinator_core.subagent_sandbox.engine._read_backpointer_subagent_type`
(that function's own docstring: "Back-pointer chain: agent_id ->
em_session_id -> dispatched-agents.txt row") -- reading
`.git/coordinator-sessions/.agents/<agent_id>/em-session-id.txt` and
validating its first line against the same session-id-format regex that
module already exports. This module only needs the FIRST hop (agent_id ->
em_session_id); the second hop (em_session_id -> dispatched-agents.txt row)
belongs to that module's own agent-type resolution and is out of scope
here. A dispatched subagent's own marker check therefore resolves against
its EM's session id, so a single `touch` by (or on behalf of) the EM stands
the bump down for every subagent it dispatches too -- one clear, one
session's worth of coverage, matching the plan's AC7.

Note: this back-pointer's OWN path is a literal `<git_root>/.git/...` join,
unlike this module's `resolve_gitdir()`. That is the existing, ported shape
(see `subagent_sandbox/engine.py` around line 271) and is left as-is here
rather than "fixed" to go through `resolve_gitdir()` -- changing an
established cross-module path convention is out of scope for this chunk,
and the back-pointer file's location is not itself the worktree-sensitive
gitdir this module's own marker lives under.

SESSION-END SWEEP -- `sweep_stale_markers()` (chunk C8, docs/plans/2026-08-02-
write-confinement-guards.md). Hygiene ONLY, never load-bearing for
correctness -- restated because it is the one property this function must
never lose. There is deliberately NO read-path expiry to complement (see "NO
BOUNDED-AGE EXPIRY" above): `marker_present()` honours a matching marker for
the whole session unconditionally, so this sweep is the ONLY cleanup this
plan's marker mechanism has, and it must remain cleanup -- a caller that
starts depending on this function having run for correctness would be
depending on SessionEnd, which is a KNOWN-UNRELIABLE trigger in this
codebase's own roadmap (a session can crash, be force-quit, or simply never
emit SessionEnd for reasons unrelated to this module). Every function above
in this module keeps working identically whether or not this sweep has ever
been called even once.

"Demonstrably over," not guessed: `sweep_stale_markers()` takes an explicit
`ended_session_ids` collection from its caller and removes ONLY markers
matching one of those ids -- it does not itself decide which sessions are
over, does not scan for "old-looking" markers, and carries no wall-clock or
mtime heuristic of any kind. The intended caller is a SessionEnd hook
passing exactly the one session id the SessionEnd *payload itself* names as
ending -- that payload IS the positive evidence "this session is over," not
an inference. A caller with no such positive evidence should pass an empty
collection (or not call this function at all) rather than guess; per this
module's design posture, "leave the marker" is always the safe wrong answer
and "sweep a live session's marker mid-task" is the one sweep-side mistake
this function must never make. This is also why the function's contract is
narrower than "sweep everything old" -- it has no notion of "old" at all,
only "named as ended by the caller."

Fail-open, unconditionally, same as every other function in this module: an
unresolvable/unlistable `gitdir`, an unremovable entry (permissions, a race
where it vanished between listing and unlinking), or an empty/absent
`ended_session_ids` all degrade to "swept nothing," never a raise. A marker
this sweep fails to remove is exactly the same as a marker it was never
asked to remove -- residue, not a defect; the marker's basename-encoded
identity means a future session's own id will never collide with it anyway
(see "NO BOUNDED-AGE EXPIRY" above).

UNREACHABLE FOR TARGET-SIDE MARKERS, NOT "KEEPS WORKING" (chunk C3). This
function has no production caller today, and per-target marker relocation
(see "MARKER SCOPE" above) does not change that: this function's own
contract is per-gitdir, and its only plausible caller (a SessionEnd hook)
can resolve only ITS OWN session's gitdir -- structurally unable to reach a
marker this module has now left in a sibling repo's or publish mirror's
`.git/`. Disposition: ACCEPT THE RESIDUE. A zero-byte marker file in a
foreign/mirror `.git/` dir is untracked and harmless, and its basename
carries a session id that can never collide with a future session's own.
Building a per-session index of every target gitdir a marker has been
placed in, purely so this function could be pointed at each one, is NEW
machinery this chunk deliberately does not add.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Dict, Optional

from coordinator_core.subagent_sandbox.engine import _SESSION_ID_FORMAT_RE

_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: The marker's basename prefix. The full basename a caller creates is
#: `f"{MARKER_PREFIX}{session_id}"`; matching on read is by this same
#: prefix, not exact equality -- see module docstring.
MARKER_PREFIX = "allow-xrepo-write-"

#: PER-PROCESS MEMO for `resolve_gitdir()` -- chunk C2,
#: docs/plans/2026-08-07-n-plus-one-git-spawn-class-and-amplification-gate.md.
#: Fixes all three bump-guard call sites (`bump_foreign_repo_write.py`,
#: `bump_outside_repo_write.py`, `write_guards/bump_out_of_repo_tool_write.py`)
#: with ZERO call-site edits, because every one of them already routes
#: through this module's `resolve_gitdir()`.
#:
#: CONTRACT (binding for C2b, which may consume this memo directly):
#:   - Key: `_gitdir_memo_key(cwd)` -- the ABSOLUTE, `os.path.abspath`-
#:     normalized form of `cwd`, with `cwd is None` resolved against
#:     `os.getcwd()` at call time (matching what a bare `subprocess.run(...,
#:     cwd=None)` itself would use as its working directory). Two distinct
#:     string spellings of the same directory (a trailing slash, a mixed
#:     separator on Windows, a symlink NOT resolved -- deliberately not
#:     `Path.resolve()`, which would also collapse distinct symlinked
#:     worktree probe dirs that legitimately want independent entries) key
#:     identically once `abspath`-normalized; two different concrete
#:     directories never collide.
#:   - Value: `Optional[Path]`, the exact return value `resolve_gitdir()`
#:     would otherwise have recomputed -- a hit `None` is cached identically
#:     to a hit `Path`, since a fail-open "no repo here" answer for a given
#:     cwd is exactly as stable within one process as a resolved gitdir is.
#:   - Lifetime: PROCESS-SCOPED ONLY, never persisted, never shared across
#:     processes. Claude-klabauter is spawn-per-call with no resident daemon (plan
#:     anti-scope item 18) -- this dict is created fresh, and dies, with
#:     every process. This is the ceiling for this chunk, not a starting
#:     point for a cross-process cache.
#:   - Invalidation: NONE within a process. A single command-type invocation
#:     never changes which directory is or isn't a git repo out from under
#:     itself mid-call; if that assumption is ever untrue for a caller, that
#:     caller must not share this memo (call `reset_gitdir_memo()` first, or
#:     bypass via a private `resolve_gitdir.__wrapped__`-style escape --
#:     neither exists today because no in-tree caller has needed one).
#:   - Test-only reset: `reset_gitdir_memo()`, below -- clears the memo so
#:     a test can force `resolve_gitdir()` to re-spawn. Production callers
#:     never call it.
_GITDIR_MEMO: Dict[str, Optional[Path]] = {}


def _gitdir_memo_key(cwd: Optional[str]) -> str:
    """Normalizes `cwd` into `_GITDIR_MEMO`'s key shape -- see that dict's
    docstring comment for the full contract. Never raises: `os.path.abspath`
    on a `str` cwd or on `os.getcwd()`'s own return value cannot fail for
    the inputs this module ever passes it."""
    return os.path.abspath(cwd) if cwd else os.path.abspath(os.getcwd())


def reset_gitdir_memo() -> None:
    """TEST-ONLY escape hatch: clears `_GITDIR_MEMO` so a test can force the
    next `resolve_gitdir()` call to re-spawn `git rev-parse --git-dir`
    instead of returning a memoized answer. No production caller in this
    package calls this -- the memo's contract is "process-scoped, never
    invalidated" (see `_GITDIR_MEMO`'s docstring comment); this function
    exists solely so a test that mutates a directory's git-repo status
    mid-test (e.g. `git init`-ing a path already probed) is not silently
    poisoned by an earlier probe's cached answer.
    """
    _GITDIR_MEMO.clear()


def resolve_gitdir(cwd: Optional[str] = None) -> Optional[Path]:
    """`git rev-parse --git-dir`, resolved to an absolute path.

    Fail-open: `None` on any spawn error, timeout, non-zero exit, or empty
    output (no git binary, `cwd` not inside a git repo, ...). Never raises.
    `git rev-parse --git-dir` may print a path RELATIVE to `cwd` (`.git`,
    `../.git`, `../.git/worktrees/<name>`) -- resolved against `cwd` (or the
    process's own cwd when `cwd` is `None`) so callers get a path usable
    regardless of this process's own working directory. Deliberately mirrors
    `coordinator_core.git.commit_trailers._resolve_git_dir` and
    `coordinator_core.write_guards.block_subagent_plan_body_write._resolve_git_dir`
    rather than inventing a third variant of the same nine lines.

    PER-PROCESS MEMOIZED (chunk C2) -- see `_GITDIR_MEMO`'s docstring
    comment above for the full contract. A repeated call with an
    (abspath-normalized) equal `cwd` returns the prior answer without a
    second spawn; this is transparent to every existing caller, including
    the three bump-guard call sites this chunk fixes with zero edits to any
    of them.
    """
    memo_key = _gitdir_memo_key(cwd)
    if memo_key in _GITDIR_MEMO:
        return _GITDIR_MEMO[memo_key]
    resolved = _resolve_gitdir_uncached(cwd)
    _GITDIR_MEMO[memo_key] = resolved
    return resolved


def _resolve_gitdir_uncached(cwd: Optional[str]) -> Optional[Path]:
    """The actual `git rev-parse --git-dir` spawn -- unmemoized. Split out of
    `resolve_gitdir()` so every one of its several fail-open return points
    stores into `_GITDIR_MEMO` exactly once, at the single call site above,
    rather than each needing its own memo-write."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=cwd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            # House value (dispatch_checks._run_git) -- was 10s with no
            # stated reason; brought down 2026-08-05 hardening pass. This
            # runs on the same identity-gated hot path as every other
            # `git rev-parse` in this package and the fail-open contract
            # below is unaffected by a shorter cap.
            timeout=2.0,
            stdin=subprocess.DEVNULL,
            creationflags=_CREATIONFLAGS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    raw = (result.stdout or "").strip()
    if not raw:
        return None
    git_dir = Path(raw)
    if not git_dir.is_absolute():
        base = Path(cwd) if cwd else Path.cwd()
        git_dir = base / git_dir
    return git_dir


def marker_gitdir_is_writable(gitdir: Path) -> bool:
    """True iff `gitdir` is a real, listable directory this process can
    plausibly write a marker into -- checked via `Path.is_dir()` then
    `os.access(gitdir, os.W_OK)`.

    STAFF-ENG F0 / AC5 -- WRITE-AXIS disposition, stated on the write axis
    deliberately (see module docstring, "UNWRITABLE/UNREADABLE TARGET
    GITDIR"): a caller that sees `False` here takes the IDENTICAL
    disposition as `resolve_gitdir(...) is None` -- `continue` (ALLOW the
    write), never an unclearable deny advertising a `touch` the reader
    cannot execute. `False` here means "cannot confirm this marker location
    is usable", and the module's fail-open posture resolves that
    uncertainty toward ALLOW, exactly as every other uncertain branch in
    this module does.

    Fails to `False` (never raises) on any of: `gitdir` does not exist, is
    not a directory (a race, or a stray non-directory entry at that path),
    or `os.access`/`Path.is_dir` itself raises `OSError` (permissions, a
    vanished path). A target repo whose `.git` is read-only -- a mirror
    synced under another uid, a mounted read-only checkout -- is the
    concrete case this closes: the write is PERMITTED, same as an
    unresolvable gitdir, rather than handed a clear line it can never
    execute.
    """
    try:
        if not gitdir.is_dir():
            return False
        return os.access(gitdir, os.W_OK)
    except OSError:
        return False


def marker_basename(session_id: str) -> str:
    """The exact basename a `touch` clears this session's bump with."""
    return f"{MARKER_PREFIX}{session_id}"


def marker_path(gitdir: Path, session_id: str) -> Path:
    """The full path the guard's deny message composes and prints, and the
    same path `marker_present()` looks for a PREFIX match against (see
    module docstring)."""
    return gitdir / marker_basename(session_id)


def clear_line(gitdir: Path, session_id: str) -> str:
    """The exact, copy-pasteable clear command a deny message prints --
    `touch <resolved-gitdir>/allow-xrepo-write-<session-id>`. No stamping
    step, no race: session scoping holds by construction because the
    basename itself carries the identity (see module docstring).

    POSIX separators, always, including on Windows. The operator pastes this
    into a shell -- `!`-prefixed in the harness -- and that shell is bash. A
    native `WindowsPath` renders `<drive>:\\<repo>\\.git\\allow-...`, where
    every separator is a bash escape character: the touch silently succeeds
    against a single mangled filename with the separators eaten, in the
    CURRENT directory; the marker never lands in the gitdir, and it denies
    again with the identical message. Observed live 2026-08-07. Forward
    slashes are accepted by every Windows API and by `touch`, so the POSIX
    form is correct on both platforms rather than a Windows special case.
    """
    return f"touch {marker_path(gitdir, session_id).as_posix()}"


def marker_present(gitdir: Optional[Path], session_id: str) -> bool:
    """True iff the resolved `gitdir` contains an entry whose basename
    STARTS WITH this session's marker basename (see module docstring,
    "BASENAME MATCHING ON READ IS BY PREFIX").

    Fail-open, unconditionally: `gitdir is None`, an empty `session_id`, or
    any `OSError` listing `gitdir` (permissions, a race where it vanished,
    a stray non-directory at that path) all return `False` -- "no marker
    found", never a raise. `False` here means "this module found no
    evidence of a clear"; it is the CALLING guard's job to turn that into a
    bump, never this module's (see module docstring, "ABSENCE NEVER
    HARD-DENIES").
    """
    if gitdir is None or not session_id:
        return False
    prefix = marker_basename(session_id)
    try:
        entries = list(gitdir.iterdir())
    except OSError:
        return False
    return any(entry.name.startswith(prefix) for entry in entries)


def resolve_em_session_id(git_root: str, agent_id: str) -> str:
    """First hop of the existing back-pointer chain ported from
    `coordinator_core.subagent_sandbox.engine._read_backpointer_subagent_type`
    (see module docstring, "SUBAGENTS"): `agent_id -> em_session_id`, via
    `.git/coordinator-sessions/.agents/<agent_id>/em-session-id.txt`.

    Returns `""` (never raises) when `git_root`/`agent_id` is empty, the
    back-pointer file is missing/unreadable, or its first line does not
    match the session-id format every guard in this package already
    validates against -- lookup-fail, not an error; the caller falls back
    to its own `session_id` in that case.
    """
    if not git_root or not agent_id:
        return ""
    backptr = (
        Path(git_root) / ".git" / "coordinator-sessions" / ".agents" / agent_id / "em-session-id.txt"
    )
    try:
        content = backptr.read_text(encoding="utf-8")
    except OSError:
        return ""
    em_sid = content.splitlines()[0].strip() if content else ""
    if not _SESSION_ID_FORMAT_RE.match(em_sid):
        return ""
    return em_sid


def effective_session_id(payload_session_id: str, git_root: Optional[str], agent_id: str) -> str:
    """Resolve the session id whose marker a caller should consult: a
    dispatched subagent's EM session id (via `resolve_em_session_id`) when
    both `git_root` and `agent_id` are available and the back-pointer
    resolves, else the caller's own `payload_session_id`. This is what
    gives a dispatched subagent AC7's "inherits its EM's marker without a
    second one" property -- one `touch` by/for the EM covers every subagent
    it dispatches in the same session.
    """
    if agent_id and git_root:
        em_sid = resolve_em_session_id(git_root, agent_id)
        if em_sid:
            return em_sid
    return payload_session_id or ""


def bump_is_cleared(
    cwd: Optional[str],
    session_id: str,
    git_root: Optional[str] = None,
    agent_id: str = "",
) -> bool:
    """Convenience composition of the above for a guard that just wants one
    call: True iff a marker matching the effective session id (see
    `effective_session_id`) is present in `cwd`'s resolved gitdir. Fail-open
    at every step -- an unresolvable session id or gitdir returns `False`
    ("not cleared, guard should re-bump per its own logic"), never raises.
    """
    effective_sid = effective_session_id(session_id, git_root, agent_id)
    if not effective_sid:
        return False
    gitdir = resolve_gitdir(cwd)
    return marker_present(gitdir, effective_sid)


def sweep_stale_markers(gitdir: Optional[Path], ended_session_ids) -> int:
    """SESSION-END HYGIENE SWEEP -- remove marker entries under `gitdir` whose
    basename EXACTLY EQUALS `marker_basename(sid)` for any `sid` in
    `ended_session_ids` (see module docstring, "SESSION-END SWEEP", for the
    full contract). Chunk C8, docs/plans/2026-08-02-write-confinement-guards.md.

    EXACT MATCH, NOT PREFIX -- deliberately narrower than `marker_present()`'s
    read-path prefix match (see module docstring, "BASENAME MATCHING ON READ
    IS BY PREFIX"). A variable-length prefix match is safe to over-grant on
    the read path (it only ever widens what counts as "cleared") but is the
    one thing this function's own contract forbids on the DELETE path: an
    ended session id that is a string-prefix of a still-live session id (e.g.
    ended `abc` vs. live `abcdef`) must never unlink the live session's
    marker. `_SESSION_ID_FORMAT_RE` (imported from `subagent_sandbox.engine`,
    not tightened here -- see this plan's anti-scope) permits exactly this
    collision shape, so the delete path composes the full candidate basename
    per id and compares for equality, never `str.startswith`.

    `ended_session_ids` -- an iterable of session ids the CALLER already
    knows are over (typically a single-element list: the one session id a
    live SessionEnd event names). This function does not infer, guess, or
    age-check anything; it only removes what it is explicitly told is safe
    to remove. Non-string / empty entries in `ended_session_ids` are
    ignored, not errors.

    Returns the count of entries actually removed -- 0 on every fail-open
    path (`gitdir is None`, an unlistable `gitdir`, an empty/absent
    `ended_session_ids`, or every candidate entry failing to unlink). Never
    raises: an `OSError` unlinking one matched entry is swallowed and
    sweeping continues with the rest, exactly like `marker_present()`'s own
    per-entry fail-open contract.
    """
    if gitdir is None:
        return 0
    ids = tuple(sid for sid in (ended_session_ids or ()) if isinstance(sid, str) and sid)
    if not ids:
        return 0
    exact_names = frozenset(marker_basename(sid) for sid in ids)

    try:
        entries = list(gitdir.iterdir())
    except OSError:
        return 0

    removed = 0
    for entry in entries:
        if entry.name not in exact_names:
            continue
        try:
            entry.unlink()
            removed += 1
        except OSError:
            # Residue, not a defect -- see module docstring "SESSION-END
            # SWEEP" fail-open paragraph. Keep sweeping the rest.
            continue
    return removed
