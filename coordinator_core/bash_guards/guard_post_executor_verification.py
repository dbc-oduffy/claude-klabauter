"""coordinator_core.bash_guards.guard_post_executor_verification -- PreToolUse(Bash)
hard-deny: the main EM re-verifying a dispatched executor's work itself.

Once the session has dispatched a ``coordinator:executor``, the EM's own
verification runs are capped at one (the falsifier run); a second routes through
``coordinator:test-runner``. A verification run is pytest, ``python -c`` or
heredoc-fed (``python -``) code that imports a top-level package of the repo under
test or loads one of its ``.py`` files by quoted path, or ``python <x>.py`` whose
script sits outside the repo under a temp or scratch(pad) dir (a probe; installed
tools outside the repo are not). A ``-c`` importing
only stdlib/third-party and an in-repo ``.py`` tool are not counted. Package
detection is one ``os.scandir`` of the repo root, memoized per root for the
process; a package added mid-process is not seen. The Write/Edit half (a ``.py`` file under
``scratch/``) lives in ``hooks.nudge_em_code_dispatch.op`` and shares this
module's ``after_executor_deny``.

Executor signal: a ``coordinator:executor`` row in the session's
``dispatched-agents.txt`` -- one small file read, no spawn, no transcript. The
file records dispatch, not arrival; a foreground dispatch has returned before
the EM's next tool call, which is the case this targets.

Exempt: a scoped pytest (every target an explicit in-repo test path or node id) in an
execute-plan session -- ``session-shape.json`` records a ``plan.path`` whose plan reads
``status: executing`` (execute-plan doctrine has the EM run the chunk's tests itself);
no counter bump. Also subagents (``agent_id`` present) and a sentinel
carrying a reason line (the dispatch sentinel; each honoured one is appended to
``dispatch-escapes.jsonl``). One counter, ``post-executor-pytest-runs`` in the
session share dir, spans every verification kind; counter I/O failure fails open.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES
from coordinator_core.bash_guards._verdict import record_silent
from coordinator_core.bash_guards.block_subagent_destructive_action import (
    _normalize_executable_basename,
    _segments_from_tokens,
    _strip_leading_subshell_and_env,
    _tokenize_full_command,
)
from coordinator_core.warp_fire import fire_step

CLASS = "hard-deny"
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 41

GUARD_NAME = "guard-post-executor-verification"
COUNTER_NAME = "post-executor-pytest-runs"

_PYTHON_RE = re.compile(r"^python[0-9.]*$")
_DASH_M_BUNDLED_RE = re.compile(r"^-m(.+)$")

_DENY_BODY = (
    "Verification after an executor is the warp run's test stage, not the EM's. "
    + fire_step("<this session's sizing path>")
)


# Launchers that run their argument command; each is peeled before the pytest test.
# A leading bare duration is `timeout N` whose `timeout` the tokenizer folded into a
# `$(` assignment (`n=$(timeout 120 pytest ...)`).
_DURATION_RE = re.compile(r"^[0-9.]+[smhd]?$")
_WRAPPERS = frozenset({"with-suite-mutex", "timeout", "nice", "nohup", "time", "stdbuf", "ionice", "command", "exec"})


_SHELL_KEYWORDS = frozenset({"do", "then", "else", "{", "!"})
_SHELLS = frozenset({"bash", "sh", "zsh"})


def _peel_wrappers(working: List[str]) -> List[str]:
    working = list(working)
    i = 0
    while i < len(working):
        tok = working[i]
        if tok in _SHELL_KEYWORDS:
            i += 1
            continue
        # `n=$(cmd` / `$(cmd`: the tokenizer keeps the substitution opener on the word.
        if "$(" in tok:
            rest = tok.split("$(", 1)[1]
            if rest:
                working[i] = rest
            else:
                i += 1
            continue
        head = _normalize_executable_basename(tok)
        if head in _WRAPPERS:
            i += 1
            # wrapper options, `--`, and timeout's duration operand
            while i < len(working) and (working[i].startswith("-") or (head == "timeout" and _DURATION_RE.match(working[i]))):
                if working[i] in ("-n", "-k", "-s", "--signal", "--kill-after", "--adjustment") and i + 1 < len(working):
                    i += 1
                i += 1
            continue
        if _DURATION_RE.match(working[i]):
            i += 1
            continue
        break
    return working[i:]


_PKG_CACHE: Dict[str, frozenset] = {}
_IMPORT_RE = re.compile(r"\b(?:from\s+([A-Za-z_]\w*)|import\s+([\w.]+(?:\s*,\s*[\w.]+)*))")
_PY_OPTS_WITH_ARG = frozenset({"-W", "-X", "-Q"})


def _repo_packages(repo_root: str) -> frozenset:
    pkgs = _PKG_CACHE.get(repo_root)
    if pkgs is None:
        try:
            with os.scandir(repo_root) as it:
                pkgs = frozenset(
                    e.name for e in it
                    if e.is_dir() and os.path.exists(os.path.join(e.path, "__init__.py"))
                )
        except OSError:
            pkgs = frozenset()
        _PKG_CACHE[repo_root] = pkgs
    return pkgs


_REPO_PY_PATH_RE = re.compile(r"""["']\.?/?([A-Za-z_]\w*)/[^"'\s]*\.py["']""")
_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n(.*?)\n[ \t]*\2[ \t]*(?:\n|$)", re.S)


def _code_imports_repo(code: str, repo_root: str) -> bool:
    """Imports a repo top-level package, or loads a repo ``.py`` by quoted path."""
    pkgs = _repo_packages(repo_root)
    if not pkgs:
        return False
    for m in _IMPORT_RE.finditer(code):
        names = [m.group(1)] if m.group(1) else [n.strip().split(".")[0] for n in m.group(2).split(",")]
        if any(n in pkgs for n in names):
            return True
    return any(m.group(1) in pkgs for m in _REPO_PY_PATH_RE.finditer(code))


def _heredoc_code(cmd: str) -> str:
    """Every heredoc body in ``cmd``: the tokenizer splits them into segments of their own."""
    return "\n".join(m.group(3) for m in _HEREDOC_RE.finditer(cmd))


def _is_probe_script(path: str, repo_root: str, cwd: str) -> bool:
    """A throwaway script: outside the repo AND under a temp or scratch(pad) dir.
    Installed tools outside the repo (engine scripts, plugin bin) are not probes."""
    if "$" in path:
        return True
    full = os.path.normpath(os.path.join(cwd, os.path.expanduser(path)))
    root = os.path.normpath(repo_root)
    if full == root or full.startswith(root + os.sep):
        return False
    tmp = os.path.normpath(tempfile.gettempdir())
    parts = full.split(os.sep)
    return full.startswith(tmp + os.sep) or "scratchpad" in parts or "scratch" in parts


def _python_verification(rest: List[str], repo_root: str, cwd: str, stdin_code: str = "") -> bool:
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "-" or tok.startswith("<<"):
            return _code_imports_repo(stdin_code, repo_root)
        if tok == "-m":
            return i + 1 < len(rest) and rest[i + 1] == "pytest"
        m = _DASH_M_BUNDLED_RE.match(tok)
        if m:
            return m.group(1) == "pytest"
        if tok == "-c":
            return i + 1 < len(rest) and _code_imports_repo(rest[i + 1], repo_root)
        if tok in _PY_OPTS_WITH_ARG:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        return tok.endswith(".py") and _is_probe_script(tok, repo_root, cwd)
    return False


def _is_pytest_segment(
    working: List[str], repo_root: Optional[str] = None, cwd: Optional[str] = None, stdin_code: str = ""
) -> bool:
    working = _peel_wrappers(working)
    if not working:
        return False
    head = _normalize_executable_basename(working[0])
    if head in _SHELLS and len(working) > 2 and working[1] == "-c":
        return runs_verification(working[2], repo_root, cwd) if repo_root else runs_pytest(working[2])
    if head == "pytest":
        return True
    if not _PYTHON_RE.match(head):
        return False
    rest = working[1:]
    if repo_root:
        return _python_verification(rest, repo_root, cwd or repo_root, stdin_code)
    for i, tok in enumerate(rest):
        if tok == "-m":
            return i + 1 < len(rest) and rest[i + 1] == "pytest"
        m = _DASH_M_BUNDLED_RE.match(tok)
        if m:
            return m.group(1) == "pytest"
    return False


def runs_pytest(cmd: str) -> bool:
    """True iff some command segment's word is pytest / python -m pytest."""
    return _runs(cmd, None, None, "pytest")


