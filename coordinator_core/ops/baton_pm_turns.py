"""
coordinator_core.ops.baton_pm_turns -- the session's append-only log of PM
prompts, and the verb that reads it back.

WHAT THIS IS FOR. The launch prompt is usually the ask itself, not a preamble
to one, and every later PM turn can carry scope the EM must not paraphrase
away. The baton held only ``first_prompt``, capped by its writer; a session
that wanted turn 7 verbatim, or the launch ask uncut for a ``## PM brief``, had
nowhere to get it. ``baton.pm_turn_append`` records each human prompt as it
arrives; ``baton.pm_turns`` returns one turn, a range, or all of them as text
ready to paste or pipe. Turn 0 is the default extract.

WHY A SIBLING FILE, NOT A BATON FIELD. ``pm_turns.jsonl`` lives beside
``baton.json`` in ``<git-common-dir>/coordinator-sessions/<sid>/``. The baton
record is rewritten wholesale by every ``merge_baton``, and a torn or corrupt
read degrades to the all-defaults skeleton that the next merge writes back --
both fine for advisory scalars, both able to silently erase an uncapped log.
The hook that announces the baton also reads that whole record on every
prompt; an unbounded list inside it would make that read grow with the
session. Here each entry is one ``O_APPEND`` line, written once and never
rewritten by anything.

INVARIANTS:
  - ``turn`` is the 0-based count of newline-terminated lines already in the
    file, taken under the lock, so indices are unique and monotonic. A line
    torn by a crash is newline-terminated before the next append, keeps its
    index, and is skipped on read.
  - Prompts are stored verbatim: not stripped, not capped, not deduplicated
    ("yes" twice is two turns). The one exception is a prompt opening with a
    ``_HARNESS_NOTICE_PREFIXES`` entry: refused whole, consuming no index.
  - ``referent`` is optional and written once, on the turn's own line; a
    malformed one is dropped, never costing the prompt.

NEGATIVE SPEC:
  - Does NOT create the session directory (same rule as ``store.merge_baton``:
    a missing directory reports and records nothing).
  - Does NOT touch ``baton.json``; ``first_prompt`` stays ``session_baton.mint``'s.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, held_lock
from coordinator_core.ops.session_context import resolve_current_session_id
from coordinator_core.session import core
from coordinator_core.session_baton import store

PM_TURNS_FILENAME = "pm_turns.jsonl"

#: Characters of the answered EM message's tail a turn's referent keeps.
REFERENT_EXCERPT_CHARS = 2000
_REFERENT_KEYS = ("excerpt", "transcript_uuid")


def _clean_referent(raw: Any) -> "tuple[Optional[dict], Optional[str]]":
    """``(referent, None)`` when valid, else ``(None, reason)``. Keys are a
    non-empty subset of excerpt/transcript_uuid, each a non-empty str; an
    over-long excerpt keeps its tail and sets ``excerpt_truncated``."""
    if not isinstance(raw, dict):
        return None, "referent must be an object"
    unknown = sorted(str(k) for k in raw if k not in _REFERENT_KEYS)
    if unknown:
        return None, f"referent has unknown keys: {', '.join(unknown)}"
    if not raw:
        return None, "referent needs excerpt or transcript_uuid"
    for key, value in raw.items():
        if not isinstance(value, str) or not value:
            return None, f"referent.{key} must be a non-empty string"
    cleaned = dict(raw)
    excerpt = cleaned.get("excerpt")
    if excerpt is not None and len(excerpt) > REFERENT_EXCERPT_CHARS:
        cleaned["excerpt"] = excerpt[-REFERENT_EXCERPT_CHARS:]
        cleaned["excerpt_truncated"] = True
    return cleaned, None


#: Closed list of harness-injected notice openers; a prompt starting with one
#: is not a human turn and is never recorded.
_HARNESS_NOTICE_PREFIXES = (
    "[Cross-session idle notice]",
    "[Cross-session delivery notice]",
    "<task-notification>",
    "[SYSTEM NOTIFICATION - NOT USER INPUT]",
)

#: Same bound as the baton store's own lock: this runs on every prompt.
_LOCK_TIMEOUT_SECS = 2.0


def log_path(sid: str, cwd: Optional[str] = None) -> Optional[Path]:
    sdir = store.baton_dir(sid, cwd)
    return sdir / PM_TURNS_FILENAME if sdir is not None else None


def append_turn(prompt: str, session_id: Optional[str] = None,
                cwd: Optional[str] = None,
                referent: Optional[dict] = None) -> dict:
    empty = {"ok": False, "turn": None, "log_path": None,
             "session_id": session_id, "reason": None}

    if not isinstance(prompt, str) or not prompt.strip():
        return dict(empty, reason="prompt must be a non-empty string")

    if prompt.lstrip().startswith(_HARNESS_NOTICE_PREFIXES):
        return dict(empty, reason="harness notice; not recorded")

    cleaned, referent_reason = (None, None) if referent is None \
        else _clean_referent(referent)

    sid = session_id or resolve_current_session_id(Path(cwd) if cwd else None)
    if not sid:
        return dict(empty, reason="no resolvable session id")
    path = log_path(sid, cwd)
    if path is None:
        return dict(empty, session_id=sid, reason="session hub unresolvable")
    if not path.parent.is_dir():
        return dict(empty, session_id=sid, log_path=str(path),
                    reason="no session directory; turn not recorded")

    try:
        with held_lock(path.resolve(), timeout=_LOCK_TIMEOUT_SECS,
                       holder_label="baton.pm_turn_append"):
            existing = path.read_bytes() if path.is_file() else b""
            # Terminate a crash-torn last line so it keeps its own index.
            prefix = "\n" if existing and not existing.endswith(b"\n") else ""
            turn = existing.count(b"\n") + (1 if prefix else 0)
            record = {"turn": turn, "ts": core.now_iso(), "prompt": prompt}
            if cleaned is not None:
                record["referent"] = cleaned
            line = json.dumps(record, ensure_ascii=False)
            with open(path, "ab") as fh:
                fh.write(f"{prefix}{line}\n".encode("utf-8"))
    except (LockTimeout, OSError, RuntimeError) as exc:
        return dict(empty, session_id=sid, log_path=str(path),
                    reason=f"append failed: {exc}")

    result = {"ok": True, "turn": turn, "log_path": str(path),
              "session_id": sid, "reason": None}
    if referent is not None:
        result["referent_recorded"] = cleaned is not None
        if referent_reason:
            result["reason"] = referent_reason
    return result


def _load(path: Path) -> List[Dict[str, Any]]:
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    turns = []
    # Bytes split on "\n" only: str.splitlines also breaks on U+2028/U+0085,
    # which a verbatim prompt may contain. A torn line fails to decode or parse.
    for line in raw.split(b"\n"):
        try:
            entry = json.loads(line.decode("utf-8"))
        except ValueError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("turn"), int) \
                and isinstance(entry.get("prompt"), str):
            turns.append(entry)
    return turns


def _render(selected: List[Dict[str, Any]]) -> str:
    if len(selected) == 1:
        return selected[0]["prompt"]
    return "\n\n".join(f"## PM turn {t['turn']} ({t.get('ts')})\n\n{t['prompt']}"
                       for t in selected)


def read_turns(session_id: Optional[str] = None, cwd: Optional[str] = None, *,
               turn: Optional[int] = None, start: Optional[int] = None,
               end: Optional[int] = None, all_turns: bool = False) -> dict:
    """Select by ``all_turns``, else ``start``/``end`` (inclusive, either
    open), else ``turn`` (negative is positional from the latest recorded
    turn, so it skips index gaps), else turn 0. One
    turn renders as its verbatim prompt; several render as headed sections."""
    empty = {"ok": False, "text": "", "turns": [], "count": 0,
             "log_path": None, "session_id": session_id, "reason": None}

    sid = session_id or resolve_current_session_id(Path(cwd) if cwd else None)
    if not sid:
        return dict(empty, reason="no resolvable session id")
    path = log_path(sid, cwd)
    if path is None:
        return dict(empty, session_id=sid, reason="session hub unresolvable")

    turns = _load(path) if path.is_file() else []
    base = dict(empty, session_id=sid, log_path=str(path), count=len(turns))
    if not turns:
        return dict(base, reason="no PM turns recorded for this session")

    if all_turns:
        selected = turns
    elif start is not None or end is not None:
        lo = 0 if start is None else start
        hi = turns[-1]["turn"] if end is None else end
        selected = [t for t in turns if lo <= t["turn"] <= hi]
    else:
        want = 0 if turn is None else turn
        if want < 0:
            selected = turns[want:want + 1 or None] if -want <= len(turns) else []
        else:
            selected = [t for t in turns if t["turn"] == want]

    if not selected:
        return dict(base, reason="no recorded turn matches the selection")
    return dict(base, ok=True, text=_render(selected), turns=selected)


#: Per-turn character cap for a sizing embed; there is no whole-window budget.
SIZING_WINDOW_CHARS = 6000


def recent_window(session_id: Optional[str] = None, cwd: Optional[str] = None,
                  after_turn: Optional[int] = None) -> List[Dict[str, Any]]:
    """Every turn newer than ``after_turn``, newest first, as ``{turn, ts, text}``.
    ``after_turn`` unset reads the session's ``sized_through_turn`` mark (none:
    all turns). A single turn over ``SIZING_WINDOW_CHARS`` is head-cut and
    marked ``truncated``."""
    sid = session_id or resolve_current_session_id(Path(cwd) if cwd else None)
    path = log_path(sid, cwd) if sid else None
    if path is None or not path.is_file():
        return []
    if after_turn is None:
        mark = store.read_baton(sid, cwd).get("sized_through_turn")
        after_turn = mark if isinstance(mark, int) and not isinstance(mark, bool) else None
    window: List[Dict[str, Any]] = []
    for entry in reversed(_load(path)):
        if after_turn is not None and entry["turn"] <= after_turn:
            continue
        text = entry["prompt"]
        item = {"turn": entry["turn"], "ts": entry.get("ts"), "text": text}
        if len(text) > SIZING_WINDOW_CHARS:
            item["text"] = text[:SIZING_WINDOW_CHARS]
            item["truncated"] = True
        window.append(item)
    return window


def mark_sized_through(turn: int, session_id: Optional[str] = None,
                       cwd: Optional[str] = None) -> bool:
    """Raise the session's ``sized_through_turn`` to ``turn``; never lowers it."""
    sid = session_id or resolve_current_session_id(Path(cwd) if cwd else None)
    if not sid:
        return False
    return store.merge_baton(sid, cwd, sized_through_turn=turn) is not None


