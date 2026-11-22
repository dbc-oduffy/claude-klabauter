"""Per-run chunk order: the row ids an EM ruled to schedule first, recorded beside ``credit.jsonl``.

Invariant: the newest entry across a run and every run binding the same sizing is the order a refire
honours; an empty entry clears it. Reads and appends files only; spawns no process.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from pathlib import Path, PurePosixPath

from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, record_dir
from coordinator_core.ops.dispatch_emit.credit_gate import BOOKKEEPING_CHUNK_IDS
from coordinator_core.ops.dispatch_emit.halted_run import _DATE_PREFIX, _STAMP
from coordinator_core.ops.dispatch_emit.run_credit import _binds_sizing
from coordinator_core.session.declared_writes import declare_write

ORDER_FILE = "order.jsonl"
MAX_ENTRIES = 64
_CHUNK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def validate(raw: object) -> list[str]:
    """Normalise ``order_rows`` to a deduped list of chunk ids; raise ``ValueError`` naming the first bad one."""
    if not isinstance(raw, (list, tuple)):
        raise ValueError("order_rows must be a list of chunk ids")
    if len(raw) > MAX_ENTRIES:
        raise ValueError(f"order_rows holds {len(raw)} entries; the cap is {MAX_ENTRIES}")
    out: list[str] = []
    for i, chunk in enumerate(raw):
        if not isinstance(chunk, str) or not _CHUNK_RE.match(chunk):
            raise ValueError(f"order_rows[{i}] {chunk!r} is not a chunk id")
        if chunk in BOOKKEEPING_CHUNK_IDS:
            raise ValueError(f"order_rows[{i}] {chunk!r} is a bookkeeping chunk")
        if chunk not in out:
            out.append(chunk)
    return out


def record(dir_: Path, ids: list[str], at: str) -> Path:
    """Append one ``{order, at}`` JSON line to ``dir_/order.jsonl``; an empty ``ids`` clears the order."""
    from coordinator_core.atomic_append import append_line

    sink = Path(dir_) / ORDER_FILE
    sink.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"order": list(ids), "at": at}, ensure_ascii=False)
    append_line(sink, (line + "\n").encode("utf-8"))
    declare_write(sink)
    return sink


def _last_order(d: Path) -> list[str] | None:
    """The last well-formed order line of ``d/order.jsonl``; ``None`` when absent or unreadable."""
    try:
        text = (Path(d) / ORDER_FILE).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    found: list[str] | None = None
    for line in text.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        order = obj.get("order") if isinstance(obj, dict) else None
        if isinstance(order, list) and all(isinstance(c, str) and c for c in order):
            found = list(order)
    return found


def read_for_sizing(repo_root: Path, run_id: str, sizing_rel: str) -> list[str]:
    """Newest recorded order across ``run_id`` and every run binding ``sizing_rel``; ``[]`` when none; never raises.

    Dirs are ordered oldest stamp first (``ask-`` and ``warp-`` together, as ``credits_for_sizing``
    reads them) with ``run_id``'s own dir last; the newest dir holding an entry wins, an empty entry clears.
    """
    try:
        sizing = PurePosixPath(sizing_rel.replace("\\", "/")).as_posix()
        stem = PurePosixPath(sizing).stem
        dated = _DATE_PREFIX.match(stem)
        candidates: list[tuple[str, Path]] = []
        if dated is not None:
            floor = "".join(dated.groups())
            prefix = f"warp-{stem}-"
            try:
                scan = os.scandir(Path(repo_root) / RECORD_DIR_ROOT)
            except OSError:
                scan = contextlib.nullcontext(())
            with scan as it:
                for entry in it:
                    name = entry.name
                    if name.startswith("ask-"):
                        stamp = name[4:]
                        if not (_STAMP.fullmatch(stamp) and stamp[:8] >= floor):
                            continue
                    elif name.startswith(prefix):
                        stamp = name[len(prefix):]
                        if not _STAMP.fullmatch(stamp):
                            continue
                    else:
                        continue
                    try:
                        if entry.is_dir() and (Path(entry.path) / ORDER_FILE).is_file():
                            candidates.append((stamp, Path(entry.path)))
                    except OSError:
                        continue
        candidates.sort(key=lambda c: c[0])
        own = record_dir(Path(repo_root), run_id)
        dirs = [d for _, d in candidates if d != own and _binds_sizing(d, sizing)] + [own]
        for d in reversed(dirs):
            order = _last_order(d)
            if order is not None:
                return order
    except Exception:  # noqa: BLE001 -- unreadable records: no order
        return []
    return []
