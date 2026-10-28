"""Run revise notes: stable ids, retraction records, and the requirements-view section.

Invariant: ``notes.jsonl`` is append-only. A retraction is a line, never an edit; a note's id is its
1-based ordinal among note lines, so torn and retraction lines never shift it. Stdlib only; spawns
no process.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable

NOTE_ID_PREFIX = "N"
RETRACTS_FIELD = "retracts"
REASON_CAP = 500
DROP_FIELD = "staging_drop"
_ID_RE = re.compile(r"^N[1-9][0-9]*$")


def entries(path: Path, run_id: str) -> list[dict]:
    """Valid note lines of ``path`` oldest first as ``{id, note, at, run, retracted}``; never raises.

    ``retracted`` is ``None`` or ``{reason, at, from}`` from the first retraction line naming the id.
    Staging-drop lines are never notes.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return []
    out: list[dict] = []
    retractions: dict[str, dict] = {}
    for line in raw.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        target = obj.get(RETRACTS_FIELD)
        if isinstance(target, str):
            if _ID_RE.match(target) and target not in retractions:
                retractions[target] = {
                    "reason": obj.get("reason") if isinstance(obj.get("reason"), str) else "",
                    "at": obj.get("at") if isinstance(obj.get("at"), str) else "",
                    "from": obj.get("from") if isinstance(obj.get("from"), str) else "",
                }
            continue
        note = obj.get("note")
        if not isinstance(note, str):
            continue
        at = obj.get("at")
        out.append({
            "id": f"{NOTE_ID_PREFIX}{len(out) + 1}",
            "note": note,
            "at": at if isinstance(at, str) else "",
            "run": run_id,
            "retracted": None,
        })
    for e in out:
        e["retracted"] = retractions.get(e["id"])
    return out


def live(items: Iterable[dict]) -> list[dict]:
    """The entries not retracted."""
    return [e for e in items if e.get("retracted") is None]


def validate_retract(raw: object) -> dict:
    """Normalise ``{note_id, reason}``; raise ``ValueError`` naming the bad field."""
    if not isinstance(raw, dict):
        raise ValueError("retract_note must be an object with note_id and reason")
    note_id = raw.get("note_id")
    if not isinstance(note_id, str) or not _ID_RE.match(note_id.strip()):
        raise ValueError(f"retract_note.note_id {note_id!r} is not a note id (N<k>)")
    reason = raw.get("reason")
    reason = reason.strip() if isinstance(reason, str) else ""
    if not reason:
        raise ValueError("retract_note.reason is empty")
    if len(reason) > REASON_CAP:
        raise ValueError(f"retract_note.reason is {len(reason)} chars; the cap is {REASON_CAP}")
    return {"note_id": note_id.strip(), "reason": reason}


def retraction_line(note_id: str, reason: str, at: str, sender: str) -> bytes:
    """One JSON retraction line for ``atomic_append.append_line``; ``from`` omitted when no sender."""
    obj: dict = {RETRACTS_FIELD: note_id, "reason": reason, "at": at}
    if sender:
        obj["from"] = sender
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def drop_line(row_id: str, reason: str, at: str, run_id: str) -> bytes:
    """One JSON staging-drop line: a plan row the stage did not stage, and why."""
    obj = {DROP_FIELD: row_id, "reason": reason[:REASON_CAP], "at": at, "run": run_id}
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def drop_entries(path: Path) -> list[dict]:
    """Valid staging-drop lines of ``path`` oldest first as ``{id, reason, at, run}``; never raises."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return []
    out: list[dict] = []
    for line in raw.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict) or not isinstance(obj.get(DROP_FIELD), str):
            continue
        out.append({
            "id": obj[DROP_FIELD],
            "reason": obj.get("reason") if isinstance(obj.get("reason"), str) else "",
            "at": obj.get("at") if isinstance(obj.get("at"), str) else "",
            "run": obj.get("run") if isinstance(obj.get("run"), str) else "",
        })
    return out


def brief_block(items: list[dict]) -> str:
    """The executor-brief block quoting each live note; ``""`` when no live note exists."""
    notes = live(items)
    if not notes:
        return ""
    lines = ["## EM revise notes (the EM's words, not PM verbatim; act on them)", ""]
    for e in notes:
        lines += [f"{e['run']} {e['id']} at {e['at']}:", ""]
        # \x01 is the compose-time marker delimiter: left in, it splices note text into the script as JS.
        body = e["note"].replace("\x01", "")
        lines += [f"> {part}" if part else ">" for part in body.splitlines() or [""]]
        lines.append("")
    return "\n".join(lines)


def render_section(items: list[dict]) -> list[str]:
    """Lines of the ``## Revise notes`` view section; ``[]`` when ``items`` is empty."""
    if not items:
        return []
    lines = [
        "## Revise notes (EM framing: the EM's words, not PM verbatim; the ledger grades them)",
        "",
    ]
    for e in items:
        r = e.get("retracted")
        if r is None:
            lines += [f"{e['run']} {e['id']} at {e['at']}:", ""]
            lines += [f"> {part}" if part else ">" for part in e["note"].splitlines() or [""]]
            lines.append("")
        else:
            lines += [
                f"{e['run']} {e['id']} at {e['at']} was retracted ({r['reason']}); "
                "it is not to be acted on.",
                "",
            ]
    lines.append(
        "Withdraw a note with `workflow.revise {run_id: <the note's run>, "
        "retract_note: {note_id, reason}}`."
    )
    lines.append("")
    return lines
