"""coordinator_core.bash_guards.guard_post_executor_verification -- PreToolUse(Bash)
hard-deny: the main EM re-verifying a dispatched executor's work itself.

Once the session has dispatched a ``coordinator:executor``, the EM's own
verification runs are capped at one (the falsifier run); a second routes through
``coordinator:test-runner``. A verification run is pytest, ``python -c`` or
heredoc-fed (``python -``) code that imports a top-level package of the repo under
test or loads one of its ``.py`` files by quoted path, or ``python <x>.py`` whose
script sits outside the repo under a temp or scratch(pad) dir (a probe; installed
tools outside the repo are not). ``python -m
coordinator_core.benchmarks.measure [opts] -- <argv>`` is a verification run only when
``<argv>`` is itself a pytest run (the same scoped exemption applies); any other measured
argv is neither counted nor denied. A ``-c`` importing
only stdlib/third-party and an in-repo ``.py`` tool are not counted. Package
detection is one ``os.scandir`` of the repo root, memoized per root for the
process; a package added mid-process is not seen. The Write/Edit half (a ``.py`` file under
``scratch/``) lives in ``hooks.nudge_em_code_dispatch.op`` and shares this
module's ``after_executor_deny``.

Executor signal: only for a payload carrying its own ``session_id`` (the host-pid fallback
id is shared by every session on a warm engine and never vouches for an executor); a ``coordinator:executor`` row in the session's
``dispatched-agents.txt`` -- one small file read, no spawn, no transcript. The
file records dispatch, not arrival; a foreground dispatch has returned before
the EM's next tool call, which is the case this targets. Only rows with an epoch later than
the session's last commit attempt arm (mtime of ``commit-scope-events.jsonl``, one stat); no
such file means armed. A counter older than the newest arming row is ignored.

Exempt: a scoped pytest (every target an explicit in-repo test file, node id, or directory
under a ``tests/`` dir; a bare package dir is broad) in an
execute-plan session -- ``session-shape.json`` records a ``plan.path`` whose plan reads
``status: executing`` (execute-plan doctrine has the EM run the chunk's tests itself) --
or in a session with a ``workflow-run-<session>-*.json`` record (any Workflow run it
launched, completed or not); no counter bump. Also subagents (``agent_id`` present) and a sentinel
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

CLASS = "hard-deny"
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 41

GUARD_NAME = "guard-post-executor-verification"
COUNTER_NAME = "post-executor-pytest-runs"

_PYTHON_RE = re.compile(r"^python[0-9.]*$")
_DASH_M_BUNDLED_RE = re.compile(r"^-m(.+)$")

_DENY_BODY = (
    "A second verification run after an executor is not the EM's. "
    "Dispatch a coordinator:test-runner. "
    "To measure, run python -m coordinator_core.benchmarks.measure -- <argv>."
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


_MEASURE_MODULE = "coordinator_core.benchmarks.measure"


def _measure_tail(rest: List[str], start: int) -> Optional[List[str]]:
    """Tokens after the first ``--`` at or past ``start`` (just after ``-m measure``), or None."""
    for j in range(start, len(rest)):
        if rest[j] == "--":
            return rest[j + 1:]
    return None


def _measured_pytest(tail: Optional[List[str]]) -> bool:
    return bool(tail) and _pytest_targets(tail) is not None


def _python_verification(rest: List[str], repo_root: str, cwd: str, stdin_code: str = "") -> bool:
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "-" or tok.startswith("<<"):
            return _code_imports_repo(stdin_code, repo_root)
        if tok == "-m":
            if i + 1 < len(rest) and rest[i + 1] == _MEASURE_MODULE:
                return _measured_pytest(_measure_tail(rest, i + 2))
            return i + 1 < len(rest) and rest[i + 1] == "pytest"
        m = _DASH_M_BUNDLED_RE.match(tok)
        if m:
            if m.group(1) == _MEASURE_MODULE:
                return _measured_pytest(_measure_tail(rest, i + 1))
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
    return bool(_pytest_segments(cmd, repo_root, cwd))


def _pytest_segments(cmd: str, repo_root: Optional[str] = None, cwd: Optional[str] = None) -> List[List[str]]:
    """Working tokens of each verification segment; empty when none or untokenizable."""
    tokens = _tokenize_full_command(cmd)
    if tokens is None:
        return []
    stdin_code = _heredoc_code(cmd) if repo_root and "<<" in cmd else ""
    found = []
    for seg_tokens, _pipe_before in _segments_from_tokens(tokens):
        # The env stripper eats `n=$(python3` as an assignment; peeling handles `$(` itself.
        working = seg_tokens if seg_tokens and "$(" in seg_tokens[0] else _strip_leading_subshell_and_env(seg_tokens) if seg_tokens else []
        if working and _is_pytest_segment(working, repo_root, cwd, stdin_code):
            found.append(working)
    return found


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
            if tok == "-m" and i + 1 < len(working) and working[i + 1] == _MEASURE_MODULE:
                return _pytest_targets(_measure_tail(working, i + 2) or [])
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
    if full == root:
        return False
    if path.endswith(".py"):
        return True
    return os.path.isdir(full) and "tests" in Path(full).relative_to(root).parts


def is_scoped_pytest(cmd: str, repo_root: str, cwd: str) -> bool:
    """True iff every verification segment is a direct pytest whose every positional
    target is an explicit in-repo test path or node id, with at least one target."""
    segments = _pytest_segments(cmd, repo_root, cwd)
    return bool(segments) and all(
        (targets := _pytest_targets(working)) and all(_is_scoped_target(t, repo_root, cwd) for t in targets)
        for working in segments
    )


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


def _last_commit_time(session_dir: Path) -> float:
    """mtime of the session's ``commit-scope-events.jsonl`` (appended once per commit
    attempt by the commit route); one stat, no spawn. 0.0 when absent."""
    try:
        return os.stat(session_dir / "commit-scope-events.jsonl").st_mtime
    except OSError:
        return 0.0


def newest_arming_epoch(session_id: str, repo_root: str) -> Optional[float]:
    """Epoch of the newest executor row dispatched after the session's last commit,
    or None when nothing arms. A row with an unreadable epoch arms (0.0)."""
    from coordinator_core.lifecycle import git_common_dir

    try:
        sdir = git_common_dir(Path(repo_root)) / "coordinator-sessions" / session_id
        committed = _last_commit_time(sdir)
        newest: Optional[float] = None
        with open(sdir / "dispatched-agents.txt", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                cols = line.rstrip("\n").split("\t")
                if len(cols) > 2 and _executor_type(cols[2]):
                    try:
                        epoch = float(cols[3])
                    except (IndexError, ValueError):
                        epoch = 0.0
                    if epoch > committed or committed == 0.0 or epoch == 0.0:
                        newest = epoch if newest is None else max(newest, epoch)
    except Exception:
        return None
    return newest


def executor_reported(payload: dict, session_id: str, repo_root: str) -> bool:
    return newest_arming_epoch(session_id, repo_root) is not None


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
    sid = payload.get("session_id")
    if not repo_root or not (isinstance(sid, str) and sid.strip()):
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


_SUITE_MUTEX = "with-suite-mutex"


def _granted_suite_run(cmd: str, session_id: object, cwd: Optional[str]) -> bool:
    """Every pytest segment runs under ``with-suite-mutex`` and the session holds a live Tier-U
    grant: the EM's own broad suite, which test-runner may never run, so denying it leaves no route."""
    if _SUITE_MUTEX not in cmd or not isinstance(session_id, str) or not session_id:
        return False
    segments = _pytest_segments(cmd)
    if not segments or any(_normalize_executable_basename(w[0]) != _SUITE_MUTEX for w in segments):
        return False
    from coordinator_core.session.grant import check_tier_u_grant

    try:
        return check_tier_u_grant(cwd, session_id=session_id)[0]
    except Exception:
        return False


