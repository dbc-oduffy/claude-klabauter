"""Finding-level inversion for review.reverify: undo applied review findings by their own text.

A reviewer's ledger row names its edit exactly (`file`, `before`, `after`). `invert` replaces each
row's `after` with its `before` in the file's current bytes and touches nothing else, so executor
hunks in the same file survive. Pure apart from the ledger read: zero spawns, no git, no writes.

Traps: never diff against a snapshot to find review hunks (a write-once snapshot predates a
refire's executor writes). Never import review_reverify (it imports this module).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Sequence, Tuple

from coordinator_core.ops._path_guard import contained_path

_APPLIED = "applied"


@dataclass(frozen=True)
class FindingRef:
    sidecar: str
    id: str


@dataclass(frozen=True)
class Inversion:
    data: bytes
    reverted: List[FindingRef]
    kept: List[Tuple[FindingRef, str]]


def _norm(path: str) -> str:
    text = str(path).strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def applied_rows(
    repo_root: Path, sidecars: Sequence[str], rel: str
) -> List[Tuple[FindingRef, dict]]:
    """Applied ledger rows naming `rel`, in sidecar order then ledger order.

    A sidecar outside `repo_root` or without a ledger is skipped.
    """
    from coordinator_core.ops import review_findings_ledger

    want = _norm(rel)
    out: List[Tuple[FindingRef, dict]] = []
    for sidecar in sidecars:
        path = Path(sidecar)
        path = path if path.is_absolute() else repo_root / path
        if contained_path(path, [repo_root]) is None:
            continue
        got = review_findings_ledger.read_ledger(path)
        for row in got[1] if got else []:
            if (row.get("status") or _APPLIED) != _APPLIED:
                continue
            if _norm(str(row.get("file") or "")) != want:
                continue
            out.append((FindingRef(str(sidecar), str(row.get("id"))), row))
    return out


def invert(data: bytes, rows: Sequence[Tuple[FindingRef, dict]]) -> Inversion:
    """`data` with each row's `after` replaced by its `before`, last-applied row first.

    A row whose `after` occurs exactly once is reverted; otherwise it is kept with reason
    `after-absent`, `after-ambiguous` or `no-after`. Undecodable bytes return `data` unchanged
    with every row kept as `undecodable`.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return Inversion(data, [], [(ref, "undecodable") for ref, _ in rows])

    crlf = "\r\n" in text

    def fit(value: object) -> str:
        folded = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
        return folded.replace("\n", "\r\n") if crlf else folded

    reverted: List[FindingRef] = []
    kept: List[Tuple[FindingRef, str]] = []
    for ref, row in reversed(list(rows)):
        after = fit(row.get("after"))
        if not after:
            kept.append((ref, "no-after"))
            continue
        count = text.count(after)
        if count == 0:
            kept.append((ref, "after-absent"))
        elif count > 1:
            kept.append((ref, "after-ambiguous"))
        else:
            text = text.replace(after, fit(row.get("before")), 1)
            reverted.append(ref)
    reverted.reverse()
    kept.reverse()
    return Inversion(text.encode("utf-8"), reverted, kept)
