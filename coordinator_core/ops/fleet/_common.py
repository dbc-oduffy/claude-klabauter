"""
coordinator_core.ops.fleet._common — shared helpers for fleet.* MUTATING archival ops.

Purpose: centralised home for param validation, result-envelope assembly, D3
repo_root consistency check, worktree-root derivation, frontmatter-status reuse,
and the async DR-211 D3/D4 git-archive+commit helper.  Handler modules (archive_plans,
archive_handoffs, prune_bugs) depend on this module; it has no dependency on them.

The three fleet ops all share:
- a frozen wire envelope (contract §2.1)
- the same two-call confirm→act flow (dry_run:true preview / dry_run:false act)
- a single atomic commit scoped by construction, not by a commit pathspec, per
  DR-211 D3/D4 (amended 2026-07-26 for FORWARD-B). `rm_and_commit` still lands
  its scope through a private HEAD-seeded `GIT_INDEX_FILE`; `archive_and_commit`
  (2026-08-26) instead assembles its `{path: (mode, sha) | _ABSENT}` tree-delta
  directly, in process, and lands it via `ops.ceremony.git_native.
  _commit_via_head_spine` — no private index at all. See each function's own
  docstring for its mechanism.

Spec backlinks:
  - Plan key decisions: docs/plans/2026-07-04-pcore-11-fleet-invoke-ops.md § Key decisions 2 & 5
  - Wire contract (FROZEN): coordinator_core/contract/cockpit-invoke-producer-contract.md §2.1, §3, §5
  - DR-211: docs/decisions/DR-211-fleet-op-substrate-write-boundary.md (D3/D4, five bounds,
    FORWARD-B amendment)
  - C4 fix: docs/plans/2026-07-26-memo-disposition-flip-op-and-hand-edit-hole.md

Negative-spec:
  - Does NOT register any op — that is handler-module and ops/__init__.py responsibility.
  - Does NOT access rag's relational store (DR-211 D5).
  - Does NOT use params.repo_root as the worktree source — see check_repo_root / main_worktree_root.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Mapping, NamedTuple, Optional, Tuple, Union

if TYPE_CHECKING:
    # Type-checking only. `asyncio` is imported at FUNCTION scope by the two
    # coroutines that actually use it, so a module-scope runtime import would
    # put ~33ms of import cost on every importer of this module for a name
    # used in one annotation. `_kill_orphaned_commit`'s `proc` parameter is a
    # string annotation, so nothing evaluates it at runtime; without this
    # block the annotation is simply unresolvable to a checker.
    import asyncio

from coordinator_core.dag import _read_meta, invalidate_git_history_cache
from coordinator_core.frontmatter.primitives import read_fm_field_unquoted
from coordinator_core.git.commit_trailers import compute_missing_trailer_args
from coordinator_core.git.git_state import head_sha as _read_head_sha
from coordinator_core.git.git_state import read_tree_spine
from coordinator_core.lifecycle import git_common_dir
from coordinator_core.lifecycle import main_worktree_root  # re-export — see note below
from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT
# Peer-to-peer import, deliberate (2026-08-26, archive_and_commit tree-build
# chunk): _commit_via_head_spine is the shared "rewrite HEAD's tree spine ->
# build the commit object -> land it via a locked ref CAS" landing helper
# ops.ceremony.git_native's own commit seams use; archive_and_commit reuses
# it rather than re-deriving a second private-index-free commit mechanism.
# git_native imports nothing from ops.fleet, so this does not create a cycle.
from coordinator_core.ops.ceremony.git_native import (
    _ABSENT,
    _commit_via_head_spine,
    _hash_object_stdin_paths,
)
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.wire_paths import rel_id  # re-export — see note below

_LOG = logging.getLogger(__name__)
# Library-logger NullHandler (stdlib-recommended): keeps logging's lastResort
# handler out of the picture so `_setup_error`'s explicit stderr write is the
# single stderr line, not a duplicate of an unconfigured-logging fallback.
# Consumers that configure their own handlers still receive the record via
# propagation.
_LOG.addHandler(logging.NullHandler())

# Only valid mode in slice A — version-skew guard: unknown mode MUST fail-closed (contract §4).
_VALID_MODES = frozenset({"already-terminal"})

# deployment_state values that qualify a handoff as terminal regardless of status
# (archive_handoffs.py's terminal-deployment_state branch — active+shipped/abandoned
# off-baton handoffs are schema-valid terminal states; see archive_handoffs.py module
# docstring).  The DoE lvv-03 single-terminal-vocab consolidation has now happened
# (DR-084 plan C3): coordinator_core.lifecycle_constants is the SSOT for this
# vocabulary, and handoff_reconcile._CLOSED_DEPLOYMENT_STATES and
# reconcile.gate_eval._TERMINAL_STATES are expected to import from it too.
_TERMINAL_DEPLOYMENT_STATES = HANDOFF_TERMINAL_DEPLOYMENT

# ---------------------------------------------------------------------------
# Dest-collision vs idempotent-replay predicate (shared across archive families)
# ---------------------------------------------------------------------------

#: Skip reason for "the archive destination is occupied by a file with DIFFERENT content".
#: Deliberately NOT the AC12-pinned "already-archived" string, which means the benign
#: source-gone idempotent-replay case. This one is a wedge: the candidate cannot be
#: archived and no future sweep will unwedge it without a human reconciling the two
#: copies. Conflating the two let a stuck record report as converged.
#: Spec backlink: archive/specs/2026-07/2026-07-04-pcore-11-fleet-invoke-ops.md § AC12
#: (which pins "already-archived" to the source-gone case only).
_REASON_DEST_CONFLICT = "archive-dest-conflict"


def _is_identical_duplicate(src: Path, dst: Path) -> bool:
    """True when dst already exists AND is identical to src, modulo line endings.

    Duplicate deliveries happen: a sender can write the same memo filename into
    cross-repo/inbox/ a second time after the first copy was already archived, and
    the same shape recurs for handoffs, shipped handoffs, and bugs re-entering their
    respective source directories. Before this predicate existed, such a candidate
    was skipped as "already-archived" on EVERY sweep run and could therefore never
    leave its source directory — a permanent stranded-duplicate leak that inflated
    the apparent backlog. When the bytes match, archiving is lossless: the git mv -f
    overwrite is a no-op on content and a pure delete of the redundant source copy.

    EOL-insensitive comparison (Windows-first-class): under `core.autocrlf=true`
    a live copy checked out through git's smudge filter carries CRLF while the
    same logical record, written directly by a Python op (e.g. the archive
    write itself, or a prior sweep), carries LF — both flavours land on disk for
    the SAME record as ambient drift, not as a genuine content difference. The
    normalization (`\r\n` -> `\n`) is applied to BOTH sides for comparison only;
    neither file's on-disk bytes are ever rewritten. No other normalization
    (whitespace-trimming, BOM-stripping, unicode) is applied — EOL only.

    Returns False (skip, do not overwrite) whenever the contents differ beyond
    EOL or either file cannot be read — a differing dst is real archived
    history and must never be clobbered.
    """
    try:
        if not dst.is_file():
            return False
        src_bytes = src.read_bytes().replace(b"\r\n", b"\n")
        dst_bytes = dst.read_bytes().replace(b"\r\n", b"\n")
        return src_bytes == dst_bytes
    except OSError:
        _LOG.warning(
            "_is_identical_duplicate: comparing %s to %s failed: %s",
            src, dst, sys.exc_info()[1],
        )
        return False

# ---------------------------------------------------------------------------
# Hardened env builder for git subprocess calls (LOW env-hardening fix)
# ---------------------------------------------------------------------------

# GIT_* vars that are safe to forward (identity / signing passthrough).
_GIT_ENV_IDENTITY_PREFIXES: tuple = ("GIT_AUTHOR_", "GIT_COMMITTER_")

# Session-identity env vars consumed by the DoE prepare-commit-msg hook to
# resolve the Session-Id: commit trailer (env-only ladder:
# COORDINATOR_SESSION_ID -> CLAUDE_SESSION_ID -> CLAUDE_CODE_SESSION_ID; the
# `.current-session-id` sentinel tier was removed KS-1, 2026-08-07).  This
# allowlist forwards all three, ordered to match the precedence
# coordinator_core.session.core.resolve_session_id() / SESSION_ENV_PRECEDENCE
# actually applies — a caller relying on COORDINATOR_SESSION_ID to set the
# trailer inside a fleet git subprocess previously fell through to
# CLAUDE_SESSION_ID instead (fixed here).
# These are OPAQUE identity strings, not GIT_* execution-redirect
# vectors (unlike GIT_SSH_COMMAND / GIT_EXEC_PATH / GIT_PROXY_COMMAND /
# GIT_TEMPLATE_DIR), so forwarding them does not relax the git-execution
# security perimeter this allowlist exists to protect.  The downstream reader
# (coverage.py) UUID-shape-validates the trailer value before any `git grep`
# interpolation, so a malformed value cannot become a shell-injection vector.
_SESSION_ID_ENV_KEYS: tuple = (
    "COORDINATOR_SESSION_ID",
    "CLAUDE_SESSION_ID",
    "CLAUDE_CODE_SESSION_ID",
)

# Named reason (D1, 2026-07-28 fleet-git-env-hardening finding): `git commit`
# (not just read-only git) execs THIS repo's own prepare-commit-msg and
# post-commit hooks two processes down, so the allowlist must forward
# everything those hooks — and git-for-Windows itself — need to run, not just
# what the top-level git invocation needs. Prior to this widening the
# allowlist was narrow enough to break commits outright on native Windows
# (no HOME var survived at all) and to silently mis-resolve
# COORDINATOR_SETTINGS_HOME-relative paths in the live hooks on any
# non-default settings-home. This is the "named reason" the module docstring
# below requires before relaxing the perimeter — see also
# _make_git_env's docstring.
#
# - COORDINATOR_SETTINGS_HOME: coordinator-prepare-commit-msg (_resolve_doe_root)
#   and the live post-commit hook both read
#   ${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/machine-local/.doe-root;
#   without it they silently resolve the wrong .doe-root.
# - USERPROFILE, HOMEDRIVE, HOMEPATH: on native Windows HOME is normally
#   unset, so without these git-for-Windows cannot find global .gitconfig
#   (no user.email) and `git commit` fails outright.
# - SYSTEMROOT, PATHEXT: Windows process creation / executable resolution
#   (no named consumer reads these directly in the D1 chain either, but
#   Windows itself relies on SYSTEMROOT being set for basic process
#   creation, and PATHEXT for exe-suffix resolution when git spawns any
#   bare-name child; both are read-only environment state, not an
#   execution-redirect vector — neither NAMES an interpreter/executable the
#   way GIT_SSH_COMMAND / COMSPEC do).
# - TEMP, TMP, TMPDIR: git temp/lock/editor file placement (TMPDIR for
#   macOS, TEMP/TMP for Windows; harmless to forward all three cross-platform).
# - APPDATA, LOCALAPPDATA: %APPDATA%\git\config is git's second global
#   config file on Windows (home of credential helpers) — see the
#   config-file-location-risk paragraph below for the honest framing of
#   what this key actually accepts.
# - MSYSTEM, OS: coordinator_core/hooks/auto_push.py's _is_windows_bash()
#   reads these to select the PowerShell-SSH push branch under a
#   claude-klabauter-initiated commit; without them that branch is never selected on
#   Windows.
#
# Review: code-reviewer F1 (2026-07-28) — COMSPEC was here and is NOT: it
# names the interpreter Windows uses for shell=True/os.system()/ShellExecute
# calls, the exact "names an executable to run" shape GIT_SSH_COMMAND is
# stripped for. Checked before deciding (not assumed): this repo's own
# subprocess policy is list-argv only, no shell=True/os.system/ShellExecute
# anywhere in production code (grep confirms; see write_guards/
# nudge_windows_subprocess_popup.py, the repo-wide guard against exactly
# this shape), and no downstream consumer in the D1 chain (git's own hook
# exec via bundled sh, coordinator-prepare-commit-msg, coordinator-auto-push)
# reads COMSPEC. With no named consumer and a genuine redirect-vector shape,
# it is dropped rather than kept-and-justified — matching the citation
# discipline every other key in this set gets.
#
# The keys above split into two honest categories, not one blanket claim:
#   - Direct-injection vectors (GIT_SSH_COMMAND, GIT_EXEC_PATH,
#     GIT_PROXY_COMMAND, GIT_TEMPLATE_DIR): stripped, never forwarded. These
#     name an interpreter/executable a downstream process runs.
#   - Everything below: platform/identity state with no named consumer that
#     interprets a forwarded value as "the executable to run" — read-only
#     process-creation/locale/temp-file state, not a redirect vector.
#     (APPDATA/LOCALAPPDATA carry a narrower, already-accepted risk — see
#     the docstring's closing paragraph below.)
_EXTRA_FORWARD_ENV_KEYS: tuple = (
    "COORDINATOR_SETTINGS_HOME",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "SYSTEMROOT",
    "PATHEXT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "APPDATA",
    "LOCALAPPDATA",
    "MSYSTEM",
    "OS",
)


# ---------------------------------------------------------------------------
# Main-index resync retry budget (archive_and_commit / rm_and_commit)
# ---------------------------------------------------------------------------
#
# Sized off an empirical repro (scratch-repo, .git/index.lock held for a
# controlled duration across the resync window): the ORIGINAL fixed budget
# (3 attempts * 0.05s sleep-between = 150ms of sleeping, plus per-attempt
# subprocess spawn overhead) was reliably exhausted by a lock held for as
# little as ~0.2-0.3s of real wall time — well within the hold time of a
# genuinely concurrent `git commit` on this shared tree (this repo's own
# commit path execs prepare-commit-msg/post-commit hooks, which themselves
# shell out; see _make_git_env's D1 comment for why those hooks are not
# instant). That exhaustion is the confirmed root cause of the 2026-08-01/02
# incident: three already-archived memos left a stale `RD <dst> -> <src>`
# rename staged in the MAIN index (HEAD and the commit were correct) — the
# exact residue shape this repro reproduces on demand.
#
# Exponential backoff (not a fixed-interval retry) so a lock that clears
# quickly is retried fast, while a lock that persists gets a genuinely long
# runway without spending that whole runway densely polling. Capped attempts
# AND a capped per-sleep ceiling bound the worst case per git update-index
# call to a few seconds — "ride out contention" without "stall a sweep
# unboundedly" (a fleet sweep may resync many moves in one op call).
_INDEX_RETRY_MAX_ATTEMPTS = 8
_INDEX_RETRY_INITIAL_SLEEP_S = 0.1
_INDEX_RETRY_BACKOFF_CAP_S = 1.0

#: git's canonical empty tree — the sha `git write-tree` emits for an index
#: holding zero entries. Load-bearing, not trivia: it is what a MISSING
#: `GIT_INDEX_FILE` produces, silently. See `_empty_private_index_breach`.
EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


async def _empty_private_index_breach(
    worktree_root: Path,
    env: dict,
    caller: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Refuse a pathspec-less commit whose private index would commit NOTHING.

    Returns `(None, tree_sha)` when the index is safe to commit, or
    `(reason, None)` naming the breach — callers route that reason into their
    existing commit-failure path (reverse the disk moves, report every item
    failed) rather than committing.

    THE TREE SHA IS RETURNED, NOT DISCARDED, AND CALLERS MUST COMMIT *THAT*
    TREE (2026-08-25). This check computes the very sha `commit-tree` needs,
    and both call sites used to throw it away and spawn a second, identical
    `git write-tree` three lines later — a measured, duplicated spawn per
    batch commit on every archival and reap route. Handing it back removes
    that spawn AND narrows this guard's own time-of-check/time-of-use window
    to zero for the tree specifically: the tree that gets committed is now
    byte-identically the tree this function verified is non-empty, where
    before it was a re-read that could in principle differ. Do NOT
    "restore symmetry" by re-spawning `write-tree` in a caller.

    WHY THIS EXISTS — the incident of 2026-08-18 (`fbfbd061d`), which committed
    a tree of `4b825dc…` and thereby deleted all 26,264 files in the repo on a
    shared branch that was already pushed:

    1. `git write-tree` against a MISSING `GIT_INDEX_FILE` returns
       `EMPTY_TREE_SHA` with **exit code 0 and empty stderr** — verified on git
       2.55.0.windows.4. A zero-byte index fails loud (`index file smaller than
       expected`, rc=128); only an *absent* one fails silent. So every `.ok` /
       returncode check upstream of the commit is blind to this by
       construction, and no amount of checking `read-tree`'s exit code catches
       it — the index can go missing AFTER a successful seed.
    2. This module's two commit seams (`archive_and_commit`, `rm_and_commit`)
       commit from the private index with **no trailing pathspec**. That is
       CORRECT and must stay: a `-- <paths>` pathspec makes git read the
       WORKTREE for those paths instead of the index, which is the FORWARD-B
       hazard that laundered 34 hand-edited frontmatter changes into
       fleet-archival commits on 2026-07-26 (see `archive_and_commit`'s own
       docstring).
    3. Those two facts compose into the blast radius. WITH a pathspec, a lost
       index commits nothing; WITHOUT one, a lost index commits the empty tree
       — i.e. deletes every tracked file in the repo. The no-pathspec design is
       not the bug, and "just add a pathspec" is not the fix — that trades a
       loud, recoverable failure for a silent, recurring FORWARD-B leak.

    NEGATIVE-SPEC: do NOT "simplify" this away by giving either commit a
    pathspec. The tension in point 3 is the whole reason this check exists;
    resolving it in the pathspec direction reintroduces FORWARD-B.

    Deliberately trigger-independent: it does not care WHY the index went
    missing, only that a commit is about to erase the repo. Cost is one
    `write-tree` spawn per batch commit — per sweep, not per item — which is
    the price of the failure mode being otherwise undetectable.

    TIME-OF-CHECK/TIME-OF-USE — read this before trusting it. As of 2026-08-19
    the trigger IS known (a dispatch-timeout cancellation unlinks the private
    index mid-commit; see `_kill_orphaned_commit`), and this check does NOT
    close that path: it runs BEFORE `git commit` is spawned, while the unlink
    lands AFTER, inside the commit's pre-commit-hook window. Replayed against
    the 2026-08-18 sequence, this guard PASSES and the empty tree is still
    committed at rc=0 — had it existed that day, `fbfbd061d` would still have
    happened.

    NEGATIVE-SPEC: that is NOT a reason to remove or weaken it. It still
    catches an index already absent at commit-decision time — a class
    `_kill_orphaned_commit` does not cover — and it is the last line before the
    repo is deleted. Keep both. What must stop is describing this function as
    closing the cancellation race; it does not.
    """
    import asyncio

    proc = await asyncio.create_subprocess_exec(
        "git", "write-tree",
        cwd=str(worktree_root),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, stderr = await proc.communicate()
    if proc.returncode != 0:
        # A loud index failure (corrupt/truncated). Still a refusal — this
        # function's contract is "safe to commit", and an unreadable index is
        # not — but it is reported as itself, never as the empty-tree case.
        return (
            "private-index-unreadable: git write-tree failed — %s"
            % stderr.decode(errors="replace").strip()
        ), None

    tree_sha = out.decode(errors="replace").strip()
    if tree_sha == EMPTY_TREE_SHA:
        _LOG.error(
            "%s: REFUSED — private index resolves to git's empty tree (%s); "
            "committing it would delete every tracked file in %s. The index "
            "file named by GIT_INDEX_FILE is missing or holds zero entries.",
            caller, EMPTY_TREE_SHA, worktree_root,
        )
        return (
            "empty-private-index: git write-tree returned git's canonical "
            "EMPTY TREE (%s), meaning the private index holds zero entries — "
            "committing it with no pathspec would delete every tracked file. "
            "Refused; nothing was committed." % EMPTY_TREE_SHA
        ), None
    return None, tree_sha


def _kill_orphaned_commit(
    proc: Optional["asyncio.subprocess.Process"],
    caller: str,
) -> None:
    """Terminate a still-running private-index `git commit` before its index is unlinked.

    Call this from the `finally:` that cleans up `GIT_INDEX_FILE`, BEFORE the
    `os.unlink`. A no-op unless the child is genuinely still running.

    WHY THIS EXISTS — the root cause behind `fbfbd061d` and `0a3462b72`, which
    `_empty_private_index_breach` refuses but cannot prevent (see its
    NEGATIVE-SPEC on why it stays anyway):

    1. Op dispatch wraps async handlers in `asyncio.wait_for`
       (`ipc.py :: dispatch_message`, budget from `_timeout_for`). On expiry the
       handler is cancelled at whatever `await` it is sitting on — which, for a
       sweep that has reached its commit, is `await proc.communicate()`.
    2. asyncio does NOT terminate the child when `communicate()` is cancelled.
       The spawned `git commit` is orphaned and keeps running.
    3. The handler's `finally:` then runs during that unwind and unlinks the
       private index out from under it. git resolves `GIT_INDEX_FILE` and
       writes the tree AFTER the pre-commit hook returns, and this repo's hook
       spawns a Python interpreter that — under the 50–70-concurrent-session
       load norm — holds that window open for seconds. An unlink landing inside
       it makes `git write-tree` yield `EMPTY_TREE_SHA` at rc=0, and the
       pathspec-less commit deletes every tracked file.

    Killing the child first closes the race at the seam: a dead git writes no
    tree and updates no ref, so the unlink that follows cannot be observed by
    anyone. `proc.kill()` is synchronous and safe to call while a cancellation
    is unwinding — unlike `await proc.wait()`, which would raise `CancelledError`
    again and skip the unlink entirely.

    NEGATIVE-SPEC: `asyncio.shield` around the commit does NOT substitute for
    this. The awaiting coroutine still receives `CancelledError`, so the
    `finally:` still runs and still unlinks a live child's index.

    Cancellation at any OTHER await in these seams is already harmless: the
    `finally:` unlinks and the coroutine exits before a commit is ever spawned.
    The commit's own await is the window this covers.

    PRECISION, so a later reader does not over-trust this:

    - The reason to avoid `await proc.wait()` here is not only that a cancelled
      task may re-raise; a second await inside the `finally:` can equally just
      BLOCK for the same multi-second hook duration that caused the timeout in
      the first place. Either way the unlink is delayed or skipped, so the kill
      must be synchronous.
    - One narrower window is NOT covered: a cancellation landing inside
      `await create_subprocess_exec(...)` itself, before it returns, leaves the
      holder at `None` while the OS may already have forked the child. That
      child is unreachable from here. The window is fork/exec setup (µs) against
      the hook's multi-second hold, so it is not where a `wait_for` timeout
      realistically lands — but it is not zero, and `_empty_private_index_breach`
      is the defence that still stands behind it.
    """
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.kill()
    except (ProcessLookupError, OSError) as exc:
        _LOG.warning(
            "%s: could not kill orphaned git commit before unlinking the "
            "private index: %s",
            caller, exc,
        )
    else:
        _LOG.error(
            "%s: killed an orphaned `git commit` that was still running when "
            "the private index was about to be unlinked — the handler was "
            "cancelled mid-commit (dispatch timeout). Nothing was committed.",
            caller,
        )


async def _update_index_with_retry(argv: List[str], *, cwd: Path, env: dict) -> Optional[str]:
    """Run one git index-mutating subcommand (e.g. `update-index`, or
    `restore --staged`) with exponential-backoff retry against transient
    `.git/index.lock` contention.

    Returns None on eventual success (rc==0 on some attempt), or the LAST
    attempt's decoded stderr (never empty — falls back to a generic message)
    once `_INDEX_RETRY_MAX_ATTEMPTS` is exhausted. Never raises — this is a
    resync-residue diagnostic, not a fatal condition; callers are responsible
    for surfacing a non-None return to both the daemon log AND the wire
    envelope (see archive_and_commit / rm_and_commit's `index_resync_failed`
    per-item annotation) rather than silently discarding it, which is exactly
    the failure mode this helper's retry hardening + return contract close.

    Review: code-reviewer P2 (2026-08-13, distill.apply_disposal integration)
    — this is the ONE shared spawn point for archive_and_commit's,
    rm_and_commit's, AND distill_apply_disposal's main-index resync calls, so
    the Windows console-visibility suppression belongs here rather than
    re-litigated per call site. Routed through
    coordinator_core.win_portability.no_console_creationflags() (no hardcoded
    flag literal) — {} on POSIX (inert splat, no behavior change for the two
    pre-existing callers), {"creationflags": CREATE_NO_WINDOW} on win32.
    """
    import asyncio

    delay = _INDEX_RETRY_INITIAL_SLEEP_S
    last_err = ""
    for attempt in range(_INDEX_RETRY_MAX_ATTEMPTS):
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **no_console_creationflags(),
        )
        _out, stderr = await proc.communicate()
        if proc.returncode == 0:
            return None
        last_err = stderr.decode(errors="replace").strip()
        if attempt < _INDEX_RETRY_MAX_ATTEMPTS - 1:
            await asyncio.sleep(delay)
            delay = min(delay * 2, _INDEX_RETRY_BACKOFF_CAP_S)
    return last_err or "update-index-failed"


