"""coordinator_core.warm.publish_branch_retire -- retire merged `publish/<sha>` branches on a remote.

Every klabauter publish pushes `refs/heads/publish/<sha>` (`PUBLISH_REF_PREFIX`)
and nothing else removes it. `retire_merged_publish_branches` deletes one from the remote only when
its tip is verified an ancestor of a KEPT remote branch (`main` or `work/*`), so no commit is lost.

Spawns per call, independent of branch count: one `ls-remote`, one `cat-file --batch-check`, one
`rev-list --stdin`, one batched `push`. The caller (`push_cadence.sweep_repos`) is the only
production caller and runs it off the hot path.

Safety: only refs under `PUBLISH_REF_PREFIX` are ever named in the delete refspecs; each delete
carries a lease on the verified tip, so a ref moved since `ls-remote` is not deleted. A kept ref
whose tip is not in the local object store is not used as proof (fewer proofs, never a wrong one).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

from coordinator_core.ops.ceremony import git_native

# Defined here, not in percolate.source_preflight: this module ships in the klabauter engine tree and
# percolate/ does not, so the push side imports the prefix from here.
PUBLISH_REF_PREFIX = "refs/heads/publish/"

__all__ = [
    "KEPT_REFS",
    "PUBLISH_REF_PREFIX",
    "KEPT_REF_PREFIXES",
    "MAX_RETIRED_PER_CALL",
    "RetireOutcome",
    "retire_merged_publish_branches",
]

#: Exact kept heads and kept head prefixes. A kept ref is proof only; it is never a delete target.
KEPT_REFS = ("refs/heads/main",)
KEPT_REF_PREFIXES = ("refs/heads/work/",)

#: One push carries at most this many deletes; the remainder retires on a later call.
MAX_RETIRED_PER_CALL = 200


@dataclass(frozen=True)
class RetireOutcome:
    """`deleted`: publish refs the batched push asked the remote to delete (empty when none
    qualified or the push failed). `error`: the first failure text, or None."""

    deleted: tuple = ()
    error: Optional[str] = None


def _is_kept(ref: str) -> bool:
    return ref in KEPT_REFS or ref.startswith(KEPT_REF_PREFIXES)


def _parse_ls_remote(text: str) -> "dict[str, str]":
    heads: "dict[str, str]" = {}
    for line in text.splitlines():
        sha, _, ref = line.partition("\t")
        ref = ref.strip()
        if len(sha) >= 40 and ref.startswith("refs/heads/") and not ref.endswith("^{}"):
            heads[ref] = sha
    return heads


def retire_merged_publish_branches(
    root: Union[str, Path], remote: str, *, budget_secs: float
) -> RetireOutcome:
    """Delete every `publish/<sha>` branch on `remote` whose tip is an ancestor of a kept branch.

    Never raises; a failure of any step returns `RetireOutcome(error=...)` with nothing deleted
    beyond what a completed push already did. `budget_secs` bounds the whole call.
    """
    try:
        return _retire(Path(root), remote, budget_secs)
    except Exception as exc:  # noqa: BLE001 -- the sweep must never see an exception from here
        return RetireOutcome(error=repr(exc)[:500])


def _retire(root: Path, remote: str, budget_secs: float) -> RetireOutcome:
    end = time.monotonic() + budget_secs

    def remaining() -> float:
        return max(end - time.monotonic(), 0.5)

    listing = git_native._git(["ls-remote", "--heads", remote], cwd=root, timeout=remaining())
    if not listing.ok:
        return RetireOutcome(error=f"ls-remote failed: {(listing.stderr or '').strip()[:400]}")
    heads = _parse_ls_remote(listing.stdout)
    candidates = {ref: sha for ref, sha in heads.items() if ref.startswith(PUBLISH_REF_PREFIX)}
    kept = {ref: sha for ref, sha in heads.items() if _is_kept(ref)}
    if not candidates or not kept:
        return RetireOutcome()

    probe = sorted(set(candidates.values()) | set(kept.values()))
    check = git_native._git(
        ["cat-file", "--batch-check"], cwd=root, timeout=remaining(),
        input_data="".join(f"{sha}\n" for sha in probe),
    )
    if not check.ok:
        return RetireOutcome(error=f"cat-file failed: {(check.stderr or '').strip()[:400]}")
    present = {
        parts[0]
        for parts in (line.split() for line in check.stdout.splitlines())
        if len(parts) >= 2 and parts[1] == "commit"
    }
    kept_tips = sorted({sha for sha in kept.values() if sha in present})
    tips = sorted({sha for sha in candidates.values() if sha in present})
    if not kept_tips or not tips:
        return RetireOutcome()

    walk = git_native._git(
        ["rev-list", "--stdin"], cwd=root, timeout=remaining(),
        input_data="".join(f"{sha}\n" for sha in tips)
        + "".join(f"^{sha}\n" for sha in kept_tips),
    )
    if not walk.ok:
        return RetireOutcome(error=f"rev-list failed: {(walk.stderr or '').strip()[:400]}")
    unmerged = set(walk.stdout.split())

    doomed = sorted(
        (ref, sha)
        for ref, sha in candidates.items()
        if sha in present and sha not in unmerged and ref.startswith(PUBLISH_REF_PREFIX)
    )[:MAX_RETIRED_PER_CALL]
    if not doomed:
        return RetireOutcome()

    argv = ["push", remote]
    argv += [f"--force-with-lease={ref}:{sha}" for ref, sha in doomed]
    argv += [f":{ref}" for ref, _sha in doomed]
    pushed = git_native._git(argv, cwd=root, timeout=remaining())
    if not pushed.ok:
        return RetireOutcome(error=f"delete push failed: {(pushed.stderr or '').strip()[:400]}")
    return RetireOutcome(deleted=tuple(ref for ref, _sha in doomed))
