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

Sequence, scoped to the derived path set (non-empty only — zero p4 spawns
on an empty path set):
    0. ``p4 -s -d <repo_root> -ztag fstat -T clientFile,haveRev,change,action
       -- <paths>`` — hoisted here (D4b) ahead of ``reconcile`` so the SAME
       read drives both D4a's read-only-bit restore (``haveRev``, at the
       back of this sequence) and D4b's orphaned-open adoption (``change``,
       right here) — one read, two consumers, never a second fstat spawn.
       A path whose ``change`` names a pending CL of THIS client/user that
       is not ``effective_cl`` is this session's own orphan (a file still
       open in an EXITED session's changelist, D5's zero-spawn writable
       fast-path never having looked at it since it is WRITABLE) —
       ``p4 -s reopen -c <effective_cl> <paths>`` (one spawn, ONLY when
       step 0 found at least one orphan) moves its open record into this
       session's CL before reconcile runs. A path open by another user or
       client is never reopened (fstat's own ``change``/``action`` fields
       report only THIS client's local open state — another client's open
       shows as ``otherOpen0`` instead, which this module never reads or
       acts on).
    1. ``p4 -s -d <repo_root> reconcile -c <CL> -e -a -d <paths>``
       (the leading ``-d <dir>`` is p4's GLOBAL working-directory option —
       distinct from ``reconcile``'s own ``-d`` "detect deletes" flag — and
       is how the repo-relative paths resolve; there is no ``--``
       end-of-options token, which real p4 refuses outright). ``-a``
       (``P4IGNORE`` semantics) is dropped when registration authored
       ``.p4ignore`` from scratch — see ``_p4ignore_absent`` (F3).
    2. ``p4 -s revert -a -c <CL>``
    3. ``p4 -s shelve -r -c <CL>``
    Back of the sequence (D4a): every path whose step-0 record carried
    ``haveRev`` (synced to the depot) and no ``action`` (not open at step 0)
    gets its local read-only bit restored via a plain ``os.chmod`` — never
    ``p4 clean``, ``reconcile -w``, or ``sync -f`` (the invariant is never
    repaired by overwriting the worktree). This is the mode-only-drift
    residue a git worktree rewrite leaves behind: content identical to the
    depot, nothing opened, read-only bit cleared — ``reconcile -e`` above
    catches the CONTENT half of that rewrite, never the mode-only half.

Happy-path budget unchanged at 4 p4 spawns (fstat replaces what used to be
a bare 3-spawn sequence, one-for-one); the orphan-adoption path is 5.

On success, ``p4_shelved_at`` (now, UTC ISO) and ``p4_shelved_sha`` (HEAD at
this shelve) are written through ``session/core.py::update_meta_fields``.

Returns a typed ``ShelveOutcome`` and never raises into `push_outstanding`'s
git leg — a classified runner refusal (lock held, ticket expired, timeout)
is carried on ``ShelveOutcome.error``, never swallowed and never an
exception. ``ShelveOutcome.restored``/``.adopted`` carry D4a/D4b's counts
for the caller's own telemetry arm — this module records no telemetry
itself.

Negative-spec:
  - Never a two-point ``git diff <base>..HEAD`` for the path set (D4 — a
    shared-tree race across peer sessions and `push_with_retry`'s own
    rebase-on-reject).
  - Never reads an unreachable base as an empty path set — always a loud
    re-mint (D4).
  - Never raises — every failure, git-side or p4-side, is a typed
    ``ShelveOutcome``.
  - Never ``p4 clean``/``reconcile -w``/``sync -f`` to repair the
    read-only-bit invariant — always a local ``os.chmod`` (D4a).
  - Never reopens a path open by another user or client (D4b) — only a
    path whose fstat record shows an open under THIS client's own
    ``change``/``action`` fields.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from coordinator_core.git.run import run_git
from coordinator_core.p4 import runner, session_change
from coordinator_core.p4.workspace import P4Identity
from coordinator_core.session.core import update_meta_fields


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
    #: D4a — count of paths whose read-only bit was restored at the back of
    #: the sequence (mode-only-drift residue).
    restored: int = 0
    #: D4b — count of paths adopted out of an exited session's CL via
    #: ``reopen`` before reconcile ran.
    adopted: int = 0


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


