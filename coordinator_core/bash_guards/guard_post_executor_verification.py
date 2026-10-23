"""coordinator_core.bash_guards.guard_post_executor_verification -- PreToolUse(Bash)
hard-deny: the main EM re-verifying a dispatched executor's work itself.

Once the session has dispatched a ``coordinator:executor``, the EM's own
verification runs are capped at one (the falsifier run); a second routes through
``coordinator:test-runner``. A verification run is pytest, ``python -c`` or
heredoc-fed (``python -``) code that imports a top-level package of the repo under
test or loads one of its ``.py`` files by quoted path AND names a test runner
(pytest/unittest/doctest), or ``python <x>.py`` whose script sits outside the repo
under a temp or scratch(pad) dir and whose source (read once, size-capped, unreadable
fails open) imports such a package or loads such a file AND names a test runner (a
probe; a stdlib-only script and installed tools outside the repo are not). Read-only
scripts and in-process apply calls are bookkeeping. ``python -m
coordinator_core.benchmarks.measure [opts] -- <argv>`` is a verification run only when
``<argv>`` is itself a pytest run (the same scoped exemption applies); any other measured
argv is neither counted nor denied. A ``-c`` importing
only stdlib/third-party and an in-repo ``.py`` tool are not counted. Package
detection is one ``os.scandir`` of the repo root, memoized per root for the
process; a package added mid-process is not seen. The Write/Edit half (a ``.py`` file under
``scratch/``) lives in ``hooks.nudge_em_code_dispatch.op`` and shares this
module's ``after_executor_deny``; it denies only when the written text itself names a test
runner or the measure harness and the file imports repo code (``written_text_verifies``).

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

import io
import json
import os
import re
import tempfile
import tokenize
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

_DENY_BODY = "A second verification run after an executor is not the EM's."


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


_RUNNER_RE = re.compile(r"\b(?:pytest|unittest|doctest)\b")
_MEASURE_RE = re.compile(r"\bbenchmarks\.measure\b")


_SPAWN_RE = re.compile(
    r"\b(?:subprocess|runpy)\s*\.|\bfrom\s+(?:subprocess|runpy)\b|"
    r"\bimport\s+(?:[\w.]+(?:\s+as\s+\w+)?\s*,\s*)*(?:subprocess|runpy)\b|"
    r"\bfrom\s+[\w.]+\s+import\b[^\n]*\b(?:Popen|check_call|check_output|run_module|system|popen"
    r"|execv\w*|spawn\w*|import_module)\b|"
    r"\b(?:Popen|check_call|check_output|run_module|system|popen"
    r"|execv\w*|spawn\w*|__import__|import_module|exec|eval)\s*\("
)
_TEXT_TOKENS = frozenset({tokenize.STRING, tokenize.COMMENT, getattr(tokenize, "FSTRING_MIDDLE", -1)})


def _code_names_runner(code: str) -> bool:
    """Python ``code`` imports or references a test runner outside string literals and
    comments. A runner named only in text is not a run; code that spawns a process or
    module keeps its strings (``subprocess.run(["pytest"])``). Unlexable code is
    judged raw."""
    if _RUNNER_RE.search(code) is None:
        return False
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(code).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return True
    code_only = " ".join(t.string for t in toks if t.type not in _TEXT_TOKENS)
    if _RUNNER_RE.search(code_only) is not None:
        return True
    return _SPAWN_RE.search(code_only) is not None


def _code_verifies(code: str, repo_root: str) -> bool:
    """Repo-importing code that also invokes a test runner; read-only or apply code is bookkeeping."""
    return _code_names_runner(code) and _code_imports_repo(code, repo_root)


def _script_runs_suite(path: str, cwd: str) -> bool:
    """A probe script invoking a test runner. An unreadable script runs nothing we can see."""
    if "$" in path:
        return True
    try:
        with open(os.path.join(cwd, os.path.expanduser(path)), "r", encoding="utf-8", errors="replace") as fh:
            return _code_names_runner(fh.read(65536))
    except OSError:
        return False


def _heredoc_code(cmd: str) -> str:
    """Every heredoc body in ``cmd``: the tokenizer splits them into segments of their own."""
    return "\n".join(m.group(3) for m in _HEREDOC_RE.finditer(cmd))


_PROBE_READ_CAP = 65536


def _is_probe_script(path: str, repo_root: str, cwd: str) -> bool:
    """A throwaway script outside the repo under a temp or scratch(pad) dir whose
    source imports a repo package or loads a repo ``.py`` by quoted path. Installed
    tools outside the repo are not probes; an unreadable script fails open (not one)."""
    if "$" in path:
        return True
    full = os.path.normpath(os.path.join(cwd, os.path.expanduser(path)))
    root = os.path.normpath(repo_root)
    if full == root or full.startswith(root + os.sep):
        return False
    tmp = os.path.normpath(tempfile.gettempdir())
    parts = full.split(os.sep)
    if not (full.startswith(tmp + os.sep) or "scratchpad" in parts or "scratch" in parts):
        return False
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as fh:
            source = fh.read(_PROBE_READ_CAP)
    except OSError:
        return False
    return _code_imports_repo(source, repo_root)


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
            return _code_verifies(stdin_code, repo_root)
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
            return i + 1 < len(rest) and _code_verifies(rest[i + 1], repo_root)
        if tok in _PY_OPTS_WITH_ARG:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        return tok.endswith(".py") and _is_probe_script(tok, repo_root, cwd) and _script_runs_suite(tok, cwd)
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


# A post-run scoped pytest checks the EM's own residue edit. A run naming more targets
# is a regression sweep over work the run's test stage and review already verified
# (ladder XL: 199 files, an hour), so it counts toward the cap.
_POST_RUN_SCOPED_TARGET_CAP = 10


def _test_target_count(cmd: str) -> int:
    return len(re.findall(r"(?:^|\s)['\"]?[^\s'\"]*(?:\.py|::)[^\s'\"]*", cmd))


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


def _deny_record(event_sentinel: str):
    from coordinator_core.bash_guards._rewrite_support import _deny_record as _build
    from coordinator_core.guard_messages.alternative_record import AdaptableOffer

    return _build(
        GUARD_NAME,
        f"{_DENY_BODY} No self-escape: if an executor BLOCKs, a reason line in {event_sentinel} is honoured once.",
        (
            AdaptableOffer(
                template="dispatch a coordinator:test-runner",
                what_to_adapt="the verification command it runs",
            ),
            AdaptableOffer(
                template="python -m coordinator_core.benchmarks.measure -- <argv>",
                what_to_adapt="<argv>, the command to measure",
            ),
        ),
        em_text=(
            f"{_DENY_BODY} Dispatch a coordinator:test-runner. "
            "To measure, run python -m coordinator_core.benchmarks.measure -- <argv>. "
            f"No self-escape: if an executor BLOCKs, a reason line in {event_sentinel} is honoured once."
        ),
    )


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


def _written_texts(tool_input: object) -> List[str]:
    """The text a Write (``content``), Edit (``new_string``) or MultiEdit (``edits[].new_string``) adds."""
    if not isinstance(tool_input, dict):
        return []
    texts = [tool_input.get("content"), tool_input.get("new_string")]
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        texts.extend(e.get("new_string") for e in edits if isinstance(e, dict))
    return [t for t in texts if isinstance(t, str)]


def written_text_verifies(payload: dict, file_path: str, repo_root: str) -> bool:
    """True iff the text this Write/Edit adds names a test runner or the measure harness and the
    file (the added text plus, for an Edit, the file's existing source) imports repo code.
    A write whose text carries no such call is configuration, not a verification run."""
    tool_input = payload.get("tool_input")
    added = "\n".join(_written_texts(tool_input))
    if _RUNNER_RE.search(added) is None and _MEASURE_RE.search(added) is None:
        return False
    source = added
    if isinstance(tool_input, dict) and "content" not in tool_input:
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
                source += "\n" + fh.read(_PROBE_READ_CAP)
        except OSError:
            pass
    return _code_imports_repo(source, repo_root)


def after_executor_deny(payload: dict, subject: str) -> Optional[dict]:
    """Deny envelope when the EM has an executor behind it, the written text runs a test
    runner or measurement against repo code, and no escape; else None. The Write/Edit
    hook that calls this is not the Bash dispatcher, so the record is rendered here."""
    from coordinator_core.hooks import dispatch_escape as nd

    session_id, repo_root, verdict = _gate(payload, subject)
    if verdict != "deny" or not written_text_verifies(payload, subject, repo_root):
        return None
    if nd._take_escape(payload, session_id, subject, GUARD_NAME):
        return None
    from coordinator_core.guard_messages.alternative_record import Audience
    from coordinator_core.guard_messages.render import render
    from coordinator_core.session.dispatch_nudge_sentinel import sentinel_path

    return render(_deny_record(str(sentinel_path(session_id))), Audience.EM)


_SUITE_MUTEX = "with-suite-mutex"


def _granted_suite_run(cmd: str, session_id: object, cwd: Optional[str]) -> bool:
    """Every pytest segment runs under ``with-suite-mutex`` and the session holds a live Tier-U
    grant: the EM's own broad suite, which test-runner may never run, so denying it leaves no route."""
    if _SUITE_MUTEX not in cmd or not isinstance(session_id, str) or not session_id:
        return False
    segments = _pytest_segments(cmd)
    if not segments or any(_normalize_executable_basename(w[0]) != _SUITE_MUTEX for w in segments):
        return False
    from coordinator_core.session.suite_authority import suite_authority

    return suite_authority(cwd, session_id=session_id).authorized


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


def check(payload: dict):
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

        if session_workflow_run_ids(session_id) and _test_target_count(clean) <= _POST_RUN_SCOPED_TARGET_CAP:
            record_silent(GUARD_NAME, "scoped pytest after a workflow run: exempt")
            return None
    if _bump_counter(repo_root, session_id, newest_arming_epoch(session_id, repo_root) or 0.0) < 1:
        return None
    from coordinator_core.hooks import dispatch_escape as nd

    if nd._take_escape(payload, session_id, cmd[:200], GUARD_NAME):
        return None
    from coordinator_core.session.dispatch_nudge_sentinel import sentinel_path

    return _deny_record(str(sentinel_path(session_id)))
