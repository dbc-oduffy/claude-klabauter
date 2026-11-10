"""Stale-base detector for a scratch ``GIT_INDEX_FILE`` index about to be committed.

An index built on an older HEAD, committed after a peer commit landed, writes a
tree that undoes the peer's change. ``stale_base_reverted_paths`` finds that
shape from the index alone: a staged blob that equals the pre-image of a recent
first-parent HEAD commit.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

_RECENT_COMMITS = 20

_PROBE_TIMEOUT = 5.0


def _raw_entry(line: str) -> Optional[Tuple[str, str, str]]:
    """``(pre_blob, post_blob, path)`` of one ``:<m1> <m2> <sha1> <sha2> <S>\\t<path>`` record."""
    if not line.startswith(":") or "\t" not in line:
        return None
    meta, _, path = line.partition("\t")
    fields = meta.split()
    if len(fields) < 5:
        return None
    return fields[2], fields[3], path


def stale_base_reverted_paths(
    index_file: str,
    cwd: Optional[str],
    run_git: Callable[..., Tuple[int, str]],
) -> Optional[List[str]]:
    """Sorted paths where the index at ``index_file`` undoes a recent HEAD commit.

    A path counts when its staged blob differs from HEAD's and equals the
    pre-image of ANY of the last ``_RECENT_COMMITS`` first-parent commits that
    touched it. A deliberate deletion of a file added within the window, or a
    hand revert of a recent edit, is indistinguishable from a stale base and is
    named too. ``None`` on a probe failure (fail open). ``run_git`` takes
    ``(args, cwd, timeout=, extra_env=)`` and returns ``(returncode, stdout)``.
    """
    rc, staged_out = run_git(
        ["diff-index", "--cached", "--raw", "--no-abbrev", "--no-renames", "HEAD"],
        cwd, timeout=_PROBE_TIMEOUT, extra_env={"GIT_INDEX_FILE": index_file},
    )
    if rc != 0:
        return None
    staged: Dict[str, Tuple[str, str]] = {}
    for line in staged_out.splitlines():
        entry = _raw_entry(line)
        if entry is not None:
            staged[entry[2]] = (entry[0], entry[1])
    if not staged:
        return []

    rc, log_out = run_git(
        ["log", "-n", str(_RECENT_COMMITS), "--first-parent", "--format=%H",
         "--raw", "--no-abbrev", "--no-renames", "HEAD"],
        cwd, timeout=_PROBE_TIMEOUT,
    )
    if rc != 0:
        return None
    pre_images: Dict[str, set] = {}
    for line in log_out.splitlines():
        entry = _raw_entry(line)
        if entry is not None:
            pre_images.setdefault(entry[2], set()).add(entry[0])

    return sorted(
        path
        for path, (head_blob, index_blob) in staged.items()
        if index_blob != head_blob and index_blob in pre_images.get(path, ())
    )
