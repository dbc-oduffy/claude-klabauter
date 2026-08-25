"""
coordinator_core.ops.decision_record_mint — "decision_record.mint_id" op.

Purpose: allocate the next free decision-record (DR) number AND reserve it,
so a plan body carries an already-claimed number rather than a guessed
``max + 1`` that can go stale between authoring and execution (hours to
days on this box's plan lifecycle). Reservation is the substance of this op
— see `state/improvement-queue/2026-08-23-nothing-allocates-dr-numbers-so-a-
plan-s-7aa417a58bce.yaml`, filed after `docs/plans/2026-08-22-a-commit-is-
one-spawn-not-eleven.md` was authored against `DR-351` and a peer committed
an unrelated record under that same number before it executed
(`9bd982a76`) — the executor caught the collision and refused, but it cost a
dispatch, a stopped workflow run, and a renumber across 17 sites.

Reservation mechanism (the choice this docstring justifies)
-------------------------------------------------------------
A reservation is a small marker file, one per number, created via
``os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)`` — the same primitive
Windows and POSIX both give an atomic "create iff absent" guarantee for
(NTFS honours O_EXCL exactly like a POSIX filesystem; Python exposes both
flags on every platform). This is deliberately NOT `coordinator_core.
locked_write.locked_rmw` or `held_lock`: those serialise a read-modify-write
or a scope around ONE named target that every caller agrees on in advance.
Minting has no such fixed target — two concurrent callers must be able to
land on DIFFERENT numbers without waiting on each other at all, and the
number a call gets is not known until the scan below picks a candidate. An
`O_CREAT|O_EXCL` race on the candidate PATH itself is exactly a
non-blocking, per-number "first writer wins" primitive, no lock file, no
holder to time out waiting for, no separate lock namespace to keep in sync.
The reservation record lives entirely inside `state/`, this repo's own
authoritative disk-truth per CLAUDE.md § Architecture — never inside
`docs/decisions/` itself, so a caller's `ls docs/decisions/*.md` (the exact
scan this op replaces) never sees reservation bookkeeping mixed into the
DR corpus.

Reservations are UNTRACKED (`.gitignore`'s new `state/decision-record-
reservations/` entry): every session on this box shares ONE working tree
(CLAUDE.md § Engineering Defaults — "parallel agents share one tree"), so a
plain on-disk marker is visible to every concurrent mint call without any
git round-trip, and nothing outside this tree needs to see it.

No process spawn: the whole algorithm is `Path.iterdir()`/`os.open`/
`os.unlink` — no `git`, matching the brightline's "process creation is the
cost" framing (CLAUDE.md § The brightline) for what should be a
sub-millisecond, in-process allocation.

Reclamation (stated per the remit's own requirement — a reserved-but-never-
used number must not leak silently)
-------------------------------------------------------------------------
Every mint call first sweeps `_RESERVATIONS_DIR` and deletes any reservation
file older than `_RESERVATION_TTL_DAYS` (14 days — well past the "hours to
days" staleness window the originating incident described, so a live plan's
reservation is never swept while its author is still working, but an
abandoned one does not survive indefinitely). Deleting the stale file, not
merely skipping it, is what makes the number reclaimable rather than
permanently retired: the candidate-selection scan below only ever floors
against reservations still on disk, so a swept file's number becomes a
normal candidate on the very next mint. A caller that finishes with a
reservation early (a plan is abandoned before it ever reaches a DR body) can
also call `decision_record.release_id` to delete it immediately rather than
waiting out the TTL — see that op's own docstring.

Candidate selection + the actual race
----------------------------------------
1. Scan `docs/decisions/DR-<N>-*.md` (and the bare `DR-<N>.md` shape) for the
   highest existing `N`.
2. Sweep expired reservations (above), then scan what remains for the
   highest reserved `N`.
3. candidate = max(existing_max, reserved_max) + 1.
4. Attempt `os.open(reservation_path(candidate), O_CREAT|O_EXCL|O_WRONLY)`.
   Success -> that candidate is now reserved; write holder metadata; return
   it. `FileExistsError` (a concurrent caller won the same candidate, or an
   unswept file sits at that exact number) -> `candidate += 1`, retry.
   Bounded at `_MAX_MINT_ATTEMPTS` (1000) — a caller that cannot find a free
   number in 1000 tries has a bug elsewhere, not a numbering problem; this op
   fails loud rather than spinning.

Step 4 is the entire concurrency guarantee. Steps 1-3 merely PICK a starting
point cheaply; they are not required to be race-free, because two callers
computing the identical candidate from a stale scan still cannot both win
the create at step 4 — one gets the file, the other gets `FileExistsError`
and moves on to the next integer. This is the property
`test_decision_record_mint.py::test_concurrent_mints_never_collide` proves
empirically (real OS-level processes, not threads sharing one interpreter's
GIL-serialised `open()` — see that test's own docstring for why threads
would not have exercised this).

Negative-spec:
    - Do NOT add a lock file around this algorithm. The reservation create
      IS the lock, scoped to exactly one number instead of the whole
      directory — a directory-wide lock would serialise unrelated concurrent
      mints for no correctness benefit and directly worsens the "50-70
      concurrent sessions" load profile this op exists to survive.
    - Do NOT spawn `git` to find the current DR max. `docs/decisions/` is a
      plain directory on the SAME shared working tree every caller already
      has open; a directory listing answers the question with no process
      creation at all.
    - Do NOT write the reservation file with `Path.write_text` — this repo's
      `core.autocrlf=true` turns `\n` into `\r\n` on write, and while this
      particular file is never git-tracked (state/decision-record-
      reservations/ is gitignored), the raw-bytes convention is kept
      uniform with every other engine writer that touches this directory
      tree, per CLAUDE.md § Runtime conventions.
    - Do NOT treat an emptied-but-present reservation directory as "no
      reservations ever" — `_RESERVATIONS_DIR` is created lazily by the
      first mint call in a fresh clone; its absence means zero reservations
      outstanding, not an error.
    - Do NOT make the TTL sweep required for correctness. It is purely a
      leak-avoidance housekeeping step; a mint call would still be correct
      (just wasteful of number-space) if the sweep silently failed to
      delete a stale file, because `os.rmdir`/`unlink` errors are caught and
      ignored — a permissions problem sweeping stale reservations must never
      block minting a fresh one.

Spec backlink: state/improvement-queue/2026-08-23-nothing-allocates-dr-
numbers-so-a-plan-s-7aa417a58bce.yaml
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root

_DECISIONS_DIRNAME = "decisions"
_DECISIONS_PARENT = "docs"
_RESERVATIONS_RELDIR = Path("state") / "decision-record-reservations"

_DR_FILENAME_RE = re.compile(r"^DR-(\d+)(?:-.*)?\.md$", re.IGNORECASE)
_RESERVATION_FILENAME_RE = re.compile(r"^DR-(\d+)\.reserved$", re.IGNORECASE)

_RESERVATION_TTL_DAYS = 14
_MAX_MINT_ATTEMPTS = 1000


def _decisions_dir(worktree_root: Path) -> Path:
    return worktree_root / _DECISIONS_PARENT / _DECISIONS_DIRNAME


def _reservations_dir(worktree_root: Path) -> Path:
    return worktree_root / _RESERVATIONS_RELDIR


def _reservation_path(reservations_dir: Path, number: int) -> Path:
    return reservations_dir / f"DR-{number}.reserved"


def _existing_dr_max(decisions_dir: Path) -> int:
    """Highest `N` among `docs/decisions/DR-<N>[-*].md`, or 0 if none/absent."""
    if not decisions_dir.is_dir():
        return 0
    best = 0
    for entry in decisions_dir.iterdir():
        if not entry.is_file():
            continue
        m = _DR_FILENAME_RE.match(entry.name)
        if m:
            best = max(best, int(m.group(1)))
    return best


def _sweep_expired_reservations(reservations_dir: Path) -> None:
    """Delete reservation files older than `_RESERVATION_TTL_DAYS`, best-effort.

    Failure to delete (permissions, a concurrent unlink already won) is
    swallowed — see module docstring's negative-spec: the sweep is
    housekeeping, never a correctness dependency.
    """
    if not reservations_dir.is_dir():
        return
    cutoff = datetime.now(timezone.utc).timestamp() - (_RESERVATION_TTL_DAYS * 86400)
    for entry in reservations_dir.iterdir():
        if not entry.is_file() or not _RESERVATION_FILENAME_RE.match(entry.name):
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            pass


def _reserved_max(reservations_dir: Path) -> int:
    """Highest `N` among reservation files still on disk after the sweep, or 0."""
    if not reservations_dir.is_dir():
        return 0
    best = 0
    for entry in reservations_dir.iterdir():
        if not entry.is_file():
            continue
        m = _RESERVATION_FILENAME_RE.match(entry.name)
        if m:
            best = max(best, int(m.group(1)))
    return best


def mint_next_dr_id(worktree_root: Path, *, holder: str = "", title: str = "") -> int:
    """Allocate + reserve the next free DR number under `worktree_root`. Returns the int.

    See module docstring for the full algorithm and the concurrency
    guarantee (step 4's `O_CREAT|O_EXCL`, not the scan, is what makes this
    race-safe). Raises `RuntimeError` if `_MAX_MINT_ATTEMPTS` consecutive
    candidates are all taken.
    """
    decisions_dir = _decisions_dir(worktree_root)
    reservations_dir = _reservations_dir(worktree_root)
    reservations_dir.mkdir(parents=True, exist_ok=True)

    _sweep_expired_reservations(reservations_dir)

    candidate = max(_existing_dr_max(decisions_dir), _reserved_max(reservations_dir)) + 1

    payload = json.dumps(
        {
            "reserved_at": datetime.now(timezone.utc).isoformat(),
            "holder": holder,
            "title": title,
            "pid": os.getpid(),
        }
    ).encode("utf-8") + b"\n"

    for _ in range(_MAX_MINT_ATTEMPTS):
        path = _reservation_path(reservations_dir, candidate)
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            candidate += 1
            continue
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        return candidate

    raise RuntimeError(
        f"decision_record.mint_id: exhausted {_MAX_MINT_ATTEMPTS} consecutive "
        f"candidates starting at {candidate - _MAX_MINT_ATTEMPTS}; something "
        "other than numbering is wrong (a filler process, a stuck sweep, or a "
        "corrupt reservations directory)."
    )


def release_dr_id(worktree_root: Path, number: int) -> bool:
    """Delete the reservation for `number`, if present. Returns whether it existed.

    Explicit early-release path for a caller whose reservation will never
    reach a DR body (an abandoned plan) — see module docstring's
    Reclamation section. Never touches `docs/decisions/` itself: releasing
    a number that has ALREADY been written as a real DR file is a no-op on
    the (already-vacated-by-the-first-mint-consumer) reservation file only.
    """
    path = _reservation_path(_reservations_dir(worktree_root), number)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------------------
# JSON-RPC handlers
# ---------------------------------------------------------------------------


@register_op("decision_record.mint_id")
def _mint_handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC ``decision_record.mint_id`` handler — see module docstring.

    ``repo_root`` receives ``git_common_dir(caller_worktree)`` via the
    ``_OP_KEY_SCOPE: common_dir`` mechanism (ipc.py) — same keying class as
    ``queue.append``. The handler calls ``main_worktree_root(repo_root)`` to
    derive the caller's worktree root before any ``docs/``/``state/`` path
    construction.

    Optional params:
        holder (str) — caller-supplied label (e.g. a plan slug or session
                id) recorded in the reservation's metadata for operator
                debugging. Never validated or required.
        title  (str) — caller-supplied decision title, recorded the same way.

    Returns:
        exit_code int    0=ok, 1=error
        error     str|None
        id        str|None  "DR-<N>" (the canonical on-disk spelling)
        number    int|None
    """
    if repo_root is None:
        return {"exit_code": 1, "error": "decision_record.mint_id: unresolvable repo root",
                "id": None, "number": None}
    worktree_root = main_worktree_root(repo_root)

    holder = params.get("holder") or ""
    title = params.get("title") or ""

    try:
        number = mint_next_dr_id(worktree_root, holder=str(holder), title=str(title))
    except Exception as exc:  # noqa: BLE001 — surfaced verbatim, no fabricated shape
        return {"exit_code": 1, "error": str(exc), "id": None, "number": None}

    return {"exit_code": 0, "error": None, "id": f"DR-{number}", "number": number}


@register_op("decision_record.release_id")
def _release_handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC ``decision_record.release_id`` handler — early-release a reservation.

    Required params:
        number (int) — the DR number to release.

    Returns:
        exit_code int   0=ok, 1=error
        error     str|None
        released  bool  whether a reservation existed and was deleted
    """
    if repo_root is None:
        return {"exit_code": 1, "error": "decision_record.release_id: unresolvable repo root",
                "released": False}
    worktree_root = main_worktree_root(repo_root)

    number = params.get("number")
    if not isinstance(number, int):
        return {"exit_code": 1, "error": "decision_record.release_id requires an integer `number`",
                "released": False}

    released = release_dr_id(worktree_root, number)
    return {"exit_code": 0, "error": None, "released": released}