def runs_verification(cmd: str, repo_root: str, cwd: Optional[str] = None) -> bool:
    """True iff a segment runs pytest, repo-importing ``python -c``, or an out-of-repo ``python x.py``."""
    return _runs(cmd, repo_root, cwd, "python")


def _runs(cmd: str, repo_root: Optional[str], cwd: Optional[str], needle: str) -> bool:
    if needle not in cmd and "pytest" not in cmd:
        return False
    tokens = _tokenize_full_command(cmd)
    if tokens is None:
        return False
    stdin_code = _heredoc_code(cmd) if repo_root and "<<" in cmd else ""
    for seg_tokens, _pipe_before in _segments_from_tokens(tokens):
        # The env stripper eats `n=$(python3` as an assignment; peeling handles `$(` itself.
        working = seg_tokens if seg_tokens and "$(" in seg_tokens[0] else _strip_leading_subshell_and_env(seg_tokens) if seg_tokens else []
        if working and _is_pytest_segment(working, repo_root, cwd, stdin_code):
            return True
    return False


_PYTEST_OPTS_WITH_ARG = frozenset({
    "-k", "-m", "-p", "-c", "-o", "-W", "-n", "--maxfail", "--rootdir", "--deselect",
    "--ignore", "--ignore-glob", "--tb", "--junitxml", "--basetemp", "--durations", "--timeout",
    "--cov", "--cov-report", "--confcutdir", "--import-mode", "--log-level", "--color", "--capture",
})
_REDIRECT_RE = re.compile(r"^\d*[<>]")


