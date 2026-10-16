"""Paths a merge in progress brings in, so the foreign-hunk check can let a merge conclusion stage them."""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Set, Tuple


def merge_incoming_paths(
    seq_probe: Callable[[List[str], Optional[Dict[str, Any]]], Optional[Tuple[str, str]]],
    run_git: Callable[..., Tuple[int, str]],
    seg_tokens: List[str],
    payload: Optional[Dict[str, Any]],
    cwd: Optional[str],
) -> Set[str]:
    """Paths differing between HEAD and MERGE_HEAD while a merge is in progress.

    Empty -- the deny stays -- when no merge is in progress or any probe fails. Path-wide
    by design: a foreign edit to an incoming path made after the merge staged is also
    exempt, because the index cannot tell the two apart.
    """
    seq = seq_probe(seg_tokens, {"cwd": cwd} if cwd else payload)
    if seq is None or seq[0] != "MERGE_HEAD":
        return set()
    rc, out = run_git(["diff", "--name-only", "HEAD", "MERGE_HEAD"], cwd)
    return {ln for ln in out.splitlines() if ln} if rc == 0 else set()