def _make_git_env(*, idx_path: Optional[str] = None) -> dict:
    """Build a hardened allowlist env for git subprocess calls.

    Forwards HOME, PATH, GIT_AUTHOR_*/GIT_COMMITTER_* identity vars, the
    three session-identity vars (COORDINATOR_SESSION_ID, CLAUDE_SESSION_ID,
    CLAUDE_CODE_SESSION_ID) consumed by the prepare-commit-msg hook to stamp
    the real Session-Id: trailer instead of falling through to a stale
    sentinel value, and the D1 extra-forward set
    (_EXTRA_FORWARD_ENV_KEYS — see its comment) needed
    by this repo's own prepare-commit-msg/post-commit hooks and by native
    Windows git itself, since `git commit` execs those hooks two processes
    down. Strips execution-redirect vectors: GIT_SSH_COMMAND, GIT_EXEC_PATH,
    GIT_PROXY_COMMAND, GIT_TEMPLATE_DIR, and all other uninherited GIT_* vars.

    idx_path: when provided, sets GIT_INDEX_FILE (private-index calls).
              Absent for main-index resync calls.

    Negative-spec: does NOT forward arbitrary os.environ — only the explicitly
    listed keys are permitted.  This is the security perimeter; do not relax
    it without a named reason in the commit message.  The session-id keys and
    the D1 extra-forward set ARE the named reasons on record for the two
    widenings applied so far: the session-id keys are opaque provenance
    strings consumed only by the commit-msg hook, and the D1 set is
    platform/identity state the hooked commit path needs to run at all on
    Windows and under non-default settings-homes.

    Review: code-reviewer F2 (2026-07-28) — the D1 set is NOT a single
    uniform "not a redirect vector" claim; it splits into two honestly
    distinct risk categories, and only one of them is actually clean:
      - Direct-injection vectors (stripped, never forwarded): GIT_SSH_COMMAND,
        GIT_EXEC_PATH, GIT_PROXY_COMMAND, GIT_TEMPLATE_DIR. These directly
        name an interpreter/executable a downstream git process runs.
      - Accepted config-file-location risk (forwarded): HOME, USERPROFILE,
        APPDATA, LOCALAPPDATA. Forwarding these lets the environment pick
        WHICH git config file git reads (~/.gitconfig on POSIX via HOME;
        %APPDATA%\\git\\config on Windows via APPDATA/LOCALAPPDATA), and that
        config file can itself set core.sshCommand / credential.helper /
        core.pager / core.hooksPath — the config-file equivalent of the four
        stripped env vars above. This is NOT a new hole introduced by this
        widening: HOME was already forwarded before this change and already
        controlled ~/.gitconfig on POSIX, so APPDATA/LOCALAPPDATA merely
        extend an already-accepted POSIX risk to its Windows equivalent
        rather than opening a new one. It is accepted, not closed — a
        caller controlling this process's env before `git commit` runs
        could already point HOME somewhere with a malicious .gitconfig; that
        was true pre-D1 and remains true post-D1.
      - Everything else in the D1 set (SYSTEMROOT, PATHEXT, TEMP/TMP/TMPDIR,
        MSYSTEM, OS, COORDINATOR_SETTINGS_HOME) is read-only platform/
        identity state with no config-file or interpreter-selection role —
        genuinely outside both categories above.
    COMSPEC was considered and dropped (not forwarded) rather than folded
    into either accepted category — see the comment above
    _EXTRA_FORWARD_ENV_KEYS for the checked-not-assumed reasoning.
    """
    env: dict = {}
    for key, val in os.environ.items():
        if key in ("HOME", "PATH"):
            env[key] = val
        elif key in _SESSION_ID_ENV_KEYS:
            env[key] = val
        elif key in _EXTRA_FORWARD_ENV_KEYS:
            env[key] = val
        elif any(key.startswith(pfx) for pfx in _GIT_ENV_IDENTITY_PREFIXES):
            env[key] = val
        # All other vars — including GIT_SSH_COMMAND, GIT_EXEC_PATH,
        # GIT_PROXY_COMMAND, GIT_TEMPLATE_DIR, etc. — are NOT forwarded.
    if idx_path is not None:
        env["GIT_INDEX_FILE"] = idx_path
    return env


