"""
coordinator_core.hooks.nudge_em_research_scoping — advisory op: an EM hand-scoping research.

Purpose: the shared pieces of the research-scoping nudge, called by the Stop dispatcher's
``_research_drafting_stop_handler``. ``_scan_turn`` counts this turn's read-only calls over
research substrate (docs/research/, roadmap handoffs, the improvement queue, corpus/, census
output) from the transcript tail; the Stop leg fires once per turn at READ_THRESHOLD.

Negative-spec:
  - Never denies: context only.
  - Reads only the last TAIL_CAP bytes of the transcript; no spawn.
  - Once-per-turn is best-effort: an unlocked read-modify-write of a tempdir sentinel.
  - Not a registered op: only the Stop dispatcher calls it.
"""

from __future__ import annotations

import json
import os
import re
import tempfile

READ_THRESHOLD = 5
TAIL_CAP = 512 * 1024
_ASK_CAP = 240

_KILL_SWITCH = "COORDINATOR_HOOK_NUDGE_EM_RESEARCH_SCOPING_DISABLED"

_RESEARCH_PATH = re.compile(
    r"(?:^|[/\s'\"=])docs/research/"
    r"|(?:^|[/\s'\"=])state/handoffs/[^/\s]*roadmap"
    r"|(?:^|[/\s'\"=])state/improvement-queue/"
    r"|(?:^|[/\s'\"=])corpus/"
    r"|(?:^|[/\s'\"=])[^/\s]*census[^/\s]*\.(?:json|jsonl|md|csv|txt)",
    re.IGNORECASE,
)
_READ_COMMAND = re.compile(r"(?:^|[\s;&|(])(?:cat|ls|grep|rg|head|tail|find)\s")
_FIRE_COMMAND = re.compile(r"engine-ask|coordinator:research|(?:^|\s)/research\b")
_FIRE_NAME = re.compile(r"engine-ask|research")

NUDGE = (
    "[research-scoping] You're scoping research by hand. Fire /coordinator:research or "
    "engine-ask {ask: %s} now; the engine sizes it."
)
_NO_ASK = "<latest PM verbatim>"


def _norm(value: object) -> str:
    return str(value or "").replace("\\", "/")


def _is_research_read(name: str, tool_input: dict) -> bool:
    if name == "Read":
        return bool(_RESEARCH_PATH.search(_norm(tool_input.get("file_path"))))
    if name == "Grep":
        target = _norm(tool_input.get("path")) + " " + _norm(tool_input.get("glob"))
        return bool(_RESEARCH_PATH.search(target))
    if name == "Bash":
        command = _norm(tool_input.get("command"))
        return bool(_READ_COMMAND.search(command) and _RESEARCH_PATH.search(command))
    return False


def _is_fire(name: str, tool_input: dict) -> bool:
    if name == "Workflow":
        return bool(_FIRE_NAME.search(str(tool_input.get("name") or "")))
    if name == "Skill":
        return bool(_FIRE_NAME.search(str(tool_input.get("skill") or "")))
    if name == "Bash":
        return bool(_FIRE_COMMAND.search(str(tool_input.get("command") or "")))
    return False


def _real_user_text(rec: dict) -> str | None:
    """The text of a human-typed user record; None for tool results, meta and harness-injected text."""
    if rec.get("type") != "user" or rec.get("isMeta"):
        return None
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        text = "\n".join(
            str(b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    else:
        return None
    text = text.strip()
    if not text or text.startswith("<"):
        return None
    return text


def _scan_turn(transcript_path: str) -> tuple[int, bool, int, str]:
    """(research reads, fire seen, turn key, latest PM text) for the turn the transcript tail ends in."""
    size = os.stat(transcript_path).st_size
    base = max(0, size - TAIL_CAP)
    with open(transcript_path, "rb") as fh:
        fh.seek(base)
        data = fh.read()
    if base:
        cut = data.find(b"\n")
        base += cut + 1 if cut >= 0 else len(data)
        data = data[cut + 1 :] if cut >= 0 else b""
    key, ask, reads, fired = base, "", 0, False
    offset = base
    for raw in data.splitlines(keepends=True):
        line_at, offset = offset, offset + len(raw)
        if b'"user"' not in raw and b'"tool_use"' not in raw:
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        text = _real_user_text(rec)
        if text is not None:
            key, ask, reads, fired = line_at, text, 0, False
            continue
        if rec.get("type") != "assistant":
            continue
        content = (rec.get("message") or {}).get("content")
        for block in content if isinstance(content, list) else ():
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = str(block.get("name") or "")
            tool_input = block.get("input")
            tool_input = tool_input if isinstance(tool_input, dict) else {}
            if _is_fire(name, tool_input):
                fired = True
            elif _is_research_read(name, tool_input):
                reads += 1
    return reads, fired, key, ask


def _sentinel_path(sid: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", sid)
    return os.path.join(tempfile.gettempdir(), f"coordinator-research-scoping-{safe}.json")


def _already_signalled(path: str, key: int) -> bool:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("turn_key") == key
    except (OSError, ValueError, AttributeError):
        return False


def mark_signalled(path: str, key: int) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"turn_key": key}, fh)


def nudge_text(ask: str) -> str:
    one_line = " ".join(ask.split())
    if len(one_line) > _ASK_CAP:
        one_line = one_line[: _ASK_CAP - 1] + "…"
    return NUDGE % (json.dumps(one_line) if one_line else _NO_ASK)