def _pytest_targets(working: List[str]) -> Optional[List[str]]:
    """Positional targets of a direct pytest / python -m pytest segment, or None
    when the segment is not one (nested shells, probes) or uses ``--pyargs``."""
    working = _peel_wrappers(working)
    if not working:
        return None
    head = _normalize_executable_basename(working[0])
    if head == "pytest":
        rest = working[1:]
    elif _PYTHON_RE.match(head):
        rest = None
        for i, tok in enumerate(working[1:], 1):
            if tok == "-m" and i + 1 < len(working) and working[i + 1] == "pytest":
                rest = working[i + 2:]
                break
            if tok.startswith("-") or tok in _PY_OPTS_WITH_ARG:
                continue
            return None
        if rest is None:
            return None
    else:
        return None
    targets: List[str] = []
    i = 0
    while i < len(rest):
        tok = rest[i]
        i += 1
        if tok == "--pyargs":
            return None
        if _REDIRECT_RE.match(tok):
            if re.fullmatch(r"\d*[<>]+&?\d*", tok):
                i += 1
            continue
        if tok.startswith("-"):
            if "=" not in tok and tok in _PYTEST_OPTS_WITH_ARG:
                i += 1
            continue
        targets.append(tok)
    return targets


def _is_scoped_target(target: str, repo_root: str, cwd: str) -> bool:
    path = target.split("::", 1)[0]
    if not path or "$" in path or "*" in path:
        return False
    full = os.path.normcase(os.path.normpath(os.path.join(cwd, path)))
    root = os.path.normcase(os.path.normpath(repo_root))
    if full != root and not full.startswith(root + os.sep):
        return False
    return full != root and (path.endswith(".py") or os.path.isdir(full))


def is_scoped_pytest(cmd: str, repo_root: str, cwd: str) -> bool:
    """True iff every verification segment is a direct pytest whose every positional
    target is an explicit in-repo test path or node id, with at least one target."""
    tokens = _tokenize_full_command(cmd)
    if tokens is None:
        return False
    stdin_code = _heredoc_code(cmd) if "<<" in cmd else ""
    seen = False
    for seg_tokens, _pipe_before in _segments_from_tokens(tokens):
        working = seg_tokens if seg_tokens and "$(" in seg_tokens[0] else _strip_leading_subshell_and_env(seg_tokens) if seg_tokens else []
        if not working or not _is_pytest_segment(working, repo_root, cwd, stdin_code):
            continue
        targets = _pytest_targets(working)
        if not targets or not all(_is_scoped_target(t, repo_root, cwd) for t in targets):
            return False
        seen = True
    return seen


