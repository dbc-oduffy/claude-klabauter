"""Halt-commit import closure: which finished chunks must stay out so every landing importer resolves.

`open_chunks` judges the post-commit tree (the gate paths plus HEAD), not the worktree, by running
authoring_leaks detectors over the landing set. A chunk with a finding is withheld whole; dropping
its paths can leave another chunk's import unresolved, so the check repeats to a fixed point.
Detectors are injected and nothing here spawns a process.

Invariant: a detector that raises withholds every chunk it could have judged; the gate never passes
what it could not evaluate. Findings on paths no chunk owns (run artifacts) are ignored here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Mapping, Sequence, Set

from coordinator_core.authoring_leaks import HeadView, LeakFinding, normalize_path


def _name(detector) -> str:
    for attr in ("NAME", "name"):
        value = getattr(detector, attr, None)
        if isinstance(value, str) and value:
            return value
    return getattr(detector, "__name__", type(detector).__name__).rsplit(".", 1)[-1]


def partial_chunks(owner: Mapping[str, str], pending: Sequence[str], landing: Sequence[str]) -> Dict[str, List[str]]:
    """Return `{chunk id: its pending paths missing from landing}` for chunks to withhold whole.

    `pending` is every owned path with work to land (changed, declared); `landing` is the subset that
    survived withholding. A chunk landing only part of its write set would leave HEAD without the
    rest, so any missing path names the chunk. Pure set arithmetic; spawns nothing.
    """
    keep = set(landing)
    out: Dict[str, List[str]] = {}
    for p in pending:
        cid = owner.get(p)
        if cid is not None and p not in keep:
            out.setdefault(cid, []).append(p)
    return out


def open_chunks(
    worktree_root: Path,
    owner: Mapping[str, str],
    landing: Sequence[str],
    detectors: Sequence,
) -> Dict[str, List[str]]:
    """Return `{chunk id: rendered findings}` for chunks to withhold from `landing`.

    `owner` maps each chunk-declared path to its chunk id; `landing` is every path the commit would
    carry. A finding whose path is in `owner` opens that chunk; every path of an opened chunk then
    leaves the gate and the detectors run again until no new chunk opens.
    """
    root = Path(worktree_root)
    owner_n = {normalize_path(p): cid for p, cid in owner.items()}
    gate = {normalize_path(p) for p in landing}
    opened: Dict[str, List[str]] = {}

    while gate:
        newly: Dict[str, List[str]] = {}
        paths = sorted(gate)

        def open_for(path_set, text: str) -> None:
            for p in path_set:
                cid = owner_n.get(p)
                if cid is not None and cid not in opened:
                    newly.setdefault(cid, []).append(text)

        cands: List[tuple] = []
        union: Set[str] = set()
        for det in detectors:
            try:
                got = {normalize_path(p) for p in det.candidates(paths)}
            except Exception as exc:
                open_for(gate, f"detector {_name(det)} raised {type(exc).__name__}: {exc}")
                continue
            cands.append((det, got))
            union |= got

        if cands:
            try:
                head = HeadView(root, union)
            except Exception as exc:
                head = None
                open_for(gate, f"HEAD read raised {type(exc).__name__}: {exc}")
            if head is not None:
                for det, got in cands:
                    try:
                        findings: List[LeakFinding] = det.detect(root, paths, head)
                    except Exception as exc:
                        open_for(got, f"detector {_name(det)} raised {type(exc).__name__}: {exc}")
                        continue
                    for f in findings:
                        open_for([normalize_path(f.path)], f.render())

        if not newly:
            break
        for cid, texts in newly.items():
            opened[cid] = texts
        gate -= {p for p, cid in owner_n.items() if cid in newly}
    return opened
