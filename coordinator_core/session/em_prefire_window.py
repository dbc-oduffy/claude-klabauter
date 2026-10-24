"""EM pre-fire survey window: un-exempt main-thread tool calls since the last genuine user record.

State lives at `<session hub>/<sid>/em-prefire.json`, beside `em-turns.json`. `advance` reads only
transcript bytes appended since the stored offset, never the whole file, and spawns nothing.

Negative-spec:
  - No full-transcript walk: a pending delta over `em_turn_count.READ_CAP` jumps the offset to the
    tail and marks the record `partial` (counts are then a floor).
  - A subagent record (`isSidechain`) is never counted, and never opens or disarms a window.
  - The `Skill` disarm is a closed literal tuple; a prefix match on `coordinator:` is refused.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

from coordinator_core import warp_fire
from coordinator_core.session.em_turn_count import READ_CAP, save_state

COUNTED_TOOLS = ("Read", "Grep", "Glob", "Bash")
OPENING_TEXT_CAP = 8 * 1024

# Pickup, handoff, review and close-out class skills of DoE coordinator/skills; exact names only.
DISARM_SKILLS = (
    "pickup",
    "handoff",
    "review",
    "parallel-code-review",
    "enrich-and-review",
    "quick-wrap",
    "workstream-complete",
    "finishing-a-development-branch",
)

EXEMPT_PATH_SEGMENTS = ("/state/warp/", "/.coordinator-local/", "/cross-repo/inbox/")

_STATE_FILENAME = "em-prefire.json"
_USER_MARK = b'"role":"user"'
_ASSISTANT_MARK = b'"role":"assistant"'
_TOOL_USE_MARK = b'"tool_use"'
_ASSIGN_RE = re.compile(r"""\s*[A-Za-z_][A-Za-z0-9_]*=(?:"[^"]*"|'[^']*'|\S*)""")
_TOKEN_RE = re.compile(r"""\s*("[^"]*"|'[^']*'|\S+)""")
# A chained or substituted tail could run a survey behind the exempt CLI; such a command counts.
_CHAIN_RE = re.compile(r"[;&|`<>\n]|\$\(")
_LITERAL_BINS = (
    "${COORDINATOR_SETTINGS_HOME:-${CLAUDE_HOME:-$HOME}/.coordinator-claude-settings}/bin/",
    "${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/",
)


def state_path(repo_root, sid: str) -> Path:
    """`em-prefire.json` beside the session's `xs-lane.json`; raises when the hub is unresolvable."""
    from coordinator_core.session import xs_lane_budget

    return xs_lane_budget.lane_path(repo_root, sid).with_name(_STATE_FILENAME)


def fresh_state(offset: int = 0) -> dict:
    return {
        "offset": offset,
        "window": 0,
        "opening": "",
        "pre": [],
        "all": [],
        "fired": False,
        "disarmed": False,
        "revised": False,
        "fire_seen": False,
        "fires": 0,
        "landings": {},
    }


def load_state(path, transcript_path) -> dict:
    """The stored record; absent starts at offset 0, malformed resets at the transcript tail."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return fresh_state()
    except OSError:
        return fresh_state(_tail(transcript_path))
    try:
        rec = json.loads(raw)
    except ValueError:
        return fresh_state(_tail(transcript_path))
    if (
        not isinstance(rec, dict)
        or not isinstance(rec.get("offset"), int)
        or isinstance(rec.get("offset"), bool)
        or rec["offset"] < 0
        or not isinstance(rec.get("pre"), list)
        or not isinstance(rec.get("all"), list)
        or not isinstance(rec.get("landings"), dict)
    ):
        return fresh_state(_tail(transcript_path))
    base = fresh_state()
    base.update(rec)
    return base


def _tail(transcript_path) -> int:
    try:
        return os.stat(transcript_path).st_size
    except OSError:
        return 0


def _norm(path: str) -> str:
    return path.replace("\\", "/")


def path_exempt(path: object, opening: str) -> bool:
    """True for a path under a recorded-artifact directory, or one named verbatim in the window's opening text."""
    if not isinstance(path, str) or not path:
        return False
    norm = _norm(path)
    padded = "/" + norm.lstrip("/")
    if any(seg in padded for seg in EXEMPT_PATH_SEGMENTS):
        return True
    if not opening or not norm.strip("./"):
        return False
    return any(
        re.search(r"(?<![\w./-])" + re.escape(cand) + r"(?![\w/-])", text)
        for cand, text in ((norm, _norm(opening)), (path, opening))
    )


def _unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    return token


def _first_command_token(command: str) -> tuple[str, str]:
    """(first token after `VAR=value` assignments, the text after it)."""
    pos = 0
    while True:
        m = _ASSIGN_RE.match(command, pos)
        if not m or not command[m.end():m.end() + 1].isspace():
            break
        pos = m.end()
    m = _TOKEN_RE.match(command, pos)
    return (_unquote(m.group(1)), command[m.end():]) if m else ("", "")


