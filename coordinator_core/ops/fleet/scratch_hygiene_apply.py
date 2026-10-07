"""Apply executor for ``fleet.scratch_hygiene``: delete the planned would-delete set, never following a link.

In-process ``os.unlink``/``os.rmdir`` only; no subprocess is spawned. Each entry is re-gated
immediately before deletion, so one that became young, live or linked since planning is left in place.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from coordinator_core.install.junction import is_junction, remove_junction
from coordinator_core.ops.fleet.scratch_hygiene_plan import _is_link
from coordinator_core.ops.fleet.scratch_hygiene_records import purge_record
from coordinator_core.temp_layout import FLEET_DIRNAME, coordinator_temp_root

Recheck = Callable[[Path], Mapping[str, Any]]


def _unlink_link(path: Path) -> None:
    if is_junction(path):
        remove_junction(path)
        return
    try:
        os.unlink(path)
    except OSError:
        os.rmdir(path)


def _unlink_file(path: Path) -> None:
    try:
        os.unlink(path)
    except PermissionError:
        os.chmod(path, stat.S_IWRITE)
        os.unlink(path)


def remove_tree_no_follow(path: Path) -> None:
    """Remove ``path`` and its contents; a link met anywhere is unlinked as a link and never entered."""
    if _is_link(path):
        _unlink_link(path)
        return
    stack: list[tuple[Path, bool]] = [(path, False)]
    while stack:
        current, visited = stack.pop()
        if visited:
            try:
                os.rmdir(current)
            except PermissionError:
                os.chmod(current, stat.S_IWRITE)
                os.rmdir(current)
            continue
        stack.append((current, True))
        with os.scandir(current) as it:
            children = list(it)
        for child in children:
            child_path = Path(child.path)
            if _is_link(child_path):
                _unlink_link(child_path)
            elif child.is_dir(follow_symlinks=False):
                stack.append((child_path, False))
            else:
                _unlink_file(child_path)


def _contained_roots(repo_root: Path) -> tuple[Path, Path]:
    return repo_root / "scratch", coordinator_temp_root(repo_root)


def _is_under(child: Path, root: Path) -> bool:
    try:
        child.relative_to(root)
    except ValueError:
        return False
    return child != root


def refusal(repo_root: Path, entry: Path) -> str | None:
    """Reason ``entry`` may not be deleted, or None. Compared lexically after resolving the parent only."""
    if not entry.is_absolute() or ".." in entry.parts:
        return "refused:not-normalized"
    parent = Path(os.path.realpath(entry.parent))
    candidate = parent / entry.name
    for root in _contained_roots(repo_root):
        real_root = Path(os.path.realpath(root))
        if _is_under(candidate, real_root):
            if FLEET_DIRNAME in candidate.relative_to(real_root).parts:
                return "refused:fleet"
            return None
    return "refused:outside-roots"


def _entry_path(repo_root: Path, rec: Mapping[str, Any]) -> Path:
    p = Path(str(rec["path"]))
    return p if p.is_absolute() else repo_root / p


def apply_purge(
    repo_root: Path | str,
    records: Iterable[Mapping[str, Any]],
    *,
    recheck: Recheck,
) -> list[dict[str, Any]]:
    """Return one purge record per input purge record; ``would-delete`` entries become ``deleted``.

    ``recheck(entry_path)`` returns the planner's current record for the entry; any action other than
    ``would-delete`` is reported as given and the entry is left in place. A refused path or a failed
    deletion is reported ``skipped-live`` with a ``refused:*`` or ``delete-failed:*`` reason.
    """
    root = Path(repo_root)
    repo = root.name
    out: list[dict[str, Any]] = []
    for rec in records:
        if rec.get("kind") != "purge" or rec.get("action") != "would-delete":
            if rec.get("kind") == "purge":
                out.append(dict(rec))
            continue
        entry = _entry_path(root, rec)
        sizes = {
            "bytes": int(rec.get("bytes", 0)),
            "files": int(rec.get("files", 0)),
            "age_days": float(rec.get("age_days", 0.0)),
        }

        def emit(action: str, reason: str = "", _entry: Path = entry, _sizes: dict = sizes) -> dict[str, Any]:
            return purge_record(repo, _entry, action, reason=reason, repo_root=root, **_sizes)

        why = refusal(root, entry)
        if why is not None:
            out.append(emit("skipped-live", why))
            continue
        if not os.path.lexists(entry):
            out.append(emit("deleted", "already-gone"))
            continue
        if _is_link(entry):
            out.append(emit("skipped-link", "top-level-link"))
            continue
        now = recheck(entry)
        action = str(now.get("action", ""))
        if action != "would-delete":
            out.append(emit(action or "skipped-live", str(now.get("reason", ""))))
            continue
        try:
            remove_tree_no_follow(entry)
        except OSError as exc:
            out.append(emit("skipped-live", f"delete-failed:{exc.errno}"))
            continue
        out.append(emit("deleted"))
    return out
