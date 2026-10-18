"""Day-keyed JSONL resume checkpoint for ceremony apply.

One file per (ceremony, local day, for_date/only key) under
`<git-common-dir>/coordinator-sessions/<ceremony>-apply/`. Rows are appended with
`atomic_append.append_line`: `start`, `dispatching`, `landed`, `finished`. A rerun after a kill
reads the file and skips every id with a `landed` row; an id with `dispatching` and no `landed`
is `interrupted` and dispatches again, because nothing proves it landed.

Fail-open: an unresolvable sessions dir, an unreadable file or a malformed line never fails a
ceremony. The checkpoint degrades to empty and says so once on stderr. No subprocess is spawned.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from coordinator_core.atomic_append import append_line
from coordinator_core.daily_day import local_day
from coordinator_core.session.core import ensure_session, sessions_dir

_DAY_LEN = len("YYYY-MM-DD")


def _warn(message: str) -> None:
    print(f"apply-checkpoint: {message}", file=sys.stderr)


def args_digest(args: Any) -> str:
    """Short stable digest of a directive's args; equal args give an equal digest."""
    blob = json.dumps(args, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _run_key(for_date: "str | None", only_mode: bool) -> str:
    blob = json.dumps([for_date, bool(only_mode)], separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:8]


def _read_rows(path: Path) -> "list[dict[str, Any]] | None":
    """Parsed rows in append order, skipping malformed lines; None when the file is unreadable."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError):
        return None
    rows: list[dict[str, Any]] = []
    bad = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            bad += 1
    if bad:
        _warn(f"{bad} malformed line(s) skipped in {path.name}; resume may miss those rows")
    return rows


class Checkpoint:
    """Resume state for one apply run; every method is safe to call when resume is unavailable."""

    def __init__(self, path: "Path | None", rows: "list[dict[str, Any]]") -> None:
        self._path = path
        self.stale_checkpoint: "str | None" = None
        self._landed: dict[str, str] = {}
        self._stdout: dict[str, str] = {}
        self._pending: set[str] = set()
        self._dirty_before: "dict | None" = None
        self._has_start = False
        self._replay(rows)

    @property
    def available(self) -> bool:
        """True when rows are being persisted for this run."""
        return self._path is not None

    def _reset(self) -> None:
        self._landed.clear()
        self._stdout.clear()
        self._pending.clear()
        self._dirty_before = None
        self._has_start = False

    def _replay(self, rows: "list[dict[str, Any]]") -> None:
        self._reset()
        for row in rows:
            kind = row.get("kind")
            if kind == "finished":
                self._reset()
            elif kind == "start":
                if not self._has_start:
                    self._has_start = True
                    dirty = row.get("dirty_before")
                    self._dirty_before = dirty if isinstance(dirty, dict) else None
            elif kind == "dispatching" and isinstance(row.get("id"), str):
                self._pending.add(row["id"])
            elif kind == "landed" and isinstance(row.get("id"), str):
                did = row["id"]
                self._pending.discard(did)
                self._landed[did] = str(row.get("args_digest", ""))
                if isinstance(row.get("stdout"), str):
                    self._stdout[did] = row["stdout"]

    def _append(self, row: "dict[str, Any]") -> None:
        if self._path is None:
            return
        line = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            append_line(self._path, line.encode("utf-8"))
        except OSError as exc:
            _warn(f"resume unavailable, append failed: {exc}")
            self._path = None

    def landed_ids(self) -> "frozenset[str]":
        """Which directives of this day's run already landed?"""
        return frozenset(self._landed)

    def interrupted_ids(self) -> "frozenset[str]":
        """Which directive was in flight when the process died?"""
        return frozenset(self._pending - set(self._landed))

    def stdout_of(self, directive_id: str) -> "str | None":
        """What did a stdin producer print before the kill?"""
        return self._stdout.get(directive_id)

    def dirty_before(self) -> "dict | None":
        """The pre-run dirty snapshot the dead run recorded, or None."""
        return self._dirty_before

    def args_changed(self, directive_id: str, args: Any) -> bool:
        """Do these args differ from the ones recorded when the directive landed?"""
        recorded = self._landed.get(directive_id)
        return recorded is not None and recorded != args_digest(args)

    def record_dispatching(self, directive_id: str) -> None:
        self._pending.add(directive_id)
        self._append({"kind": "dispatching", "id": directive_id})

    def record_landed(self, directive_id: str, args: Any, stdout: "str | None" = None) -> None:
        digest = args_digest(args)
        row: dict[str, Any] = {"kind": "landed", "id": directive_id, "args_digest": digest}
        if stdout is not None:
            row["stdout"] = stdout
        self._pending.discard(directive_id)
        self._landed[directive_id] = digest
        self._append(row)

    def finish(self) -> None:
        """Is this day's run complete, so the next run starts fresh?"""
        self._append({"kind": "finished"})
        self._reset()


def _sweep_other_days(directory: Path, today: str, own: str) -> "str | None":
    """Delete finished other-day files; return the name of one unfinished other-day file."""
    stale: "str | None" = None
    try:
        names = sorted(p.name for p in directory.iterdir() if p.suffix == ".jsonl")
    except OSError:
        return None
    for name in names:
        if name == own or name[:_DAY_LEN] == today:
            continue
        rows = _read_rows(directory / name)
        if rows is None:
            continue
        if rows and rows[-1].get("kind") == "finished":
            try:
                (directory / name).unlink()
            except OSError:
                pass
        elif rows and stale is None:
            stale = str(directory / name)
    return stale


def open_checkpoint(
    repo_root: "Path | str",
    ceremony: str,
    *,
    for_date: "str | None" = None,
    only_mode: bool = False,
    dirty_before: "dict | None" = None,
) -> Checkpoint:
    """Open (creating if fresh) today's checkpoint for this ceremony and run key.

    `dirty_before` is recorded in the `start` row of a fresh run. Never raises.
    """
    try:
        base = sessions_dir(str(repo_root))
        if not base:
            _warn("resume unavailable, no git sessions dir")
            return Checkpoint(None, [])
        today = local_day(str(repo_root))
        directory = Path(
            ensure_session(f"{ceremony}-apply", str(repo_root), sessions_base=base)
        )
        if not directory.is_dir():
            _warn("resume unavailable, session dir not created")
            return Checkpoint(None, [])
        name = f"{today}-{_run_key(for_date, only_mode)}.jsonl"
        path = directory / name
        rows = _read_rows(path)
        if rows is None:
            _warn(f"resume unavailable, {name} is unreadable")
            return Checkpoint(None, [])
        stale = _sweep_other_days(directory, today, name)
        cp = Checkpoint(path, rows)
        if stale:
            cp.stale_checkpoint = stale
            _warn(f"unfinished checkpoint from an earlier day kept: {stale}")
        if not cp._has_start:
            cp._append({"kind": "start", "dirty_before": dirty_before})
            cp._has_start = True
            cp._dirty_before = dirty_before if isinstance(dirty_before, dict) else None
        return cp
    except Exception as exc:  # fail-open: a checkpoint failure never fails a ceremony
        _warn(f"resume unavailable: {exc}")
        return Checkpoint(None, [])