def _fstat_records(stdout: str) -> List[Dict[str, str]]:
    """Parses ``-ztag fstat``'s ``... field value`` lines into one dict per
    file — blank-line-separated blocks, mirroring
    ``p4_checkout_before_edit.py::_parse_ztag``'s single-record shape,
    widened here to the multi-path case. Order is NOT load-bearing: the
    caller keys these records by the ``clientFile`` each record itself
    names (never a positional zip against ``paths`` — see
    ``_key_fstat_records_by_path``), since p4 may return fewer records
    than paths given (a path not yet in the depot, a filtered record, an
    error line with no ``... `` block)."""
    records: List[Dict[str, str]] = []
    current: Dict[str, str] = {}
    for line in stdout.splitlines():
        if not line.strip():
            if current:
                records.append(current)
                current = {}
            continue
        if not line.startswith("... "):
            continue
        name, _, value = line[4:].partition(" ")
        if name:
            current[name] = value
    if current:
        records.append(current)
    return records


#: `clientFile` is depot-view syntax, always `//<client>/<rest>` -- the
#: client name in that prefix is whichever client fstat resolved the path
#: through (this call's own `-c <client>`; `change`/`action` still report
#: only THIS client's own open state per the module docstring), so the
#: match strips the generic two-slash prefix rather than requiring THIS
#: identity's own client name literally.
_CLIENT_FILE_PREFIX_RE = re.compile(r"^//[^/]+/(.+)$")


def _key_fstat_records_by_path(
    records: List[Dict[str, str]], paths: List[str]
) -> "tuple[Dict[str, Optional[Dict[str, str]]], List[str]]":
    """Review: code-reviewer F1 (integrator-applied) -- keys ``records`` by
    the ``clientFile`` each record itself names, NEVER by position against
    ``paths``. ``zip()`` silently truncates to the shorter sequence, and
    worse, mispairs every path after a short row against the wrong path's
    record -- exactly the shape that fed D4b's orphan-adoption and D4a's
    restore off a misclassified path while still returning ``ok=True``.

    ``clientFile`` is depot-view syntax (``//<client>/<path>``, always
    forward-slash, never an OS filesystem path), so the match is a plain
    string strip-and-compare against ``paths`` (themselves
    forward-slash-relative, from ``git log --name-only``) -- no
    ``os.path`` normalization needed or wanted here.

    Returns ``(per_path, unreconciled)``. ``per_path`` maps every
    requested path to its record or ``None`` -- a path with no matching
    record is a legitimate state (not yet in the depot), never a record
    belonging to a different path. ``unreconciled`` lists any record whose
    ``clientFile`` could not be matched to a requested path at all (p4
    reported something this call never asked about); non-empty there means
    the record/path correspondence cannot be trusted by name either, and
    the caller must fail closed rather than proceed."""
    path_set = set(paths)
    per_path: Dict[str, Optional[Dict[str, str]]] = {p: None for p in paths}
    unreconciled: List[str] = []
    for rec in records:
        client_file = rec.get("clientFile")
        match = _CLIENT_FILE_PREFIX_RE.match(client_file) if client_file else None
        if not match:
            unreconciled.append(client_file or "<missing clientFile>")
            continue
        rel = match.group(1)
        if rel not in path_set:
            unreconciled.append(client_file)
            continue
        per_path[rel] = rec
    return per_path, unreconciled


def _restore_read_only(repo_root: str, paths: List[str]) -> int:
    """D4a — the back of the sequence. Never ``p4 clean``/``reconcile
    -w``/``sync -f``: a plain local ``os.chmod`` clearing the write bits,
    the invariant restored rather than the worktree repaired."""
    restored = 0
    for rel_path in paths:
        abs_path = os.path.join(repo_root, rel_path)
        if not os.path.exists(abs_path):
            continue
        try:
            current_mode = os.stat(abs_path).st_mode
            os.chmod(abs_path, current_mode & ~(stat.S_IWRITE | stat.S_IWGRP | stat.S_IWOTH))
        except OSError:
            continue
        restored += 1
    return restored