def is_settings_bin_cli(command: object, settings_home: Optional[str]) -> bool:
    """True when the command's first token (after `VAR=value` assignments) is a CLI under settings-home `bin/`."""
    if not isinstance(command, str):
        return False
    first, rest_of_command = _first_command_token(command)
    if _CHAIN_RE.search(rest_of_command):
        return False
    token = _norm(first)
    prefixes = list(_LITERAL_BINS)
    if settings_home:
        prefixes.append(_norm(str(settings_home)).rstrip("/") + "/bin/")
    for prefix in prefixes:
        if token.startswith(prefix):
            rest = token[len(prefix):]
            return bool(rest) and "/" not in rest
    return False


def classify(name: object, tool_input: object, opening: str, settings_home: Optional[str]) -> tuple[bool, bool]:
    """(pre_counted, all_counted) for one main-thread tool call.

    `pre_counted` applies the pre-fire exemptions (paths, settings-home CLI); `all_counted` keeps the
    path exemptions only, for the post-landing window.
    """
    if name not in COUNTED_TOOLS:
        return False, False
    inp = tool_input if isinstance(tool_input, dict) else {}
    if name == "Bash":
        return (not is_settings_bin_cli(inp.get("command"), settings_home)), True
    target = inp.get("file_path") if name == "Read" else inp.get("path")
    if path_exempt(target, opening):
        return False, False
    return True, True


def _opening_text(content) -> str:
    if isinstance(content, str):
        return content[:OPENING_TEXT_CAP]
    parts = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
    return "\n".join(parts)[:OPENING_TEXT_CAP]


def _genuine_user(rec: dict):
    """The opening text of a user record that is not all tool_result, else None."""
    if rec.get("isMeta") is True:
        return None
    msg = rec.get("message")
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return None
    content = msg.get("content")
    if isinstance(content, list) and content and all(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    ):
        return None
    return _opening_text(content)


def _skill_disarms(inp: object) -> bool:
    name = inp.get("skill") if isinstance(inp, dict) else None
    if not isinstance(name, str):
        return False
    bare = name[len("coordinator:"):] if name.startswith("coordinator:") else name
    return bare in DISARM_SKILLS


def _add(ids: list, tid: object) -> None:
    if isinstance(tid, str) and tid and tid not in ids:
        ids.append(tid)


def advance(
    transcript_path,
    state: dict,
    current_tool_use_id: Optional[str] = None,
    *,
    settings_home: Optional[str] = None,
    current_call: Optional[tuple] = None,
) -> dict:
    """Fold transcript bytes appended since `state['offset']` into the window; returns a new state.

    `current_tool_use_id` joins the counted sets, classified from `current_call` (tool name, tool
    input) against the window's opening text as folded here, so the count holds whether or not the
    harness has written the current call's record. A trailing partial line is left for the next
    call. An unreadable transcript returns the state unchanged.
    """
    st = dict(state)
    for key in ("pre", "all"):
        st[key] = list(state.get(key) or [])
    st["landings"] = dict(state.get("landings") or {})
    try:
        with open(transcript_path, "rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            offset = int(st.get("offset") or 0)
            if offset > size:
                offset = size
            if size - offset > READ_CAP:
                st["offset"] = size
                st["partial"] = True
                data = b""
            else:
                fh.seek(offset)
                data = fh.read(size - offset)
    except OSError:
        return st
    end = data.rfind(b"\n")
    if end >= 0:
        st["offset"] = offset + end + 1
        for line in data[:end].split(b"\n"):
            line = line.rstrip(b"\r")
            is_user = _USER_MARK in line
            is_tool = _ASSISTANT_MARK in line and _TOOL_USE_MARK in line
            if not (is_user or is_tool):
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict) or rec.get("isSidechain") is True:
                continue
            if is_user:
                opening = _genuine_user(rec)
                if opening is not None:
                    st.update(
                        window=int(st.get("window") or 0) + 1,
                        opening=opening,
                        pre=[],
                        all=[],
                        fired=False,
                        disarmed=False,
                        revised=False,
                    )
                continue
            msg = rec.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                _fold_tool_use(st, block, settings_home)
    if isinstance(current_tool_use_id, str) and current_tool_use_id:
        name, inp = current_call if current_call else (None, None)
        pre, every = classify(name, inp, st.get("opening") or "", settings_home)
        if pre:
            _add(st["pre"], current_tool_use_id)
        if every:
            _add(st["all"], current_tool_use_id)
    return st


def _fold_tool_use(st: dict, block: dict, settings_home: Optional[str]) -> None:
    name = block.get("name")
    inp = block.get("input")
    if name == "Workflow":
        if isinstance(inp, dict) and warp_fire.is_submit_name(inp.get("name")):
            st["fired"] = True
            st["fire_seen"] = True
            st["fires"] = int(st.get("fires") or 0) + 1
        return
    if name == "Skill":
        if _skill_disarms(inp):
            st["disarmed"] = True
        return
    pre, every = classify(name, inp, st.get("opening") or "", settings_home)
    if name == "Bash" and isinstance(inp, dict) and "workflow.revise" in str(inp.get("command") or ""):
        st["revised"] = True
    if pre:
        _add(st["pre"], block.get("id"))
    if every:
        _add(st["all"], block.get("id"))


__all__ = [
    "COUNTED_TOOLS",
    "DISARM_SKILLS",
    "advance",
    "classify",
    "fresh_state",
    "is_settings_bin_cli",
    "load_state",
    "path_exempt",
    "save_state",
    "state_path",
]
