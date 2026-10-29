"""
coordinator_core.ops.pm_directive_map -- which PM directives of a session have no task,
plan row or recorded disposition, and the two verbs that act on that answer.

WHY. A PM turn carrying several imperatives can be followed by none of them, and the pointer
to ``pm_turns.jsonl`` that compaction keeps is not a line anyone acts on. ``map_directives``
lists every directive of every recorded PM turn with what it maps to; the unmapped ones are
surfaced at PreCompact steering and at Stop; SessionEnd records the final list to
``pm_directive_unmapped.json`` beside the turn log (the DoE tripwire is its reader).
Flag only, never deny.

MAPPING. A directive is mapped when (1) a session task's subject/description cites
``turn <N>`` for its turn, or shares enough content words with it, or (2) a row of
``pm_directive_dispositions.jsonl`` (beside ``pm_turns.jsonl``) names its ``{turn, index}``;
a plan row maps through such a row with ``ref: plan:<path>#<row id>``.

NEGATIVE SPEC:
  - No git spawn, no subprocess, no docs/plans walk: this runs on the hook hot path.
  - ``extract_directives`` is lexical, no model; a miss is recoverable by a disposition row.
  - Does NOT create the session directory; a missing one records nothing.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core._hook_envelope import payload_of
from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.hooks._envelope import no_advisory
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, held_lock
from coordinator_core.ops import baton_pm_turns
from coordinator_core.session import core

DISPOSITIONS_FILENAME = "pm_directive_dispositions.jsonl"
UNMAPPED_FILENAME = "pm_directive_unmapped.json"

#: Content words a directive shares with a task, at least this many and this fraction of its own.
_OVERLAP_MIN_WORDS = 3
_OVERLAP_MIN_FRACTION = 0.5
_MIN_WORD_LEN = 4
_LOCK_TIMEOUT_SECS = 2.0
#: Longest text a directive entry carries.
_TEXT_CAP = 400
#: Width and count caps every surfacing site (PreCompact steering, Stop advisory) applies.
DIRECTIVE_LINE_CAP = 240
DIRECTIVE_LINES_MAX = 10

_IMPERATIVE_VERBS = frozenset("""
add address audit build change check clean close commit confirm consume create define delete
deploy diagnose document draft drop dispatch enforce ensure expose extract fetch file find fix
flag follow gate generate get give go grep implement import include inject install investigate
keep land launch let list log look make map merge migrate move name open patch pin plan port
post prepare print provide publish pull push put read rebuild record refactor register remove
rename reorder replace report reproduce research resolve restore retire review revert rewrite
run save scan search send set ship show split start stop store study summarize surface take
test trace track trim try turn update use verify wire write
""".split())
_LEADERS = frozenset({"please", "and", "also", "then", "now", "just"})
_PHRASE_RE = re.compile(
    r"\b(i\s+(?:want|need|asked\s+for|expect)|make\s+sure|don'?t|do\s+not|never|always)\b",
    re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_STOPWORDS = frozenset("""
that this with from have been were will would should could there their them then than what
where which while about into over under only your they
""".split())


def _words(text: str) -> List[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def extract_directives(text: str) -> List[str]:
    """Sentences of ``text`` that read as directives: an opening imperative verb (after
    optional please/and/also), or an "I want/need/asked for", "make sure" or
    "don't/never/always" phrase. Pasted blocks are ignored. Pure, no I/O."""
    if not isinstance(text, str):
        return []
    found: List[str] = []
    for raw in _SENTENCE_SPLIT_RE.split(baton_pm_turns.strip_pasted(text)):
        sentence = raw.strip().lstrip("-*>#0123456789.) \t")
        if not sentence or sentence.endswith("?"):
            continue
        words = _words(sentence)
        while words and words[0] in _LEADERS:
            words = words[1:]
        if (words and words[0] in _IMPERATIVE_VERBS) or _PHRASE_RE.search(sentence):
            found.append(sentence)
    return found


def _content_words(text: str) -> set:
    return {w for w in _words(text) if len(w) >= _MIN_WORD_LEN and w not in _STOPWORDS}


def _tasks_dir(session_id: str, home: Optional[Path]) -> Path:
    base = home if home is not None else Path(os.environ.get("HOME") or str(Path.home()))
    return base / ".claude" / "tasks" / session_id


def _load_tasks(session_id: str, home: Optional[Path]) -> List[Dict[str, Any]]:
    task_dir = _tasks_dir(session_id, home)
    tasks: List[Dict[str, Any]] = []
    if not task_dir.is_dir():
        return tasks
    for f in sorted(task_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        text = " ".join(str(data.get(k) or "") for k in ("subject", "description"))
        tasks.append({"name": f.stem, "text": text, "words": _content_words(text)})
    return tasks


def _load_dispositions(path: Optional[Path]) -> Dict[tuple, Dict[str, Any]]:
    rows: Dict[tuple, Dict[str, Any]] = {}
    if path is None:
        return rows
    try:
        raw = path.read_bytes()
    except OSError:
        return rows
    for line in raw.split(b"\n"):
        try:
            row = json.loads(line.decode("utf-8"))
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("turn"), int) \
                and isinstance(row.get("index"), int) and row.get("disposition"):
            rows[(row["turn"], row["index"])] = row
    return rows


def _task_match(turn: int, text: str, tasks: List[Dict[str, Any]]) -> Optional[str]:
    cite = re.compile(rf"\bturn\s*#?{turn}\b", re.IGNORECASE)
    for task in tasks:
        if cite.search(task["text"]):
            return f"task:{task['name']}"
    mine = _content_words(text)
    if len(mine) < _OVERLAP_MIN_WORDS:
        return None
    for task in tasks:
        shared = len(mine & task["words"])
        if shared >= _OVERLAP_MIN_WORDS and shared / len(mine) >= _OVERLAP_MIN_FRACTION:
            return f"task:{task['name']}"
    return None


def map_directives(session_id: Optional[str], cwd: Optional[str] = None, *,
                   home: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Every directive of every recorded PM turn as ``{turn, index, text, mapped_to}``;
    ``mapped_to`` is None when unmapped. ``[]`` when the session has no turns."""
    turns = baton_pm_turns.read_turns(session_id=session_id, cwd=cwd, all_turns=True)
    if not turns.get("ok"):
        return []
    sid = turns["session_id"]
    sdir = baton_pm_turns.log_path(sid, cwd)
    dispositions = _load_dispositions(sdir.parent / DISPOSITIONS_FILENAME if sdir else None)
    tasks = _load_tasks(sid, home)
    out: List[Dict[str, Any]] = []
    for entry in turns["turns"]:
        for index, text in enumerate(extract_directives(entry["prompt"])):
            turn = entry["turn"]
            disposition = dispositions.get((turn, index))
            mapped = (f"disposition:{disposition.get('ref') or disposition['disposition']}"
                      if disposition else _task_match(turn, text, tasks))
            out.append({"turn": turn, "index": index, "text": text[:_TEXT_CAP],
                        "mapped_to": mapped})
    return out


