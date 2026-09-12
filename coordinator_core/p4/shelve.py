"""
coordinator_core/p4/shelve.py — the p4 leg of push.outstanding: reconcile,
revert -a, shelve -r (C3, D4).

Spec backlink: docs/plans/2026-09-12-perforce-second-class-commit-and-shelve.md § C3, § D4.

`shelve_outstanding(repo_root, sid, sdir, identity, cl, base_sha)` is the one
entry point `push_outstanding.py`'s p4 leg calls. It derives the path set
from THIS SESSION'S OWN COMMITS —
``git log <base_sha>..HEAD --grep='Session-Id: <sid>' --name-only
--format=``, one spawn — never a two-point ``git diff`` (a shared-tree
race: a plain diff also collects every peer session's commits minted since
this session's CL, and `push_with_retry`'s rebase-on-reject pulls the
upstream's commits into the range too — D4).

`base_sha` reachability is verified FIRST (`git cat-file -e
<sha>^{commit}`) — a rebase or reset in the shared worktree can drop the
recorded base out of history, and `<base>..HEAD` then either errors or
returns EMPTY, which left unchecked reads as "nothing to shelve" and
silently skips the leg — the same silent-skip failure mode as a genuinely
empty path set, arriving by a different route. An unreachable base is
therefore a LOUD re-mint, never an empty path set: this module falls back
to the merge-base of HEAD and the upstream, re-mints the session CL
(``session_change.remint_session_change``), and records the re-mint on the
returned ``ShelveOutcome.remint``.

Sequence, scoped to the derived path set:
    1. ``p4 -s -d <repo_root> reconcile -c <CL> -e -a -d <paths>``
       (the leading ``-d <dir>`` is p4's GLOBAL working-directory option —
       distinct from ``reconcile``'s own ``-d`` "detect deletes" flag — and
       is how the repo-relative paths resolve; there is no ``--``
       end-of-options token, which real p4 refuses outright)
    2. ``p4 -s revert -a -c <CL>``
    3. ``p4 -s shelve -r -c <CL>``

On success, ``p4_shelved_at`` (now, UTC ISO) and ``p4_shelved_sha`` (HEAD at
this shelve) are written through ``session/core.py::update_meta_fields``.

Returns a typed ``ShelveOutcome`` and never raises into `push_outstanding`'s
git leg — a classified runner refusal (lock held, ticket expired, timeout)
is carried on ``ShelveOutcome.error``, never swallowed and never an
exception.

Negative-spec:
  - Never a two-point ``git diff <base>..HEAD`` for the path set (D4 — a
    shared-tree race across peer sessions and `push_with_retry`'s own
    rebase-on-reject).
  - Never reads an unreachable base as an empty path set — always a loud
    re-mint (D4).
  - Never raises — every failure, git-side or p4-side, is a typed
    ``ShelveOutcome``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from coordinator_core.git.run import run_git
from coordinator_core.p4 import runner
from coordinator_core.p4.workspace import P4Identity
from coordinator_core.session.core import update_meta_fields

# `coordinator_core.p4.__init__` re-exports `workspace.session_change` (the
# D9 reader function) under the package attribute name `session_change`,
# shadowing this submodule on `from coordinator_core.p4 import
# session_change` -- see `p4/tests/test_session_change.py`'s own note. The
# dotted-module import binds the submodule directly.
import coordinator_core.p4.session_change as session_change


@dataclass(frozen=True)
class ShelveOutcome:
    """``ok`` — the leg completed (including the zero-spawn "empty path
    set" case, which is a success with ``paths == []``). ``error`` is
    populated only on a git- or p4-side failure and is never raised.
    ``remint`` is True iff the recorded ``base_sha`` was unreachable and
    this call re-minted the session CL (D4)."""

    ok: bool
    paths: List[str] = field(default_factory=list)
    shelved_at: Optional[str] = None
    shelved_sha: Optional[str] = None
    remint: bool = False
    cl: Optional[int] = None
    error: Optional[runner.P4Error] = None


def _base_reachable(repo_root: str, base_sha: str) -> bool:
    result = run_git(["-C", repo_root, "cat-file", "-e", f"{base_sha}^{{commit}}"])
    return (not result.timed_out) and result.returncode == 0


def _merge_base_with_upstream(repo_root: str) -> Optional[str]:
    """The merge-base of HEAD and the branch's own upstream tracking ref, or
    ``None`` if there is no upstream to merge-base against (git itself then
    fails the resolve) — the D4 fallback base when the recorded
    ``p4_base_sha`` is unreachable.

    Prefers ``@{u}@{1}`` (the upstream ref's reflog entry ONE BEFORE its
    current position) over a plain ``@{u}``. This leg always runs AFTER
    `push_outstanding`'s own git leg has already landed the push (module
    docstring's "additive... after the git leg"), so by the time this
    fallback fires, ``@{u}`` has already been fast-forwarded to (or past)
    HEAD by that same push — a plain ``merge-base HEAD @{u}`` then resolves
    to HEAD itself, collapsing ``<base>..HEAD`` to an empty range and
    silently reproducing the exact "empty path set" failure mode this
    fallback exists to avoid (measured: a from-scratch e2e run with a real
    p4d found ZERO shelved changes on the re-mint leg until this was
    fixed). ``@{u}@{1}`` reads the upstream ref's value from immediately
    BEFORE that push, which is exactly the pre-push base D4's Session-Id
    walk needs. Falls back to a plain ``@{u}`` when no such reflog entry
    exists (e.g. the branch's first-ever push, or reflogs disabled) —
    strictly no worse than the prior behaviour in that narrower case."""
    result = run_git(["-C", repo_root, "merge-base", "HEAD", "@{u}@{1}"])
    if not result.timed_out and result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    result = run_git(["-C", repo_root, "merge-base", "HEAD", "@{u}"])
    if result.timed_out or result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _path_set(repo_root: str, base_sha: str, sid: str) -> Optional[List[str]]:
    """One spawn: ``git log <base>..HEAD --grep='Session-Id: <sid>'
    --name-only --format=``. Returns ``None`` on a git-level failure —
    never confused with a genuinely empty, successfully-resolved path set
    (``[]``)."""
    result = run_git(
        [
            "-C",
            repo_root,
            "log",
            f"{base_sha}..HEAD",
            f"--grep=Session-Id: {sid}",
            "--name-only",
            "--format=",
        ]
    )
    if result.timed_out or result.returncode != 0:
        return None
    return [line for line in result.stdout.splitlines() if line.strip()]


def _head_sha(repo_root: str) -> Optional[str]:
    result = run_git(["-C", repo_root, "rev-parse", "HEAD"])
    if result.timed_out or result.returncode != 0:
        return None
    return result.stdout.strip() or None


def shelve_outstanding(
    repo_root: str,
    sid: str,
    sdir: str,
    identity: P4Identity,
    cl: int,
    base_sha: str,
) -> ShelveOutcome:
    """The p4 leg proper. ``cl``/``base_sha`` are the caller's
    already-resolved session CL / recorded ``p4_base_sha`` — this module
    does not mint or re-read them itself, except for the reachability
    re-mint below, which is this leg's own concern (D4)."""
    effective_cl = cl
    effective_base = base_sha
    remint = False

    if not _base_reachable(repo_root, base_sha):
        fallback = _merge_base_with_upstream(repo_root)
        if fallback is None:
            return ShelveOutcome(
                ok=False,
                cl=cl,
                error=runner.P4Error(
                    kind="refused",
                    raw="p4_base_sha unreachable and no upstream merge-base to fall back to",
                ),
            )
        effective_base = fallback
        effective_cl = session_change.remint_session_change(repo_root, sid)
        remint = True

    paths = _path_set(repo_root, effective_base, sid)
    if paths is None:
        return ShelveOutcome(
            ok=False,
            cl=effective_cl,
            remint=remint,
            error=runner.P4Error(kind="refused", raw="git log path-set spawn failed"),
        )
    if not paths:
        return ShelveOutcome(ok=True, paths=[], cl=effective_cl, remint=remint)

    # `p4 reconcile` has no end-of-options token: its usage line is
    # `[-c change#] [-a -e -d -M -f -I -l -m -n -t] [-w [-K]] [--parallel=N]
    # [file ...]` and a real p4d 2026.1 refuses `--` with `Invalid option: --.`,
    # so the whole leg failed before reaching revert/shelve. Passing `--` was
    # a git habit; p4 does not share it.
    #
    # `--` was doing real work, though — without it a path beginning with `-`
    # is parsed as an option. p4 offers no separator to replace it, so refuse
    # such a path rather than hand p4 an argv that means something else. Git
    # permits these names; a silent mis-invocation here would reconcile the
    # wrong fileset into a shelved CL.
    option_shaped = [p for p in paths if p.startswith("-")]
    if option_shaped:
        return ShelveOutcome(
            ok=False,
            paths=paths,
            cl=effective_cl,
            remint=remint,
            error=runner.P4Error(
                kind="refused",
                raw=(
                    "path(s) begin with '-' and p4 reconcile has no end-of-options "
                    f"token to protect them: {option_shaped}"
                ),
            ),
        )

    result = runner.run(
        identity.port,
        identity.user,
        identity.client,
        # `paths` are repo-relative (git log --name-only). `subprocess.Popen`'s
        # own `cwd=` kwarg is silently ignored by this box's `p4.exe` when
        # resolving relative file arguments (measured: `p4 add a.txt` with
        # `cwd=<client-root>` still resolves `a.txt` against the PARENT
        # process's cwd and fails "not under client's root") — `p4`'s own
        # `-d <dir>` global option is the only thing that works, so it is
        # passed as a leading arg here rather than as `runner.run`'s `cwd=`.
        # Left unset, reconcile matches nothing and exits 0: a silent empty
        # shelve rather than an error. The other two spawns below are
        # CL-scoped and take no paths, so they are unaffected.
        ["-d", repo_root, "reconcile", "-c", str(effective_cl), "-e", "-a", "-d", *paths],
    )
    if not result.ok:
        return ShelveOutcome(ok=False, paths=paths, cl=effective_cl, remint=remint, error=result.error)

    result = runner.run(
        identity.port, identity.user, identity.client, ["revert", "-a", "-c", str(effective_cl)]
    )
    if not result.ok:
        return ShelveOutcome(ok=False, paths=paths, cl=effective_cl, remint=remint, error=result.error)

    result = runner.run(
        identity.port, identity.user, identity.client, ["shelve", "-r", "-c", str(effective_cl)]
    )
    if not result.ok:
        return ShelveOutcome(ok=False, paths=paths, cl=effective_cl, remint=remint, error=result.error)

    shelved_sha = _head_sha(repo_root)
    shelved_at = datetime.now(timezone.utc).isoformat()
    update_meta_fields(sdir, {"p4_shelved_at": shelved_at, "p4_shelved_sha": shelved_sha})

    return ShelveOutcome(
        ok=True,
        paths=paths,
        shelved_at=shelved_at,
        shelved_sha=shelved_sha,
        cl=effective_cl,
        remint=remint,
    )
