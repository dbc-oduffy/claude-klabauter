"""Reader for an EM's pre-existing ruling on a run's failing test ids (structured jsonl or a live revise note)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable

from coordinator_core.ops._path_guard import safe_id
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import run_notes

PRE_EXISTING_FILE = "pre_existing_ruled.jsonl"

_RULING_WORD = re.compile(r"pre[-_]existing", re.IGNORECASE)
_NEGATED = re.compile(r"\b(?:not|never|isn'?t|non)[\s-]+(?:a[\s-]+)?pre[-_]existing", re.IGNORECASE)
_ID_CHAR = re.compile(r"[A-Za-z0-9_.:\[\]-]")


def _structured(dir_: Path) -> dict[str, str]:
    try:
        text = (dir_ / PRE_EXISTING_FILE).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        nodeid, reason = row.get("nodeid"), row.get("reason")
        if isinstance(nodeid, str) and nodeid:
            out[nodeid] = reason if isinstance(reason, str) else ""
    return out


def _names_whole(text: str, nodeid: str) -> bool:
    """True when `nodeid` occurs in `text` not followed by an id character (so `t::a` never matches `t::ab`)."""
    start = 0
    while (i := text.find(nodeid, start)) != -1:
        end = i + len(nodeid)
        if end == len(text) or not _ID_CHAR.match(text[end]):
            return True
        start = i + 1
    return False


def ruled_pre_existing(repo_root: "Path | str", run_key: str, failing: Iterable[str]) -> dict[str, str]:
    """Map each id in `failing` the EM ruled pre-existing to its reason.

    Sources: `PRE_EXISTING_FILE` rows `{nodeid, reason, at}` of the run and its `resumed_from` chain
    (at most `CARRIED_HOP_CAP` hops, cycle-safe), and live (unretracted) carried revise notes
    matching pre-existing|pre_existing that contain the id as a whole token. Ids outside `failing`
    are never returned. Missing or malformed files yield nothing; raises LedgerRefused on an unsafe run_key.
    """
    root = Path(repo_root)
    wanted = [f for f in dict.fromkeys(failing) if isinstance(f, str) and f]
    own_dir = rl.run_dir(root, run_key)
    ruled: dict[str, str] = _structured(own_dir)
    visited = {run_key}
    cur_dir = own_dir
    for _ in range(rl.CARRIED_HOP_CAP):
        prev = (rl.read_run(cur_dir) or {}).get(rl.RESUMED_FROM_FIELD)
        if not (isinstance(prev, str) and safe_id(prev)) or prev in visited:
            break
        visited.add(prev)
        cur_dir = rl.run_dir(root, prev)
        for k, v in _structured(cur_dir).items():
            ruled.setdefault(k, v)
    out = {f: ruled[f] for f in wanted if f in ruled}
    pending = [f for f in wanted if f not in out]
    if pending:
        for entry in run_notes.live(rl.carried_notes(root, run_key)):
            text = entry["note"]
            if not _RULING_WORD.search(text) or _NEGATED.search(text):
                continue
            for f in pending:
                if f not in out and _names_whole(text, f):
                    out[f] = text
    return out
