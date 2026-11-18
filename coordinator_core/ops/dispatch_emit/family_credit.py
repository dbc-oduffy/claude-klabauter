"""Checkpoint credit for a family-stage row's declared writes.

A family stage (agy, codex, grok) writes through its vendor CLI inside the runner's Bash call, so the
Write/Edit print hook never prints those writes. A declared path is credited only when both hold:

- the row's return is a family ``DONE`` whose every attempt's verdict is ``OK``: read from the run's
  ``returns.json``, or, in the fire the row ran in (``returns.json`` is written only at a halt), from the
  checkpoint message's ``Checkpoint-Family-Ok:`` line, which the engine writes from that same reply;
- the path's bytes differ from the row-start snapshot (``capture``, run as the runner's first command).

Trap: an all-OK return alone never credits. A declared path unchanged since the row started holds a
peer's edit or nothing of the row's, so it is withheld and named. Stdlib only; spawns no process.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Iterable, Optional

ROW_START_DIR = "row-start"
FAMILY_OK_TRAILER = "Checkpoint-Family-Ok"
_TRAILER_RE = re.compile(rf"^{FAMILY_OK_TRAILER}:\s*(?P<ids>.+)$", re.M)
_SAFE_ROW_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _norm(path: object) -> str:
    s = str(path).replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return s


def _family_rows(rec: Path) -> dict[str, list[str]]:
    """``{row id: declared writes}`` for the manifest's family-stage-runner rows."""
    from coordinator_core.ops.dispatch_emit.stage_family import RUNNER_AGENT

    try:
        doc = json.loads((rec / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, list[str]] = {}
    for row in (doc.get("rows") if isinstance(doc, dict) else None) or ():
        if isinstance(row, dict) and row.get("agent_type") == RUNNER_AGENT and isinstance(row.get("id"), str):
            out[row["id"]] = [_norm(p) for p in row.get("writes") or () if isinstance(p, str)]
    return out


def _snapshot_path(rec: Path, row_id: str) -> Optional[Path]:
    return rec / ROW_START_DIR / f"{row_id}.json" if _SAFE_ROW_RE.match(row_id) else None


def capture(root: Path, run_id: str, row_id: str) -> Optional[Path]:
    """Write ``<record dir>/row-start/<row>.json``: each declared write's raw-bytes fingerprint (None when
    absent). None, writing nothing, when ``row_id`` is not a family row of the run."""
    from coordinator_core.ops.dispatch_emit import write_baseline
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
    from coordinator_core.session.declared_writes import declare_write

    rec = record_dir(Path(root), run_id)
    writes = _family_rows(rec).get(row_id)
    target = _snapshot_path(rec, row_id)
    if writes is None or target is None:
        return None
    snap = {p: write_baseline.fingerprint(Path(root), p) for p in writes}
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(snap, sort_keys=True, indent=2) + "\n")
    declare_write(target)
    return target


def all_ok(text: object, row_id: str) -> bool:
    """True when ``text`` is ``DONE: <row> via <family> {stage record}`` whose every attempt is OK."""
    if not isinstance(text, str):
        return False
    head = f"DONE: {row_id} via "
    body = text.strip()
    if not body.startswith(head) or "{" not in body:
        return False
    try:
        rec = json.loads(body[body.index("{"):])
    except ValueError:
        return False
    attempts = rec.get("attempts") if isinstance(rec, dict) else None
    return bool(attempts) and all(isinstance(a, dict) and a.get("verdict") == "OK" for a in attempts)


def _ok_rows(rec: Path, message: str) -> set[str]:
    from coordinator_core.ops.dispatch_emit import row_returns

    ok = {row for row, entry in row_returns.read(rec).items() if all_ok(entry.get("text"), row)}
    for m in _TRAILER_RE.finditer(message or ""):
        ok |= {r.strip() for r in m.group("ids").split(",") if r.strip()}
    return ok


def credit(root: Path, run_id: str, paths: Iterable[str], message: str = "") -> tuple[frozenset[str], dict[str, str]]:
    """``(credited, why)``: the ``paths`` an all-OK family row of ``run_id`` changed since its start, and
    ``{path: reason}`` for each path a family row declares but this does not credit. Never raises."""
    from coordinator_core.ops.dispatch_emit import write_baseline
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    credited: set[str] = set()
    why: dict[str, str] = {}
    try:
        root = Path(root)
        rec = record_dir(root, run_id)
        rows = _family_rows(rec)
        ok = _ok_rows(rec, message)
        for path in dict.fromkeys(_norm(p) for p in paths):
            for row_id, writes in rows.items():
                if path not in writes:
                    continue
                if row_id not in ok:
                    why.setdefault(path, f"declared by family row {row_id}, whose return is not an all-OK family DONE")
                    continue
                target = _snapshot_path(rec, row_id)
                try:
                    snap = json.loads(target.read_text(encoding="utf-8")) if target else None
                except (OSError, ValueError):
                    snap = None
                if not isinstance(snap, dict) or path not in snap:
                    why.setdefault(path, f"declared by family row {row_id}, but the row recorded no start snapshot")
                elif write_baseline.fingerprint(root, path) == snap[path]:
                    why[path] = f"declared by family row {row_id}, but unchanged since the row started: not the row's write"
                else:
                    credited.add(path)
                    why.pop(path, None)
                    break
    except Exception:  # noqa: BLE001 -- unknown credits nothing: the checkpoint withholds
        return frozenset(), {}
    return frozenset(credited), why


def main(argv: list[str]) -> int:
    """``start --root R --run-id X --row Y``: the family runner's first command."""
    import argparse

    ap = argparse.ArgumentParser(prog="family_credit")
    sub = ap.add_subparsers(dest="cmd", required=True)
    start = sub.add_parser("start")
    start.add_argument("--root", required=True)
    start.add_argument("--run-id", required=True)
    start.add_argument("--row", required=True)
    args = ap.parse_args(argv)
    path = capture(Path(args.root), args.run_id, args.row)
    print(json.dumps({"ok": path is not None, "snapshot": path.as_posix() if path else None}))
    return 0 if path is not None else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