def _bump_counter(repo_root: str, session_id: str, armed_since: float = 0.0) -> int:
    """Return runs already counted since the newest arming row, then record one more."""
    from coordinator_core.session.machinery_paths import share_dir

    d = share_dir(repo_root, session_id)
    path = os.path.join(d, COUNTER_NAME)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            prior = int(fh.read().strip() or "0")
        if os.stat(path).st_mtime < armed_since:
            prior = 0
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
    if _granted_suite_run(clean, payload.get("session_id"), cwd):
        record_silent(GUARD_NAME, "suite under with-suite-mutex and a live Tier-U grant: exempt")
        return None
    if is_scoped_pytest(clean, repo_root, cwd):
        if execute_plan_session(session_id, repo_root):
            record_silent(GUARD_NAME, "scoped pytest in an execute-plan session: exempt")
            return None
        from coordinator_core.hooks.flag_em_poll_in_flight import session_workflow_run_ids

        if session_workflow_run_ids(session_id):
            record_silent(GUARD_NAME, "scoped pytest after a workflow run: exempt")
            return None
    if _bump_counter(repo_root, session_id, newest_arming_epoch(session_id, repo_root) or 0.0) < 1:
        return None
    from coordinator_core.hooks import dispatch_escape as nd

    if nd._take_escape(payload, session_id, cmd[:200], GUARD_NAME):
        return None
    from coordinator_core.session.dispatch_nudge_sentinel import sentinel_path

    return _deny(str(sentinel_path(session_id)))