def unmapped_directives(session_id: Optional[str], cwd: Optional[str] = None, *,
                        home: Optional[Path] = None) -> List[Dict[str, Any]]:
    """The entries of ``map_directives`` with no task, plan row or disposition."""
    return [d for d in map_directives(session_id, cwd, home=home) if d["mapped_to"] is None]


def append_disposition(session_id: str, turn: int, index: int, disposition: str,
                       ref: Optional[str] = None, cwd: Optional[str] = None) -> dict:
    """Append one disposition row beside ``pm_turns.jsonl``; ``{ok, reason}``."""
    path = baton_pm_turns.log_path(session_id, cwd)
    if path is None or not path.parent.is_dir():
        return {"ok": False, "reason": "no session directory; disposition not recorded"}
    target = path.parent / DISPOSITIONS_FILENAME
    row = {"turn": turn, "index": index, "disposition": disposition, "ref": ref,
           "ts": core.now_iso()}
    try:
        with held_lock(target.resolve(), timeout=_LOCK_TIMEOUT_SECS,
                       holder_label="baton.pm_directive_dispose"):
            existing = target.read_bytes() if target.is_file() else b""
            prefix = "\n" if existing and not existing.endswith(b"\n") else ""
            with open(target, "ab") as fh:
                fh.write(f"{prefix}{json.dumps(row, ensure_ascii=False)}\n".encode("utf-8"))
    except (LockTimeout, OSError, RuntimeError) as exc:
        return {"ok": False, "reason": f"append failed: {exc}"}
    return {"ok": True, "reason": None, "path": str(target)}


def _int(params: dict, key: str) -> int:
    value = params.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value


@register_op("baton.pm_directive_unmapped")
def _pm_directive_unmapped(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC ``baton.pm_directive_unmapped``: params ``session_id`` (optional).
    Returns ``{unmapped: [...], mapped: [...]}``, entries ``{turn, index, text, mapped_to}``."""
    entries = map_directives(params.get("session_id"),
                             str(repo_root) if repo_root else None)
    return {"unmapped": [e for e in entries if e["mapped_to"] is None],
            "mapped": [e for e in entries if e["mapped_to"] is not None]}


@register_op("baton.pm_directive_dispose")
def _pm_directive_dispose(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC ``baton.pm_directive_dispose``: params ``turn``, ``index`` (ints),
    ``disposition`` (non-empty str), ``ref`` (str, optional; ``plan:<path>#<row id>``),
    ``session_id`` (optional). Returns ``{ok, reason}``."""
    try:
        turn, index = _int(params, "turn"), _int(params, "index")
    except ValueError as exc:
        return {"ok": False, "reason": str(exc)}
    disposition = params.get("disposition")
    if not isinstance(disposition, str) or not disposition.strip():
        return {"ok": False, "reason": "disposition must be a non-empty string"}
    ref = params.get("ref")
    if ref is not None and not isinstance(ref, str):
        return {"ok": False, "reason": "ref must be a string"}
    cwd = str(repo_root) if repo_root else None
    sid = params.get("session_id")
    if not sid:
        from coordinator_core.ops.session_context import resolve_current_session_id
        sid = resolve_current_session_id(Path(cwd) if cwd else None)
    if not sid:
        return {"ok": False, "reason": "no resolvable session id"}
    return append_disposition(sid, turn, index, disposition.strip(), ref, cwd)


@register_op("hooks.sessionend_pm_directive_check")
def _sessionend_pm_directive_check(params: dict, repo_root=None) -> dict:
    """SessionEnd: write the session's unmapped directives to ``pm_directive_unmapped.json``
    beside its turn log. Never advises; silent on any failure."""
    payload = payload_of(params)
    session_id = payload.get("session_id") or ""
    if not isinstance(session_id, str) or not session_id:
        return no_advisory()
    cwd = payload.get("cwd") or ""
    cwd = cwd if isinstance(cwd, str) and cwd else None
    try:
        path = baton_pm_turns.log_path(session_id, cwd)
        if path is not None and path.parent.is_dir():
            body = json.dumps({"session_id": session_id,
                               "unmapped": unmapped_directives(session_id, cwd)},
                              ensure_ascii=False, indent=2)
            atomic_write_bytes(str(path.parent / UNMAPPED_FILENAME), body.encode("utf-8"))
    except Exception:
        pass
    return no_advisory()
