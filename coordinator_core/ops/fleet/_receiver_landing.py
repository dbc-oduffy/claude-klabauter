"""Which ref a memo delivery lands on in a receiver clone, and whether HEAD may move.

``resolve_receiver_landing`` only reads and decides; ``apply_receiver_landing`` mints the
day-branch ref and switches HEAD. Both are zero-spawn (ref/symref CAS through
``git_objects``). Under no arm does a delivery land on ``main``.

Negative-spec -- do NOT call ``session_ensure_branch`` (it spawns ``git checkout -b`` in the
cwd and pushes inline) or a ``git switch`` subprocess. Only its day-branch name derivation
and ``day_branch_cut_lock`` are reused.

Negative-spec -- HEAD moves only off a clean-attached ``main``. Any other non-``work/*``
branch, a detached HEAD, or a mid-rebase/merge/bisect/cherry-pick checkout keeps its
checkout; the delivery goes ``ref-direct`` onto the day-branch ref (DR-214 A1 hazard).
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple, Optional

from coordinator_core import daily_branch, daily_day, machine_resolver
from coordinator_core.git import git_objects
from coordinator_core.git.git_dir import resolve_git_common_dir, resolve_git_dir
from coordinator_core.session import day_branch_cut_lock

MODE_HEAD = "head"
MODE_SWITCHED = "switched"
MODE_REF_DIRECT = "ref-direct"

_HEADS = "refs/heads/"
_MID_OPERATION_MARKERS = (
    "rebase-merge",
    "rebase-apply",
    "MERGE_HEAD",
    "BISECT_LOG",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
)


class ReceiverLanding(NamedTuple):
    """`ref_relpath` is the ref the delivery commit lands on. On a decision `minted` and
    `switched` mean "apply will mint / switch"; on an applied landing, "did mint / did switch"."""

    mode: str
    ref_relpath: str
    minted: bool
    switched: bool
    root: Optional[Path] = None
    mint_sha: Optional[str] = None
    from_ref: Optional[str] = None


def _mid_operation(head_gitdir: Path) -> bool:
    return any((head_gitdir / marker).exists() for marker in _MID_OPERATION_MARKERS)


def resolve_receiver_landing(receiver_repo_path: str | Path) -> ReceiverLanding:
    root = Path(receiver_repo_path)
    head_gitdir = resolve_git_dir(root)
    common_dir = resolve_git_common_dir(root)
    head_ref = git_objects._head_symref_target(head_gitdir)
    head_branch = head_ref[len(_HEADS):] if head_ref and head_ref.startswith(_HEADS) else None

    if head_ref and head_branch is not None and (
        daily_branch.is_work_branch(head_branch)
        or daily_branch.read_configured_day_branch(root) == head_branch
    ):
        return ReceiverLanding(MODE_HEAD, head_ref, False, False, root, None, head_ref)

    day_ref = f"{_HEADS}work/{machine_resolver.compute_machine()}/{daily_day.local_day(str(root))}"
    day_sha = git_objects.read_ref_loose_or_packed(common_dir, day_ref)
    head_sha = git_objects.read_ref_loose_or_packed(common_dir, head_ref) if head_ref else None
    clean_main = head_ref == f"{_HEADS}main" and not _mid_operation(head_gitdir)

    if clean_main and head_sha and (day_sha is None or day_sha == head_sha):
        return ReceiverLanding(
            MODE_SWITCHED, day_ref, day_sha is None, True, root, head_sha, head_ref
        )

    if day_sha is not None:
        return ReceiverLanding(MODE_REF_DIRECT, day_ref, False, False, root, None, head_ref)

    mint_sha = git_objects.read_ref_loose_or_packed(common_dir, f"{_HEADS}main") or git_objects.read_ref_loose_or_packed(common_dir, f"{_HEADS}master")
    return ReceiverLanding(
        MODE_REF_DIRECT, day_ref, mint_sha is not None, False, root, mint_sha or head_sha, head_ref
    )


def _reflog_committer(root: Path) -> Optional[str]:
    from coordinator_core.ops.ceremony import git_native

    identity = git_native._resolve_commit_identity(root)
    if identity is None:
        return None
    name, email = identity
    return f"{name} <{email}> {git_native._author_stamp()}"


def apply_receiver_landing(landing: ReceiverLanding) -> ReceiverLanding:
    """Mint and switch as `landing` decided; returns what actually happened.

    A lost cut lock, a lost symref CAS, or a `main` tip that moved since the decision degrades
    `switched` to `ref-direct`. A lost mint CAS is benign when the ref now exists.
    """
    if landing.mode == MODE_HEAD or landing.root is None:
        return landing
    root = landing.root
    common_dir = resolve_git_common_dir(root)
    head_gitdir = resolve_git_dir(root)
    ref = landing.ref_relpath

    degraded = landing._replace(mode=MODE_REF_DIRECT, switched=False)
    holds_lock = day_branch_cut_lock.acquire(root).acquired
    try:
        minted = False
        if landing.minted and landing.mint_sha:
            minted = git_objects.cas_ref(common_dir, ref, None, landing.mint_sha)
        if not git_objects.read_ref_loose_or_packed(common_dir, ref):
            return degraded._replace(minted=False)
        if landing.mode != MODE_SWITCHED:
            return degraded._replace(minted=minted)
        if not holds_lock or git_objects.read_ref_loose_or_packed(common_dir, landing.from_ref or "") != landing.mint_sha:
            return degraded._replace(minted=minted)
        if git_objects.read_ref_loose_or_packed(common_dir, ref) != landing.mint_sha:
            return degraded._replace(minted=minted)
        committer = _reflog_committer(root)
        from_branch = (landing.from_ref or "")[len(_HEADS):]
        switched = git_objects.cas_head_symref(
            head_gitdir,
            landing.from_ref or "",
            ref,
            reflog_committer=committer,
            reflog_message=f"checkout: moving from {from_branch} to {ref[len(_HEADS):]}",
            sha=landing.mint_sha,
        )
        if not switched:
            return degraded._replace(minted=minted)
        return landing._replace(minted=minted, switched=True)
    finally:
        if holds_lock:
            day_branch_cut_lock.release(root)