def execute_plan_session(session_id: str, repo_root: str) -> bool:
    """This session claimed a plan through execute-plan: ``claim_plan(for_execution=True)``
    stamps the plan ``executing`` and records ``plan.path`` in the session's
    ``session-shape.json``. Two small reads, no spawn; any failure reads as False."""
    from coordinator_core.lifecycle import git_common_dir

    try:
        sdir = git_common_dir(Path(repo_root)) / "coordinator-sessions" / session_id
        with open(sdir / "session-shape.json", "r", encoding="utf-8") as fh:
            rel = ((json.load(fh).get("plan") or {}).get("path")) or ""
        if not rel:
            return False
        with open(Path(repo_root) / rel, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(4096)
    except Exception:
        return False
    return re.search(r"^status:\s*['\"]?executing['\"]?\s*$", head, re.M) is not None


def _executor_type(value: str) -> bool:
    folded = value.strip().casefold()
    if folded.startswith("coordinator:"):
        folded = folded[len("coordinator:"):]
    return folded == "executor"


def executor_reported(payload: dict, session_id: str, repo_root: str) -> bool:
    from coordinator_core.lifecycle import git_common_dir

    try:
        ledger = git_common_dir(Path(repo_root)) / "coordinator-sessions" / session_id / "dispatched-agents.txt"
        with open(ledger, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                cols = line.rstrip("\n").split("\t")
                if len(cols) > 2 and _executor_type(cols[2]):
                    return True
    except Exception:
        return False
    return False


def _deny(event_sentinel: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"{_DENY_BODY} No self-escape: if an executor BLOCKs, a reason line in {event_sentinel} is honoured once.",
        }
    }


def _gate(payload: dict, subject: str):
    """(session_id, repo_root, verdict) where verdict is 'exempt' | 'deny'."""
    from coordinator_core.hooks import dispatch_escape as nd
    from coordinator_core.hooks import nudge_unrouted_sizing as sizing
    from coordinator_core.session.dispatch_nudge_sentinel import sentinel_path

    session_id = nd._resolve_session_id_op(payload)
    repo_root = sizing._repo_root({"cwd": payload.get("cwd") or os.getcwd()})
    if not repo_root:
        return session_id, repo_root, "exempt"
    if not executor_reported(payload, session_id, repo_root):
        return session_id, repo_root, "exempt"
    return session_id, repo_root, "deny"


def after_executor_deny(payload: dict, subject: str) -> Optional[dict]:
    """Deny envelope when the EM has an executor behind it and no escape; else None."""
    from coordinator_core.hooks import dispatch_escape as nd

    session_id, _repo_root, verdict = _gate(payload, subject)
    if verdict != "deny" or nd._take_escape(payload, session_id, subject, GUARD_NAME):
        return None
    from coordinator_core.session.dispatch_nudge_sentinel import sentinel_path

    return _deny(str(sentinel_path(session_id)))


def _bump_counter(repo_root: str, session_id: str) -> int:
    """Return runs already counted this session, then record one more."""
    from coordinator_core.session.machinery_paths import share_dir

    d = share_dir(repo_root, session_id)
    path = os.path.join(d, COUNTER_NAME)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            prior = int(fh.read().strip() or "0")
    except (OSError, ValueError):
        prior = 0
    try:
        os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(str(prior + 1))
    except OSError:
        pass
    return prior


def check(payload: dict) -> Optional[dict]:
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None
    if "agent_id" in payload:
        record_silent(GUARD_NAME, "subagent caller: exempt, executors run their own tests")
        return None
    tool_input = payload.get("tool_input")
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd or "python" not in cmd and "pytest" not in cmd:
        return None
    session_id, repo_root, verdict = _gate(payload, cmd[:200])
    clean = cmd.replace("\r", "")
    cwd = payload.get("cwd") or repo_root
    if verdict != "deny" or not runs_verification(clean, repo_root, cwd):
        return None
    if is_scoped_pytest(clean, repo_root, cwd) and execute_plan_session(session_id, repo_root):
        record_silent(GUARD_NAME, "scoped pytest in an execute-plan session: exempt")
        return None
    if _bump_counter(repo_root, session_id) < 1:
        return None
    from coordinator_core.hooks import dispatch_escape as nd

    if nd._take_escape(payload, session_id, cmd[:200], GUARD_NAME):
        return None
    from coordinator_core.session.dispatch_nudge_sentinel import sentinel_path

    return _deny(str(sentinel_path(session_id)))
