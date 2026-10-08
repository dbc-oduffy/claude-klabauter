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
    ("yes" twice is two turns).

NEGATIVE SPEC:
  - Does NOT create the session directory (same rule as ``store.merge_baton``:
    a missing directory reports and records nothing).
  - Does NOT touch ``baton.json``; ``first_prompt`` stays ``session_baton.mint``'s.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, held_lock
from coordinator_core.ops.session_context import resolve_current_session_id
from coordinator_core.session import core
from coordinator_core.session_baton import store

PM_TURNS_FILENAME = "pm_turns.jsonl"

#: Same bound as the baton store's own lock: this runs on every prompt.
_LOCK_TIMEOUT_SECS = 2.0


def log_path(sid: str, cwd: Optional[str] = None) -> Optional[Path]:
    sdir = store.baton_dir(sid, cwd)
    return sdir / PM_TURNS_FILENAME if sdir is not None else None


def append_turn(prompt: str, session_id: Optional[str] = None,
                cwd: Optional[str] = None) -> dict:
    empty = {"ok": False, "turn": None, "log_path": None,
             "session_id": session_id, "reason": None}

    if not isinstance(prompt, str) or not prompt.strip():
        return dict(empty, reason="prompt must be a non-empty string")

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
            line = json.dumps({"turn": turn, "ts": core.now_iso(), "prompt": prompt},
                              ensure_ascii=False)
            with open(path, "ab") as fh:
                fh.write(f"{prefix}{line}\n".encode("utf-8"))
    except (LockTimeout, OSError, RuntimeError) as exc:
        return dict(empty, session_id=sid, log_path=str(path),
                    reason=f"append failed: {exc}")

    return {"ok": True, "turn": turn, "log_path": str(path),
            "session_id": sid, "reason": None}


def _load(path: Path) -> List[Dict[str, Any]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    turns = []
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
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
    open), else ``turn`` (negative counts from the latest), else turn 0. One
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


#: Total characters of PM text a sizing embeds. Sized for "the last few
#: messages": a launch ask plus two or three follow-ups, not a transcript.
SIZING_WINDOW_CHARS = 6000


def recent_window(session_id: Optional[str] = None, cwd: Optional[str] = None,
                  budget: int = SIZING_WINDOW_CHARS) -> List[Dict[str, Any]]:
    """The newest turns, newest first, as ``{turn, ts, text}``, stopping before
    the first whole turn that would overrun ``budget``. A newest turn that alone
    exceeds it is the one entry, cut to ``budget`` and marked ``truncated``."""
    sid = session_id or resolve_current_session_id(Path(cwd) if cwd else None)
    path = log_path(sid, cwd) if sid else None
    if path is None or not path.is_file():
        return []
    window: List[Dict[str, Any]] = []
    used = 0
    for entry in reversed(_load(path)):
        text = entry["prompt"]
        if used + len(text) > budget:
            if not window:
                window.append({"turn": entry["turn"], "ts": entry.get("ts"),
                               "text": text[:budget], "truncated": True})
            break
        window.append({"turn": entry["turn"], "ts": entry.get("ts"), "text": text})
        used += len(text)
    return window


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
    optional). Returns ``ok``, ``turn``, ``log_path``, ``session_id``, ``reason``.
    """
    return append_turn(
        params.get("prompt", ""),
        session_id=params.get("session_id"),
        cwd=str(repo_root) if repo_root else None,
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