#: A turn is quoted by an intent when they share a run of this many words (or the intent holds
#: the whole turn, for a turn at least `_QUOTE_MIN_WORDS` long).
_QUOTE_RUN_WORDS = 6
_QUOTE_MIN_WORDS = 3


def _words(text: str) -> List[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def quotes_turn(intent: str, turn_text: str) -> bool:
    """True when `intent` quotes `turn_text`: the whole turn, or a run of `_QUOTE_RUN_WORDS`
    words of it, appears in the intent (case and punctuation ignored)."""
    have, turn = _words(intent), _words(turn_text)
    if len(turn) < _QUOTE_MIN_WORDS or not have:
        return False
    joined = " " + " ".join(have) + " "
    if " " + " ".join(turn) + " " in joined:
        return True
    n = _QUOTE_RUN_WORDS
    runs = {" ".join(have[i:i + n]) for i in range(len(have) - n + 1)}
    return any(" ".join(turn[i:i + n]) in runs for i in range(len(turn) - n + 1))


def bound_window(intent: Optional[str], session_id: Optional[str] = None, cwd: Optional[str] = None,
                 budget: int = SIZING_WINDOW_CHARS) -> List[Dict[str, Any]]:
    """The turns a sizing embeds, bound to its ask by content: every turn `intent` quotes comes
    first, whatever its age (one over the budget left is cut and marked `truncated`), then the
    newest unquoted turns past the session's ``sized_through_turn`` mark fill what budget is left,
    stopping before the first whole turn that would overrun it. Returned newest first. An intent that quotes no turn (the EM's paraphrase)
    falls back to `recent_window`, as does a missing intent."""
    if not intent or not intent.strip():
        return recent_window(session_id, cwd)
    sid = session_id or resolve_current_session_id(Path(cwd) if cwd else None)
    path = log_path(sid, cwd) if sid else None
    if path is None or not path.is_file():
        return []
    turns = list(reversed(_load(path)))
    quoted = [t for t in turns if quotes_turn(intent, t["prompt"])]
    if not quoted:
        return recent_window(session_id, cwd)
    window: List[Dict[str, Any]] = []
    used = 0
    for entry in quoted:
        text = entry["prompt"]
        room = budget - used
        if len(text) > room:
            if room > 0:
                window.append({"turn": entry["turn"], "ts": entry.get("ts"), "text": text[:room], "truncated": True})
                used = budget
            continue
        window.append({"turn": entry["turn"], "ts": entry.get("ts"), "text": text})
        used += len(text)
    taken = {t["turn"] for t in quoted}
    mark = store.read_baton(sid, cwd).get("sized_through_turn")
    for entry in turns:
        if entry["turn"] in taken or (
            isinstance(mark, int) and not isinstance(mark, bool) and entry["turn"] <= mark
        ):
            continue
        if used + len(entry["prompt"]) > budget:
            break
        window.append({"turn": entry["turn"], "ts": entry.get("ts"), "text": entry["prompt"]})
        used += len(entry["prompt"])
    return sorted(window, key=lambda t: t["turn"], reverse=True)


def _int_param(params: dict, key: str) -> Optional[int]:
    value = params.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value


@register_op("baton.pm_turn_append")
def _pm_turn_append(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC ``baton.pm_turn_append`` handler.

    Params: ``prompt`` (str, required, stored verbatim), ``session_id`` (str,
    optional), ``referent`` (object, optional: ``excerpt`` and/or
    ``transcript_uuid`` of the EM message answered). Returns ``ok``, ``turn``,
    ``log_path``, ``session_id``, ``reason``; plus ``referent_recorded`` when a
    referent was passed (a malformed one still appends the prompt).
    """
    return append_turn(
        params.get("prompt", ""),
        session_id=params.get("session_id"),
        cwd=str(repo_root) if repo_root else None,
        referent=params.get("referent"),
    )


@register_op("baton.pm_turns")
def _pm_turns(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC ``baton.pm_turns`` handler.

    Params: ``session_id`` (str, optional); one selector of ``all`` (bool),
    ``start``/``end`` (int, inclusive), or ``turn`` (int, negative from the
    latest); none means turn 0. Returns ``ok``, ``text``, ``turns``, ``count``
    (turns recorded), ``log_path``, ``session_id``, ``reason``.
    """
    try:
        turn, start, end = (_int_param(params, k) for k in ("turn", "start", "end"))
    except ValueError as exc:
        return {"ok": False, "text": "", "turns": [], "count": 0, "log_path": None,
                "session_id": params.get("session_id"), "reason": str(exc)}
    return read_turns(
        session_id=params.get("session_id"),
        cwd=str(repo_root) if repo_root else None,
        turn=turn, start=start, end=end, all_turns=bool(params.get("all")),
    )
