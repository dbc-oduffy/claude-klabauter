"""EM pre-fire survey window: un-exempt main-thread tool calls since the last genuine user record.

State lives at `<session hub>/<sid>/em-prefire.json`, beside `em-turns.json`. `advance` reads only
transcript bytes appended since the stored offset, never the whole file, and spawns nothing.

Negative-spec:
  - No full-transcript walk: a pending delta over `em_turn_count.READ_CAP` jumps the offset to the
    tail and marks the record `partial` (counts are then a floor).
  - A subagent record (`isSidechain`) is never counted, and never opens or disarms a window.
  - The `Skill` disarm is a closed literal tuple; a prefix match on `coordinator:` is refused.
  - A lifecycle verb counts toward no budget only as an invocation (`lifecycle_op`); a mention counts.
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
_QUOTED_RE = re.compile(r"\"[^\"]*\"|'[^']*'")
# Engine lifecycle and comms verbs: an invocation is not survey, so it counts toward no budget.
LIFECYCLE_OPS = (
    "sizing.supersede",
    "memo.transition",
    "workflow.revise",
    "baton.awaiting_gate_recheck",
    "roadmap.plan_gate",
)
SIBLING_PROBE_GIT_VERBS = ("merge-base", "log")
LIFECYCLE_BINS = ("cross-repo-memo", "pickup-assemble")
_VALUE_FLAGS = ("--params-file", "--params", "--repo-root")
_INVOKERS =("coordinator-invoke", "invoke")
_PYTHONS = re.compile(r"^python[0-9.]*(\.exe)?$")
PUBLISH_GIT_VERBS = ("merge-tree", "commit-tree", "push", "fetch")
PUBLISH_CLONE_GIT_VERBS = ("checkout", "switch", "reset", "clean")
_PUBLISH_CLONE_RE = re.compile(r"(^|/)scratch/[^/\s]*-pub(/|$)")
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
        "test_used": False,
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


def _plain_args(rest: str) -> Optional[list]:
    """Shell-quote-aware words of a command tail, or None when it chains, substitutes or is unterminated.

    Single quotes are literal (backticks, `;`, `$(` inside are data); inside double quotes only a
    backtick or `$(` is live; outside quotes any of `;&|<>` or a newline chains. A backslash outside
    single quotes escapes the next character, which is how `'\\''` embeds an apostrophe.
    """
    words: list = []
    cur: list = []
    started = False
    i, n = 0, len(rest)
    while i < n:
        ch = rest[i]
        if ch == "\\":
            if i + 1 >= n:
                return None
            cur.append(rest[i + 1])
            started = True
            i += 2
        elif ch == "'":
            j = rest.find("'", i + 1)
            if j < 0:
                return None
            cur.append(rest[i + 1:j])
            started = True
            i = j + 1
        elif ch == '"':
            i += 1
            while True:
                if i >= n:
                    return None
                c = rest[i]
                if c == '"':
                    break
                if c == "`" or rest.startswith("$(", i):
                    return None
                if c == "\\" and i + 1 < n and rest[i + 1] in '"\\$`':
                    i += 1
                    c = rest[i]
                cur.append(c)
                i += 1
            started = True
            i += 1
        elif ch in ";&|<>\n`" or rest.startswith("$(", i):
            return None
        elif ch.isspace():
            if started:
                words.append("".join(cur))
                cur, started = [], False
            i += 1
        else:
            cur.append(ch)
            started = True
            i += 1
    if started:
        words.append("".join(cur))
    return words


def lifecycle_op(command: object) -> Optional[str]:
    """The lifecycle verb a Bash command invokes, else None.

    An invocation is `cross-repo-memo` / `pickup-assemble` as the executable, or `coordinator-invoke`
    / `python -m coordinator_core.invoke` followed by a LIFECYCLE_OPS verb as its first argument.
    Trap: a verb named anywhere else (grep, cat, echo, a path) is a mention and returns None, as
    does a chained or substituted tail, which could run a survey behind the verb.
    """
    if not isinstance(command, str):
        return None
    first, rest = _first_command_token(command)
    if not first:
        return None
    args = _plain_args(rest)
    if args is None:
        return None
    base = _norm(first).rsplit("/", 1)[-1]
    if base.endswith(".py"):
        base = base[:-3]
    if base in LIFECYCLE_BINS:
        return base
    if _PYTHONS.match(base):
        if args[:2] != ["-m", "coordinator_core.invoke"]:
            return None
        args = args[2:]
    elif base not in _INVOKERS:
        return None
    kept: list = []
    skip = False
    for a in args:
        if skip:
            skip = False
        elif a in _VALUE_FLAGS:
            skip = True
        elif not a.startswith("-"):
            kept.append(a)
    args = kept
    return args[0] if args and args[0] in LIFECYCLE_OPS else None


def _segment_exempt(seg: str, state: dict) -> bool:
    seg = seg.strip()
    seg = re.sub(r"^[A-Za-z_][A-Za-z0-9_]*=", "", seg).strip()
    if seg.startswith("$(") and seg.endswith(")"):
        seg = seg[2:-1].strip()
    if not seg or re.search(r"[|<>&`]|\$\(", seg):
        return False
    toks = [_unquote(t) for t in seg.split()]
    if toks[0] == "cd":
        if len(toks) == 2 and _PUBLISH_CLONE_RE.search(_norm(toks[1])):
            state["clone"] = True
            return True
        return False
    base = _norm(toks[0]).rsplit("/", 1)[-1]
    if _PYTHONS.match(base):
        toks = toks[1:]
        return bool(toks) and _norm(toks[0]).rsplit("/", 1)[-1] in ("percolate-round.py", "percolate-round")
    if base in ("percolate-round.py", "percolate-round", "percolate-round.cmd"):
        return True
    if base != "git":
        return False
    rest = toks[1:]
    clone = state.get("clone", False)
    sibling = False
    while rest and rest[0] == "-C" and len(rest) > 1:
        clone = clone or bool(_PUBLISH_CLONE_RE.search(_norm(rest[1])))
        sibling = True
        rest = rest[2:]
    if not rest:
        return False
    if sibling and rest[0] in SIBLING_PROBE_GIT_VERBS:
        return not any(t.startswith("--output") for t in rest[1:])
    if rest[0] in PUBLISH_GIT_VERBS:
        return True
    return rest[0] in PUBLISH_CLONE_GIT_VERBS and clone


def publish_mechanics(command: object) -> bool:
    """True when every `&&`/`;`/newline segment of a Bash command is publish or build mechanics.

    Exempt: `git merge-tree|commit-tree|push|fetch`, `git checkout|switch|reset|clean` and `cd` only
    inside a publish clone (`scratch/*-pub`), `percolate-round.py`, and the read-only gate probes
    `git -C <dir> merge-base|log`. Trap: a pipe, redirect, backtick or nested substitution anywhere
    in a segment makes the command count, as does any other git verb, and a `git log`/`merge-base`
    without `-C` (diff, show, grep, bare log stay survey).
    """
    if not isinstance(command, str) or not command.strip():
        return False
    flat = _QUOTED_RE.sub("Q", command)
    if re.search(r"[|<>`]", flat):
        return False
    state: dict = {}
    segs = [s for s in re.split(r"&&|;|\n", command) if s.strip()]
    return bool(segs) and all(_segment_exempt(s, state) for s in segs)


def is_test_run(command: object) -> bool:
    """True for one plain `pytest` / `python -m pytest` naming explicit test files, no chain."""
    if not isinstance(command, str) or _CHAIN_RE.search(command):
        return False
    toks = [_unquote(t) for t in command.split()]
    if toks and toks[0].startswith("cd "):
        return False
    if toks[:1] and _PYTHONS.match(_norm(toks[0]).rsplit("/", 1)[-1]):
        toks = toks[1:]
        if toks[:2] != ["-m", "pytest"]:
            return False
        toks = toks[2:]
    elif toks[:1] and _norm(toks[0]).rsplit("/", 1)[-1] == "pytest":
        toks = toks[1:]
    else:
        return False
    targets = [t for t in toks if not t.startswith("-")]
    return bool(targets) and all(re.search(r"(^|/)test_[^/]*\.py(::\S+)?$", t) for t in targets)


def classify(
    name: object,
    tool_input: object,
    opening: str,
    settings_home: Optional[str],
    test_used: bool = False,
) -> tuple[bool, bool]:
    """(pre_counted, all_counted) for one main-thread tool call.

    `pre_counted` applies the pre-fire exemptions (paths, settings-home CLI); `all_counted` keeps the
    path exemptions only, for the post-landing window, plus the publish-mechanics exemption and the
    window's one test run (`test_used` False).
    """
    if name not in COUNTED_TOOLS:
        return False, False
    inp = tool_input if isinstance(tool_input, dict) else {}
    if name == "Bash":
        cmd = inp.get("command")
        if lifecycle_op(cmd):
            return False, False
        if publish_mechanics(cmd):
            return False, False
        pre = not is_settings_bin_cli(cmd, settings_home)
        if not test_used and is_test_run(cmd):
            return pre, False
        return pre, True
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
                        test_used=False,
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
        pre, every = classify(name, inp, st.get("opening") or "", settings_home, bool(st.get("test_used")))
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
            st["test_used"] = False
            st["fire_seen"] = True
            st["fires"] = int(st.get("fires") or 0) + 1
        return
    if name == "Skill":
        if _skill_disarms(inp):
            st["disarmed"] = True
        return
    pre, every = classify(name, inp, st.get("opening") or "", settings_home, bool(st.get("test_used")))
    if name == "Bash" and isinstance(inp, dict):
        if lifecycle_op(inp.get("command")) == "workflow.revise":
            st["revised"] = True
        elif not st.get("test_used") and is_test_run(inp.get("command")):
            st["test_used"] = True
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
    "is_test_run",
    "publish_mechanics",
    "lifecycle_op",
    "load_state",
    "path_exempt",
    "save_state",
    "state_path",
]
