"""Which ref a memo delivery lands on in a receiver clone: the receiver repo's live work branch.

The live branch is the ``work/*`` tip with the newest committer time among the clone's local
``refs/heads/work/*`` and every remote's ``refs/remotes/<remote>/work/*`` (loose + packed-refs).
The name is read off the receiver's refs, never derived from the sending box or from which
branch the clone happens to have checked out. Everything here is zero-spawn.

``resolve_receiver_landing`` only reads and decides; ``apply_receiver_landing`` mints the local
branch ref when the winner is remote-only, and fast-forwards a local ref that trails the winning
remote tip. Under no arm does a delivery land on ``main``.

Negative-spec -- HEAD never moves on any arm; delivery goes ref-direct onto the branch ref
(DR-214 A1 hazard), or ``MODE_HEAD`` when HEAD already sits on it.

Negative-spec -- do NOT call ``session_ensure_branch`` (it spawns ``git checkout -b`` in the
cwd and pushes inline) or a ``git switch`` subprocess.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import NamedTuple, Optional

from coordinator_core import daily_branch
from coordinator_core.git import git_objects
from coordinator_core.git.git_dir import resolve_git_common_dir, resolve_git_dir

MODE_HEAD = "head"
MODE_REF_DIRECT = "ref-direct"
MODE_NONE = "none"

_HEADS = "refs/heads/"
_REMOTES = "refs/remotes/"
_WORK = "work/"
_HEX40 = re.compile(r"[0-9a-f]{40}")


class ReceiverLanding(NamedTuple):
    """`ref_relpath` is the ref the delivery commit lands on (empty for `MODE_NONE`, a receiver
    with no live work branch). On a decision `minted` means "apply will mint the local ref at
    `mint_sha`"; on an applied landing, "did mint". `advance_from` set: the local ref sits behind
    the winning remote tip (its ancestor) and apply fast-forwards it from `advance_from` to
    `mint_sha`."""

    mode: str
    ref_relpath: str
    minted: bool
    switched: bool
    root: Optional[Path] = None
    mint_sha: Optional[str] = None
    from_ref: Optional[str] = None
    advance_from: Optional[str] = None


def _walk_loose(base: Path, prefix: str, out: dict[str, str]) -> None:
    if not base.is_dir():
        return
    for dirpath, _dirs, files in os.walk(base):
        for name in files:
            if name.endswith(".lock"):
                continue
            path = Path(dirpath) / name
            sha = git_objects._read_ref_raw(path)
            if sha and _HEX40.fullmatch(sha):
                rel = path.relative_to(base).as_posix()
                out[prefix + rel] = sha


def _work_refs(common_dir: Path) -> dict[str, str]:
    """Full ref name -> sha for every local and remote-tracking `work/*` ref; loose wins over packed.

    Symrefs (`refs/remotes/<r>/HEAD`) never match: only `work/*` subtrees are scanned and a
    non-hex ref body is dropped.
    """
    refs: dict[str, str] = {}
    try:
        text = (common_dir / "packed-refs").read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    for line in text.splitlines():
        if not line or line[0] in "#^":
            continue
        sha, _, name = line.partition(" ")
        name = name.strip()
        if not _HEX40.fullmatch(sha):
            continue
        if name.startswith(_HEADS + _WORK):
            refs[name] = sha
        elif name.startswith(_REMOTES) and name[len(_REMOTES):].partition("/")[2].startswith(_WORK):
            refs[name] = sha
    _walk_loose(common_dir / "refs" / "heads" / "work", _HEADS + _WORK, refs)
    remotes = common_dir / "refs" / "remotes"
    if remotes.is_dir():
        for remote in remotes.iterdir():
            _walk_loose(remote / "work", f"{_REMOTES}{remote.name}/{_WORK}", refs)
    return refs


def _branch_of(ref: str) -> str:
    if ref.startswith(_HEADS):
        return ref[len(_HEADS):]
    return ref[len(_REMOTES):].partition("/")[2]


def _committer_time(common_dir: Path, sha: str) -> int:
    """Committer epoch from the commit header; 0 when the object is unreadable."""
    obj = git_objects.read_object(common_dir, sha)
    if obj is None or obj[0] != "commit":
        return 0
    for line in obj[1].partition(b"\n\n")[0].split(b"\n"):
        if line.startswith(b"committer "):
            fields = line.rsplit(b" ", 2)
            try:
                return int(fields[-2])
            except (ValueError, IndexError):
                return 0
    return 0


def _live_work_branch(common_dir: Path, head_branch: Optional[str]) -> Optional[tuple[str, str]]:
    """`(branch, tip_sha)` of the `work/*` ref that moved last, or None when there is none.

    Equal committer times tie-break to the HEAD branch, then lexically on the ref name.
    """
    refs = _work_refs(common_dir)
    if not refs:
        return None
    times = {sha: _committer_time(common_dir, sha) for sha in set(refs.values())}
    ref = min(
        refs,
        key=lambda r: (-times[refs[r]], _branch_of(r) != head_branch, r),
    )
    return _branch_of(ref), refs[ref]


_ANCESTRY_WALK_LIMIT = 4096


def _is_ancestor(common_dir: Path, ancestor: str, tip: str) -> bool:
    """True when `ancestor` is reachable from `tip` within the walk limit; any unreadable object ends it False."""
    seen: set[str] = set()
    stack = [tip]
    while stack and len(seen) < _ANCESTRY_WALK_LIMIT:
        sha = stack.pop()
        if sha == ancestor:
            return True
        if sha in seen:
            continue
        seen.add(sha)
        obj = git_objects.read_object(common_dir, sha)
        if obj is None or obj[0] != "commit":
            continue
        for line in obj[1].partition(b"\n\n")[0].split(b"\n"):
            if line.startswith(b"parent "):
                stack.append(line[7:].decode("ascii", "replace").strip())
    return False


def _tracking_tip(common_dir: Path, branch: str) -> Optional[str]:
    remotes = common_dir / "refs" / "remotes"
    names = sorted(p.name for p in remotes.iterdir()) if remotes.is_dir() else []
    for name in names:
        sha = git_objects.read_ref_loose_or_packed(common_dir, f"{_REMOTES}{name}/{branch}")
        if sha:
            return sha
    return None


def resolve_receiver_landing(receiver_repo_path: str | Path) -> ReceiverLanding:
    root = Path(receiver_repo_path)
    common_dir = resolve_git_common_dir(root)
    head_ref = git_objects._head_symref_target(resolve_git_dir(root))
    head_branch = head_ref[len(_HEADS):] if head_ref and head_ref.startswith(_HEADS) else None

    live = _live_work_branch(common_dir, head_branch)
    if live is not None:
        branch, tip = live
        ref = _HEADS + branch
        local = git_objects.read_ref_loose_or_packed(common_dir, ref)
        mode = MODE_HEAD if branch == head_branch else MODE_REF_DIRECT
        if local is None:
            return ReceiverLanding(mode, ref, True, False, root, tip, head_ref)
        if mode == MODE_REF_DIRECT and local != tip and _is_ancestor(common_dir, local, tip):
            return ReceiverLanding(mode, ref, True, False, root, tip, head_ref, local)
        return ReceiverLanding(mode, ref, False, False, root, None, head_ref)

    configured = daily_branch.read_configured_day_branch(root)
    if configured:
        ref = _HEADS + configured
        mode = MODE_HEAD if configured == head_branch else MODE_REF_DIRECT
        if git_objects.read_ref_loose_or_packed(common_dir, ref) is not None:
            return ReceiverLanding(mode, ref, False, False, root, None, head_ref)
        seed = (
            _tracking_tip(common_dir, configured)
            or git_objects.read_ref_loose_or_packed(common_dir, f"{_HEADS}main")
            or git_objects.read_ref_loose_or_packed(common_dir, f"{_HEADS}master")
        )
        return ReceiverLanding(mode, ref, seed is not None, False, root, seed, head_ref)

    return ReceiverLanding(MODE_NONE, "", False, False, root, None, head_ref)


def apply_receiver_landing(landing: ReceiverLanding) -> ReceiverLanding:
    """Mint the local branch ref at `landing.mint_sha` when the decision said to; HEAD never moves.

    A lost mint CAS is benign when the ref now exists; otherwise `minted` reads False.
    """
    if landing.mode == MODE_NONE or landing.root is None or not landing.minted or not landing.mint_sha:
        return landing
    common_dir = resolve_git_common_dir(landing.root)
    ref = landing.ref_relpath
    minted = git_objects.cas_ref(common_dir, ref, landing.advance_from, landing.mint_sha)
    if not git_objects.read_ref_loose_or_packed(common_dir, ref):
        return landing._replace(minted=False)
    return landing._replace(minted=minted)
