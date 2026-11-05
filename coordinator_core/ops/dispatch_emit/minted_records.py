"""Per-run manifest of the record files an engine writer minted, and the one-commit settle of them.

A run appends ``{path, created}`` lines to ``<run_dir>/minted.jsonl``. ``settle`` lands every recorded
path in ONE ``commit_paths`` commit; when the commit is refused it deletes only the paths the run
created that HEAD never carried. A path a live peer claims is withheld: never committed, never deleted.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Iterable, Optional

MANIFEST_FILE = "minted.jsonl"


def record(run_dir: Path, entries: Iterable[dict]) -> Path:
    """Append one ``{path, created}`` JSON line per entry to ``run_dir/minted.jsonl``.

    ``path`` is repo-relative; ``created`` is True when this run made the file, False when it reused one.
    Raises ``ValueError`` on an unclean path or a non-bool ``created``, before anything is written."""
    from coordinator_core.atomic_append import append_line
    from coordinator_core.ops.dispatch_emit.run_artifacts import _norm_rel

    lines: list[str] = []
    for i, entry in enumerate(entries):
        path = _norm_rel(entry.get("path")) if isinstance(entry, dict) else None
        if path is None:
            raise ValueError(f"minted entry {i} has no clean repo-relative path: {entry!r}")
        created = entry.get("created")
        if not isinstance(created, bool):
            raise ValueError(f"minted entry {i} ({path}) needs a bool 'created', got {created!r}")
        lines.append(json.dumps({"path": path, "created": created}, ensure_ascii=False) + "\n")
    sink = Path(run_dir) / MANIFEST_FILE
    sink.parent.mkdir(parents=True, exist_ok=True)
    for line in lines:
        append_line(sink, line.encode("utf-8"))
    return sink


def read(run_dir: Path) -> list[dict]:
    """The manifest as ``[{path, created}]``, one entry per path in first-seen order.

    A path recorded twice is ``created`` when any record of it says so. An absent manifest, a torn
    line or a malformed row reads as nothing for that line, never an error."""
    sink = Path(run_dir) / MANIFEST_FILE
    try:
        text = sink.read_text(encoding="utf-8")
    except OSError:
        return []
    merged: dict[str, bool] = {}
    for raw in text.splitlines():
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(row, dict) or not isinstance(row.get("path"), str) or not isinstance(row.get("created"), bool):
            continue
        merged[row["path"]] = merged.get(row["path"], False) or row["created"]
    return [{"path": p, "created": c} for p, c in merged.items()]


def _live_peer_held(main_root: Path, paths: list[str]) -> set[str]:
    from coordinator_core.ops.dispatch_emit.grind_landing_select import _live_peer_held as held

    return held(main_root, paths)


def _carried_by_head(root: Path, paths: list[str]) -> set[str]:
    """Those of `paths` HEAD's tree carries as a non-directory entry, read in process. An unreadable
    HEAD tree answers every path (nothing is reaped on a read it could not make)."""
    from coordinator_core.git.git_state import read_tree_spine

    spine = read_tree_spine(root, paths)
    if spine is None:
        return set(paths)
    out: set[str] = set()
    for p in paths:
        parent, _, name = p.rpartition("/")
        entry = spine.get(parent, {}).get(name)
        if entry is not None and entry[0] != 0o40000:
            out.add(p)
    return out


def settle(
    main_root: Path,
    repo_root: Path,
    run_id: str,
    run_dir: Path,
    *,
    message: str,
    peer_held: Optional[Callable[[Path, list], Iterable[str]]] = None,
) -> dict:
    """Commit the manifest's paths as one commit, or reap what the run created.

    Returns ``{sha, reaped, withheld}``. ``sha`` is the commit, or None when nothing committed.
    ``withheld`` lists live-peer-claimed paths, left untouched. On ``NothingToCommit`` nothing is
    reaped. On ``CommitRefused`` every path the run created, HEAD does not carry and no peer claims is
    deleted, ``refused`` carries the reason, and a reused (tracked or ``created: False``) path stays,
    listed in ``kept`` (left uncommitted and dirty).
    ``repo_root`` is the caller's common dir, kept for signature parity with ``commit_grind_rows``;
    the commit and every read go through ``main_root``. ``run_id`` is for the caller's message."""
    from coordinator_core.git.commit import CommitRefused, NothingToCommit, commit_paths

    entries = read(run_dir)
    paths = [e["path"] for e in entries]
    if not paths:
        return {"sha": None, "reaped": [], "withheld": []}
    held = set((peer_held or _live_peer_held)(main_root, paths))
    withheld = sorted(p for p in paths if p in held)
    landable = [p for p in paths if p not in held and (main_root / p).is_file()]
    if not landable:
        return {"sha": None, "reaped": [], "withheld": withheld}
    try:
        outcome = commit_paths(main_root, landable, message)
    except NothingToCommit:
        return {"sha": None, "reaped": [], "withheld": withheld}
    except CommitRefused as err:
        created = {e["path"] for e in entries if e["created"]}
        in_head = _carried_by_head(main_root, landable)
        reaped: list[str] = []
        for p in landable:
            if p in created and p not in in_head:
                try:
                    (main_root / p).unlink()
                except FileNotFoundError:
                    continue
                reaped.append(p)
        kept = [p for p in landable if p not in reaped]
        return {"sha": None, "reaped": reaped, "withheld": withheld, "kept": kept, "refused": str(err)}
    return {"sha": outcome.sha, "reaped": [], "withheld": withheld}