def _p4ignore_absent(repo_root: str) -> bool:
    """Review: overengineering-reviewer F3 (integrator-applied) -- reads back
    ``p4.<repo_key>.p4ignore_absent``, the row `register.py` writes when a
    registration authored ``.p4ignore`` from scratch (empty of any pattern
    but the ``.git/`` line this floor needs). That row's own message says
    reconcile should drop ``-a`` rather than rely on ``.p4ignore`` for full
    ignore semantics in that case -- previously recorded but never read;
    this is what reads it. Fails safe (``False``, keep ``-a``) when the
    repo_key cannot be resolved, matching the unconditional-``-a`` behaviour
    this call had before F3."""
    from coordinator_core.machine_resolver import registry_get

    try:
        repo_key = session_change._resolve_repo_key(repo_root)
    except session_change.P4SessionChangeError:
        return False
    return registry_get(f"p4.{repo_key}.p4ignore_absent") == "true"


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

    # D4b step 0 — hoisted ahead of reconcile: one fstat read drives both
    # D4b's orphan adoption (right here, `change`) and D4a's read-only-bit
    # restore (at the back, `haveRev`) — one read, two consumers, zero
    # extra spawns beyond this single call.
    fstat_result = runner.run(
        identity.port,
        identity.user,
        identity.client,
        ["-d", repo_root, "-ztag", "fstat", "-T", "clientFile,haveRev,change,action", *paths],
    )
    if not fstat_result.ok:
        return ShelveOutcome(
            ok=False, paths=paths, cl=effective_cl, remint=remint, error=fstat_result.error
        )

    records = _fstat_records(fstat_result.stdout)
    per_path, unreconciled = _key_fstat_records_by_path(records, paths)
    if unreconciled:
        return ShelveOutcome(
            ok=False,
            paths=paths,
            cl=effective_cl,
            remint=remint,
            error=runner.P4Error(
                kind="refused",
                raw=(
                    "fstat returned record(s) that could not be matched by "
                    f"clientFile to the requested path set: {unreconciled}"
                ),
            ),
        )

    orphan_paths = [
        p
        for p, rec in per_path.items()
        if rec and rec.get("change") and rec.get("change") != str(effective_cl)
    ]
    adopted = 0
    if orphan_paths:
        reopen_result = runner.run(
            identity.port,
            identity.user,
            identity.client,
            ["-d", repo_root, "reopen", "-c", str(effective_cl), *orphan_paths],
        )
        if not reopen_result.ok:
            return ShelveOutcome(
                ok=False, paths=paths, cl=effective_cl, remint=remint, error=reopen_result.error
            )
        adopted = len(orphan_paths)

    restore_candidates = [
        p for p, rec in per_path.items() if rec and rec.get("haveRev") and not rec.get("action")
    ]

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
        #
        # `-a` is dropped when `.p4ignore` was authored by registration from
        # scratch (F3, `_p4ignore_absent` above) -- the row's own message
        # promised this and it was previously unkept.
        [
            "-d",
            repo_root,
            "reconcile",
            "-c",
            str(effective_cl),
            "-e",
            *([] if _p4ignore_absent(repo_root) else ["-a"]),
            "-d",
            *paths,
        ],
    )
    if not result.ok:
        return ShelveOutcome(ok=False, paths=paths, cl=effective_cl, remint=remint, error=result.error)

    result = runner.run(
        identity.port, identity.user, identity.client, ["revert", "-a", "-c", str(effective_cl)]
    )
    if not result.ok:
        return ShelveOutcome(ok=False, paths=paths, cl=effective_cl, remint=remint, error=result.error)

    # D4a — the back of the sequence: restore the read-only bit on every
    # path that was synced-and-unopened at step 0 (mode-only-drift residue).
    restored = _restore_read_only(repo_root, restore_candidates) if restore_candidates else 0

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
        restored=restored,
        adopted=adopted,
    )