def _message_with_hookless_trailers(
    subject: str,
    worktree_root: Union[str, Path],
    paths: Optional[List[str]] = None,
) -> str:
    """Build the commit message for a `commit-tree` landing, carrying the
    Session-Id/Deliverable-Id trailers `prepare-commit-msg` would have
    stamped if this were still a `git commit`.

    `commit-tree`/`update-ref` fire NO hooks, so the HEAD-race CAS ladder
    below would otherwise silently drop trailers this repo's commit-trailer
    producer contract (`coordinator_core/contract/commit-trailer-producer-
    contract.md`) treats as produced by every hooked commit path.
    `compute_missing_trailer_args` exists for exactly this caller class ("a
    caller (`git commit-tree` et al.) that hooks never fire for") and makes
    the identical resolve-or-omit decision the hook makes — it NEVER guesses
    and returns `[]` when nothing is resolvable.

    Spawn-free by construction: the resolver reads env/frontmatter only, and
    the resolved trailers are appended in-process rather than through the
    `git interpret-trailers` round trip `git_native`'s own ladder uses. The
    ladder is already +2 spawns over the `git commit` it replaced and its
    own comment names that as "not a place to add more" — appending after a
    single-line subject that carries no trailer block is byte-identical to
    what `interpret-trailers` would produce there.

    Never raises: a resolver failure returns the bare subject, mirroring the
    hook's own contract that a trailer problem must never block a commit.
    """
    subject_text = subject.rstrip("\n")
    fd, msg_path = tempfile.mkstemp(prefix="fleet-git-msg-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(subject_text + "\n")
        try:
            trailer_args = compute_missing_trailer_args(
                msg_path, worktree_root, paths=paths
            )
        except Exception as exc:  # noqa: BLE001 — never block a commit on a trailer
            _LOG.warning(
                "fleet commit: trailer resolution failed, landing untrailered — %s", exc
            )
            return subject_text + "\n"
    finally:
        try:
            os.unlink(msg_path)
        except OSError:
            pass

    lines: List[str] = []
    i = 0
    while i < len(trailer_args) - 1:
        if trailer_args[i] == "--trailer":
            lines.append(trailer_args[i + 1])
            i += 2
        else:
            i += 1
    if not lines:
        return subject_text + "\n"
    return subject_text + "\n\n" + "\n".join(lines) + "\n"


# `_replay_post_commit_hook` (the `coordinator-auto-push` post-commit replay)
# was REMOVED HERE (C2, 2026-08-26, docs/plans/2026-08-26-the-archival-seam-
# stops-asking-git-at-all.md) — PM ruling, ratified and restated on re-ask:
# auto-push moves to other ceremonies and does not belong on this path. The
# replay was the single largest process-count item left in this call: it
# spawned `auto_push.main()`, which itself spawns `rev-parse` and `push`, and
# `push` spawns its OWN children (`receive-pack`, `index-pack`) invisible to
# an in-process spy — roughly 8 of the call's 19 processes, larger than
# everything C1 (this same plan) touches.
#
# WHAT IS LOST, in the removed docstring's own words: "A fleet archival
# commit that never replays it never pushes, which surfaces downstream as
# auto-push lag with no attributable cause." Removing the call means an
# archival commit landed via `archive_and_commit`/`rm_and_commit` is pushed
# by whatever pushes next (the installed `post-commit` hook on a caller's own
# `git commit`, a scheduled sync sweep, or another ceremony's own push step),
# never by itself. See each function's own docstring for the same note at
# its call site.
#
# `auto_push.py` itself is untouched — it is in a DIFFERENT plan's scope
# (`pln-the-commit-op-stops-asking-git-f5a8a9`, landed `f9a44e039`) and is
# actively edited by a concurrent session; only the call from this module is
# removed, never the callee.


# ---------------------------------------------------------------------------
# Move descriptor — unit of work for archive_and_commit
# ---------------------------------------------------------------------------

class Move(NamedTuple):
    """One git-mv rename for archive_and_commit.

    src and dst are absolute paths.  candidate_id is the repo-relative source
    path used as the wire 'id' field throughout the result envelope.

    force=True permits an existing dst to be overwritten (os.replace always
    clobbers an existing dst; archive_and_commit enforces fail-on-existing-dst
    explicitly for force=False, since os.replace itself has no such mode).
    Callers MUST only set it when the overwrite is provably lossless (e.g. a
    byte-identical duplicate delivery — see _is_identical_duplicate in this
    module).  Default False preserves the fail-on-existing-dst behaviour for
    every other archival family.

    restage_src is STILL LOAD-BEARING after the F-5 os.replace swap (repair,
    2026-08-21/C5-REPAIR) — but its job narrowed, not disappeared. The
    original reason (routing around `git mv`'s re-keying of a private
    index's stale HEAD-seeded blob for src) genuinely went away: os.replace
    always moves src's CURRENT on-disk content, so there is no stale blob to
    re-key. What survived is the disk/HEAD DRIFT REFUSAL that reason used to
    gate: archive_and_commit now refuses (rather than commits) a
    restage_src=False move whose src has uncommitted disk content diverging
    from HEAD — see the batched drift check ahead of the move loop below.
    restage_src=True is the caller's assertion that IT authored the current
    on-disk content on purpose immediately before queuing the move (e.g.
    archive_handoffs._stamp_heir_shipped stamping the candidate just before
    archiving it), so drift is expected and legitimate rather than
    suspicious — those moves are exempt from the refusal. Do NOT describe
    restage_src as vestigial or unconsulted; a comment doing so previously
    shipped a correctness regression (an archive-gate bypass on an
    uncommitted `deployment_state: shipped` write — the drift check is the
    ONLY thing that can refuse it once the SHA gate passes).
    """

    src: Path           # absolute source path (must exist and be git-tracked)
    dst: Path           # absolute destination path (parent directory will be created)
    candidate_id: str   # repo-relative source path — the wire 'id' (contract §2.1 :209-215)
    force: bool = False  # permit os.replace to clobber an existing dst — lossless-overwrite opt-in only
    restage_src: bool = False  # True: caller authored src's current disk content on purpose — exempt from the disk/HEAD drift refusal below


# ---------------------------------------------------------------------------
# Param validation (contract §2.1 :158-174)
# ---------------------------------------------------------------------------

def validate_params(
    params: dict,
) -> Union[Tuple[str, bool, Optional[List[str]]], dict]:
    """Validate fleet.* op params and return parsed values or a setup-error envelope.

    Returns (mode, dry_run, candidate_ids) on success.
    Returns the exit_code:1 standard setup-error envelope dict on any failure.

    Rules:
    - mode: required; only "already-terminal" is valid in slice A.  Unknown value
      MUST fail-closed to exit_code:1 — this is the version-skew guard (contract §4).
    - dry_run: required bool.
    - candidate_ids: required and non-empty on dry_run:false (act).
      Null/omit is acceptable on dry_run:true (preview — mutates nothing).

    The exit_code:1 envelope is the STANDARD echoed shape (mode, dry_run, empty
    candidates/acted/skipped/failed); no reason/error top-level field is added —
    the frozen envelope is not expanded.  Human-readable reason is logged daemon-side.
    """
    mode = params.get("mode")
    dry_run = params.get("dry_run")
    candidate_ids = params.get("candidate_ids")

    # Mode check: fail-closed on unknown value (version-skew guard, contract §4 :375-381).
    if mode not in _VALID_MODES:
        reason = (
            f"unknown mode {mode!r}; only {sorted(_VALID_MODES)} valid in slice A — "
            f"fail-closed per contract §4 version-skew guard"
        )
        return _setup_error(mode, dry_run, reason)

    # dry_run type check.
    if not isinstance(dry_run, bool):
        reason = f"dry_run must be bool, got {type(dry_run).__name__!r}"
        return _setup_error(mode, dry_run, reason)

    # candidate_ids: required and non-empty on act (dry_run:false), contract §3.1 :238-241.
    if not dry_run:
        if not candidate_ids:
            reason = (
                "candidate_ids is required and must be non-empty on dry_run:false; "
                "no 'act on all' fallback (contract §3.1)"
            )
            return _setup_error(mode, dry_run, reason)

    return (mode, dry_run, candidate_ids)


def _emit_warm_diagnostic(text: str) -> None:
    """Forward one diagnostic line to the warm per-request sink, if one is bound.

    Function-local import and fail-open by construction: this helper runs on the
    refusal path of every fleet op, including in a cold spawn where nothing has
    bound a sink and where `coordinator_core.warm` has no reason to be imported
    at module scope.  A failure here must never convert a well-formed refusal
    into an exception — the reason is already on stderr by the time this is
    called, so the worst case is that a warm caller falls back to today's
    behaviour rather than losing anything it has today.
    """
    try:
        from coordinator_core.warm.entry_seam import emit_diagnostic

        emit_diagnostic(text)
    except Exception:  # noqa: BLE001 — diagnostics never fail the op
        pass


def _setup_error(mode, dry_run, reason: str) -> dict:
    """Internal: log reason to stderr and return the exit_code:1 standard-echoed envelope.

    The reason is NOT added as a top-level wire field.  The frozen envelope shape
    (contract §2.1) is not expanded by setup errors; cockpit branches on exit_code:1
    alone (contract §3.2).  So stderr is the ONLY diagnostic channel a setup error
    has, and it must not depend on logging configuration: `_LOG.error` reaches
    stderr today only via logging's lastResort handler, which any consumer that
    calls `logging.basicConfig()` or attaches a handler silently diverts.  The
    explicit write below guarantees the reason is on the op process's stderr
    regardless.  stderr carries no structured contract on the exit-0 success path
    (`_fatal_stderr`'s JSON error envelope is the nonzero-exit path only), so this
    is a diagnostic addition, not a wire change.

    Reaching THIS process's stderr is ALSO not enough under the warm engine, where
    "this process" is the SERVER, not the caller's child — the reason lands on the
    server's stderr and the caller reads an empty stream.  `entry_seam.emit_diagnostic`
    below is the warm half of the same channel: the server binds a per-request sink
    and returns what it collected on the response frame.  The stderr write stays
    unconditional and is what a cold caller reads; neither replaces the other.

    Reaching THIS process's stderr is where claude-klabauter's guarantee stops.  A setup error
    is a JSON-RPC *success* response, so `invoke._exit_code_for_response` exits the
    process 0 while the envelope says exit_code:1 — a consumer that spawns this
    process and reads stderr only on a nonzero exit will capture the reason and
    discard it.  Consumers must read stderr on the rc==0 path; both halves are pinned
    by tests/test_setup_error_stderr_channel.py.
    """
    _LOG.error("fleet op setup error: %s", reason)
    print(f"fleet op setup error: {reason}", file=sys.stderr, flush=True)
    _emit_warm_diagnostic(f"fleet op setup error: {reason}")
    return {
        "exit_code": 1,
        "mode": mode,
        "dry_run": dry_run,
        "candidates": [],
        "acted": [],
        "skipped": [],
        "failed": [],
    }


def build_setup_error_result(mode, dry_run, reason: str) -> dict:
    """Public alias for build_setup_error_result used by handlers for D3 / re-verify errors.

    Logs reason daemon-side; returns the exit_code:1 standard-echoed envelope.
    The frozen wire envelope is NOT expanded — no reason/error top-level field.
    """
    return _setup_error(mode, dry_run, reason)


# ---------------------------------------------------------------------------
# Envelope builders (contract §2.1 / §3.2)
# ---------------------------------------------------------------------------

def build_dry_run_result(mode: str, candidates: list) -> dict:
    """Build the exit_code:0 dry_run:true response envelope (contract §2.1 :176-215).

    candidates: list of candidate dicts, each with:
        id, title, status, family, terminal_since (nullable), note (nullable)
    Mutates nothing — the preview phase is read-only.
    """
    return {
        "exit_code": 0,
        "mode": mode,
        "dry_run": True,
        "candidates": candidates,
        "acted": [],
        "skipped": [],
        "failed": [],
    }


def build_act_result(
    mode: str,
    acted: list,
    skipped: list,
    failed: list,
) -> dict:
    """Build the dry_run:false response envelope with derived exit_code (contract §3.2 :281-299).

    exit_code:
    - 0 — failed[] empty; every candidate_id acted or cleanly skipped.
    - 2 — DETERMINATE-PARTIAL; failed[] non-empty; per-item truth enumerated.
          (NOT coverage.gate's indeterminate-retry-whole-batch exit_code:2.)

    exit_code:1 is for setup errors only — call build_setup_error_result instead.

    WIRE-SAFETY (2026-08-02): strips archive_and_commit's/rm_and_commit's
    additive `index_resync_failed` per-item annotation from `acted` before it
    reaches this FROZEN envelope (contract §2.1 pins `acted[]`'s shape as
    exactly `{id, archived: true}`; that per-item shape carries no documented
    additive-extension discipline the way the `mode` enum / async job-handle
    reservations do — see this module's archive_and_commit docstring for the
    annotation's origin). Landing a new key on this wire is a bilateral,
    cross-repo-memo-gated change, NOT something this fix unilaterally ships.
    The annotation still reaches claude-klabauter-internal consumers that read
    archive_and_commit/rm_and_commit's return value directly, BEFORE it flows
    through this function — see session.boot_sweep's `_index_resync_warnings`,
    which is the sanctioned surfacing path today.
    """
    exit_code = 2 if failed else 0
    wire_safe_acted = [
        {k: v for k, v in item.items() if k != "index_resync_failed"}
        if "index_resync_failed" in item
        else item
        for item in acted
    ]
    return {
        "exit_code": exit_code,
        "mode": mode,
        "dry_run": False,
        "candidates": [],
        "acted": wire_safe_acted,
        "skipped": skipped,
        "failed": failed,
    }


# ---------------------------------------------------------------------------
# Worktree root derivation (Key Decision 5)
# ---------------------------------------------------------------------------

# main_worktree_root was RELOCATED (2026-07-31) to coordinator_core.lifecycle, placed
# adjacent to git_common_dir (its exact inverse).  Re-exported here unchanged so every
# existing `from coordinator_core.ops.fleet._common import main_worktree_root` stays
# valid and there is still exactly ONE implementation.  A hook importing from ops/fleet/
# would be a layering violation — no hook imports ops/** today, and this relocation
# must not set that precedent.  See lifecycle.main_worktree_root for the
# NEGATIVE-SPEC / derivation rationale.


# ---------------------------------------------------------------------------
# D3 repo_root consistency check (contract §3.3 :301-318)
# ---------------------------------------------------------------------------

def check_repo_root(
    param_root: Optional[str],
    common_dir_arg: Path,
) -> Optional[str]:
    """D3 consistency check: if params.repo_root is provided, validate it resolves
    to the same git common dir as the handler's engine-supplied common_dir_arg.

    Returns None on success (no mismatch, or param_root not provided).
    Returns a human-readable reason string on genuine mismatch — callers should
    call build_setup_error_result(mode, dry_run, reason) to build the wire error.

    Canonicalization: both sides are Path.resolve()-ed before comparison so that
    cosmetic differences (trailing slash, /var↔/private/var on macOS, symlinks)
    are NOT treated as a mismatch — only a genuine different-repo path triggers
    exit_code:1.

    NEGATIVE-SPEC:
    - params.repo_root is NEVER the worktree-root resolution source.  The socket
      selects the repo; the handler derives the worktree via main_worktree_root(common_dir_arg).
      repo_root is an optional caller-side consistency assertion only (contract §3.3).
    - A mismatch here means the caller's stated repo and the socket's repo differ —
      fail-closed with exit_code:1 reason:"repo_root-mismatch".  Never silently proceed.
    """
    if param_root is None:
        return None  # optional param absent — no check required

    try:
        candidate_common = git_common_dir(Path(param_root).resolve())
    except (RuntimeError, OSError) as exc:
        return (
            f"repo_root-mismatch: could not resolve git_common_dir for "
            f"params.repo_root={param_root!r}: {exc}"
        )

    # Both sides are already .resolve()-ed (git_common_dir returns an absolute path;
    # common_dir_arg arrives resolved from the engine's resolve_op_repo_key).
    # Compare as resolved Paths so symlink / macOS /var↔/private/var differences vanish.
    if candidate_common.resolve() != common_dir_arg.resolve():
        return (
            f"repo_root-mismatch: params.repo_root resolves to common_dir "
            f"{candidate_common} but socket-authoritative common_dir is "
            f"{common_dir_arg} — the socket selects the repo; "
            f"params.repo_root cannot override it"
        )

    return None  # consistent


# ---------------------------------------------------------------------------
# Shared handoffs filesystem helpers — live-scanner + archive-dest
# ---------------------------------------------------------------------------
# Review: code-reviewer F1/F2 — extracted from identical definitions in
# archive_handoffs (C1) and archive_shipped_handoffs (C2); single home
# prevents divergence when handoffs directory or archive destination convention changes.


# rel_id was PROMOTED (2026-07-20) to coordinator_core.wire_paths — the same
# native-separator wire defect spans ops/handoff_*, ops/records_query,
# distill/delete_guard, percolate/*, session_ledger/*, none of which can sanely
# import a private module of this subpackage.  Re-exported here unchanged so every
# existing `from coordinator_core.ops.fleet._common import rel_id` stays valid and
# there is still exactly ONE implementation.  See wire_paths.rel_id for the
# contract §2.1 :209-215 rationale.


# Fixed search order for "where might a swept/archived handoff live" — first
# match wins where a caller does an rglob search (resolve_swept_baton.py); a
# fixed set of allowed containment roots where a caller does a containment
# check (handoff_archive_transition.py's mode="supersede" archived-target
# widening). LIFTED HERE (2026-07-28, d6-archived-predecessor fix) from
# resolve_swept_baton.py's own module-local `_ARCHIVE_SUBDIRS` — that module
# now imports this constant rather than defining a second copy; a third copy
# anywhere else is a defect, not a convenience. All three are relative to the
# resolved worktree root.
ARCHIVE_ROOT_SUBDIRS: Tuple[str, ...] = (
    "cross-repo/archive",
    "archive/handoffs",
    "archive/completed",
)


def collect_live_handoff_paths(worktree_root: Path) -> List[Path]:
    """Return sorted absolute paths for all live handoffs in state/handoffs/*.md.

    Named export shared by archive_handoffs (C1) and archive_shipped_handoffs (C2).

    Raises OSError when state/handoffs/ exists but cannot be enumerated (e.g.
    permission-denied) — uses iterdir(), NOT glob("*.md"): Path.glob()'s
    selector silently swallows PermissionError while walking (verified:
    unreadable dir -> glob() yields an empty iterator, no exception), which
    made the previous bare `except OSError: return []` here dead code for the
    exact permission-denied case it existed to guard (mirrors roadmap_dag.py's
    identical fix). Callers MUST catch OSError and degrade to "no live
    handoffs visible this call" (skip archival for anything depending on this
    enumeration) — never let it crash the caller, and never conflate "scan
    raised" with "directory genuinely empty".
    """
    handoff_dir = worktree_root / "state" / "handoffs"
    if not handoff_dir.is_dir():
        return []
    try:
        entries = list(handoff_dir.iterdir())
    except OSError as exc:
        _LOG.warning(
            "collect_live_handoff_paths: cannot scan %s — %s", handoff_dir, exc,
        )
        raise
    return sorted(p.resolve() for p in entries if p.suffix == ".md" and p.is_file())


# ---------------------------------------------------------------------------
# Claim-dir path convention — SINGLE shared source of truth (code-reviewer F1,
# 2026-07-14 claim-lock-liveness slice1 review)
# ---------------------------------------------------------------------------
#
# Review: code-reviewer — session.reap._reap_orphaned_claims and
# archive_handoffs._is_terminal Check 4 must agree on the SAME claim-dir path
# convention (<common_dir>/coordinator-sessions/<claim-class>-claims/<name>) or
# a future rename silently no-ops one side (the plan's own warning). Hoisted
# here so both ops/session/reap.py and ops/fleet/archive_handoffs.py import
# the same constants/helper instead of re-literaling the path segments.
# coordinator_core.ops.session.reap re-exports _CLAIM_SUBDIRS/_sessions_dir
# from this module for backward-compatible test imports.

# Claim class subdirectory names (mirrors cs_reap_stale_claims no-arg coverage).
_CLAIM_SUBDIRS: tuple = ("handoff-claims", "memo-claims", "plan-claims")

# `_sessions_dir` / `handoff_claim_dir` re-homed to `coordinator_core.claim_state`
# (2026-08-07, claim-state-ledger-first-authoritative-read plan, C1) — that leaf
# module is now the single source of truth for the handoff-claim-dir
# convention, so a new canonical ledger-first claim accessor can depend on it
# without importing anything under `coordinator_core.ops.*` (see
# `claim_state.py`'s own module docstring for the import-cycle rationale).
# Re-exported here, unchanged in behavior, for this module's existing
# importers (`baton_assemble/apply.py`, `ops/handoff_close_origin_stub.py`,
# `ops/handoff_reconcile.py`, `ops/fleet/archive_handoffs.py`,
# `ops/session/reap.py`, `ops/session/record_pickup.py`).
from coordinator_core.claim_state import _sessions_dir, handoff_claim_dir  # noqa: E402,F401


def plan_claim_dir(common_dir: Path, plan_path: Path) -> Path:
    """Derive the plan-execution claim-lock dir for a given plan path.

    <common_dir>/coordinator-sessions/plan-claims/<stem> — _CLAIM_SUBDIRS[2],
    the SAME key session.reap._reap_orphaned_claims sweeps and
    coordinator_core.session.claims.claim_plan/claim_artifact("plan", slug, ...)
    writes.  Keyed on ``plan_path.stem`` (filename MINUS the ``.md`` suffix),
    NOT the full filename — this is the one divergence from
    ``handoff_claim_dir`` (which keys on the full ``handoff_path.name``):
    plan claims drop the extension to match ``cs_claim_plan``'s slug contract
    (execute-plan/SKILL.md: ``slug="$(basename "$ARGUMENTS" .md)"``) and its
    Python port (``claims.claim_plan(slug, ...)``).  Single source of truth for
    archive_plans.py's claim-liveness guard (T1/T3) — do NOT hand-roll this
    path convention at a second call site (mirrors the handoff_claim_dir
    code-reviewer F1 rationale above).
    """
    return _sessions_dir(common_dir) / _CLAIM_SUBDIRS[2] / plan_path.stem


def handoff_archive_dest(worktree_root: Path, handoff_path: Path) -> Path:
    """Derive archive destination: archive/handoffs/YYYY-MM/<filename>.

    YYYY-MM is extracted from the filename prefix (YYYY-MM-DD-slug.md convention).
    Falls back to archive/handoffs/ (flat) when the filename carries no YYYY-MM-DD prefix.

    Named export shared by archive_handoffs (C1) and archive_shipped_handoffs (C2);
    both ops archive handoffs to the same archive/handoffs/YYYY-MM/<filename> tree.
    """
    stem_parts = handoff_path.name.split("-")
    yyyymm = None
    if len(stem_parts) >= 2:
        try:
            year = int(stem_parts[0])
            month = int(stem_parts[1])
            if 1900 <= year <= 2100 and 1 <= month <= 12:
                yyyymm = f"{year:04d}-{month:02d}"
        except ValueError:
            # Non-numeric prefix -- falls back to the flat archive path below,
            # per the docstring contract above.
            pass
    if yyyymm:
        return worktree_root / "archive" / "handoffs" / yyyymm / handoff_path.name
    return worktree_root / "archive" / "handoffs" / handoff_path.name


# ---------------------------------------------------------------------------
# Frontmatter-status helper (reuses dag._read_meta cached parse)
# ---------------------------------------------------------------------------

def parse_frontmatter_field(path: Path, key: str) -> Optional[str]:
    """Return `key`'s value from YAML frontmatter, or None if absent/unreadable.

    The fence-tolerant primitive every field reader in this package routes
    through. `parse_frontmatter_status` is `key="status"`; see its docstring
    for the caching contract and for why the unfenced fallback exists.
    """
    meta = _read_meta(str(path))
    if meta:
        return meta.get(key)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if text.startswith("---"):
        return None
    return read_fm_field_unquoted(text[:4096], key)


def parse_frontmatter_status(path: Path) -> Optional[str]:
    """Return the 'status' value from YAML frontmatter, or None if absent/unreadable.

    Delegates to coordinator_core.dag._read_meta (cached by content-hash sha256;
    dag.py:367-405) — re-keyed from mtime to content hash in C3 to eliminate
    TOCTOU between stat and read.  I/O cost: one file read per cache miss.

    Returns None gracefully on any I/O or parse error (dag._read_meta contract).

    UNFENCED FALLBACK: `_read_meta` parses MARKDOWN frontmatter and returns `{}`
    for any file that does not open with a `---` fence. Sizing-objects are bare
    YAML documents with no fence, so every one of them read as `status: None` —
    which made `fleet.archive_terminal_sizings` a permanent no-op that reported
    a clean zero-candidate sweep while 81 terminal records accumulated in
    `state/sizings/` (measured on this corpus, 2026-08-26). The fence-tolerant
    read below is the same one `session_facts._read_frontmatter_status` already
    used to count those 81 correctly; the two readers disagreeing is what hid
    the defect, since the surface that REPORTS the backlog and the op that
    CLEARS it did not share a status reader.

    NEGATIVE SPEC: the fallback fires only when there is no fence to parse. A
    fenced file's status still comes from `_read_meta` alone -- including a
    fenced file whose frontmatter genuinely carries no `status:` -- so nothing
    changes for plans, handoffs, or any other markdown record.
    """
    return parse_frontmatter_field(path, "status")


def _mkdir_and_track_created(dst_parent: Path) -> List[Path]:
    """mkdir(parents=True) for dst_parent, returning the ancestor dirs it newly
    created (deepest-first), or [] if dst_parent already existed.

    archive_and_commit (C9 fix, 2026-08-05) must create dst.parent BEFORE the
    git mv (git mv will not create directories), but a fully- or
    partially-failed move must not leave that freshly-created, still-empty
    directory tree behind — observed residue: zero files archived, empty
    destination dirs left on disk, which then (a) trips the dest.exists()
    collision guard on retry and (b) makes _find_existing_archive's glob
    falsely report already_archived:true over zero committed files. Returning
    the created chain (rather than a bool) lets the caller remove exactly the
    directories THIS call created, in the correct deepest-first rmdir order,
    without touching any pre-existing ancestor or a sibling move's directory.
    """
    created: List[Path] = []
    p = dst_parent
    while not p.exists():
        created.append(p)
        p = p.parent
    dst_parent.mkdir(parents=True, exist_ok=True)
    return created


def _cleanup_created_dirs(created: List[Path]) -> None:
    """Remove directories _mkdir_and_track_created created, deepest-first,
    stopping at the first non-empty one (a later move may have populated a
    shared ancestor, or the dir may already have unrelated content — either
    way, correctly a stop condition, not an error).

    Never raises — this is best-effort residue cleanup on a failure path, not
    a mechanism the archival result depends on. Called for a move's dirs the
    instant that move fails to land content at dst (immediate git-mv failure)
    AND, separately, for every acted move's dirs when the batch commit itself
    fails and dst is rename-reversed back to src (see the commit-failure
    branch below) — both are "no content ended up at dst" shapes, and both
    must leave dst.parent's newly-created chain exactly as it was pre-call.
    """
    for d in created:
        try:
            d.rmdir()
        except OSError:
            break


# ---------------------------------------------------------------------------
# async archive_and_commit — DR-211 D3/D4 git helper (Key Decision 2)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Post-commit main-index resync — extracted seam (C4a, 2026-08-11)
# ---------------------------------------------------------------------------
#
# Lifted out of archive_and_commit / rm_and_commit's post-commit tail so a
# test can inject a fake `run_git` and assert on the argv sequence with no
# subprocess at all. Roughly six inline asyncio.create_subprocess_exec calls
# (git mv/rm, stage, commit, reset, commit-verify) run UPSTREAM of the resync
# inside each caller, so the resync was not reachable from a test that only
# fakes its own git calls without a brittle module-wide create_subprocess_exec
# patch — see AC5 in docs/plans/2026-08-11-resync-leaves-a-bare-staged-deletion-whe.md.
# Pure extraction: same git commands, same order, same arguments, same retry
# semantics via _update_index_with_retry, same `index_resync_failed`
# per-item annotation, same _LOG.error on persistent failure. `run_git` is
# keyword-only and defaults to the real retry-wrapped runner, so every
# existing caller is unchanged — only a test passes a fake.


def _persist_index_resync_failure(
    *,
    worktree_root: Path,
    candidate_id: str,
    reason: str,
    op_label: str,
) -> None:
    """Give the `index_resync_failed` annotation a durable, queryable sink.

    2026-08-11 fix (tasks/2026-08-11-resync-annotation-sink/SPEC.md). Step 1
    of that spec established BY EXECUTION (not a code read) that the
    `index_resync_failed` annotation this function's two call sites already
    set on the acted[]/reaped[] item is otherwise ephemeral: `build_act_result`
    strips it from every fleet.* op's wire response before it reaches a caller
    (WIRE-SAFETY comment above), and session.boot_sweep's own
    `_index_resync_warnings` folding of it into `warnings[]` is itself only
    ever returned over one JSON-RPC call, never written to disk anywhere in
    the composite-sweep handler. Nothing in either path persists it — the
    `_LOG.error` call immediately above each of this function's two callers
    reaches, at best, THIS process's stderr (see this module's `_setup_error`
    docstring for why that guarantee stops at the process boundary).

    Sink choice: one `bug-backlog` record via
    `coordinator_core.ops.queue_append.append_queue_entry`, called in-process
    (no queue.append IPC round-trip, no schema-cli.js/Node subprocess —
    `append_queue_entry` validates via the in-process
    `coordinator_core.frontmatter.schema_validate` bridge and writes a plain
    YAML file with no git shell-out of its own). Rejected alternatives, in
    the order Step 1 surfaced them:
      - `tasks/orphan-sweep-notes.md` (boot_sweep's own `_append_warn_marker` /
        WARN-marker mechanism) — inspected directly
        (`coordinator_core/ops/session/rotate_orphan_sweep_log.py` module
        docstring: "does NOT preserve the tail anywhere on disk ... the
        caller ... has already read/consumed them by contract"). Read-and-
        rotate by design: `/workday-start` Step 0.8 consumes it once and
        `session.rotate_orphan_sweep_log` truncates it back to a 4-line
        header. Fails constraint 2 (queryable by a LATER reader) outright,
        and is scoped to a different domain (consumed-handoff disposition,
        not main-index resync).
      - `review_trail_write.py` / `state/review-trail/` — wrong domain
        entirely (reviewer verdicts against a sha_range); reusing it would
        overload an unrelated schema's meaning.
      - A brand-new bespoke append log (e.g. a fresh JSONL ledger under
        `state/`) — exactly the shape constraint 3 names and forbids ("Not a
        new log file ... If you find yourself adding a log, stop and
        reconsider").
      - the sovereign-tracker event-log module named in
        docs/decisions/DR-241-sovereign-tracker-substrate-write-carveout.md
        (module name deliberately not spelled out here: the DR-241
        ops-tree referencer-allowlist guard, in coordinator_core's own test
        suite, scans coordinator_core/ops/**/*.py for the bare module-name
        substring unconditionally — docstring mentions included by design,
        unlike its AST-aware top-level-walk sibling — and this module is
        not, and should not become, a DR-241-affirmed referencer: it
        explicitly does not write to that store) — opt-in-by-existence per
        repo (its own module docstring: "a library, not a fleet-wide
        service"), and its negative-spec explicitly says do not grow it
        into a general query surface (DEC-12) or add readers/writers
        outside its sat-01/sat-01b sovereign-tracker contract. A resync
        failure can happen in a repo with no sovereign tracker at all, so
        it cannot be the sink unconditionally.
    `bug-backlog` was chosen because it is the one artifact already carrying
    a wide, existing reader ecosystem (`coordinator_core/ops/records_query.py`,
    the `backlog_grind_assemble/readers_*.py` family, `workday_complete/brief.py`,
    and others — grepped, not assumed) that already answers "what unresolved
    anomalies exist" — the exact retrospective question this sink exists to
    answer for "did a resync fail for path X". `append_queue_entry`'s
    `created_by_agent` parameter is a first-class, schema-declared field
    (`state/bug-backlog/*.yaml`'s optional-field set does NOT include it —
    it lives in the `system:` provenance block instead), i.e. an
    engine-authored bug-backlog record is an already-sanctioned shape, not a
    misuse of a human-curated queue.

    Constraint 6 (a sink-write failure must never fail the archival op, which
    already committed and is authoritative): every exception this raises is
    caught here and degraded to a single `_LOG.error` — this function never
    propagates, by construction. Deferred (function-local) import of
    `coordinator_core.ops.queue_append` — that module imports
    `main_worktree_root` FROM this module (`_common.py`) at its own top
    level, so a module-level import here would be circular.
    """
    try:
        from coordinator_core.ops.queue_append import append_queue_entry

        append_queue_entry(
            "bug-backlog",
            title=f"index-resync-failed: {op_label} {candidate_id}",
            body=(
                f"session.boot_sweep / fleet.* main-index resync exhausted its "
                f"retry budget for candidate_id={candidate_id!r} during "
                f"{op_label}. The archival commit is authoritative and already "
                f"landed; only post-commit `git status` hygiene against the "
                f"main index failed. Reason: {reason}"
            ),
            status="open",
            surface="main-index-resync",
            severity="P3",
            tags=["index-resync-failed", "auto-filed"],
            evidence=reason,
            caller_worktree=worktree_root,
            created_by_agent="coordinator_core.ops.fleet._common",
        )
    except Exception as exc:  # noqa: BLE001 — degrade-safe, never fail the op (constraint 6)
        _LOG.error(
            "_persist_index_resync_failure: could not persist bug-backlog "
            "record for candidate_id=%r (%s) — index_resync_failed annotation "
            "still set on the wire item, but no durable sink was written: %s",
            candidate_id, op_label, exc,
        )


async def _resync_main_index_for_moves(
    moves: List[Move],
    acted_by_id: dict,
    *,
    worktree_root: Path,
    env: dict,
    run_git=_update_index_with_retry,
) -> None:
    """Resync the MAIN index (real .git/index) to the new HEAD for archive_and_commit's moved paths.

    A commit that advances HEAD without touching `.git/index` leaves the main
    index holding stale entries at src paths.  git status --porcelain therefore
    reports orphaned residue (AC4/AC10 clean-index requirement).

    MEASURED 2026-08-26, and stated MECHANISM-INDEPENDENTLY on purpose.  The
    original wording named the private-index commit as the thing that desynced
    the index.  That mechanism is gone -- `dccf2fc01` deleted the private index
    and `_commit_via_head_spine` now lands a direct locked-ref CAS -- and the
    EM on the successor plan reasoned from its absence that this resync had
    become vestigial.  It has not.  The desync source was never the private
    index; it is that HEAD advances and `.git/index` does not, which is true of
    every commit mechanism this function has ever run behind.

    Probed rather than argued, because the vestigial reading was plausible and
    wrong: one real 20-move `restage_src=False` batch with this function stubbed
    to a no-op leaves **40 dirty `git status --porcelain` lines** -- an `RD`
    (rename in index, deleted in worktree) for every src and a `??` for every
    dst.  With this function running: **zero**.  ~50 peer sessions share this
    index and read `git status`, so that residue is theirs, not ours.

    WHAT THIS SPAWN ACTUALLY IS: THE UNFINISHED HALF OF OUR OWN COMMIT.  A
    plain `git commit` updates `.git/index` as part of committing, which is why
    no ordinary caller has to think about this at all.  We do not use
    `git commit`: `_commit_via_head_spine` hand-rolls one -- assemble a tree
    from the spine, CAS the ref -- specifically to avoid git's process cost.
    The index update is the part of git's commit we did not reimplement, so we
    spawn git to do that one leftover piece.  This resync is not an inherent
    cost of committing and not a separate concern; it is the step our own
    commit mechanism stops short of.

    Two conclusions follow, and they are NOT the same conclusion.  (1) Do not
    delete the call site on the argument that the private-index commit is gone:
    that argument is about a mechanism, the requirement outlived it, and the
    40-vs-0 above is what deleting it actually costs the ~50 peers sharing this
    index.  (2) Do not treat it as permanent either.  The correct retirement is
    to FINISH the mechanism -- write `.git/index` back in-process, the way
    git's own commit would have -- which owes `TREE`/`UNTR` extension
    preservation and races `index.lock` against the whole fleet, and so needs
    its own spike before anyone attempts it.  Finish the commit; do not keep
    paying git to clean up after it, and do not simply stop cleaning up.

    Path-scoped index-from-HEAD restore (2026-08-11, C2; batched 2026-08-19,
    amplification burn-down C4) — ONE call for the ENTIRE batch of moved files,
    not one call per move:

        git restore --staged -- <src1> <dst1> <src2> <dst2> ...

    `git restore --staged` accepts an arbitrary pathspec list in one
    invocation, so every relevant move's src/dst pair is folded into a single
    argv rather than spawning once per move — the (items × repos) fan-out this
    fleet-wide helper multiplies is exactly why a per-item spawn here is the
    most expensive shape in the tier. A batch failure is reported against
    every item in the batch (see the failure branch below) rather than
    isolated per move: unlike archive_and_commit's git-mv loop, this resync
    runs strictly AFTER the archival commit has already landed — it is
    best-effort post-commit index hygiene, not a per-item success/failure
    gate, so collapsing per-item retry independence into one batched call
    (still exponential-backoff retried as a unit via `run_git`) does not
    change what the archival op itself committed.

    Both paths converge to what HEAD records: src (which the archival commit
    removed from HEAD) loses its index entry, dst (which HEAD now holds) is
    restored at the COMMITTED blob rather than whatever is currently on disk.

    HISTORY, so the supersession is legible rather than re-derived: this replaced a
    2026-08-05 three-step cacheinfo form (`ls-tree HEAD -- dst`, then
    `update-index --remove -- src`, then `update-index --add --cacheinfo`), which
    itself replaced a plain `--remove`/`--add` pair. The cacheinfo form's `--add`
    half was skipped entirely when the ls-tree lookup returned None, and could fail
    on its own after retries — leaving the index without dst while HEAD held it,
    which reads as a staged deletion of a file present on disk. That is the residue
    this single-call form exists to close. Do not reintroduce either older shape.

    The peer-edit guarantee the cacheinfo form bought is PRESERVED, and for the same
    reason: restoring from HEAD stages dst at the committed blob, not at disk
    content, so a peer's UNCOMMITTED worktree edit at dst stays an unstaged
    modification (git status " M dst") instead of being silently staged under this
    function's commit subject (git status "M  dst"). The pre-2026-08-05 plain
    `--add -- dst` form read disk at resync time and is exactly the gap that could
    stage a peer's edit under our subject line.

    Path-scoped is also why this is safe on a shared index: unrelated content
    already staged in the main index is untouched, whereas a full `git read-tree
    HEAD` would obliterate it — see DR-211 D3 isolation invariant and
    test_archive_and_commit_private_index_isolation.

    UNMEASURED: whether a peer's DELIBERATE pre-archival staging of dst (as opposed
    to an uncommitted worktree edit) survives this resync is outside the spike's
    evidence base — this docstring asserts no specific outcome for that case. Note
    the question is inherited from the cacheinfo form and was not re-opened by the
    move to `restore --staged`; both stage dst from HEAD, not from disk.

    Origin: state/lessons/2026-08-03-an-interrupted-git-mv-leaves-the-shared-
    907008cbcb3c.yaml prescribes an index-only, path-scoped restoration from HEAD
    (`git restore --staged`) as the MANUAL remedy for exactly this residue shape;
    this now converts that lesson into engine behaviour via the lesson's own
    literal invocation — a single `git restore --staged -- <src> <dst>` covering
    BOTH paths in one call, rather than the two-step `--remove` / `--cacheinfo`
    approximation this comment used to describe. See also DoE-claude coordinator/docs/wiki/
    concurrent-em-hazards.md and coordinator/docs/wiki/scoped-safety-commits.md
    for "staged = claimed, unstaged = contestable" on a shared tree — the framing
    this resync's guarantee is built to preserve.

    Runs WITHOUT GIT_INDEX_FILE so operations target the real .git/index.
    The restore is retried with exponential backoff to ride out transient
    index.lock contention — see _update_index_with_retry / _INDEX_RETRY_*
    for the empirical sizing (Review: code-reviewer — F4: non-fatal but
    loud on persistent failure, AC10). Non-fatal to the already-committed
    archival (commit is authoritative) — but NOT silent: exhaustion is
    annotated onto the acted[] item as `index_resync_failed` (additive
    key — absent on the success path, so a consumer reading only
    `{id, archived: true}` per the frozen contract shape is unaffected) in
    addition to the daemon-side _LOG.error, closing the "log line nobody
    reads" gap that let the 2026-08-01/02 incident's residue sit unnoticed
    across two sweeps.

    Mutates each acted_by_id[move.candidate_id] item in place, adding
    `index_resync_failed` only on persistent failure. Does not return a value.
    """
    import asyncio

    relevant_moves = [m for m in moves if m.candidate_id in acted_by_id]
    if not relevant_moves:
        return

    # Review: code-reviewer F1 — see this function's docstring
    # ("Path-scoped index-from-HEAD restore") for the full rationale on
    # why a single `git restore --staged` over both paths preserves the
    # unconditional-on-lookup guarantee atomically. Batched 2026-08-19
    # (amplification burn-down C4): every relevant move's src/dst pair is
    # folded into ONE argv covering the whole batch, not one call per move.
    #
    # `--staged` keeps this index-only: it must NOT touch the
    # worktree. `boot_sweep._commit_consumed_metadata` makes a second,
    # explicit-pathspec commit that reads worktree content for dst;
    # a worktree-touching restore would destroy the stamps that
    # commit is about to read. `git restore --staged -- <path>`
    # resolves its pathspec against cwd like any other `-- <path>`
    # form here, so (unlike `--cacheinfo`'s legacy 3-arg path slot)
    # no relativization is needed.
    # Batched, but BOUNDED (2026-08-21): one spawn per argv chunk, still never
    # one per move. Windows caps a command line at 32767 characters and these
    # are absolute paths, so an unbounded batch is not merely large but
    # unrunnable — a 169-rename sweep died `FileNotFoundError: [WinError 206]`
    # here, after the archival commit had landed, leaving exactly the staged
    # residue this function exists to clear. See `_ARGV_PATHSPEC_BUDGET`; the
    # sibling drift-check and restage spawns are chunked the same way.
    #
    # A chunk that fails is annotated against the moves IT covered, not the
    # whole batch, and the remaining chunks still run: each chunk is
    # independent index hygiene over a disjoint path set, so stopping early
    # would leave residue nothing else clears.
    for chunk in _argv_group_chunks(
        [(m, (str(m.src), str(m.dst))) for m in relevant_moves]
    ):
        argv = ["git", "restore", "--staged", "--"]
        for _move, tokens in chunk:
            argv.extend(tokens)

        restore_err = await run_git(argv, cwd=worktree_root, env=env)
        if restore_err is None:
            continue

        reason = f"restore-staged-failed: {restore_err}"
        _LOG.error(
            "archive_and_commit: main-index resync FAILED after %d attempts"
            " for batch of %d move(s): %s (main index may be dirty — AC10)",
            _INDEX_RETRY_MAX_ATTEMPTS, len(chunk), reason,
        )
        for move, _tokens in chunk:
            item = acted_by_id.get(move.candidate_id)
            if item is not None:
                item["index_resync_failed"] = reason
            await asyncio.to_thread(
                _persist_index_resync_failure,
                worktree_root=worktree_root,
                candidate_id=move.candidate_id,
                reason=reason,
                op_label="archive_and_commit",
            )


async def _resync_main_index_for_reaps(
    paths: List[Path],
    reaped_by_id: dict,
    *,
    worktree_root: Path,
    env: dict,
    run_git=_update_index_with_retry,
) -> None:
    """Resync the MAIN index for rm_and_commit's reaped paths — --remove only, no --add
    counterpart (the file is deleted, there is nothing to re-add). Same
    exponential-backoff retry pattern as _resync_main_index_for_moves (see
    _update_index_with_retry / _INDEX_RETRY_* for empirical sizing); non-fatal
    but loud on persistent failure, and — same as the moves resync — NOT
    silent: exhaustion is annotated onto the reaped[] item as
    `index_resync_failed` (additive key, absent on success) in addition to the
    daemon-side _LOG.error.

    See rm_and_commit's docstring INVARIANT note (2026-08-11, DR-211 residue
    audit C3) for why this --remove-only shape cannot reproduce
    archive_and_commit's D + ?? residue shape — that reasoning is about THIS
    call site's safety and stays attached to rm_and_commit's own docstring,
    not here. That borrowed argument depends on a precondition specific to
    rm_and_commit: its pre-commit plain `git rm` has already deleted the
    worktree file before this resync runs. A future second caller of this
    function must hold that same precondition (or re-derive its own
    residue-shape safety argument) — it does not carry over automatically
    just because the call shape matches.

    Mutates each reaped_by_id[candidate_id] item in place, adding
    `index_resync_failed` only on persistent failure. Does not return a value.

    Batched 2026-08-19 (amplification burn-down C4): every relevant path is
    folded into ONE `git update-index --remove -- <path1> <path2> ...` call
    covering the whole batch, not one call per path — `update-index` accepts
    an arbitrary pathspec list in a single invocation.  A batch failure is
    reported against every relevant item (same reason string) rather than
    isolated per path: like the moves resync above, this runs strictly AFTER
    the reap commit has already landed, so it is best-effort post-commit
    index hygiene, not a per-item success/failure gate.
    """
    relevant: List[Tuple[Path, str]] = []
    for path in paths:
        # rel_id raises ValueError for a path outside worktree_root. For
        # rm_and_commit (the only current caller), this can't actually fire:
        # `paths` was already reassigned to the pre-filtered `valid_paths`
        # before this function is called, and out-of-root paths were routed
        # into `pre_failed[]` upstream — see rm_and_commit's `_rel_id`
        # closure (code-reviewer slice1 F1). The try/except here is
        # defensive, not load-bearing, for that caller. It IS load-bearing
        # for any FUTURE caller that skips an equivalent upstream filter: an
        # uncaught raise would sink the whole batch, and — unlike
        # rm_and_commit's pre_failed[] accounting — a path swallowed here
        # gets no failed[]/reaped[] record at all. A new caller passing
        # unfiltered paths must do its own upstream filtering; this guard
        # alone does not provide that accounting.
        try:
            candidate_id = rel_id(path, worktree_root)
        except ValueError:
            continue
        if candidate_id not in reaped_by_id:
            continue
        relevant.append((path, candidate_id))

    if not relevant:
        return

    # Bounded batching — see the same block in _resync_main_index_for_moves for
    # why an unbounded argv is unrunnable on Windows, and why a failed chunk
    # neither sinks the remaining chunks nor is attributed beyond its own paths.
    for chunk in _argv_group_chunks(
        [((path, candidate_id), (str(path),)) for path, candidate_id in relevant]
    ):
        argv = ["git", "update-index", "--remove", "--"]
        for _payload, tokens in chunk:
            argv.extend(tokens)
        remove_err = await run_git(argv, cwd=worktree_root, env=env)
        if remove_err is None:
            continue
        await _annotate_reap_resync_failure(
            [payload for payload, _tokens in chunk],
            reaped_by_id,
            remove_err=remove_err,
            worktree_root=worktree_root,
        )


async def _annotate_reap_resync_failure(
    relevant: List[Tuple[Path, str]],
    reaped_by_id: dict,
    *,
    remove_err: str,
    worktree_root: Path,
) -> None:
    """Record a persistently-failed reap index-resync against every path it covered.

    Split out of `_resync_main_index_for_reaps` when that call was chunked: the
    annotation is now per-chunk rather than per-batch, and inlining it in the
    chunk loop buried the loop's control flow.
    """
    import asyncio

    reason = f"remove-failed: {remove_err}"
    _LOG.error(
        "rm_and_commit: main-index resync remove FAILED after %d attempts"
        " for batch of %d path(s): %s (main index may be dirty — AC10)",
        _INDEX_RETRY_MAX_ATTEMPTS, len(relevant), reason,
    )
    for path, candidate_id in relevant:
        item = reaped_by_id.get(candidate_id)
        if item is not None:
            item["index_resync_failed"] = reason
        await asyncio.to_thread(
            _persist_index_resync_failure,
            worktree_root=worktree_root,
            candidate_id=candidate_id,
            reason=reason,
            op_label="rm_and_commit",
        )


#: Per-spawn argv budget, in characters, for a batched `-- <paths>` pathspec.
#: Windows caps a CreateProcess command line at 32767 characters TOTAL, and the
#: batched spawns below pass ABSOLUTE paths, so the cap is reachable on a large
#: sweep: 335 memos measured at 35,468 characters, overflowing with
#: `FileNotFoundError: [WinError 206]`. The headroom under 32767 covers the
#: git.exe path, the subcommand and flags, and the environment block. Same cap
#: and same rationale as `coordinator/bin/scoped-git-commit`'s
#: `--pathspec-from-file`, which solved this for the commit path.
_ARGV_PATHSPEC_BUDGET = 24000


def _argv_group_chunks(groups: list, budget: int = _ARGV_PATHSPEC_BUDGET) -> list:
    """Split `(payload, tokens)` groups into batches each short enough for one spawn.

    A group's tokens are never split across two chunks: the index resyncs pass a
    move's src AND dst in one pathspec, and a chunk that carried only half of a
    pair would resync half a rename -- the exact residue shape
    `_resync_main_index_for_moves` exists to close. Keeping the pair whole also
    makes a chunk failure attributable to exactly the items that chunk covered.

    A single group longer than `budget` still gets its own chunk rather than
    being dropped -- an over-long pathspec is git's error to report, not this
    helper's to swallow.
    """
    chunks: list = []
    current: list = []
    size = 0
    for group in groups:
        _payload, tokens = group
        span = sum(len(t) + 1 for t in tokens)
        if current and size + span > budget:
            chunks.append(current)
            current, size = [], 0
        current.append(group)
        size += span
    if current:
        chunks.append(current)
    return chunks


def _argv_path_chunks(paths: list, budget: int = _ARGV_PATHSPEC_BUDGET) -> list:
    """Split `paths` into groups each short enough to survive one spawn.

    Chunking is semantics-preserving for all three call sites: the drift check
    unions its per-chunk output into one set that is then membership-tested per
    move, and the restage add / reset are all-or-error over the same path set
    either way.
    """
    return [
        [tokens[0] for _payload, tokens in chunk]
        for chunk in _argv_group_chunks([(p, (str(p),)) for p in paths], budget)
    ]


def _assembled_commit_is_noop(
    spine: Mapping[str, Mapping[str, Tuple[int, str]]],
    assembled: Mapping[str, Union[Tuple[int, str], object]],
) -> bool:
    """True iff every entry in `assembled` already matches HEAD's tree --
    the RE-SITED form, for `archive_and_commit`'s assembled-dict commit path,
    of the guarantee `_empty_private_index_breach` (above) provides for a
    private-index seam.

    Read `_empty_private_index_breach`'s own docstring first -- it exists
    because `git write-tree` against a MISSING `GIT_INDEX_FILE` silently
    returns git's canonical empty tree at rc=0, and a pathspec-less commit of
    that tree deletes every tracked file (the 2026-08-18 `fbfbd061d`
    incident). `archive_and_commit` no longer opens a `GIT_INDEX_FILE` at
    all -- there is no private index left to go missing, so that EXACT
    failure mode cannot recur here, and this function does NOT reuse
    `_empty_private_index_breach` (which still guards `rm_and_commit`'s own
    private-index seam, unchanged, below).

    What survives, re-sited to this mechanism's own shape: a commit whose
    assembled tree would land byte-identical to HEAD's tree is the same
    "nothing to commit" case reached a different way -- not the empty-TREE
    case (`assembled` is never empty here; `archive_and_commit` returns
    before this point whenever `acted` is empty), but the empty-DELTA case,
    where every assembled change already matches what HEAD already records.
    Refusing it here, loud and before the commit, is the same discipline the
    original guard applied to its own failure mode: never land a no-op
    archival commit silently.

    TAKES THE SPINE, DOES NOT READ ONE (2026-08-27). This function used to
    call `read_tree_spine(worktree_root, assembled.keys())` itself, which
    made it the SECOND full walk of HEAD's tree inside a single
    `archive_and_commit` -- its caller had already walked the same tree for
    the src paths a few lines earlier. The two walks were cheap in a warm
    process (`git_objects`' pack caches serve the second one from memory)
    and that is not a reason to make them: caching a redundant call is not
    the same as not making it. The caller now walks once over src UNION dst
    and passes the result here, so the second walk and this function's own
    `worktree_root` parameter are both gone.

    `spine` is what `read_tree_spine` returned for a path set covering every
    key in `assembled` -- a SUPERSET is fine (lookups are by key), a subset
    is a caller bug that reads as "entry absent from HEAD". A `None` spine
    is the caller's to refuse before calling: `archive_and_commit` already
    branches on `head_spine is None` into `spine_error` and never reaches
    this guard.
    """
    for path, val in assembled.items():
        parent, _, leaf = path.rpartition("/")
        existing = spine.get(parent, {}).get(leaf)
        if val is _ABSENT:
            if existing is not None:
                return False  # a real deletion
            continue
        if existing != val:
            return False  # a real add/change
    return True


async def archive_and_commit(
    worktree_root: Path,
    moves: List[Move],
    subject: str,
) -> Tuple[List[dict], List[dict]]:
    """DR-211 D3/D4 git-archive helper: os.replace each Move in-process, then
    land the whole batch as ONE commit via `_commit_via_head_spine` (imported
    from `ops.ceremony.git_native` — peer-to-peer import, deliberate; see the
    module-level import comment above). (F-5 swap, 2026-08-21; private-index
    dance retired in favour of `_commit_via_head_spine`, 2026-08-26.)

    `git mv` against a private HEAD-seeded GIT_INDEX_FILE used to be the ONE
    genuinely per-item spawn here — 68.65ms/file measured (F-5), against
    0.33ms/file for `os.replace` (209x). The archival mover now does the
    rename itself, in-process. Staging and committing no longer go through a
    private `GIT_INDEX_FILE` at all (that dance — `git read-tree HEAD`, a
    batched `git add -- src dst`, `git write-tree`, `git commit-tree`, `git
    update-ref` — is GONE): the batch's `{path: (mode, sha) | _ABSENT}`
    tree-delta is assembled directly, in process, and handed to
    `_commit_via_head_spine`, which rewrites HEAD's tree spine, builds the
    commit object, and lands it via a locked `cas_ref` compare-and-swap.
    An all-`restage_src=False` batch issues ZERO git processes of its own
    (C1 of docs/plans/2026-08-26-the-archival-seam-stops-asking-git-at-all.md,
    2026-08-26): such a move is a pure rename of bytes already in the object
    store, so BOTH `head_entry[0]` (mode) and `head_entry[1]` (blob sha) come
    straight from `read_tree_spine`, spawn-free. The only git spawn left
    anywhere in this build is one batched `git hash-object -w --stdin-paths`
    call, over the `restage_src=True` subset ONLY (never called at all when
    that subset is empty) — for each such move, dst's blob sha comes from
    hashing dst's CURRENT on-disk content (never HEAD's — its content was
    authored fresh on disk just before this call and may legitimately differ
    from HEAD's own blob for src); dst's MODE is still inherited from HEAD's
    entry for src either way, read spawn-free via `read_tree_spine`, which
    PRESERVES an executable bit (`100755`) HEAD already recorded — a plain
    `git add` staging pass would silently normalise that bit away under
    `core.filemode=false` (AC-5). src is marked `_ABSENT` (deleted). A
    `restage_src=False` src with no HEAD tree entry at all is refused
    (`untracked-at-head`, in failed[]) rather than defaulted or hashed — see
    the assembled-tree-build comment below.

    For each Move:
    1. Ensures dst.parent exists (os.replace will not create directories).
    2. If move.force is False and dst already exists: fails the move
       (`dst-exists`) rather than calling os.replace — os.replace has no
       "fail if dst exists" mode (unlike `git mv` without `-f`, which does),
       so that contract is enforced explicitly here.
    3. Calls `os.replace(src, dst)` — one atomic, in-process syscall, no
       subprocess. On OSError: item lands in failed[] (`replace-failed`);
       processing continues for remaining moves. os.replace has no
       split-failure mode (unlike `git mv`, which could rename on disk and
       then fail its own index update) — it either lands whole or raises.

    This structurally removes the STALE-BLOB hazard (b3e61bd00) that the old
    `git mv`-against-a-stale-index path required a guard for: `git mv`
    re-keyed whichever blob the read-tree-HEAD-seeded private index held for
    src (src's LAST-COMMITTED content), not what was on disk — os.replace
    always moves current on-disk bytes, so there is no stale blob to re-key.

    That is NOT the whole of what the drift guard used to do (2026-08-21 F-5
    through 2026-08-25): it also refused to archive a candidate whose disk
    content had diverged from HEAD but was never committed. The C1 chunk of
    docs/plans/2026-08-26-the-archival-seam-stops-asking-git-at-all.md
    RETIRED that refusal outright, by PM ruling, rather than re-siting it:

        PM RULING, 2026-08-26 — "Archival moves can assume no peer touches a
        terminal handoff between the sweep and the commit — yes, especially
        if the op is meant to run subsecond." (PM, verbatim.) The candidates
        are already terminal: claimed, closed, selected by a sweep that
        refuses live-claimed records — a peer editing a closed record inside
        a one-call window is not a shape this fleet produces, and the
        sweep's own live-claim refusal is the check that actually covers it.
        This is a DIFFERENT guard from the HEAD-race CAS inside
        `_commit_via_head_spine` (b4f0bfe88) — that CAS catches a peer
        COMMIT landing mid-sweep and is untouched by this ruling; the drift
        gate caught src's BYTES changing on disk mid-sweep, with no
        incident ever behind it. No stat-based or in-process substitute
        replaces it — reintroducing one re-opens the fork
        `git_state.py`'s "THE WORKTREE HASH DOES NOT WORK" comment closes.

    After all moves: the `assembled` tree-delta built directly from the acted
    moves (see above) IS the exact scope of the commit — an explicitly-keyed
    src maps to `_ABSENT` (deletion), dst maps to `(mode, sha)` (addition), so
    this one dict captures both halves of every rename. HARD CONSTRAINT: a
    move left out of `assembled` (i.e. not in `acted`) contributes nothing —
    there is no wildcard or implicit scope the way `git add -A` would be. If
    building `assembled` fails (HEAD's spine unresolvable, the batched
    `git hash-object` spawn fails, or its output does not align one-to-one
    with the acted moves): every acted move (os.replace already landed on
    disk) is reversed via `Path.rename` back to src and reclassified to
    failed[] — the one post-move split-failure point os.replace itself
    cannot produce.

    Then ONE call to `_commit_via_head_spine` lands `assembled` with NO
    trailing pathspec — the dict built above is already the exact scope of
    the commit; a `-- <paths>` pathspec on a `git commit`/`commit-tree` would
    re-read the WORKTREE for those paths, bypassing the assembled content and
    reopening the FORWARD-B hazard this function exists to close (see below).

    If the commit fails (or `_commit_via_head_spine` declines on a
    precondition, or the re-sited empty-spine-commit guard below refuses):
    all acted items are reversed on disk and reclassified to failed[].

    No private index exists to clean up any more — `_commit_via_head_spine`
    reads no `GIT_INDEX_FILE` and mutates no index, private or shared.

    DOES NOT PUSH (C2, 2026-08-26, PM ruling — auto-push moves to other
    ceremonies and does not belong on this path). A landed commit is pushed
    by whatever pushes next — the installed `post-commit` hook firing on a
    caller's own later `git commit`, a scheduled sync sweep, or another
    ceremony's own push step — never by this function itself. See the
    removal note where `_replay_post_commit_hook` used to be defined, above,
    for the cost this traded away.

    FORWARD-B hazard (DR-211 D3, amended 2026-07-26): a `git commit -- <paths>` call
    commits WORKTREE content for the named paths, not the intended content — so a dirty
    unrelated edit on a swept path could be silently absorbed into this function's commit,
    misattributing that edit to the archival subject line. This is the same
    worktree-absorption mechanism named in
    state/lessons/0000-00-00-commit-only-your-hunk-when-a-sibling-s-u.yaml (universal,
    EM-fan-out-collision context) — not a defect newly diagnosed here. The fix is to
    commit the assembled tree-delta directly with no trailing pathspec (the delta is
    already scoped by construction), never to add a trailing pathspec to a commit
    seam — that reads worktree content for the named paths and reproduces the hazard
    under a different call shape. FORWARD-B is distinct from "the REVERSE residual"
    (a different, more tolerable hazard: a concurrent op absorbing OUR staged paths,
    not us absorbing a foreign worktree edit) — do not conflate the two when
    reasoning about this fix.

    Assembling dst's blob from a fresh `git hash-object -w --stdin-paths` of
    dst's on-disk content does NOT reopen FORWARD-B: dst's content at hash
    time IS this function's own os.replace output (content this call itself
    just placed there), not a third party's worktree edit absorbed from
    behind our back — the hazard FORWARD-B names is a foreign edit landing on
    a path we did not just author, and hashing our own just-completed rename
    is the opposite of that.

    NEGATIVE-SPEC:
    - NEVER uses git add -A, git add ., or any private-index staging at all —
      the assembled tree-delta is scoped by construction, keyed exactly to
      the acted moves' src and dst, never a wildcard.
    - NEVER commits with a trailing `-- <paths>` pathspec — that reads WORKTREE
      content and reopens FORWARD-B (see above). The assembled dict IS the pathspec.
    - NEVER uses blocking subprocess.run for the remaining git spawn (`git
      hash-object`) — routed through `_hash_object_stdin_paths`, itself
      `asyncio`-safe (DR-211 D4 async mandate; ipc.py:80 "single asyncio
      event loop").
    - NEVER uses `git mv` for the rename itself — os.replace is the mover;
      rename detection at diff time is by content similarity and needs no
      index hint. See F-5.
    - NEVER opens a `GIT_INDEX_FILE` — `_commit_via_head_spine` reads and
      mutates no index, private or shared, at all.
    - NEVER hashes a blob in-process (e.g. a hand-rolled `sha1("blob "+len+
      "\0"+content)`) to avoid the remaining `git hash-object` spawn —
      `core.autocrlf`/`core.filemode`/smudge filters make an in-process
      worktree hash wrong for a meaningful fraction of paths on this repo;
      see `git_state.py`'s "THE WORKTREE HASH DOES NOT WORK" comment. C1 of
      docs/plans/2026-08-26-the-archival-seam-stops-asking-git-at-all.md
      reaches zero spawns by having NOTHING TO HASH for a `restage_src=False`
      batch, never by hashing more cheaply.
    - NEVER reintroduces a disk/HEAD drift check (stat-based or otherwise) —
      retired outright by PM ruling, 2026-08-26; see the comment where it
      used to sit, above the per-move os.replace loop.
    - stdout/stderr captured as bytes (not text=True) — decoded with errors="replace" on failure.

    Spec: docs/plans/2026-07-26-memo-disposition-flip-op-and-hand-edit-hole.md (C4);
    docs/plans/2026-08-20-the-close-ceremony-stops-paying-for-the-join.md (C5, F-5 swap)

    Args:
        worktree_root: absolute path to the main git worktree (from main_worktree_root()).
        moves: list of Move(src, dst, candidate_id) — src and dst are absolute paths.
        subject: git commit message subject line.

    Returns:
        (acted, failed) where:
            acted  = [{"id": str, "archived": True}, ...]
            failed = [{"id": str, "reason": str}, ...]
        An acted[] item MAY additionally carry `"index_resync_failed": str` —
        present ONLY when the post-commit main-index resync (see below)
        exhausted its retry budget for that item's src/dst pair. This is an
        ADDITIVE key (absent on the success path), not a change to the frozen
        `{id, archived: true}` shape a v1 consumer expects — see the resync
        block's docstring note for the incident this closes.

    Spec backlinks (main-index resync hardening, 2026-08-02): the fixed
    3-attempt/0.05s-sleep resync retry budget (150ms of sleeping) was
    reliably exhausted by ordinary `.git/index.lock` contention from a
    genuinely concurrent `git commit` on this shared tree, silently leaving
    stale rename residue staged in the MAIN index while HEAD/the commit were
    correct — the root cause of three memos resurrecting from archive/ back
    into a re-deliverable staged state across 2026-08-01/02. See
    _update_index_with_retry / _INDEX_RETRY_* for the exponential-backoff
    replacement and its empirical sizing.
    """
    # asyncio deferred to first use here (not module scope) — this module is imported by
    # goal_append.py (an eager-loaded op module) and several other sync-only fleet ops;
    # module-scope `import asyncio` dragged asyncio.base_events (~8ms) into every one of
    # those import paths even when they never call archive_and_commit/rm_and_commit.
    # Spec: docs/plans/2026-07-24-canonical-resolution-engine.md task W0-1.
    import asyncio

    try:
        # HEAD-race CAS anchor (b4f0bfe88 etc., 2026-08-18/20): captured once,
        # here, spawn-free (reads .git/HEAD directly) -- and used below both
        # as the commit's parent and as `_commit_via_head_spine`'s CAS
        # `old_head`, so a HEAD that moved since this line is caught, never
        # silently inherited. (The read-tree-HEAD private-index seed this
        # comment used to describe is gone -- see the module-level import
        # note above `_commit_via_head_spine` -- but the race this anchor
        # closes is the same one: a peer commit landing in the os.replace
        # window between here and the commit below must not be silently
        # reverted.)
        old_head = _read_head_sha(worktree_root)
        if old_head is None:
            return [], [
                {
                    "id": m.candidate_id,
                    "reason": "index-init-failed: HEAD has no resolvable commit",
                }
                for m in moves
            ]

        acted: List[dict] = []
        failed: List[dict] = []
        # Per-move ancestor dirs THIS call newly created (C9) — cleaned up on
        # any path where the move ends up NOT landing content at dst (both the
        # immediate-failure branches below and, later, the commit-failure
        # reversal loop for moves that succeeded here but got un-done there).
        created_dirs_by_id: dict = {}

        # ---------------------------------------------------------------
        # NO DISK/HEAD DRIFT CHECK HERE (deliberate absence, C1 of
        # docs/plans/2026-08-26-the-archival-seam-stops-asking-git-at-all.md,
        # 2026-08-26) — this function used to spawn a batched
        # `git diff --name-only HEAD -- <srcs>` at this point, chunked via
        # `_argv_path_chunks`, and refuse any restage_src=False move whose
        # src had uncommitted disk content diverging from HEAD.
        #
        # PM RULING, 2026-08-26 — "Archival moves can assume no peer touches
        # a terminal handoff between the sweep and the commit — yes,
        # especially if the op is meant to run subsecond." (PM, verbatim.)
        # See archive_and_commit's own docstring above for the full ruling
        # text and why the HEAD-race CAS inside `_commit_via_head_spine`
        # (b4f0bfe88) is a DIFFERENT guard, untouched by this removal. No
        # stat-based or in-process substitute replaces this check — do not
        # re-add one; that reopens the fork `git_state.py`'s "THE WORKTREE
        # HASH DOES NOT WORK" comment closes.
        # ---------------------------------------------------------------
        # Per-item move: os.replace, in-process (F-5 swap, 2026-08-21).
        #
        # `git mv` against the private index cost 68.65ms/file (F-5) for
        # what is, on disk, a single rename syscall — 0.33ms via os.replace,
        # a 209x difference. os.replace makes the restage_src STAGING
        # machinery this block used to run (a targeted `git add -- src`
        # before each move's own git mv) moot: that existed solely to make
        # `git mv` re-key src's CURRENT on-disk content instead of the
        # read-tree-HEAD blob, and os.replace always moves current on-disk
        # bytes regardless, so there is nothing left for that staging to do
        # — it is correctly gone (the new unconditional batched `git add --
        # src dst` below stages current on-disk bytes for every move
        # already). There is no disk/HEAD drift check left to run ahead of
        # this loop either — see the removal comment immediately above.
        #
        # os.replace has no split-failure mode (unlike `git mv`, which could
        # rename on disk and then fail the index update) — it is one atomic
        # syscall that either lands or raises. The single remaining
        # post-move split-failure point is the staging step below.
        for move in moves:
            # Create destination directory — os.replace will not mkdir.
            # Tracked so a failed move (this loop), a failed stage (below),
            # or a failed commit (further below) can remove the empty tree
            # it created rather than leaving destination residue.
            created_dirs_by_id[move.candidate_id] = _mkdir_and_track_created(move.dst.parent)

            if not move.force and move.dst.exists():
                # os.replace has no "fail if dst exists" mode (unlike plain
                # `git mv` without `-f`) — it always clobbers. Preserve the
                # force=False fail-on-existing-dst contract explicitly here.
                _cleanup_created_dirs(created_dirs_by_id[move.candidate_id])
                failed.append({
                    "id": move.candidate_id,
                    "reason": "dst-exists: refusing overwrite (force=False)",
                })
                continue

            try:
                os.replace(str(move.src), str(move.dst))
            except OSError as exc:
                _cleanup_created_dirs(created_dirs_by_id[move.candidate_id])
                failed.append({
                    "id": move.candidate_id,
                    "reason": f"replace-failed: {exc}",
                })
                continue

            acted.append({"id": move.candidate_id, "archived": True})

        if not acted:
            return acted, failed

        acted_ids = {a["id"] for a in acted}
        acted_moves = [m for m in moves if m.candidate_id in acted_ids]

        # Build `assembled` -- {path: (mode, sha) | _ABSENT} -- directly, in
        # process: this REPLACES the read-tree-HEAD private index + batched
        # `git add -- src dst` staging dance entirely. `_commit_via_head_spine`
        # (imported from ops.ceremony.git_native, see the module-level import
        # note) lands straight from this dict via a locked ref CAS with ZERO
        # git spawns of its own. A `restage_src=False` move (a rename of
        # bytes already in the object store) needs no spawn at all -- see the
        # PM-ruling block below; the only spawn left anywhere in this build
        # is one batched `git hash-object -w --stdin-paths`, over the
        # `restage_src=True` subset ONLY, and only when that subset is
        # non-empty (C1 of docs/plans/2026-08-26-the-archival-seam-stops-
        # asking-git-at-all.md).
        src_rel_by_id = {m.candidate_id: rel_id(m.src, worktree_root) for m in acted_moves}
        dst_rel_by_id = {m.candidate_id: rel_id(m.dst, worktree_root) for m in acted_moves}

        # HEAD's (mode, sha) for each acted src, spawn-free (`read_tree_spine`
        # reads loose/packed tree objects in process). Repathing a
        # HEAD-tracked blob to its new dst this way PRESERVES the mode HEAD
        # recorded for it (AC-5) -- including `100755` -- which a `git add`
        # staging pass would silently normalise away under
        # `core.filemode=false`. For a `restage_src=False` move this ALSO
        # supplies the blob sha directly (see below) -- such a move is a pure
        # rename, so `os.replace` moved exactly the bytes HEAD already has
        # recorded for src, and `head_entry[1]` (probed identical to
        # `git hash-object`'s own answer, 20/20 paths) needs no spawn to
        # confirm.
        # ONE walk of HEAD's tree for this whole call, over src UNION dst.
        # src supplies each move's (mode, sha); dst is walked here only so
        # `_assembled_commit_is_noop` below can be handed this same spine
        # instead of walking the tree a second time for the assembled keys
        # (= src union dst). Widening the path set is strictly cheaper than a
        # second walk: `read_tree_spine` descends only the directory
        # components its paths need, and the dst dirs are a handful of
        # archive/ parents. Extra keys are inert -- both consumers look up by
        # key. (2026-08-27: the second walk was cheap warm, because
        # git_objects' pack caches served it from memory. That is not a
        # reason to make it.)
        head_spine = read_tree_spine(
            worktree_root,
            list(src_rel_by_id.values()) + list(dst_rel_by_id.values()),
        )

        # `restage_src=True` moves are the ONLY ones needing a fresh hash:
        # their content was authored fresh on disk immediately before this
        # call (e.g. a terminality stamp) and may legitimately differ from
        # what HEAD still has recorded for src -- only the MODE is inherited
        # from HEAD for those, never the content. Scoped to just this subset,
        # and not called at all when it is empty (the common archival shape)
        # -- `git hash-object --stdin-paths` with no input is a spawn that
        # buys nothing. `_hash_object_stdin_paths` is a SYNCHRONOUS wrapper
        # (git_native's own `_git()`, subprocess.run under the hood) -- this
        # module's own NEGATIVE-SPEC forbids a blocking call on this
        # coroutine's thread (DR-211 D4, "single asyncio event loop"), so it
        # is offloaded via `asyncio.to_thread`, the same pattern this
        # function already uses for `release_committed_claims` below.
        restage_moves = [m for m in acted_moves if m.restage_src]
        restage_dst_rel_list = [dst_rel_by_id[m.candidate_id] for m in restage_moves]
        hash_result = None
        if restage_dst_rel_list:
            hash_result = await asyncio.to_thread(
                _hash_object_stdin_paths, restage_dst_rel_list, cwd=worktree_root,
            )

        assembled: Dict[str, Union[Tuple[int, str], object]] = {}
        spine_error: Optional[str] = None
        untracked_ids: List[str] = []
        if head_spine is None:
            spine_error = "spine-unresolvable: could not read HEAD's tree spine"
        elif hash_result is not None and not hash_result.ok:
            spine_error = (
                "hash-object-failed: "
                + (hash_result.stderr.strip() or "git hash-object -w --stdin-paths failed")
            )
        elif hash_result is not None and len(hash_result.stdout.splitlines()) != len(restage_moves):
            spine_error = (
                "hash-object-failed: `git hash-object --stdin-paths` "
                f"returned {len(hash_result.stdout.splitlines())} sha(s) for "
                f"{len(restage_moves)} requested path(s) -- refusing to guess "
                "an alignment"
            )
        else:
            dst_sha_by_id = {}
            if hash_result is not None:
                dst_sha_by_id = {
                    m.candidate_id: dst_sha
                    for m, dst_sha in zip(restage_moves, hash_result.stdout.splitlines())
                }
            for m in acted_moves:
                src_rel = src_rel_by_id[m.candidate_id]
                dst_rel = dst_rel_by_id[m.candidate_id]
                parent, _, leaf = src_rel.rpartition("/")
                head_entry = head_spine.get(parent, {}).get(leaf)
                if m.restage_src:
                    # Default to the ordinary file mode when src somehow
                    # carries no HEAD entry -- never invent an executable
                    # bit that was not already there. The sha always comes
                    # from the fresh hash above, never from head_entry.
                    mode = head_entry[0] if head_entry is not None else 0o100644
                    dst_sha = dst_sha_by_id[m.candidate_id]
                else:
                    # `head_entry is None` is now a REFUSAL, not a default:
                    # once `head_entry[1]` is load-bearing for the blob sha
                    # (see the PM-ruling comment above), a src with no HEAD
                    # tree entry has no sha to invent -- an untracked src is
                    # a caller error, not something to paper over with a
                    # spawn or a fabricated empty-blob sha.
                    if head_entry is None:
                        untracked_ids.append(m.candidate_id)
                        continue
                    mode, dst_sha = head_entry[0], head_entry[1]
                assembled[dst_rel] = (mode, dst_sha)
                assembled[src_rel] = _ABSENT

        # A restage_src=False move whose src carries no HEAD tree entry never
        # makes it into `assembled` above -- reverse its os.replace (mirrors
        # the commit-failure reversal loop further below), drop its now-empty
        # created dir, and reclassify it to failed[] individually. This does
        # NOT fail the rest of the batch: unlike a spine/hash-object error
        # (which is a property of the whole call), "this one src is
        # untracked" is a property of that one move alone.
        if untracked_ids:
            untracked_id_set = set(untracked_ids)
            for m in acted_moves:
                if m.candidate_id not in untracked_id_set:
                    continue
                if m.dst.exists() and not m.src.exists():
                    try:
                        m.dst.rename(m.src)
                    except OSError as exc:
                        _LOG.warning(
                            "archive_and_commit: could not reverse rename for "
                            "untracked-at-head move %s -> %s: %s",
                            m.dst, m.src, exc,
                        )
                if not m.dst.exists():
                    _cleanup_created_dirs(created_dirs_by_id[m.candidate_id])
                failed.append({
                    "id": m.candidate_id,
                    "reason": (
                        "untracked-at-head: src has no HEAD tree entry, so "
                        "there is no blob sha to repath without a spawn -- "
                        "refusing rather than inventing one (see the "
                        "restage_src=False branch of archive_and_commit's "
                        "assembled-tree build)"
                    ),
                })
            acted_ids -= untracked_id_set
            acted = [a for a in acted if a["id"] not in untracked_id_set]
            acted_moves = [m for m in acted_moves if m.candidate_id not in untracked_id_set]
            if not acted:
                return acted, failed

        # Re-sited AC-7 guarantee. `_empty_private_index_breach` refused a
        # pathspec-less commit whose PRIVATE INDEX resolved to git's empty
        # tree (read its docstring for the 2026-08-18 incident this guards)
        # -- this build has no private index left to go missing, so that
        # exact failure mode cannot recur here. What survives is the
        # guarantee, re-sited to this mechanism's own shape: a commit whose
        # assembled tree would land byte-identical to HEAD's tree is the same
        # "nothing to commit" case reached a different way, and is refused
        # the same way -- loud, before the commit, never silently landed as
        # a no-op archival commit. `_empty_private_index_breach` itself is
        # UNCHANGED and still guards `rm_and_commit`'s own private-index seam
        # below; this is a second, independent guard for this function only.
        # Review: code-reviewer (2026-08-27) — traced the full spine_error
        # if/elif chain above and PROVED `head_spine is not None` is
        # unreachable-as-False here: `head_spine is None` sets spine_error on
        # the very first branch, so `spine_error is None` already implies
        # `head_spine is not None`. Dropped per PM ruling ("costing 0ms is no
        # reason to have extraneous functions and unnecessary code") rather
        # than kept-as-stated-precondition — a proven-redundant guard is dead
        # weight, not documentation.
        if spine_error is None and _assembled_commit_is_noop(
            head_spine, assembled
        ):
            spine_error = (
                "empty-spine-commit: computed tree equals HEAD's tree -- "
                "nothing to commit; refused (re-sited form of "
                "_empty_private_index_breach's guarantee for a caller with "
                "no private index to go missing)"
            )

        if spine_error is not None:
            commit_rc, err_msg = 1, spine_error
        else:
            # Message on stdin-equivalent (a real temp file, `_commit_via_
            # head_spine` reads it in process): it carries the trailers the
            # hookless CAS landing would otherwise drop. See
            # `_message_with_hookless_trailers`.
            message = _message_with_hookless_trailers(
                subject,
                worktree_root,
                [str(m.dst) for m in moves if m.candidate_id in acted_ids],
            )
            msg_fd, msg_path = tempfile.mkstemp(prefix="fleet-git-msg-")
            try:
                with os.fdopen(msg_fd, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(message)
                # HEAD-race CAS landing (see `old_head` capture above):
                # `_commit_via_head_spine` rewrites HEAD's tree spine, builds
                # the commit object, and lands it via a locked `cas_ref` CAS
                # keyed on `old_head` -- the same atomicity the prior
                # write-tree + commit-tree -p <old_head> + 4-arg `update-ref`
                # ladder provided, now with ZERO git spawns for the landing
                # itself. `create_missing_dirs=True`: archival destinations
                # live under archive/handoffs/<month>/, which may not exist
                # in HEAD yet -- without the flag the helper would decline to
                # the ladder (which no longer exists here) instead of filling
                # in the new spine level.
                spine_result = _commit_via_head_spine(
                    worktree_root,
                    assembled,
                    old_head,
                    msg_path,
                    create_missing_dirs=True,
                    caller="archive_and_commit",
                )
            finally:
                try:
                    os.unlink(msg_path)
                except OSError:
                    pass

            if spine_result is None:
                commit_rc, err_msg = 1, (
                    "spine-commit-preconditions-failed: _commit_via_head_spine "
                    "declined (unresolvable HEAD tree spine, a structurally "
                    "missing parent directory, an unresolvable CAS ref "
                    "target, a lock-held ref, or no resolvable commit "
                    "identity) -- nothing was committed"
                )
            elif not spine_result.ok:
                commit_rc = spine_result.returncode
                err_msg = spine_result.stderr.strip() or "spine-commit-failed"
            else:
                commit_rc = 0
                err_msg = ""
                # The commit is landed. `_commit_via_head_spine` itself fires
                # no hooks (a direct locked-ref CAS, same as the
                # `commit-tree` + `update-ref` plumbing it replaces), and the
                # `post-commit` (auto-push) replay that used to run here was
                # REMOVED (C2, 2026-08-26, PM ruling — auto-push moves to
                # other ceremonies) — see the removal note where
                # `_replay_post_commit_hook` used to be defined, above, for
                # the cost and what is lost. This commit is pushed by
                # whatever pushes next, not by itself.

        if commit_rc != 0:
            _LOG.error(
                "archive_and_commit: git commit failed (cwd=%s): %s",
                worktree_root, err_msg,
            )
            # Reverse all acted renames on disk — commit failed, nothing was committed.
            orphaned_reversal_ids = set()
            for move in moves:
                if move.candidate_id in acted_ids:
                    if move.dst.exists() and not move.src.exists():
                        try:
                            move.dst.rename(move.src)
                        except OSError as exc:
                            _LOG.warning(
                                "archive_and_commit: could not reverse rename after "
                                "commit failure %s → %s: %s",
                                move.dst, move.src, exc,
                            )
                    elif move.dst.exists():
                        # src has reappeared — the rename-back cannot run
                        # without clobbering it, so dst is left as an
                        # untracked orphan.
                        _LOG.warning(
                            "archive_and_commit: skipping reversal for %s → %s "
                            "after commit failure — source has reappeared, "
                            "leaving destination orphaned",
                            move.src, move.dst,
                        )
                        orphaned_reversal_ids.add(move.candidate_id)
                    # C9: same "only remove if genuinely empty" guard as the
                    # per-move git-mv-failure branch above — a failed
                    # reverse-rename leaves dst populated, and that dir must
                    # not be removed out from under the still-present file.
                    if not move.dst.exists():
                        _cleanup_created_dirs(created_dirs_by_id[move.candidate_id])
            commit_failed = [
                {
                    "id": a["id"],
                    "reason": (
                        f"commit-failed: {err_msg}; reversal-skipped-src-reappeared: dst orphaned"
                        if a["id"] in orphaned_reversal_ids
                        else f"commit-failed: {err_msg}"
                    ),
                }
                for a in acted
            ]
            return [], failed + commit_failed

        # Commit succeeded — this repo's git history just changed. Invalidate
        # dag._git_path_ever_tracked's process-lifetime cache so a subsequent
        # lookup for a path this commit just tracked (or that a future commit
        # touches) doesn't read a pre-commit-cached stale result. See the
        # invalidation contract in dag.py's module-level comment above
        # _EVER_TRACKED_CACHE.
        invalidate_git_history_cache()

        # Resync the MAIN index to the new HEAD for the moved paths — see
        # _resync_main_index_for_moves (C4a, 2026-08-11) for the full
        # rationale, moved there along with the code.
        main_env = _make_git_env()
        acted_by_id = {a["id"]: a for a in acted}
        await _resync_main_index_for_moves(
            moves, acted_by_id, worktree_root=worktree_root, env=main_env,
        )

        # Post-commit claim release (C3, AC1): same worktree, this session's
        # own sid, and a bounded pathspec — even though the git commit ABOVE
        # was deliberately issued with no trailing pathspec (FORWARD-B, see
        # this function's own docstring), the set of paths it actually
        # covered is fully known here: every acted Move's src (vacated) and
        # dst (now tracked at dst). Released ONCE for the whole batch (not
        # per-Move) — this loop already committed everything together, and
        # `release_committed_claims` itself costs one `git status
        # --porcelain` call regardless of how many paths are passed.
        # Offloaded via `asyncio.to_thread`: this module's own NEGATIVE-SPEC
        # ("NEVER uses blocking subprocess.run — all git calls are
        # asyncio.create_subprocess_exec + await, DR-211 D4 async mandate")
        # applies to this call too — `release_committed_claims` issues a
        # synchronous `git status --porcelain` subprocess.
        try:
            release_paths = [
                rel_id(p, worktree_root) for m in moves if m.candidate_id in acted_ids
                for p in (m.src, m.dst)
            ]
            if release_paths:
                await asyncio.to_thread(
                    session_scope.release_committed_claims,
                    session_core.resolve_session_id(str(worktree_root)),
                    release_paths,
                    str(worktree_root),
                )
        except Exception:
            _LOG.debug(
                "archive_and_commit: release_committed_claims failed "
                "post-commit; claim(s) retained",
                exc_info=True,
            )

        return acted, failed

    finally:
        # Nothing to clean up: this function no longer opens a private
        # GIT_INDEX_FILE temp file or spawns a `commit-tree`/`update-ref`
        # child of its own -- `_commit_via_head_spine` lands the commit
        # in process via a locked ref CAS with zero git spawns, so there is
        # no orphaned-commit race for `_kill_orphaned_commit` to guard here
        # (unlike `rm_and_commit` below, whose private-index seam this
        # chunk deliberately does not touch -- see the dispatch brief).
        # The `try:` is retained rather than removed outright so this
        # function's body keeps its existing indentation.
        pass


# ---------------------------------------------------------------------------
# async rm_and_commit — DR-211 D3/D4 git-rm SIBLING (delete semantic)
# ---------------------------------------------------------------------------

async def rm_and_commit(
    worktree_root: Path,
    paths: List[Path],
    subject: str,
) -> Tuple[List[dict], List[dict]]:
    """DR-211 D3/D4 git-rm helper: reap each tracked path and commit all successes.

    Sibling of archive_and_commit — still isolates its per-path staging
    through a private HEAD-seeded `GIT_INDEX_FILE` (unlike archive_and_commit,
    which retired that seam on 2026-08-26 — see this module's docstring), but
    the eventual commit no longer reads that index: it lands via
    `_commit_via_head_spine` (imported from `ops.ceremony.git_native`, peer-
    to-peer import, see the module-level import comment above
    `archive_and_commit`), same as archive_and_commit's own landing call
    (C3, 2026-08-26; `commit-tree`/`update-ref` retired in favour of it). A
    genuinely different reversal mechanism from archive_and_commit either
    way, because the unit of work is a DELETE, not a rename.

    For each path:
    1. Runs awaited asyncio.create_subprocess_exec("git", "rm", path) with
       GIT_INDEX_FILE pointing to a private temp-file index (DR-211 D3 isolation
       — a concurrent 'git add -A' cannot absorb our staging). The private
       index is seeded via `git read-tree HEAD` first, so `git rm` can locate
       each tracked path — deleting that seed step reopens "pathspec did not
       match any files" before `git rm`'s own refusal check ever runs.
    2. Plain `git rm` (NEVER `git rm -f`) — see NEGATIVE-SPEC below for why this
       is load-bearing, not an oversight.
    3. On per-item git rm failure: the file was NOT deleted (plain git rm refuses
       before deleting) — item lands in failed[]; processing continues.

    After all paths: ONE call to `_commit_via_head_spine` lands an `assembled`
    dict mapping every successfully-reaped path to `_ABSENT`, with NO trailing
    pathspec — the dict already IS the exact scope (every successfully-reaped
    path, nothing else), mirroring archive_and_commit's own no-trailing-
    pathspec landing. The private index still guards `git rm`'s per-path
    fail-closed refusal (see WHY note below); it is simply no longer the
    commit's own source of scope.

    WHY this site is safe without ever having exhibited FORWARD-B (DR-211 D3
    amendment): unlike archive_and_commit, a trailing `-- <paths>` pathspec here
    was never observed to launder foreign worktree content, because plain `git rm`
    (never `-f`, see NEGATIVE-SPEC) REFUSES to stage a deletion when the worktree
    file differs from HEAD — a foreign dirty edit on a swept path blocks the
    reap before the commit call is ever reached, landing that id in failed[]
    instead. The pathspec is dropped here anyway (not merely justified and kept)
    so this fleet commit site does not diverge from the amended Invariant 4's
    index-sourced primary form while carrying a "ONE commit from the private
    index" comment that a trailing pathspec would make false again.

    If the commit fails: every already-reaped path is restored via
    `git checkout HEAD -- <path>` (there is nothing to rename back — git rm
    already deleted the worktree file by commit time) and reclassified to
    failed[].

    After a successful commit: main-index resync via `git update-index --remove`
    for each reaped path (no --add counterpart — the file is gone), with the
    same exponential-backoff retry pattern as archive_and_commit (see
    _update_index_with_retry / _INDEX_RETRY_* for the live constants — do not
    restate the numbers here, they have drifted from prose once already);
    non-fatal but loud on persistent failure.

    INVARIANT (2026-08-11, DR-211 residue audit C3): this --remove-only resync
    cannot produce the archive_and_commit-style `D` (staged deletion) + `??`
    (untracked file present on disk) residue shape. That shape arises there
    because the resync's `--add` half is SKIPPED or FAILS while its `--remove`
    half has already run — leaving the index without `dst` at a moment when HEAD
    holds `dst` and the file is present on disk. It is a half-completed resync,
    NOT a race and NOT a stat mismatch; describing it as either sends the next
    reader hunting for a concurrency bug that is not there. This site has no
    `--add` half to lose, so the shape has no way to form.

    Benign BECAUSE the commit already succeeded and HEAD no longer holds the
    path — not unconditionally: post-commit, HEAD has dropped the path AND the
    worktree file is already gone (plain `git rm` deleted it before the commit
    ran), so `--remove` only converges the main index toward a HEAD it already
    matches. On commit FAILURE this resync block is never reached at all —
    reversal there is `git checkout HEAD -- <path>` (see the failure branch
    above), not this resync, and that branch carries its own residue analysis.

    What a FAILED `--remove` here leaves is a different shape, not this one: the
    index still recording a path that HEAD has dropped and disk no longer has.
    That is what the `index_resync_failed` annotation below exists to surface.

    DOES NOT PUSH (C2, 2026-08-26, PM ruling — auto-push moves to other
    ceremonies and does not belong on this path). A landed commit is pushed
    by whatever pushes next, never by this function itself — see
    archive_and_commit's own DOES NOT PUSH note, and the removal note where
    `_replay_post_commit_hook` used to be defined, for the cost this traded
    away.

    Private index is always cleaned up in the finally block.

    Empty paths → returns ([], []) with no read-tree, no commit (a clean no-op).

    NEGATIVE-SPEC:
    - NEVER `git rm -f`. Plain `git rm` refuses to stage a deletion when the
      worktree file differs from HEAD (locally modified / concurrently
      touched). This is the DESIRED fail-closed-on-concurrent-modification
      behavior on a shared tree: a sidecar modified out from under this reap
      is retained (lands in failed[]), never force-deleted. Do not "fix" this
      to -f — that reintroduces silent data loss on a shared tree.
    - Reversal on commit failure is `git checkout HEAD -- <path>` (restore
      from HEAD), NEVER Path.rename — by commit time the worktree file is
      already gone; there is no dst to rename back from. This is the one
      genuine mechanism divergence from archive_and_commit's rename-reversal.
    - NEVER uses git add -A / git add . / a directory-prefix pathspec — the
      private index is scoped by construction via git rm against a HEAD-seeded
      index (DR-211 D3 Invariant 4, amended for FORWARD-B).
    - NEVER commits with a trailing `-- <paths>` pathspec — see the WHY note
      above for why this site was never FORWARD-B-affected in practice, and why
      the pathspec is dropped anyway to match the amended primary form.
    - NEVER uses blocking subprocess.run — all git calls are
      asyncio.create_subprocess_exec + await (DR-211 D4 async mandate).
    - GIT_INDEX_FILE isolates `git rm`'s per-path staging only; the commit
      landing itself reads no index (`_commit_via_head_spine`). The main
      index is only touched in the post-commit resync step, and only via
      --remove (no --add). See the INVARIANT note above the resync code for
      why --remove-only cannot reproduce archive_and_commit's D + ?? residue
      shape — conditional on the commit-success path, not a blanket
      "--remove is always safe" claim.
    - stdout/stderr captured as bytes — decoded with errors="replace" on failure.

    Spec: docs/plans/2026-07-26-memo-disposition-flip-op-and-hand-edit-hole.md (C4)

    Args:
        worktree_root: absolute path to the main git worktree (from main_worktree_root()).
        paths: absolute paths to tracked files under worktree_root to reap.
        subject: git commit message subject line.

    Returns:
        (reaped, failed) where:
            reaped = [{"id": str, "reaped": True}, ...]
            failed = [{"id": str, "reason": str}, ...]
        id is the repo-relative POSIX path string (rel_id(path, worktree_root)).
        A reaped[] item MAY additionally carry `"index_resync_failed": str` —
        present ONLY when the post-commit main-index resync exhausted its
        retry budget for that path. Additive key, absent on the success path
        — see archive_and_commit's identical annotation for the incident this
        closes and _update_index_with_retry for the retry mechanics.

    Spec backlinks:
        - DR-211: docs/decisions/DR-211-fleet-op-substrate-write-boundary.md (D3/D4 mechanics)
        - DR-218: docs/decisions/DR-218-review-trail-aged-unintegrated-reap-boundary.md
          (authorization to apply these mechanics to a delete semantic against review-trail)
    """
    import asyncio

    if not paths:
        return [], []

    def _rel_id(p: Path) -> Optional[str]:
        """Return the repo-relative id for p, or None if p is not under worktree_root.

        Review: code-reviewer — slice1 F1: a bare .relative_to() call raises
        ValueError uncaught for a path outside worktree_root, which would sink
        the whole batch and break the per-item isolation this function otherwise
        guarantees. Callers must treat a None return as "route this path into
        failed[] with reason not-under-worktree-root", never propagate.
        """
        try:
            return rel_id(p, worktree_root)
        except ValueError:
            return None

    idx_fd, idx_path = tempfile.mkstemp(prefix="fleet-git-idx-")
    os.close(idx_fd)

    # Holder for the in-flight pathspec-less commit, read by the `finally:`
    # below. Declared out here so it survives into the cancellation unwind.
    # See `_kill_orphaned_commit`.
    commit_proc: Optional["asyncio.subprocess.Process"] = None

    try:
        base_env = _make_git_env(idx_path=idx_path)

        # Review: code-reviewer — slice1 F1: classify not-under-worktree-root paths
        # up front, once, so every downstream site (read-tree-failure comprehension,
        # main loop, commit-failure restore loop, index-resync loop) only ever sees
        # paths that DO resolve to a valid id — no per-site guard needed.
        valid_paths: List[Path] = []
        pre_failed: List[dict] = []
        for p in paths:
            rid = _rel_id(p)
            if rid is None:
                pre_failed.append({"id": str(p), "reason": "not-under-worktree-root"})
            else:
                valid_paths.append(p)
        paths = valid_paths

        if not paths:
            return [], pre_failed

        # Initialise the private index from HEAD so git rm can locate tracked files.
        proc = await asyncio.create_subprocess_exec(
            "git", "read-tree", "HEAD",
            cwd=str(worktree_root),
            env=base_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _out, stderr = await proc.communicate()
        if proc.returncode != 0:
            err_msg = stderr.decode(errors="replace").strip()
            _LOG.error(
                "rm_and_commit: git read-tree HEAD failed (cwd=%s): %s",
                worktree_root, err_msg,
            )
            return [], pre_failed + [
                {"id": _rel_id(p), "reason": f"index-init-failed: {err_msg}"}
                for p in paths
            ]

        # HEAD-race CAS anchor — see archive_and_commit's identical capture
        # for the full incident this closes (b4f0bfe88 etc.): read-tree HEAD
        # above seeds the private index, but the per-path git rm loop and
        # the eventual commit are un-bridged from it unless this sha is
        # captured now and used as the commit's CAS parent below.
        old_head = _read_head_sha(worktree_root)
        if old_head is None:
            return [], pre_failed + [
                {
                    "id": _rel_id(p),
                    "reason": "index-init-failed: HEAD has no resolvable commit",
                }
                for p in paths
            ]

        reaped: List[dict] = []
        failed: List[dict] = list(pre_failed)

        for path in paths:
            candidate_id = _rel_id(path)

            # Plain `git rm` — NEVER -f. This refuses (no delete) when the
            # worktree file differs from HEAD, which is the desired
            # fail-closed-on-concurrent-modification safety property on a
            # shared tree: a concurrently-modified sidecar is retained in
            # failed[], not force-deleted.
            proc = await asyncio.create_subprocess_exec(
                "git", "rm", "--", str(path),
                cwd=str(worktree_root),
                env=base_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _out, stderr = await proc.communicate()

            if proc.returncode != 0:
                err_msg = stderr.decode(errors="replace").strip()
                # Plain git rm refuses BEFORE deleting on failure — the file
                # was not touched; nothing to reverse.
                failed.append({
                    "id": candidate_id,
                    "reason": err_msg if err_msg else "git-rm-failed",
                })
            else:
                reaped.append({"id": candidate_id, "reaped": True})

        if not reaped:
            return reaped, failed

        reaped_ids = {r["id"] for r in reaped}

        # ONE commit from the private index — NO trailing pathspec. See the WHY
        # note in this function's docstring: `git rm` (never -f) already refused
        # to stage any path whose worktree content diverged from HEAD, so the
        # index is the exact reaped-path scope by construction.
        # Empty-private-index refusal MUST precede the commit — same reasoning
        # as archive_and_commit's: no pathspec means an empty index deletes the
        # repo rather than committing nothing. See `_empty_private_index_breach`.
        index_breach, tree_sha = await _empty_private_index_breach(
            worktree_root, base_env, "rm_and_commit"
        )
        if index_breach is not None:
            commit_rc, err_msg = 1, index_breach
        else:
            # HEAD-race CAS landing via `_commit_via_head_spine` (imported
            # from ops.ceremony.git_native — peer-to-peer import, deliberate,
            # see the module-level import note above archive_and_commit).
            # `assembled` maps every reaped path to `_ABSENT` — a plain
            # deletion set, mirroring archive_and_commit's own `{path:
            # (mode, sha) | _ABSENT}` shape but with no additions here since
            # `git rm` (above) already deleted the worktree content; there
            # is no fresh blob to hash. This replaces `git commit-tree
            # tree_sha -p old_head` + a 4-arg `git update-ref` (net -2
            # spawns) with the same CAS atomicity, landed in process —
            # `tree_sha` above is `_empty_private_index_breach`'s own
            # write-tree output, kept only for the empty-index refusal it
            # already performs; it is not passed to the spine helper, which
            # computes its own tree from `assembled` against HEAD.
            # Message on stdin-equivalent (a real temp file, read in
            # process) — see archive_and_commit's identical block and
            # `_message_with_hookless_trailers`. The reaped paths are
            # already deleted from disk here, so tier-0 artifact resolution
            # finds nothing and the resolver falls through to its
            # session-keyed tiers; passing them anyway keeps both call
            # sites' argument shape identical.
            message = _message_with_hookless_trailers(
                subject,
                worktree_root,
                [str(p) for p in paths if _rel_id(p) in reaped_ids],
            )
            assembled: Dict[str, Union[Tuple[int, str], object]] = {
                _rel_id(p): _ABSENT for p in paths if _rel_id(p) in reaped_ids
            }
            msg_fd, msg_path = tempfile.mkstemp(prefix="fleet-git-msg-")
            try:
                with os.fdopen(msg_fd, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(message)
                spine_result = _commit_via_head_spine(
                    worktree_root,
                    assembled,
                    old_head,
                    msg_path,
                    caller="rm_and_commit",
                )
            finally:
                try:
                    os.unlink(msg_path)
                except OSError:
                    pass

            if spine_result is None:
                commit_rc, err_msg = 1, (
                    "spine-commit-preconditions-failed: _commit_via_head_spine "
                    "declined (unresolvable HEAD tree spine, a structurally "
                    "missing parent directory, an unresolvable CAS ref "
                    "target, a lock-held ref, or no resolvable commit "
                    "identity) -- nothing was committed"
                )
            elif not spine_result.ok:
                commit_rc = spine_result.returncode
                err_msg = spine_result.stderr.strip() or "spine-commit-failed"
            else:
                commit_rc = 0
                err_msg = ""
                # The commit is landed. `_commit_via_head_spine` itself
                # fires no hooks (a direct locked-ref CAS), and the
                # `post-commit` (auto-push) replay that used to run here
                # was REMOVED (C2, 2026-08-26, PM ruling — auto-push moves
                # to other ceremonies) — see the removal note where
                # `_replay_post_commit_hook` used to be defined, above, for
                # the cost and what is lost. This commit is pushed by
                # whatever pushes next, not by itself.

        if commit_rc != 0:
            _LOG.error(
                "rm_and_commit: git commit failed (cwd=%s): %s",
                worktree_root, err_msg,
            )
            # Restore-from-HEAD reversal: git rm already deleted the worktree
            # file, so there is nothing to rename back — restore each reaped
            # path from HEAD instead.
            main_env_for_restore = _make_git_env()
            # Review: code-reviewer — slice1 F2: track restore rc per path. When
            # the restore-from-HEAD itself fails, the file is genuinely gone
            # (real data-loss on a shared tree) — that is a more severe condition
            # than an ordinary commit-failed reversion and must surface distinctly.
            restore_failed_ids: set = set()
            for path in paths:
                candidate_id = _rel_id(path)
                if candidate_id not in reaped_ids:
                    continue
                proc = await asyncio.create_subprocess_exec(
                    "git", "checkout", "HEAD", "--", str(path),
                    cwd=str(worktree_root),
                    env=main_env_for_restore,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _out, restore_stderr = await proc.communicate()
                if proc.returncode != 0:
                    restore_failed_ids.add(candidate_id)
                    _LOG.warning(
                        "rm_and_commit: could not restore-from-HEAD %s after "
                        "commit failure: %s",
                        path, restore_stderr.decode(errors="replace").strip(),
                    )
            commit_failed = [
                {
                    "id": r["id"],
                    "reason": (
                        f"commit-failed: {err_msg}; RESTORE-FROM-HEAD-ALSO-FAILED"
                        if r["id"] in restore_failed_ids
                        else f"commit-failed: {err_msg}"
                    ),
                }
                for r in reaped
            ]
            return [], failed + commit_failed

        # Commit succeeded — see archive_and_commit's identical invalidation
        # comment above for the correctness rationale.
        invalidate_git_history_cache()

        # Resync the MAIN index for reaped paths — see
        # _resync_main_index_for_reaps (C4a, 2026-08-11) for the full
        # rationale, moved there along with the code. See this function's
        # own docstring INVARIANT note for why the --remove-only shape here
        # is safe — that reasoning stays attached to THIS site's meaning.
        main_env = _make_git_env()
        reaped_by_id = {r["id"]: r for r in reaped}
        await _resync_main_index_for_reaps(
            paths, reaped_by_id, worktree_root=worktree_root, env=main_env,
        )

        # Post-commit claim release (C3, AC1) — same worktree, this
        # session's own sid, bounded scope (every genuinely reaped path,
        # `reaped_ids`, not `paths`, which may still hold pre_failed
        # entries). See archive_and_commit's identical block for the
        # no-trailing-pathspec-but-bounded-scope rationale; offloaded via
        # `asyncio.to_thread` for the same D4 (never-blocking-subprocess)
        # reason given there.
        try:
            release_paths = [
                rid for p in paths
                for rid in [_rel_id(p)]
                if rid in reaped_ids
            ]
            if release_paths:
                await asyncio.to_thread(
                    session_scope.release_committed_claims,
                    session_core.resolve_session_id(str(worktree_root)),
                    release_paths,
                    str(worktree_root),
                )
        except Exception:
            _LOG.debug(
                "rm_and_commit: release_committed_claims failed post-commit; "
                "claim(s) retained",
                exc_info=True,
            )

        return reaped, failed

    finally:
        # Order is load-bearing — see archive_and_commit's matching `finally:`
        # and `_kill_orphaned_commit`.
        _kill_orphaned_commit(commit_proc, "rm_and_commit")
        # Always clean up the private index temp file; already gone is not an error.
        try:
            os.unlink(idx_path)
        except OSError:
            pass
