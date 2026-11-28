"""Clean-overlay verify: run a landing set's scoped tests in ONE persistent clean mirror of the repo, moved to
the measured candidate commit (HEAD plus only the landing set), so a peer's dirty tracked file or uncommitted
module can neither fail nor falsely green the verdict.

The mirror is a `clone --shared` under `temp_layout.coordinator_temp_root(root)`, advanced between verifies by
`diff-tree` plus `update-index --index-info` and `checkout-index` (`read-tree` re-reads every tree object: seconds) so a verify writes only the paths that differ, and serialised on a per-mirror
lock. Runs in the workflow verify stage (`python -m coordinator_core.ops.dispatch_emit.clean_overlay_verify
--run-id <run>`), never inside an engine op: it writes `overlay-verdict.json` beside the run's `measured-rev.json`,
keyed to the landing set's blob shas and the base sha. `dispatch.land_run` only calls `check_verdict`: a file read, plus one
`git diff --name-only` when the base advanced. `--export-run -- <argv>` runs a test command in the mirror at HEAD plus the run's declared paths."""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable, Collection, ContextManager, Iterator, Mapping, Optional, Sequence

from coordinator_core import run_scratch, temp_layout
from coordinator_core.git.run import _git_exe, run_git
from coordinator_core.locked_write import LockTimeout, held_lock
from coordinator_core.ops._path_guard import safe_id
from coordinator_core.ops.dispatch_emit.ask_contract import measured_rev_record_path
from coordinator_core.ops.dispatch_emit.interpreter_pin import REMEDY, SOURCE_KEY, resolve_test_runner_python
from coordinator_core.session.record_homes import home_rel
from coordinator_core.testing import scoped_pytest, spawn_depth
from coordinator_core.win_portability import no_console_creationflags

VERDICT_FILE = "overlay-verdict.json"

MIRROR_DIRNAME = "clean-mirror"
MIRROR_SLOTS = 4
MIRROR_WAIT_S = 90.0
MIRROR_POLL_S = 0.25
SETUP_TIMEOUT_S = 60
TEST_TIMEOUT_S = 120
PYTEST_STALL_S = 120
STALL_RETRY_SCALE_MIN = 2.0
STALL_RETRY_SCALE_MAX = 8.0
# Silence allowed before pytest's first byte: a cold start (imports, conftest, collection) prints nothing, and its
# length tracks interpreter start under peer load, not test progress, so it is never measured against the stall window.
PYTEST_STARTUP_S = 120
IMPORTER_CAP = 25
_NOTE_FAILING_IDS = 8
_PYTEST_FAILED = 1
_PYTEST_INTERRUPTED = 2
_PYTEST_NO_TESTS = 5
_EMPTY_CAUSE = "\n[overlay-verify] collected zero tests serially and unnarrowed with no tier deselection\n"
_OUTPUT_TAIL = 2000
_STATE_FILE = "state.json"
_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_ADD_BATCH = 200


@dataclass(frozen=True)
class OverlayVerdict:
    """`verdict` is "green" (tests ran and passed), "red" (a test failed or a test file failed to collect),
    "pre_existing" (every failing test also fails at the base sha: lands with an advisory),
    "environment_defect" (the pinned interpreter does not resolve, or a dependency the repo does not carry is
    not installed under the interpreter: the tests could not judge the candidate; lands with an advisory) or "inconclusive"
    (no mirror or no test ran: land_run refuses it unless `timed_out`). "missing" (no verdict was produced) is
    built by `check_verdict`: land_run lands it with an advisory. `detail` is the tail of the runner output or
    the reason. `timed_out` is True only on an "inconclusive" whose seeds pytest stalled (no output for
    `PYTEST_STALL_S`); every other inconclusive keeps it False. `failing` is the FAILED/ERROR node ids that made
    a "red" or "pre_existing" verdict (empty on every other verdict). `argv` is the pytest argv (with its
    `--basetemp`) of each run behind the verdict, one shell-joined string per run; `cause` is
    `ZERO_RUN_TEST_INFRA` when every outcome was a setup error, else `""`. `guard_refusal` is the verbatim text
    of the guard that ended or refused the run's pytest command, else `""`; a zero-run halt quotes it."""

    verdict: str
    detail: str = ""
    seeds: tuple = field(default_factory=tuple)
    timed_out: bool = False
    failing: tuple = field(default_factory=tuple)
    argv: tuple = field(default_factory=tuple)
    cause: str = ""
    guard_refusal: str = ""

    @property
    def red(self) -> bool:
        return self.verdict == "red"


class PytestStalled(subprocess.TimeoutExpired):
    """A watched run produced no output for its stall interval; `str(exc)` names the silence and the last
    progress point. A `TimeoutExpired`, so every timeout handler of a bounded run also catches it."""

    def __init__(self, argv, stall_s: float, elapsed_s: float, progress: str):
        super().__init__(argv, stall_s)
        self.progress = progress
        self._text = f"pytest stalled: no output for {stall_s:g}s (after {elapsed_s:.0f}s running); last progress: {progress}"

    def __str__(self) -> str:
        return self._text

    def after(self, first: "PytestStalled") -> "PytestStalled":
        """This stall as the retry of `first`: the text names both attempts."""
        self._text = f"pytest stalled on both attempts: attempt 1: {first}; attempt 2 (retry): {self._text}"
        return self


def stall_retry_window() -> float:
    """The no-output window of the one retry after a stall: `PYTEST_STALL_S` times the 1-minute load per core,
    clamped to `[STALL_RETRY_SCALE_MIN, STALL_RETRY_SCALE_MAX]`. `os.getloadavg` is POSIX-only, so a host
    without it (Windows) gets the minimum scale. Never below `PYTEST_STALL_S`."""
    return PYTEST_STALL_S * _load_scale()


def _load_scale() -> float:
    """The 1-minute load per core clamped to `[STALL_RETRY_SCALE_MIN, STALL_RETRY_SCALE_MAX]`; the minimum
    where `os.getloadavg` is absent (Windows)."""
    try:
        per_core = os.getloadavg()[0] / (os.cpu_count() or 1)
    except (AttributeError, OSError):
        per_core = 0.0
    return min(STALL_RETRY_SCALE_MAX, max(STALL_RETRY_SCALE_MIN, per_core))


_RESULT_LINE_RE = re.compile(r"^[.FEsxX]+(?:\s+\[\s*\d+%\])?$")


def _last_progress(raw: bytes) -> str:
    """Where a pytest `-q` run's output stopped: the count of test results reported (dots, F, E, s, x) and
    the last output line, or "no output yet" when it never printed."""
    lines = [ln.strip() for ln in raw.decode("utf-8", errors="replace").splitlines() if ln.strip()]
    if not lines:
        return "no output yet (still starting or collecting)"
    done = sum(len(ln.split("[")[0].strip()) for ln in lines if _RESULT_LINE_RE.match(ln))
    return f"{done} test result(s) reported, last output line {lines[-1][-200:]!r}"


_now = time.monotonic  # the stall clock's seam: tests swap it to cross a stall window without waiting


def _watched(proc, close, argv: list, stall_s: float) -> subprocess.CompletedProcess:
    """Drain `proc`'s stdout and stderr on reader threads until it exits; any new output, or a change in the
    tree's CPU time (`close.tree_cpu_s`, where readable), resets the stall clock.
    A silence of `stall_s` (`collect_startup_s()` if longer, before the first byte) with an idle tree closes the
    tree and raises `PytestStalled`. Output is read in whatever chunks the pipe
    holds (pytest `-q` prints dots with no newline), so a slow test file that keeps reporting never stalls.
    Collection prints no count to a pipe, so tree CPU is the collect-progress signal: a collecting tree burns
    CPU and keeps extending the window. Where `tree_cpu_s` is absent or returns None the window is wall-clock."""
    import threading

    arrived: list = []
    streams: tuple = ([], [])
    clock = [_now()]
    started = clock[0]
    startup_s = collect_startup_s()

    def drain(index: int, pipe) -> None:
        try:
            while True:
                chunk = pipe.read1(65536) if hasattr(pipe, "read1") else pipe.read(65536)
                if not chunk:
                    return
                streams[index].append(chunk)
                arrived.append(chunk)
                clock[0] = _now()
        except (OSError, ValueError):
            return

    readers = [threading.Thread(target=drain, args=(i, p), daemon=True) for i, p in enumerate((proc.stdout, proc.stderr))]
    for reader in readers:
        reader.start()
    tick = min(0.5, max(stall_s / 4, 0.01))
    cpu_of = getattr(close, "tree_cpu_s", None)
    last_cpu = None
    try:
        while True:
            try:
                proc.wait(timeout=tick)
                break
            except subprocess.TimeoutExpired:
                if cpu_of is not None:
                    try:
                        cpu = cpu_of()
                    except Exception:
                        cpu = None
                    if cpu is not None:
                        if last_cpu is not None and cpu != last_cpu:
                            clock[0] = _now()
                        last_cpu = cpu
                window = stall_s if arrived else max(stall_s, startup_s)
                if _now() - clock[0] >= window:
                    progress = _last_progress(b"".join(arrived))
                    close()
                    proc.wait()
                    raise PytestStalled(argv, window, _now() - started, progress)
    finally:
        close()
        for reader in readers:
            reader.join(timeout=5)
    out, err = (b"".join(s).decode("utf-8", errors="replace") for s in streams)
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


_PLAIN_GIT = frozenset({"ls-tree", "update-index", "diff-tree", "write-tree", "add", "grep"})


def _plain_git_args(args: Sequence[str]) -> bool:
    """`args` (after `git`) run a `_PLAIN_GIT` subcommand, past any leading global option."""
    return next((a for a in args if not a.startswith("-")), "") in _PLAIN_GIT


def _run(
    argv: list, cwd: Path, timeout: Optional[float], env: Optional[dict] = None, stdin_text: Optional[str] = None,
    *, stall_s: Optional[float] = None,
) -> subprocess.CompletedProcess:
    """Run `argv` in a capped job (a killpg session off Windows); the tree is closed on every exit, so a
    stalled or timed-out pytest leaves no grandchild. `stdin_text` (NUL-terminated records, no newline) feeds
    stdin. With `stall_s` the run has no total wall: it ends when it exits, or raises `PytestStalled` after
    `stall_s` seconds of silence (stdin is closed and the child runs unbuffered, because a piped pytest holds its
    dots until exit and would read as silent; `timeout` is ignored). Otherwise raises
    `subprocess.TimeoutExpired` at `timeout`. A short git plumbing call (`_PLAIN_GIT`) goes through `run_git`
    instead: the capped job costs ~230ms of process time per spawn, and git plumbing holds no grandchild."""
    if stall_s is None and env is None and len(argv) > 1 and argv[0] == _git_exe() and _plain_git_args(argv[1:]):
        done = run_git(
            argv[1:], cwd=str(cwd), input=None if stdin_text is None else stdin_text.encode("utf-8", "surrogateescape"),
        )
        if done.timed_out:
            raise subprocess.TimeoutExpired(argv, TEST_TIMEOUT_S if timeout is None else timeout)
        return subprocess.CompletedProcess(argv, done.returncode, done.stdout, done.stderr)
    from coordinator_core.diagnostics.contained_run import spawn_in_capped_job

    if stall_s is not None:
        proc, close = spawn_in_capped_job(
            argv,
            popen_kwargs=dict(
                cwd=str(cwd), env={**(os.environ if env is None else env), "PYTHONUNBUFFERED": "1"}, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                **no_console_creationflags(),
            ),
            background=True,
        )
        return _watched(proc, close, argv, stall_s)
    proc, close = spawn_in_capped_job(
        argv,
        popen_kwargs=dict(
            cwd=str(cwd), env=env, stdin=subprocess.DEVNULL if stdin_text is None else subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, errors="replace", **no_console_creationflags(),
        ),
        background=True,
    )
    try:
        out, err = proc.communicate(input=stdin_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        close()
        proc.communicate()
        raise
    finally:
        close()
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _gate_python(root: Path) -> str:
    """The interpreter the repo's test gate pins (same ladder as `retest_targeted`), via the shared
    `coordinator_core.pyresolve.pytest_python` the review test leg also uses.
    `verify` never reaches the unresolved-pin fallback: it returns an environment defect first."""
    from coordinator_core.pyresolve import pytest_python

    return pytest_python(root)


ENV_DEFECT = "environment_defect"

_MISSING_MODULE_RE = re.compile(r"\b(?:ModuleNotFoundError|ImportError): No module named ['\"]([\w.]+)['\"]")
_WALK_SKIP = frozenset({".git", "__pycache__", "node_modules", ".venv", "venv", "site-packages"})


def _repo_module_names(root: Path) -> frozenset:
    """Every top-level-importable name the repo carries (each directory component and each `.py` stem of
    every tracked or untracked path), from one `git ls-files` listing; one `os.walk` when git fails."""
    names: set = set()
    try:
        out = run_git(
            ["ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=str(root), timeout=30,
        ).stdout
        paths = [p for p in out.split("\0") if p]
    except (OSError, subprocess.SubprocessError):
        paths = []
    if not paths:
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in _WALK_SKIP]
            names.update(dirs)
            names.update(f[:-3] for f in files if f.endswith(".py"))
        return frozenset(names)
    for path in paths:
        parts = path.split("/")
        names.update(parts[:-1])
        if parts[-1].endswith(".py"):
            names.add(parts[-1][:-3])
    return frozenset(names)


_SUMMARY_LINE_RE = re.compile(r"^(?:FAILED|ERROR) .*$", re.MULTILINE)


def _is_non_import_failure(line: str) -> bool:
    """A pytest FAILED/ERROR summary line that is not an import failure; a bare `ERROR <file>` (a collection
    error pytest prints without its message) is not counted, its message sits in the output body."""
    if "ImportError" in line or "No module named" in line:
        return False
    return line.startswith("FAILED") or " - " in line


def _missing_dependencies(output: str, root: Path) -> list:
    """Top-level modules the test output reports as not installed that the repo does not carry, sorted; empty
    when nothing is missing, any missing module belongs to the repo (a red), or any FAILED/ERROR summary
    line is not an import failure (a real red the missing module would otherwise mask)."""
    names = {m.split(".")[0] for m in _MISSING_MODULE_RE.findall(output)}
    if not names:
        return []
    if any(_is_non_import_failure(ln) for ln in _SUMMARY_LINE_RE.findall(output)):
        return []
    carried = _repo_module_names(root)
    return [] if names & carried else sorted(names)


_TEST_OUTCOME_RE = re.compile(
    r"\b\d+ (?:passed|failed|errors?|skipped|deselected|xfailed|xpassed)\b|\bno tests ran\b|\bcollected \d+ items?\b"
)


def guard_exit(returncode: int, output: str) -> bool:
    """True when a non-zero pytest exit never ran a test: no failing node id, no outcome count, no
    collection error and no teardown-only error, i.e. a repo conftest or plugin guard (an interpreter or
    dependency check) ended the process before any test was collected."""
    if returncode == 0 or failing_ids(output) or _TEST_OUTCOME_RE.search(output):
        return False
    return not (collection_error(returncode, output) or teardown_only_error(returncode, output))


def engine_python_command(root: Path) -> str:
    """`_gate_python(root)` as a token safe to paste into a Bash command: an absolute path is posix-slashed
    and double-quoted when it holds whitespace; a pinned bare name or launcher clause is returned as declared.
    Never a bare `python`: an agent's PATH `python` can lack `coordinator_core`."""
    py = _gate_python(root)
    if Path(py).is_absolute():
        py = Path(py).as_posix()
        return f'"{py}"' if any(c.isspace() for c in py) else py
    return py


_PASSED_RE = re.compile(r"\b\d+ passed\b")
_NOT_PASSED_RE = re.compile(r"\b\d+ (?:failed|errors?)\b|^(?:FAILED|ERROR) ", re.MULTILINE)
_TEARDOWN_MARKERS = ("pytest_sessionfinish", "pytest_unconfigure", "cleanup_dead_symlinks", "pytest_terminal_summary")
_PYTEST_INTERNAL_ERROR = 3
_MODULE_MISSING = "ModuleNotFoundError"
_COLLECTION_RE = re.compile(r"\berrors? during collection\b|^ERROR collecting ", re.MULTILINE)


def teardown_only_error(returncode: int, output: str) -> bool:
    """True when a non-zero pytest exit is only a session-teardown error: the run reports passed tests, no
    failed or errored test, and an INTERNALERROR (or a failure exit) whose trace sits in a sessionfinish-time
    hook. A failed test, a collection error or an INTERNALERROR with no teardown frame is not."""
    if returncode not in (_PYTEST_FAILED, _PYTEST_INTERNAL_ERROR) or not _PASSED_RE.search(output):
        return False
    if _NOT_PASSED_RE.search(output) or "INTERNALERROR" not in output:
        return False
    return any(marker in output for marker in _TEARDOWN_MARKERS)


_FORCED_EXIT_RE = re.compile(r"^\[forced-exit\] rc=\d+: (.*)$", re.MULTILINE)
_RESIDUE_CAUSE = "left in session Temp"


def temp_residue_lines(output: str) -> list:
    """The `[forced-exit]` and `managed-temp:` lines of `output` when every forced exit names a session-Temp
    residue, else `[]`."""
    causes = _FORCED_EXIT_RE.findall(output)
    if not causes or any(_RESIDUE_CAUSE not in c for c in causes):
        return []
    return [ln.strip() for ln in output.splitlines() if ln.startswith(("[forced-exit]", "managed-temp:"))]


def temp_residue_only(returncode: int, output: str) -> bool:
    """True when a pytest exit 1 is only a managed-temp residue: the run reports passed tests, no failed or
    errored test, no INTERNALERROR or collection error, and every forced exit is a session-Temp residue."""
    if returncode != _PYTEST_FAILED or not _PASSED_RE.search(output):
        return False
    if _NOT_PASSED_RE.search(output) or "INTERNALERROR" in output or _COLLECTION_RE.search(output):
        return False
    return bool(temp_residue_lines(output))


def hygiene_only_exit(returncode: int, output: str) -> bool:
    """True when a non-zero pytest exit over passed tests is only session teardown or a temp residue."""
    return teardown_only_error(returncode, output) or temp_residue_only(returncode, output)


def collection_error(returncode: int, output: str) -> bool:
    """True when pytest stopped at exit 2 because a test file failed to collect (an import error such as a
    module only a peer's worktree carries): a red of the run's own tree, never an inconclusive run."""
    return returncode == _PYTEST_INTERRUPTED and _COLLECTION_RE.search(output) is not None


_FAILED_ID_RE = re.compile(r"^(?:FAILED|ERROR) (.+?)(?: - |$)", re.MULTILINE)


_NODE_FILE_RE = re.compile(r"^[\w@./+:\\-]+\.\w+$")


def _node_id_shaped(entry: str) -> bool:
    """True when ``entry`` leads with a test file path (``a/test_x.py``, ``a/test_x.py::t[p q]``); a count line
    such as ``all 13 failed + 1 setup error`` has no file part."""
    return _NODE_FILE_RE.match(re.split(r"::| > ", entry, maxsplit=1)[0].strip()) is not None


def failing_ids(output: str) -> tuple:
    """The node ids a pytest short summary reports as FAILED or ERROR, in order, without duplicates; a
    FAILED/ERROR line whose remainder is not node-id-shaped (a summary or count line) yields none."""
    found = (m.strip() for m in _FAILED_ID_RE.findall(output))
    return tuple(dict.fromkeys(i for i in found if _node_id_shaped(i)))


_SETUP_ERROR_RE = re.compile(r"^_+ ERROR (?:at setup of|collecting) (.+?) _+$", re.MULTILINE)
_SECTION_END_RE = re.compile(r"^(?:_+ .* _+|={3,} .* ={3,})$", re.MULTILINE)
_SETUP_HEAD_LINES = 6
_SETUP_CAUSE_LINES = 6


_CONFTEST_LOAD_RE = re.compile(r"^ImportError while loading conftest '.+'\.?$", re.MULTILINE)
_CONFTEST_SPAN_LINES = 30


_RAISED_RE = re.compile(r"^E\s+([A-Za-z_][\w.]*(?:Error|Exception|Exit|Warning))\b")


def _raised_class(span: Sequence[str]) -> str:
    """The exception class of the last `E   <Class>: ...` line of a traceback span, `""` when none: the class
    the traceback raised, whatever header pytest framed it under (`ImportError while importing test module`
    heads an `AttributeError` raised at import time)."""
    found = [m.group(1) for m in map(_RAISED_RE.match, span) if m]
    return found[-1] if found else ""


def _setup_text(parts: Sequence[str], span: Sequence[str]) -> str:
    raised = _raised_class(span)
    lead = f"raised {raised} | " if raised else ""
    return "first setup ERROR: " + (lead + " | ".join(ln.strip() for ln in parts))[:_OUTPUT_TAIL]


def _conftest_load_error(output: str) -> str:
    """The `ImportError while loading conftest '<path>'` header and its first `E` cause lines (pytest exit 4, no
    `ERROR collecting` section), naming the conftest that zeroed the run; `""` when `output` holds none."""
    head = _CONFTEST_LOAD_RE.search(output)
    if head is None:
        return ""
    span = output[head.start():].splitlines()[:_CONFTEST_SPAN_LINES]
    cause = [ln for ln in span[1:] if ln.startswith("E ")][:_SETUP_CAUSE_LINES]
    return _setup_text((span[0], *cause), span)


def first_setup_error(output: str) -> str:
    """The head of the first `ERROR at setup of` or `ERROR collecting` traceback in `output`: its header, its
    first lines and its `E` cause lines, so a verdict detail cut to the output tail still names the failure
    that errored every test behind it; `""` when the output holds none."""
    output = output.replace("\r\n", "\n")
    start = _SETUP_ERROR_RE.search(output)
    if start is None:
        return _conftest_load_error(output)
    end = _SECTION_END_RE.search(output, start.end())
    section = output[start.start():end.start() if end else len(output)].strip().splitlines()
    cause = [ln for ln in section[_SETUP_HEAD_LINES:] if ln.startswith("E ")][:_SETUP_CAUSE_LINES]
    return _setup_text((*section[:_SETUP_HEAD_LINES], *cause), section)


_COLLECTING_RE = re.compile(r"^_+ ERROR collecting (.+?) _+$", re.MULTILINE)


def collection_traceback(output: str) -> str:
    """The first `ERROR collecting` section of `output` whole (header, traceback frames, `E` lines), cut to
    `_OUTPUT_TAIL` keeping its header and its end (the raising frame and the exception line); `""` when the
    output holds no collection error. A further collection error adds a count line."""
    text = output.replace("\r\n", "\n")
    starts = list(_COLLECTING_RE.finditer(text))
    if not starts:
        return ""
    end = _SECTION_END_RE.search(text, starts[0].end())
    section = text[starts[0].start():end.start() if end else len(text)].strip()
    if len(section) > _OUTPUT_TAIL:
        head = section.splitlines()[0]
        section = f"{head}\n...\n{section[-(_OUTPUT_TAIL - len(head) - 5):]}"
    more = len(starts) - 1
    return f"{section}\n(+{more} more collection error{'s' * (more > 1)})" if more else section


def run_owned_collection_path(output: str, owned: Collection[str]) -> str:
    """The first `ERROR collecting <path>` path in `output` that is in the run's write set `owned`; `""` when none."""
    for name in _COLLECTING_RE.findall(output.replace("\r\n", "\n")):
        rel = name.strip().replace("\\", "/")
        if rel in owned:
            return rel
    return ""


def kept_collection_traceback(output: str, tail: str) -> str:
    """`collection_traceback(output)` as a verdict-detail prefix (`"collection traceback:\\n<tb>\\n--\\n"`) when the
    output tail `tail` does not already hold it whole; `""` when there is none or the tail carries it."""
    found = collection_traceback(output)
    if not found or found in tail.replace("\r\n", "\n"):
        return ""
    return f"collection traceback:\n{found}\n--\n"


#: Zero-run cause for a pytest run whose every outcome is a setup error (no test completed): the harness, not a test, failed.
ZERO_RUN_TEST_INFRA = "test_infra_failure"
ZERO_RUN_OWNED_COLLECTION = "run_owned_collection_failure"
#: Re-runs of a 0-test `test_infra_failure` outcome (retest_targeted pass, test_leg_run leg); a second one is reported.
TEST_INFRA_RETRY_LIMIT = 1
#: Stderr prefix of the one typed line `--export-run` prints for a co-tenant refusal; the verify halt routes on it.
CO_TENANT_LINE = "co_tenant_dependency"
_SUMMARY_COUNTS_RE = re.compile(r"^\W*((?:\d+ [a-z]+(?:, )?)+) in \d+(?:\.\d+)?s", re.MULTILINE)
_SETUP_ERROR_LINE_RE = re.compile(r"^ERROR (\S+::.+)$", re.MULTILINE)
_COMPLETED_KINDS = ("passed", "failed", "xfailed", "xpassed")


def infra_setup_error(output: str) -> str:
    """The first setup error's text when `output` is a pytest run that completed no test and reported only
    setup errors (`248 errors in 7.78s`, an `ERROR at setup of` section or an `ERROR <id>::<test>` line, no
    `ERROR collecting`); `""` otherwise. Never empty for a run it types."""
    text = output.replace("\r\n", "\n")
    kinds: dict = {}
    for body in _SUMMARY_COUNTS_RE.findall(text):
        for n, kind in re.findall(r"(\d+) ([a-z]+)", body):
            kinds[kind] = kinds.get(kind, 0) + int(n)
    if not (kinds.get("error", 0) + kinds.get("errors", 0)) or any(kinds.get(k) for k in _COMPLETED_KINDS):
        return ""
    if re.search(r"^_+ ERROR collecting ", text, re.MULTILINE):
        return ""
    found = first_setup_error(text)
    if found:
        return found
    line = _SETUP_ERROR_LINE_RE.search(text)
    return f"first setup ERROR: {line.group(1).strip()}"[:_OUTPUT_TAIL] if line else ""


def completed_a_test(output: str) -> bool:
    """True when pytest's terminal summary in `output` counts a passed, failed, xfailed or xpassed test."""
    text = output.replace("\r\n", "\n")
    return any(
        kind in _COMPLETED_KINDS and int(n)
        for body in _SUMMARY_COUNTS_RE.findall(text)
        for n, kind in re.findall(r"(\d+) ([a-z]+)", body)
    )


_PROCESS_ARGV_CAP = 1500
_UNRUN_FILES_CAP = 12


def process_facts(proc: "subprocess.CompletedProcess", cwd: object, files: Optional[Sequence[str]] = None) -> str:
    """The pytest process behind a zero-test or process-error verdict as one `pytest process:` clause: its exit
    code, interpreter (argv[0]), cwd and argv, plus (when `files` is given) the test files that never ran. A
    halt detail carrying it lets a refire start from the failing command instead of guessing."""
    raw = proc.args if isinstance(proc.args, (list, tuple)) else [proc.args]
    argv = [str(a) for a in raw]
    text = (
        f"pytest process: exit={proc.returncode}; interpreter={argv[0] if argv else ''}; cwd={cwd}; "
        f"argv={shlex.join(argv)[:_PROCESS_ARGV_CAP]}"
    )
    if files is None:
        return text
    named = list(dict.fromkeys(str(f).split("::", 1)[0] for f in files))
    shown = ", ".join(named[:_UNRUN_FILES_CAP]) + (f" (+{len(named) - _UNRUN_FILES_CAP} more)" if len(named) > _UNRUN_FILES_CAP else "")
    return f"{text}; unrun test files: {shown or 'none named'}"


_MISSING_FILE_RE = re.compile(r"^E?\s*(?:\w+\.)*FileNotFoundError:.*?(['\"])(.+?)\1\s*$", re.MULTILINE)


_MISSING_MODULE_RE = re.compile(r"ModuleNotFoundError: No module named ['\"]([\w.]+)['\"]")


def mirror_gap(
    output: str, dest: Path, tracked: Collection[str], root: Optional[Path] = None, owned: Collection[str] = (),
) -> str:
    """The repo-relative tracked paths a `FileNotFoundError` in `output` names that `dest` (the mirror worktree)
    does not hold, as a `mirror gap` text; `""` when no missing path is both tracked and absent from the mirror
    (a file the code itself failed to create is not a gap). `tracked` is the index view already in hand.
    With `root` (the live worktree), a `ModuleNotFoundError` naming a module whose file exists in `root` but
    not in `dest` is a gap too: a collection failure the mirror caused, not a verdict on the run.
    With `owned` (the run's own write set), an `ImportError: cannot import name` naming a module in `owned`
    (the submodule `from pkg import mod` imports, or the module that should define the name) that `root` holds
    and `dest` lacks or holds without the name is a gap: the run's own delta never reached the mirror."""
    base = dest.resolve().as_posix().rstrip("/").lower() + "/"
    gaps: list = []
    text = output.replace("\r\n", "\n")
    for match in _MISSING_FILE_RE.finditer(text):
        raw = match.group(2).replace("\\\\", "/").replace("\\", "/")
        rel = raw[len(base):] if raw.lower().startswith(base) else raw.removeprefix("./")
        if rel in tracked and not (dest / rel).exists() and rel not in gaps:
            gaps.append(rel)
    if root is not None:
        for match in _MISSING_MODULE_RE.finditer(text):
            stem = match.group(1).replace(".", "/")
            for rel in (f"{stem}.py", f"{stem}/__init__.py"):
                if (root / rel).is_file() and not (dest / rel).exists() and rel not in gaps:
                    gaps.append(rel)
    if root is not None and owned:
        for symbol, module in dict.fromkeys(_MISSING_NAME_RE.findall(text)):
            stem = module.replace(".", "/")
            defn = _definition_re(symbol)
            for rel in (f"{stem}/{symbol}.py", f"{stem}/{symbol}/__init__.py", f"{stem}.py", f"{stem}/__init__.py"):
                if rel not in owned or rel in gaps or not (root / rel).is_file():
                    continue
                submodule = rel.rsplit("/", 1)[-1] == f"{symbol}.py" or rel.endswith(f"/{symbol}/__init__.py")
                try:
                    mirrored = (dest / rel).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    mirrored = None
                if mirrored is None or (not submodule and not defn.search(mirrored)):
                    gaps.append(rel)
    return f"mirror gap: path(s) absent from the mirror: {', '.join(gaps)}" if gaps else ""


_MISSING_NAME_RE = re.compile(r"ImportError: cannot import name '([^']+)' from '([\w.]+)'")


def _definition_re(symbol: str) -> "re.Pattern[str]":
    return re.compile(
        rf"^\s*(?:async\s+def|def|class)\s+{re.escape(symbol)}(?!\w)|^{re.escape(symbol)}\s*[:=]"
        rf"|^\s*(?:from\s+\S+\s+)?import\s+[^\n]*\b{re.escape(symbol)}\b",
        re.MULTILINE,
    )


def foreign_import_source(output: str, root: Path, dest: Path, owned: Collection[str]) -> str:
    """The repo-relative path of the module a collection `ImportError: cannot import name` in `output` names,
    when the live worktree `root` defines that name there and the mirror `dest` does not: a peer's uncommitted
    hunk the mirror never carries. `""` when the name is defined in the mirror, defined nowhere, or its module
    is in `owned` (the run's own recorded write set: its own delta is missing, no peer's)."""
    for symbol, module in dict.fromkeys(_MISSING_NAME_RE.findall(output.replace("\r\n", "\n"))):
        stem = module.replace(".", "/")
        defn = _definition_re(symbol)
        for rel in (f"{stem}.py", f"{stem}/__init__.py"):
            if rel in owned:
                continue
            try:
                live = (root / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            try:
                mirrored = (dest / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                mirrored = ""
            if defn.search(live) and not defn.search(mirrored):
                return rel
    return ""


LegHook = Callable[[str], ContextManager]


def _no_leg(_name: str) -> ContextManager:
    return nullcontext()


def _note(extra, **fields) -> None:
    """Hand `fields` to the leg hook when it yielded a dict (a `LegMeter`); a hook yielding None ignores them."""
    if isinstance(extra, dict):
        extra.update(fields)


class ExportError(Exception):
    """The clean mirror could not be prepared; `str(exc)` is the reason, a timeout naming its budget."""


class CoTenantDependency(ExportError):
    """A run file needs `symbol` from `peer`, which exists only in another session's uncommitted edit. Advisory and
    typed: the peer's dirt is never exported, so the run waits for the peer's commit."""

    def __init__(self, importer: str, peer: str, symbol: str, why: str, module: str = "", missing: str = "symbol") -> None:
        self.importer, self.peer, self.symbol = importer, peer, symbol
        self.module, self.missing = module, missing
        needed = symbol if missing == "module" else f"{module}.{symbol}" if module else symbol
        super().__init__(f"co_tenant_dependency: {importer} needs {needed} from {peer} ({why}; uncommitted in a peer session)")


class OwnDeltaMissing(ExportError):
    """The name a run's overlay needs is absent from a path in that run's own recorded write set: the run's own
    delta is gone from the tree, which no peer's commit can supply. Typed and never parked behind a peer."""

    def __init__(self, run_id: str, path: str, name: str) -> None:
        self.run_id, self.path, self.name = run_id, path, name
        super().__init__(f"own_delta_missing: run {run_id} recorded delta to {path} ({name}) is absent from the tree")


class _MirrorStale(Exception):
    """The mirror's recorded state no longer matches its worktree; the caller rebuilds it once."""


_SPARSE_EXCLUDE = tuple(home_rel(k) + "/" for k in ("warp", "plan-blitz", "queue-grind", "cross-repo", "sizings")) + ("archive/",)


def _write_sparse(dest: Path) -> None:
    """Keep the run-artifact trees (about half the repo's files) out of the mirror's worktree: the clone's
    config and `info/sparse-checkout` are written directly, so `read-tree -u` needs no extra spawn or flag.
    Best effort: on any OSError the mirror is a full checkout."""
    try:
        git_dir = dest / ".git"
        (git_dir / "info").mkdir(exist_ok=True)
        (git_dir / "info" / "sparse-checkout").write_text(
            "/*\n" + "".join(f"!/{p}\n" for p in _SPARSE_EXCLUDE), encoding="utf-8", newline="\n",
        )
        with open(git_dir / "config", "a", encoding="utf-8", newline="\n") as fh:
            fh.write("[core]\n\tsparseCheckout = true\n\tsparseCheckoutCone = false\n")
    except OSError:
        pass


def mirror_root(root: Path, slot: int = 0) -> Path:
    """The persistent mirror's holder for `root` and pool `slot`: `<coordinator_temp_root>/clean-mirror/<8-hex
    of the resolved repo path>` (slot 0) or `<that>-<slot>`, outside the repo. The worktree is `<holder>/<repo
    dir name>` (tests derive the repo identity from that basename); `state.json` beside it records the commit
    or tree the worktree reflects. The name matches no `temp_layout` managed-dir shape, so the dead-owner reap
    never touches it."""
    resolved = root.resolve()
    key = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:8]
    return temp_layout.coordinator_temp_root(resolved) / MIRROR_DIRNAME / (key if slot == 0 else f"{key}-{slot}")


def _state_read(mroot: Path) -> Optional[str]:
    try:
        data = json.loads((mroot / _STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    at = data.get("at") if isinstance(data, dict) else None
    return at if isinstance(at, str) and at else None


_KEEP = object()
_CLOSURE_FILE = "closure.json"


def _closure_digest(paths: Collection[str]) -> str:
    return hashlib.sha256("\0".join(sorted(paths)).encode("utf-8")).hexdigest()[:16]


def _state_digest(mroot: Path) -> Optional[str]:
    try:
        data = json.loads((mroot / _STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    digest = data.get("closure_digest") if isinstance(data, dict) else None
    return digest if isinstance(digest, str) and digest else None


def _state_write(mroot: Path, at: Optional[str], closure=_KEEP) -> None:
    """Record what the mirror reflects; `None` marks it mid-mutation, so a crash leaves it to be rebuilt.
    `closure` is the tracked paths a scoped mirror holds (recorded as a digest in state.json beside `at`, the
    list in closure.json); `None` records a full mirror and the default keeps what state.json already says.
    Every verify ends with a write, which keeps the idle-age Temp pruners off a mirror in use."""
    mroot.mkdir(parents=True, exist_ok=True)
    if closure is _KEEP:
        digest = _state_digest(mroot)
    elif closure is None:
        digest = None
    else:
        digest = _closure_digest(closure)
        (mroot / _CLOSURE_FILE).write_text(json.dumps(sorted(closure)), encoding="utf-8", newline="\n")
    state = {"at": at, **({"closure_digest": digest} if digest else {})}
    (mroot / _STATE_FILE).write_text(json.dumps(state), encoding="utf-8", newline="\n")


def _mirror_closure(mroot: Path) -> Optional[frozenset]:
    """The paths a scoped mirror holds, `None` for a full mirror. `_MirrorStale` when the recorded digest
    does not match the closure file: the mirror cannot say what it holds."""
    digest = _state_digest(mroot)
    if digest is None:
        return None
    try:
        items = json.loads((mroot / _CLOSURE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _MirrorStale(f"scoped mirror closure unreadable: {exc}") from exc
    if not isinstance(items, list) or _closure_digest(items) != digest:
        raise _MirrorStale("scoped mirror closure does not match its recorded digest")
    return frozenset(items)


def _drop(mroot: Path) -> None:
    try:
        temp_layout._rmtree_strict(mroot)
    except FileNotFoundError:
        pass
    except OSError:
        shutil.rmtree(mroot, ignore_errors=True)


@contextmanager
def _mirror_lock(root: Path, label: str = "clean-overlay") -> Iterator[Path]:
    """Hold one free slot's lock of the repo's mirror pool for the scope and yield that slot's mirror holder.
    Slots are tried in order, each without waiting, so a landing takes a free warm slot; with every slot held the
    pass repeats every `MIRROR_POLL_S` until `MIRROR_WAIT_S` elapses and takes the first slot to free. Every
    reader of a mirror holds its slot from advance through its last read. A pool still full after the wait is an
    `ExportError`."""
    try:
        holders = [mirror_root(root, slot) for slot in range(MIRROR_SLOTS)]
    except ValueError as exc:
        raise ExportError(f"clean mirror has no home for this repo: {exc}") from exc
    deadline = time.monotonic() + MIRROR_WAIT_S
    while True:
        last: Optional[LockTimeout] = None
        for mroot in holders:
            entered = False
            try:
                with held_lock(mroot, timeout=0, holder_label=label):
                    entered = True
                    yield mroot
                return
            except LockTimeout as exc:
                if entered:
                    raise
                last = exc
        if time.monotonic() >= deadline:
            raise ExportError(
                f"clean mirror busy: all {MIRROR_SLOTS} mirror slots are held after {MIRROR_WAIT_S:g}s: {last}"
            ) from last
        time.sleep(MIRROR_POLL_S)


def reset_mirror(root: Path) -> None:
    """Delete every slot of the repo's mirror pool, each under its own lock, so the next verify is a cold build
    (the measurement harness). A slot held past `export_step_timeout_s()` is an `ExportError`."""
    for slot in range(MIRROR_SLOTS):
        mroot = mirror_root(root, slot)
        if not mroot.exists():
            continue
        wait = export_step_timeout_s()
        try:
            with held_lock(mroot, timeout=wait, holder_label="clean-overlay-reset"):
                _drop(mroot)
        except LockTimeout as exc:
            raise ExportError(
                f"clean mirror busy: slot {slot} was not free within {wait:g}s ({EXPORT_TIMEOUT_ENV} sets it): {exc}"
            ) from exc


def _read_head(root: Path) -> str:
    """`HEAD`'s commit sha from `root/.git/HEAD` plus the branch's loose ref or `packed-refs`; `""` when `.git`
    is not a plain directory or the ref is anywhere else (reftable, symbolic ref chain)."""
    gdir = root / ".git"
    try:
        text = (gdir / "HEAD").read_text(encoding="utf-8").strip() if gdir.is_dir() else ""
        if _FULL_SHA.fullmatch(text):
            return text
        if not text.startswith("ref: refs/"):
            return ""
        ref = text[5:]
        try:
            loose = (gdir / ref).read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            loose = ""
        if _FULL_SHA.fullmatch(loose):
            return loose
        for line in (gdir / "packed-refs").read_text(encoding="utf-8").splitlines():
            sha, _, name = line.partition(" ")
            if name == ref and _FULL_SHA.fullmatch(sha):
                return sha
    except (OSError, ValueError):
        pass
    return ""


def _commit_sha(root: Path, rev: str) -> str:
    """`rev` as a full commit sha: a full sha passes through, `HEAD` is read from a plain `.git` directory
    without a spawn when it names a loose or packed branch, anything else is resolved by `git rev-parse` in
    `root`."""
    if _FULL_SHA.fullmatch(rev):
        return rev
    if rev == "HEAD":
        head = _read_head(root)
        if head:
            return head
    out = run_git(["rev-parse", "--verify", "-q", f"{rev}^{{commit}}"], cwd=str(root))
    sha = out.stdout.strip()
    if out.returncode or not _FULL_SHA.fullmatch(sha):
        raise ExportError(f"{rev!r} does not resolve to a commit in {root.name}")
    return sha


def _written(names: Sequence[str]) -> int:
    """Paths of `names` the mirror's sparse patterns leave on disk."""
    return sum(1 for n in names if n and not n.startswith(_SPARSE_EXCLUDE))


def _build(root: Path, mroot: Path, leg: LegHook, scoped: bool = False) -> Path:
    """A fresh `clone --shared --no-checkout` of `root` as the mirror worktree `<mroot>/<repo name>`; a full
    mirror (`scoped` False) keeps the run-artifact trees out of its worktree."""
    dest = mroot / root.resolve().name
    _drop(mroot)
    mroot.mkdir(parents=True, exist_ok=True)
    with leg("clone"):
        args = ["--no-checkout", "-q", str(root), str(dest)]
        clone = run_git(["clone", "--shared", *args], cwd=str(root))
        if clone.returncode and "unable to normalize alternate object path:" in clone.stderr:
            # A restricted process can read the repo without traversing its ancestors.
            # Copy local objects, including unreferenced measured revisions, into the mirror.
            _drop(dest)
            clone = run_git(["clone", "--local", "--no-hardlinks", *args], cwd=str(root))
    if clone.returncode:
        why = "timed out" if clone.timed_out else clone.stderr.strip()[-_OUTPUT_TAIL:]
        raise ExportError(f"overlay clone failed: {why}")
    if not scoped:
        _write_sparse(dest)
    return dest


_LS_TREE_ARGV_CHARS = 16000


def _ls_tree(dest: Path, sha: str, paths: Sequence[str]) -> list:
    """`(path, mode, oid)` of each of `paths` that is a blob under `sha`'s tree; gitlinks are left out. One
    `ls-tree -r -z` per `_LS_TREE_ARGV_CHARS` of pathspec: git descends only the trees the paths name, where a
    whole-tree listing walks and prints every tracked path. `_MirrorStale` when git fails."""
    batches: list = [[]]
    size = 0
    for p in paths:
        if size + len(p) + 1 > _LS_TREE_ARGV_CHARS and batches[-1]:
            batches.append([])
            size = 0
        batches[-1].append(p)
        size += len(p) + 1
    found: list = []
    for batch in batches:
        if not batch:
            continue
        out = _run([_git_exe(), "ls-tree", "-r", "-z", sha, "--", *batch], dest, export_step_timeout_s(len(batch)))
        if out.returncode:
            raise _MirrorStale(f"ls-tree {sha[:12]} failed: {out.stderr.strip()[-_OUTPUT_TAIL:]}")
        for rec in out.stdout.split("\0"):
            head, tab, path = rec.partition("\t")
            fields = head.split()
            if tab and len(fields) == 3 and fields[1] == "blob":
                found.append((path, fields[0], fields[2]))
    return found


def _raw_checkout_env() -> dict:
    """The environment a mirror checkout runs in: `core.autocrlf=false`, so a HEAD-seeded path lands as its raw
    blob bytes (LF), never through the box's checkout smudge, matching the override and overlay writes."""
    raw = os.environ.get("GIT_CONFIG_COUNT", "0") or "0"
    n = int(raw) if raw.isdigit() else 0
    return {**os.environ, "GIT_CONFIG_COUNT": str(n + 1), f"GIT_CONFIG_KEY_{n}": "core.autocrlf", f"GIT_CONFIG_VALUE_{n}": "false"}


def _stage_and_write(dest: Path, put: Sequence[tuple]) -> None:
    """Index `put` (`(path, mode, oid)`) by `update-index --index-info` and write them by `checkout-index`:
    two spawns, whatever the count. A directory or file in a path's way is removed first."""
    if not put:
        return
    recs = "".join(f"{mode} {oid}\t{p}\0" for p, mode, oid in put)
    budget = export_step_timeout_s(len(put))
    _must(_run([_git_exe(), "update-index", "-z", "--add", "--index-info"], dest, budget, stdin_text=recs))
    names = [p for p, _, _ in put]
    for p in names:
        target = dest / p
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        for anc in target.parents:
            if anc == dest:
                break
            if anc.is_file():
                anc.unlink()
    _must(_run([_git_exe(), "checkout-index", "-f", "-u", "-z", "--stdin"], dest, budget, env=_raw_checkout_env(), stdin_text="".join(f"{p}\0" for p in names)))


def _fill(dest: Path, mroot: Path, sha: str, closure: Optional[Collection[str]] = None) -> int:
    """First checkout of a fresh mirror. A `closure` fills index-free: one `ls-tree -r -z` of `sha`, the entries
    the closure names, then `_stage_and_write` (no tree-walk write of the repo). `None` is the full fill:
    `read-tree -m -u <sha>` into the empty index. Returns the file count.
    Bulk transfer, not local plumbing: it runs under the overlay's pytest bound, not run_git's 2s budget."""
    _state_write(mroot, None)
    if closure is not None:
        put = _ls_tree(dest, sha, sorted(closure))
        _stage_and_write(dest, put)
        _state_write(mroot, sha, frozenset(closure))
        return len(put)
    out = _run([_git_exe(), "read-tree", "-m", "-u", sha], dest, export_step_timeout_s(), env=_raw_checkout_env())
    if out.returncode:
        raise ExportError(f"overlay checkout failed: {out.stderr.strip()[-_OUTPUT_TAIL:]}")
    listing = _run([_git_exe(), "ls-tree", "-r", "--name-only", "-z", sha], dest, export_step_timeout_s())
    _state_write(mroot, sha, None)
    return _written(listing.stdout.split("\0")) if not listing.returncode else 0


def _extend(dest: Path, mroot: Path, sha: str, have: Optional[frozenset], want: Optional[Collection[str]]) -> int:
    """Materialise the paths of `want` a scoped mirror (`have`) does not hold, from `sha`'s tree, and record the
    wider closure. A full mirror (`have` None) already holds everything. Returns the files written."""
    if have is None or want is None:
        return 0
    missing = sorted(set(want) - have)
    if not missing:
        return 0
    listing = _ls_tree(dest, sha, missing)
    at = _state_read(mroot)
    _state_write(mroot, None)
    _stage_and_write(dest, listing)
    _state_write(mroot, at, have | frozenset(want))
    return len(listing)


_PLAIN_MODES = ("100644", "100755")


def _apply_diff(dest: Path, raw: str, closure: Optional[Collection[str]] = None) -> Optional[list]:
    """Apply `diff-tree -r --raw -z --no-renames` output to the mirror's index and worktree without walking the
    tree (`read-tree -m` re-reads every tree object of the repo: seconds, however few paths differ). With a
    `closure` (a scoped mirror) only its members are applied: the diff of a recorded overlay tree lists every
    out-of-closure path as added, and none of them is ever written. Returns the changed names, or `None` when
    an entry is not a plain file (symlink, submodule, type change), which the caller moves by `read-tree`
    instead."""
    toks = raw.split("\0")
    gone: list = []
    put: list = []
    i = 0
    while i + 1 < len(toks) and toks[i].startswith(":"):
        meta = toks[i][1:].split()
        status, path = meta[4][0], toks[i + 1]
        i += 2
        if closure is not None and path not in closure:
            continue
        new_mode, new_oid = meta[1], meta[3]
        if status == "D":
            if meta[0] not in _PLAIN_MODES:
                return None
            gone.append(path)
        elif status in "AM" and new_mode in _PLAIN_MODES and (status == "A" or meta[0] in _PLAIN_MODES):
            put.append((path, new_mode, new_oid))
        else:
            return None
    if gone:
        _must(_run([_git_exe(), "update-index", "-z", "--force-remove", "--stdin"], dest, export_step_timeout_s(len(gone)), stdin_text="".join(f"{p}\0" for p in gone)))
        for p in gone:
            try:
                (dest / p).unlink()
            except OSError:
                pass
    if put:
        recs = "".join(f"{mode} {oid}\t{p}\0" for p, mode, oid in put)
        _must(_run([_git_exe(), "update-index", "-z", "--add", "--index-info"], dest, export_step_timeout_s(len(put)), stdin_text=recs))
        hidden = [] if closure is not None else [p for p, _, _ in put if p.startswith(_SPARSE_EXCLUDE)]
        shown = [p for p, _, _ in put if p not in hidden]
        if hidden:
            _must(_run([_git_exe(), "update-index", "-z", "--skip-worktree", "--stdin"], dest, export_step_timeout_s(len(hidden)), stdin_text="".join(f"{p}\0" for p in hidden)))
        for p in shown:
            target = dest / p
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            for anc in target.parents:
                if anc == dest:
                    break
                if anc.is_file():
                    anc.unlink()
        if shown:
            _must(_run([_git_exe(), "checkout-index", "-f", "-u", "-z", "--stdin"], dest, export_step_timeout_s(len(shown)), env=_raw_checkout_env(), stdin_text="".join(f"{p}\0" for p in shown)))
    return [*gone, *(p for p, _, _ in put)]


def _must(out: subprocess.CompletedProcess) -> None:
    if out.returncode:
        raise _MirrorStale(f"{' '.join(out.args[1:3])} failed: {out.stderr.strip()[-_OUTPUT_TAIL:]}")


def _advance(dest: Path, mroot: Path, sha: str, closure: Optional[Collection[str]] = None) -> int:
    """Move the mirror from its recorded state to `sha`'s tree: one `diff-tree` names the paths that differ
    (none: no write at all), and `_apply_diff` rewrites only those (a scoped mirror's `closure` members only);
    a non-plain-file change falls back to `read-tree -u -m <old> <sha>` on a full mirror and is `_MirrorStale`
    on a scoped one (a whole-tree read would write outside the closure). Returns the path count. Raises
    `_MirrorStale` when the mirror cannot be advanced (it is left marked for rebuild)."""
    old = _state_read(mroot)
    if old is None:
        raise _MirrorStale("mirror state unknown")
    diff = _run([_git_exe(), "diff-tree", "-r", "--raw", "-z", "--no-renames", old, sha], dest, export_step_timeout_s())
    if diff.returncode:
        raise _MirrorStale(f"diff-tree {old[:12]}..{sha[:12]} failed: {diff.stderr.strip()[-_OUTPUT_TAIL:]}")
    if not diff.stdout.strip("\0"):
        return 0
    _state_write(mroot, None)
    names = _apply_diff(dest, diff.stdout, closure)
    if names is None:
        if closure is not None:
            raise _MirrorStale(f"non-plain-file change {old[:12]}..{sha[:12]} in a scoped mirror")
        moved = _run([_git_exe(), "read-tree", "-m", "-u", old, sha], dest, export_step_timeout_s(), env=_raw_checkout_env())
        if moved.returncode:
            raise _MirrorStale(f"read-tree {old[:12]}..{sha[:12]} failed: {moved.stderr.strip()[-_OUTPUT_TAIL:]}")
        names = [t for t in diff.stdout.split("\0") if t and not t.startswith(":")]
    _state_write(mroot, sha)
    return len(names) if closure is not None else _written(names)


def _declared(paths: Sequence[str]) -> tuple:
    """The declared repo-relative paths, normalised to posix and deduplicated; `ExportError` on one that is
    absolute, drive-qualified, climbs out of the repo or enters `.git`."""
    out: dict = {}
    for rel in paths:
        parts = PurePosixPath(rel.replace("\\", "/")).parts
        if (
            not rel or not parts or rel.startswith(("/", "\\")) or Path(rel).is_absolute() or PureWindowsPath(rel).drive
            or ".." in parts or parts[0].lower() == ".git"
        ):
            raise ExportError(f"declared path {rel!r} is not a plain repo-relative path")
        out[PurePosixPath(*parts).as_posix()] = None
    return tuple(out)


def _expand_dirs(root: Path, rels: Sequence[str]) -> tuple:
    """`rels` with each directory entry replaced by the files under it: every worktree file (untracked
    included; `.git` and `__pycache__` skipped), plus the tracked files the index holds under it that the worktree
    lacks (so a deleted directory or a deleted file in one exports as deletions). A file entry passes through."""
    from coordinator_core.ops.dispatch_emit import test_reach

    out: dict = {}
    tracked_all = None
    for rel in rels:
        src = root / rel
        if src.is_file():
            out[rel] = None
            continue
        if src.is_dir():
            for base, dirs, files in os.walk(src):
                dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
                for name in files:
                    out[(Path(base) / name).relative_to(root).as_posix()] = None
        else:
            out[rel] = None
        if tracked_all is None:
            tracked_all = test_reach._tracked(root)
        prefix = f"{rel}/"
        for tracked in tracked_all:
            if tracked.startswith(prefix):
                out[tracked] = None
    return tuple(out)


def _overlay(
    root: Path, dest: Path, mroot: Path, sha: str, rels: Sequence[str],
    closure: Optional[Collection[str]] = None, overrides: Optional[Mapping[str, bytes]] = None,
) -> tuple:
    """Put the worktree bytes of each declared path of `root` into the mirror (a path in `overrides` takes those
    bytes instead: HEAD plus the run's own hunks), staging only the paths whose bytes
    differ (a path absent from the worktree is a deletion); nothing else of the worktree is read. On a full
    mirror, paths under its sparse-excluded trees are skipped. Returns `(paths written, the mirror's new tree
    sha)`; no write leaves the state at `sha`. A scoped mirror's recorded tree holds only its closure, which
    `_advance` filters by. Raises `_MirrorStale` when git cannot record the overlay."""
    todo: list = []
    for rel in rels:
        if closure is None and rel.startswith(_SPARSE_EXCLUDE):
            continue
        src, out = root / rel, dest / rel
        if overrides and rel in overrides:
            data = overrides[rel]
            if not (out.is_file() and out.read_bytes() == data):
                todo.append((rel, data))
        elif src.is_file():
            data = src.read_bytes()
            if not (out.is_file() and out.read_bytes() == data):
                todo.append((rel, data))
        elif not src.exists() and out.is_file():
            todo.append((rel, None))
    if not todo:
        return 0, sha
    _reject_removed_imports(root, dest, sha, dict(todo), closure)
    _state_write(mroot, None)
    for rel, data in todo:
        out = dest / rel
        if data is None:
            out.unlink()
        else:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
    names = [rel for rel, _ in todo]
    for i in range(0, len(names), _ADD_BATCH):
        batch = names[i:i + _ADD_BATCH]
        added = _run([_git_exe(), "--literal-pathspecs", "add", "-f", "-A", "--", *batch], dest, export_step_timeout_s(len(batch)))
        if added.returncode:
            raise _MirrorStale(f"staging the overlay failed: {added.stderr.strip()[-_OUTPUT_TAIL:]}")
    tree = _run([_git_exe(), "write-tree"], dest, export_step_timeout_s(len(names)))
    at = tree.stdout.strip()
    if tree.returncode or not _FULL_SHA.fullmatch(at):
        raise _MirrorStale(f"recording the overlay tree failed: {tree.stderr.strip()[-_OUTPUT_TAIL:]}")
    _state_write(mroot, at)
    return len(names), at


def _defined_names(data: bytes) -> set[str]:
    import ast

    try:
        tree = ast.parse(data)
    except SyntaxError:
        return set()
    names: set[str] = set()

    def bound(target) -> None:
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                bound(element)
        elif isinstance(target, ast.Starred):
            bound(target.value)

    def walk(body) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                    bound(target)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, (ast.If, ast.With, ast.AsyncWith)):
                walk(node.body)
                walk(getattr(node, "orelse", ()))
            elif isinstance(node, ast.Try):
                for block in (node.body, node.orelse, node.finalbody, *(h.body for h in node.handlers)):
                    walk(block)

    walk(tree.body)
    return names


def _reject_removed_imports(
    root: Path, dest: Path, sha: str, writes: dict[str, bytes | None],
    closure: Optional[Collection[str]] = None,
) -> None:
    """Check surviving importers when an overlay removes a declared Python definition."""
    from coordinator_core.ops.dispatch_emit import test_reach

    removed = {}
    for rel, data in writes.items():
        before = dest / rel
        if not rel.endswith(".py") or not before.is_file():
            continue
        missing = _defined_names(before.read_bytes()) - (_defined_names(data) if data is not None else set())
        if missing:
            removed[rel] = missing
    if not removed:
        return

    names = sorted(set().union(*removed.values()))
    candidates = set(writes)
    if closure is None:
        patterns = [arg for name in names for arg in ("-e", name)]
        probe = run_git(
            ["grep", "--threads=1", "-l", "-z", "-F", *patterns, sha, "--", "*.py"],
            cwd=str(root), binary=True,
        )
        if probe.returncode not in (0, 1):
            raise ExportError("could not verify importers of Python definitions removed by clean export")
        candidates.update(
            raw.decode("utf-8", "surrogateescape").partition(":")[2]
            for raw in probe.stdout_bytes.split(b"\0") if raw
        )
    else:
        candidates.update(closure)
    encoded = tuple(name.encode("utf-8") for name in names)
    modules = test_reach._module_map(removed.keys())
    for importer in sorted(candidates):
        if not importer.endswith(".py"):
            continue
        path = dest / importer
        if importer in writes:
            data = writes[importer]
        else:
            data = path.read_bytes() if path.is_file() else None
        if data is None or not any(name in data for name in encoded):
            continue
        current = test_reach.module_name_for_file(importer)
        for record in test_reach._scan_imports(data.decode("utf-8", errors="replace")):
            module = record.module
            if record.level:
                module = test_reach.resolve_relative_module(
                    module, record.level, current, path.name == "__init__.py",
                )
            if module in modules:
                target = modules[module]
                missing = sorted(removed[target].intersection(record.names))
                if missing:
                    raise ExportError(f"clean export would split dirty imports: {importer} needs {target}: {', '.join(missing)}")


def _prepare(
    root: Path, mroot: Path, sha: str, leg: LegHook, overlay: Sequence[str] = (), closure: Optional[Collection[str]] = None,
    overrides: Optional[Mapping[str, bytes]] = None,
) -> Path:
    """The mirror worktree at commit `sha` plus the `overlay` paths' worktree bytes (`overrides` bytes where given), under the held lock. A first
    use clones (leg "clone") and fills; later uses advance by `_advance` (leg "checkout", which also covers the
    overlay and reports `files_written`). A `closure` scopes the mirror to those tracked paths: a cold fill and a
    warm advance write only them, a warm mirror missing some of them gains them (`_extend`), and a full mirror
    already holds them. `closure` None wants a full mirror, so a scoped one is rebuilt. A mirror that cannot
    advance is rebuilt once; a failure on the fresh build is an `ExportError`."""
    for rebuilt in (False, True):
        cold = rebuilt or _state_read(mroot) is None or not (mroot / root.resolve().name / ".git").exists()
        have: Optional[frozenset] = None
        if not cold:
            try:
                have = _mirror_closure(mroot)
            except _MirrorStale:
                cold = True
            else:
                cold = closure is None and have is not None
        dest = _build(root, mroot, leg, scoped=closure is not None) if cold else mroot / root.resolve().name
        try:
            with leg("checkout") as extra:
                if cold:
                    written = _fill(dest, mroot, sha, closure)
                else:
                    written = _advance(dest, mroot, sha, have) + _extend(dest, mroot, sha, have, closure)
                at = sha
                if overlay:
                    wrote, at = _overlay(root, dest, mroot, sha, overlay, closure, overrides)
                    written += wrote
                _note(extra, files_written=written)
        except _MirrorStale as exc:
            if cold:
                raise ExportError(f"clean mirror unusable after a rebuild: {exc}") from exc
            _drop(mroot)
            continue
        _state_write(mroot, at)
        return dest
    raise AssertionError("unreachable: the second pass is cold and either returns or raises")


def _prepare_guarded(
    root: Path, mroot: Path, sha: str, leg: LegHook, overlay: Sequence[str] = (), closure: Optional[Collection[str]] = None,
    overrides: Optional[Mapping[str, bytes]] = None, *, run_id: str = "",
) -> Path:
    """`_prepare` with a timeout or OS failure named as an `ExportError`."""
    try:
        _check_overlay_imports(root, overlay, **({"run_id": run_id} if run_id else {}))
        return _prepare(root, mroot, sha, leg, overlay, closure, overrides)
    except subprocess.TimeoutExpired as exc:
        raise ExportError(
            f"clean export timed out after {_timeout_text(exc)} ({EXPORT_TIMEOUT_ENV} sets the step budget): {exc}"
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise ExportError(f"clean export did not complete: {type(exc).__name__}: {exc}") from exc


def _names_a_path(root: Path, token: str) -> bool:
    """`token` can name a test file or directory: a separator, a `.py` suffix, a node id, or an existing path.
    A flag or a bare word (`python`, `pytest`) cannot, and `test_closure` scans the whole index for each token
    it cannot place."""
    return not token.startswith("-") and (
        "/" in token or "\\" in token or token.endswith(".py") or "::" in token or os.path.lexists(root / token)
    )


def _closure_for(root: Path, declared: Sequence[str], argv: Sequence[str]) -> Optional[frozenset]:
    """The tracked paths the test command `argv` needs (`export_closure.test_closure`), or `None` for a full
    fill: no `argv`, no test seed in it or the declared paths, or a closure that cannot be computed. A full fill
    says why on stderr; a closure failure never refuses the export."""
    if not argv:
        return None
    from coordinator_core.ops.dispatch_emit.export_closure import test_closure

    try:
        found = test_closure(root, declared, [t for t in argv if _names_a_path(root, t)])
    except Exception as exc:  # the closure only narrows the fill; any failure to compute it is a full fill
        print(f"[clean-export] closure not computed ({type(exc).__name__}: {exc}); full fill", file=sys.stderr)
        return None
    if found.paths is None:
        print(f"[clean-export] {found.note}", file=sys.stderr)
        return None
    return found.paths | _seed_data(root, found.seeds)


def _seed_data(root: Path, seeds: Sequence[str]) -> frozenset:
    """The tracked non-Python files under each seed's directory: a test reads its data files by path, which no
    import closure names. A seed at the repo root brings only the root's own files."""
    from coordinator_core.ops.dispatch_emit import test_reach

    dirs = {PurePosixPath(s).parent.as_posix() for s in seeds}
    prefixes = tuple(f"{d}/" for d in dirs if d != ".")
    at_root = "." in dirs
    return frozenset(
        p for p in test_reach._tracked(root)
        if not p.endswith(".py") and (p.startswith(prefixes) or (at_root and "/" not in p))
    )


@contextmanager
def clean_export(
    root: Path, run_id: str, paths: Sequence[str], leg: LegHook = _no_leg, argv: Sequence[str] = (),
    overrides: Optional[Mapping[str, bytes]] = None,
) -> Iterator[Path]:
    """Yield the mirror worktree at HEAD plus the worktree bytes of the declared `paths` only (`overrides` bytes
    for a path shared with a peer), never any other
    worktree dirt: a peer's dirty file exports at its HEAD bytes. A dirty Python overlay needing a name absent from that HEAD copy raises `CoTenantDependency`. With the test command
    `argv` the worktree holds only that command's test closure (see `_closure_for`); without it, every tracked
    path. The mirror lock is held until the `with` exits; the mirror persists. Raises `ExportError` (a timeout
    names its budget)."""
    rels = _expand_dirs(root, _declared(paths))
    closure = _closure_for(root, rels, argv)
    with _mirror_lock(root, f"clean-export {run_id}") as mroot:
        yield _prepare_guarded(root, mroot, _commit_sha(root, "HEAD"), leg, rels, closure, overrides, run_id=run_id)


def _head_sources(root: Path, rels: Collection[str]) -> dict[str, Optional[bytes]]:
    """HEAD blob bytes per path, `None` for a path HEAD does not hold as a regular file. Reads loose and packed
    objects in-process (`head_blobs` + `read_object`), so it adds no git spawn per file."""
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_objects import read_object
    from coordinator_core.git.git_state import head_blobs

    heads = head_blobs(root, sorted(rels))
    common = resolve_git_common_dir(root)
    out: dict[str, Optional[bytes]] = {}
    for rel in rels:
        entry = heads.get(rel)
        obj = read_object(common, entry[1]) if entry and entry[0] in (0o100644, 0o100755) else None
        out[rel] = obj[1] if obj is not None and obj[0] == "blob" else None
    return out


def _own_write_set(root: Path, run_id: str) -> frozenset:
    """The run's whole recorded write set, peer-held paths included (`run_write_set_full`, not the split
    forms, which drop a shared path). Empty when `run_id` is unset or the record is unreadable."""
    if not run_id:
        return frozenset()
    from coordinator_core.ops.dispatch_emit.run_write_set import run_write_set_full

    try:
        return frozenset(run_write_set_full(root, run_id, "")[0])
    except Exception:  # an unknown write set keeps the peer diagnosis
        return frozenset()


def _printed_writes(root: Path, run_id: str) -> list:
    """The paths the run's agents wrote, from their per-write blob prints (`run_write_prints`): fix-leg and
    undeclared writes, untracked non-test modules included, which no manifest or snapshot names. Scratch paths
    drop; a deleted path (print None) stays, so its deletion reaches the mirror. [] with no record."""
    if not run_id:
        return []
    from coordinator_core.ops.dispatch_emit import run_write_prints

    return [p for p in (run_write_prints.read(root, run_id) or {}) if p and not p.startswith("scratch/")]


_OVERLAID_NOTE_CAP = 8


def own_untracked_writes(root: Path, run_id: str, sha: str) -> list:
    """The run's own write-set paths (`_own_write_set`, `_printed_writes`) that are files in the worktree,
    absent from HEAD (untracked or staged-new) and from `sha`'s tree: new modules and tests the candidate commit
    does not carry, which reach the mirror only as overlay. A path HEAD holds is never named (a peer's dirty hunks stay at HEAD),
    nor a peer's untracked file the run did not write. [] with no run id or no readable write set."""
    try:
        owned = dict.fromkeys([*_own_write_set(root, run_id), *_printed_writes(root, run_id)])
    except (OSError, ValueError):  # an unreadable write record overlays nothing; any other error is a bug and surfaces
        return []
    wanted = []
    for rel in owned:
        parts = PurePosixPath(rel).parts
        if rel.startswith(_SPARSE_EXCLUDE) or ".." in parts or rel.startswith("/") or PureWindowsPath(rel).drive:
            continue
        if (root / rel).is_file():
            wanted.append(rel)
    if wanted:
        from coordinator_core.git.git_state import head_blobs

        # invariant: HEAD, not the index, decides "tracked": a staged-new file is in the index yet absent from
        # HEAD and from `sha`, so the index view dropped the run's own new test and the mirror never held it
        at_head = head_blobs(root, wanted)
        wanted = [rel for rel in wanted if rel not in at_head]
    if not wanted:
        return []
    try:
        carried = {p for p, _, _ in _ls_tree(root, sha, wanted)}
    except (_MirrorStale, OSError, subprocess.SubprocessError):
        carried = set()
    return [rel for rel in wanted if rel not in carried]


def _overlaid_note(paths: Sequence[str]) -> str:
    """`overlaid own path(s): ...` for a verdict note; `""` with no paths."""
    if not paths:
        return ""
    shown = ", ".join(paths[:_OVERLAID_NOTE_CAP])
    more = f" (+{len(paths) - _OVERLAID_NOTE_CAP} more)" if len(paths) > _OVERLAID_NOTE_CAP else ""
    return f"overlaid own path(s): {shown}{more}"


def _check_overlay_imports(root: Path, overlay: Sequence[str], *, run_id: str = "") -> None:
    """The export holds a peer's dirty module at its HEAD bytes, so a declared dirty Python file is refused only
    when it needs a name (or a whole module) that HEAD's copy of that peer module lacks: the run would test
    against code the landing commit never contains. Peer dirt the run does not reach never refuses.
    Raises `CoTenantDependency`, or `OwnDeltaMissing` when the needed target is in `run_id`'s own write set."""
    if not any(rel.endswith(".py") for rel in overlay):
        return
    from coordinator_core.ops.dispatch_emit import test_reach
    from coordinator_core.ops.dispatch_emit.worktree_snapshot import changed_python_paths

    changed = changed_python_paths(root)
    if changed is None:
        print("[clean-export] advisory: worktree status unreadable; dirty-import check skipped (the run's own tests surface a missing name)", file=sys.stderr)
        return
    declared = set(overlay)
    outside = changed - declared
    if not outside:
        return
    modules = test_reach._module_map(outside)
    needs: list[tuple[str, str, str, tuple]] = []
    for rel in sorted(changed & declared):
        for base, names in test_reach._resolved_imports(rel, test_reach._records_for(root, rel)):
            if "*" in names:
                continue
            if (target := modules.get(base)) is not None:
                needs.append((rel, target, base, tuple(names)))
            for name in names:
                if (sub := modules.get(f"{base}.{name}")) is not None:
                    needs.append((rel, sub, f"{base}.{name}", ()))
    if not needs:
        return
    heads = _head_sources(root, {target for _, target, _, _ in needs})
    own = _own_write_set(root, run_id)
    defined: dict[str, set[str]] = {}
    for rel, target, module, names in needs:
        data = heads[target]
        if data is None:
            if target in own:
                raise OwnDeltaMissing(run_id, target, module)
            raise CoTenantDependency(rel, target, module, "not in HEAD", module=module, missing="module")
        have = defined.setdefault(target, _defined_names(data))
        for name in names:
            if name not in have and modules.get(f"{module}.{name}") is None and not _clean_submodule(root, module, name, changed):
                if target in own:
                    raise OwnDeltaMissing(run_id, target, name)
                raise CoTenantDependency(rel, target, name, "absent at HEAD", module=module, missing="symbol")


def _clean_submodule(root: Path, package: str, name: str, changed: Collection[str]) -> bool:
    """`from package import name` where `name` is a submodule on disk with no dirt: the export holds it at its
    HEAD bytes, so the dirty package `__init__` never had to define it.

    Trap: without this, any dirt in a package `__init__` refused every importer of a sibling submodule
    (bf2f0d f4e08e: `from coordinator_core.ops import method_step` read as a name ops/__init__.py lacks)."""
    stem = package.replace(".", "/") + "/" + name
    return any(
        rel not in changed and (root / rel).is_file() for rel in (f"{stem}.py", f"{stem}/__init__.py")
    )


def _mirror_env(env: dict, dest: Path) -> dict:
    """`env` with `coordinator_core` resolved from the mirror alone: PYTHONPATH is the mirror root (the caller's
    repo is not on it), and no bytecode is written, so a same-size same-second edit never runs a stale module.

    Trap: `COORDINATOR_ENGINE_ROOT` is the mirror too. A `coordinator/bin` script a test imports front-inserts
    the env-named engine root onto `sys.path`, ahead of PYTHONPATH, so an inherited root (the editable-install
    checkout) rebinds `coordinator_core` away from the mirror (f68bc7: `_launcher_head` AttributeError)."""
    from coordinator_core import pyresolve

    return {
        **env, "PYTHONPATH": str(dest), "PYTHONDONTWRITEBYTECODE": "1",
        pyresolve.ENGINE_ROOT_ENV: str(dest),
    }


def _pytest_env(tmp: Path, dest: Path, selection: Sequence[str]) -> dict:
    """The mirror env of a pytest child: depth-stamped and per-run tmp. `OSError` naming the refusal when the
    nesting depth or a selection that collects the calling test forbids the spawn."""
    refusal = spawn_depth.refusal_for_spawn(os.environ) or spawn_depth.selection_includes_spawner(
        selection, os.environ, str(dest)
    )
    if refusal:
        raise OSError(refusal)
    return _mirror_env(spawn_depth.child_env(scoped_pytest.tmp_env(os.environ, tmp)), dest)


EXPORT_RUN_STALL_ENV = "COORDINATOR_EXPORT_RUN_STALL_S"


def export_run_stall_s() -> float:
    """The export run's no-output window: `COORDINATOR_EXPORT_RUN_STALL_S` when set to a positive number, else
    `PYTEST_STALL_S`. An unparsable or non-positive value falls back to the default."""
    raw = os.environ.get(EXPORT_RUN_STALL_ENV, "").strip()
    try:
        value = float(raw) if raw else 0.0
    except ValueError:
        value = 0.0
    return value if value > 0 else float(PYTEST_STALL_S)


EXPORT_TIMEOUT_ENV = "COORDINATOR_EXPORT_TIMEOUT_S"
COLLECT_STARTUP_ENV = "COORDINATOR_COLLECT_STARTUP_S"
# Seconds of git work per exported path under load. Sized from the 1416-file export, whose git steps
# (ls-tree, update-index, checkout-index, add) ran ~60-80s of process time together on a loaded box: 0.05s * 1416 = 71s.
EXPORT_STEP_S_PER_PATH = 0.05


def _positive_env_float(name: str) -> float:
    """`name` parsed as a positive float; 0.0 when unset, unparsable or non-positive."""
    raw = os.environ.get(name, "").strip()
    try:
        value = float(raw) if raw else 0.0
    except ValueError:
        return 0.0
    return value if 0 < value < float("inf") else 0.0


def export_step_timeout_s(n_paths: int = 0) -> float:
    """How long one export git step may take for `n_paths`: `COORDINATOR_EXPORT_TIMEOUT_S` when positive, else
    `TEST_TIMEOUT_S` times the load scale plus `EXPORT_STEP_S_PER_PATH` per path. The path term carries the
    budget where the load term is inert (Windows). Never below `TEST_TIMEOUT_S` except by the env override."""
    override = _positive_env_float(EXPORT_TIMEOUT_ENV)
    if override:
        return override
    return max(float(TEST_TIMEOUT_S), TEST_TIMEOUT_S * _load_scale() + EXPORT_STEP_S_PER_PATH * max(0, n_paths))


def _timeout_text(exc: subprocess.TimeoutExpired) -> str:
    """The budget `exc` expired at, as `<seconds>s`."""
    return f"{float(exc.timeout):g}s" if isinstance(exc.timeout, (int, float)) else "its budget"


def collect_startup_s() -> float:
    """The silence allowed before pytest's first byte: `COORDINATOR_COLLECT_STARTUP_S` when positive, else
    `PYTEST_STARTUP_S`."""
    return _positive_env_float(COLLECT_STARTUP_ENV) or float(PYTEST_STARTUP_S)


def argv_named_untracked(root: Path, argv: Sequence[str], owned: Collection[str] = ()) -> list:
    """Repo-relative `.py` files `argv` names (node ids cut at `::`) that are files in the worktree and tracked
    nowhere: a test the run wrote that no declared or own-written set carries reaches the mirror only through
    the command that runs it. A tracked file is never named here (a peer's dirty tracked hunks stay at HEAD),
    nor a path outside `root` or under the sparse-excluded trees. In process: one index read, no spawn."""
    from coordinator_core.ops.dispatch_emit import test_reach
    from coordinator_core.ops.dispatch_emit.export_closure import _norm

    tracked = None
    out: dict = {}
    for token in argv:
        if not token or token.startswith("-"):
            continue
        rel = _norm(token.split("::", 1)[0], root)
        if not rel.endswith(".py") or rel.startswith(_SPARSE_EXCLUDE) or ".." in PurePosixPath(rel).parts:
            continue
        try:
            if PureWindowsPath(rel).drive or rel.startswith("/") or not (root / rel).is_file():
                continue
        except (OSError, ValueError):
            continue
        if tracked is None:
            tracked = test_reach._tracked(root)
        if rel not in tracked:
            out[rel] = None
    if out and owned and tracked is not None:
        for rel in _untracked_imports(root, list(out), tracked, owned):
            out[rel] = None
    return list(out)


_UNTRACKED_IMPORT_CAP = 200


def _untracked_imports(root: Path, seeds: Sequence[str], tracked: Collection[str], owned: Collection[str]) -> list:
    """Repo `.py` files tracked nowhere that `seeds` import, transitively (each import resolved against the
    directories above its importer; capped at `_UNTRACKED_IMPORT_CAP`), limited to the `owned` paths: a module
    the run wrote beside its new test travels with the test, while a peer's untracked module stays out of the
    mirror. A tracked, unowned or unreadable import is skipped."""
    from coordinator_core.pyimports import collect_imports

    found: dict = {}
    todo = list(seeds)
    seen = set(todo)
    while todo and len(found) < _UNTRACKED_IMPORT_CAP:
        cur = todo.pop()
        try:
            tree = ast.parse((root / cur).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
            continue
        records, _ = collect_imports(tree)
        parts = cur.split("/")[:-1]
        for rec in records:
            if rec.level:
                if rec.level - 1 > len(parts):
                    continue
                anchors = ["/".join(parts[: len(parts) - (rec.level - 1)])]
            else:
                anchors = ["/".join(parts[:i]) for i in range(len(parts), -1, -1)]
            base = rec.module or ""
            mods = [base, *(f"{base}.{n}" if base else n for n in rec.names if n != "*")]
            for anchor in anchors:
                for mod in filter(None, mods):
                    segs = mod.split(".")
                    for i in range(1, len(segs) + 1):
                        stem = "/".join(([anchor] if anchor else []) + segs[:i])
                        for cand in (f"{stem}.py", f"{stem}/__init__.py"):
                            if cand in seen or cand in tracked or cand not in owned or cand.startswith(_SPARSE_EXCLUDE):
                                continue
                            if (root / cand).is_file():
                                seen.add(cand)
                                found[cand] = None
                                todo.append(cand)
    return list(found)


def run_in_export(
    root: Path, run_id: str, paths: Sequence[str], argv: Sequence[str], overrides: Optional[Mapping[str, bytes]] = None,
) -> subprocess.CompletedProcess:
    """Run `argv` in the clean export of `paths` plus the untracked test files `argv` names (cwd and PYTHONPATH
    the mirror) under the mirror lock; the lock is held across the command. `ExportError` when the export fails;
    `PytestStalled` (a `TimeoutExpired`) after `export_run_stall_s()` of silence.

    Trap: no total wall. A flat cap halted every run whose test leg is legitimately longer (example-game-repo 2da5dc needs
    ~660s); the stall watchdog fails only a command that stops making progress."""
    owned = frozenset([*_own_write_set(root, run_id), *_printed_writes(root, run_id)])
    paths = list(dict.fromkeys([*paths, *argv_named_untracked(root, argv, owned)]))
    with clean_export(root, run_id, paths, argv=argv, overrides=overrides) as dest:
        return _run(list(argv), dest, None, env=_mirror_env(dict(os.environ), dest), stall_s=export_run_stall_s())


def _is_test_path(rel: str) -> bool:
    name = PurePosixPath(rel).name
    return name.startswith("test_") or name.endswith("_test.py")


def _with_importers(dest: Path, seeds: tuple, touched: Sequence[str], sha: Optional[str] = None) -> tuple:
    """`seeds` plus the tracked test files that mention a touched module's name (one `git grep -l` in the
    mirror: over `sha`'s tree when given, so a scoped mirror's worktree never limits the scan, else over the
    worktree), capped at `IMPORTER_CAP`; returns `(set, note)`. A failed scan runs the seeds alone."""
    stems = sorted(
        {
            PurePosixPath(p.replace("\\", "/")).stem for p in touched
            if p.endswith(".py") and not _is_test_path(p.replace("\\", "/"))
        } - {"__init__", "__main__", "conftest"}
    )
    if not stems:
        return seeds, ""
    argv = [
        _git_exe(), "grep", "-l", "-z", "-w", "-F", *(a for s in stems for a in ("-e", s)),
        *([sha] if sha else []), "--", ":(glob)**/test_*.py", ":(glob)**/*_test.py",
    ]
    found = _run(argv, dest, export_step_timeout_s())
    if found.returncode not in (0, 1):
        return seeds, "importer scan failed: ran the touched tests only"
    prefix = f"{sha}:" if sha else ""
    hits = (n[len(prefix):] if prefix and n.startswith(prefix) else n for n in found.stdout.split("\0") if n)
    extra = sorted(n for n in hits if n not in seeds)
    kept = extra[:IMPORTER_CAP]
    note = f"importer cap {IMPORTER_CAP}: {len(extra) - len(kept)} importing test file(s) not run" if len(extra) > len(kept) else ""
    return (*seeds, *kept), note


def _widen(root: Path, dest: Path, mroot: Path, sha: str, picks: Sequence[str]) -> None:
    """Add each picked importer test's own closure to a scoped mirror (`_extend`, at `sha`); a full mirror
    already holds it. `ExportError` when the mirror cannot be widened."""
    try:
        have = _mirror_closure(mroot)
        if have is None:
            return
        wanted: set = set()
        for pick in picks:
            found = _closure_for(root, (), [pick])
            wanted |= found if found is not None else {pick}
        _extend(dest, mroot, sha, have, wanted)
    except _MirrorStale as exc:
        raise ExportError(f"clean mirror could not take the importer tests: {exc}") from exc


def _base_failing(
    dest: Path, mroot: Path, root: Path, run_id: str, base_sha: str, ids: Sequence[str], leg: LegHook = _no_leg,
) -> frozenset:
    """The subset of `ids` that also fail with `base_sha`'s tree in the mirror. Any setup problem or timeout
    yields the empty set: an unmeasured base never excuses a red. The whole re-run is one `leg("base_rerun")`."""
    with leg("base_rerun") as extra:
        return _base_failing_body(dest, mroot, root, run_id, base_sha, ids, extra)


RUN_HOLDER_KIND = "overlay-run"


def _claim_run_dirs(root: Path, slug: str) -> tuple:
    """(holder, basetemp, tmp), created: the holder is a run_scratch scratch claim of this process."""
    repo = re.sub(r"[^A-Za-z0-9_.-]", "-", Path(root).resolve().name) or "repo"
    holder = run_scratch.claim(repo, RUN_HOLDER_KIND, re.sub(r"[^A-Za-z0-9_.-]", "-", slug) or "run")
    basetemp, tmp = holder / "bt", holder / "tmp"
    basetemp.mkdir()
    tmp.mkdir()
    return holder, basetemp, tmp


def _base_failing_body(
    dest: Path, mroot: Path, root: Path, run_id: str, base_sha: str, ids: Sequence[str], extra,
) -> frozenset:
    run_holder = None
    try:
        try:
            _note(extra, files_written=_advance(dest, mroot, base_sha, _mirror_closure(mroot)))
        except _MirrorStale:
            return frozenset()
        run_holder, basetemp, tmp = _claim_run_dirs(root, f"{run_id}-base")
        ran = _run(
            _pytest_argv(root, dest, basetemp, ids), dest, None, stall_s=PYTEST_STALL_S,
            env=_pytest_env(tmp, dest, ids),
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    finally:
        if run_holder is not None:
            run_scratch.finish(run_holder)
    return frozenset(failing_ids(ran.stdout + ran.stderr)) & frozenset(ids)


def _run_pytest_retrying(argv: list, dest: Path, env: dict) -> subprocess.CompletedProcess:
    """Run a watched pytest; a stall is inconclusive, not a verdict, so it is re-run exactly once under
    `stall_retry_window()`. A second stall raises `PytestStalled` naming both attempts."""
    try:
        return _run(argv, dest, None, env=env, stall_s=PYTEST_STALL_S)
    except PytestStalled as first:
        try:
            return _run(argv, dest, None, env=env, stall_s=stall_retry_window())
        except PytestStalled as second:
            raise second.after(first) from None


TIER_EXCLUDED = ("cadence", "complete", "pending_fix", "designed_red")
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
_DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)


def tier_marker_expr() -> str:
    """The `-m` expression of a land's verify: the tiers a land never gates on (`TIER_EXCLUDED`) are deselected."""
    return " and ".join(f"not {m}" for m in TIER_EXCLUDED)


def _xdist_known_present(py: str) -> bool:
    """True only when `py` is this engine's own interpreter (or its console sibling) and xdist is importable here;
    a pinned gate interpreter's xdist is not probed, and `-n` to one without it is a pytest usage error (exit 4)."""
    from coordinator_core.pyresolve import _console_sibling

    return py in {sys.executable, _console_sibling(sys.executable)} and importlib.util.find_spec("xdist") is not None


def _pytest_argv(root: Path, dest: Path, basetemp: Path, targets: Sequence[str], marker: Optional[str] = None) -> list:
    """`--rootdir` is the symlink-resolved mirror: pytest roots node ids at the realpath of each test file, so a
    rootdir behind a symlink (macOS /var -> /private/var) loses the prefix match and every id becomes a file-less
    `../../..::name`. Every flag precedes `--rootdir`; the targets follow `--basetemp`. `marker` replaces the tier
    `-m` expression (the empty string selects every marker). Never a worker count above 0: when xdist is importable
    `-n 0` overrides a consumer addopts `-n`; `-p no:xdist` would make that addopts an unrecognized argument."""
    py = _gate_python(root)
    from coordinator_core.bash_guards._test_run_admission import scoped_leg_argv

    return scoped_leg_argv(py, [
        "-q", "-p", "no:cacheprovider", "-m", tier_marker_expr() if marker is None else marker,
        *(["-n", "0"] if _xdist_known_present(py) else []), "--rootdir", str(dest.resolve()),
        "--basetemp", str(basetemp), *targets,
    ])


def _test_spans(source: str) -> Optional[list]:
    """`(node id suffix, first line, last line)` of each module-level `test_*` function and each `test_*` method
    of a module-level `Test*` class (decorators included); None when `source` does not parse."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    def span(node) -> tuple:
        return min([node.lineno, *(d.lineno for d in node.decorator_list)]), node.end_lineno

    spans: list = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            spans.append((node.name, *span(node)))
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            spans.extend(
                (f"{node.name}::{sub.name}", *span(sub)) for sub in node.body
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name.startswith("test")
            )
    return spans


def _changed_test_ids(rel: str, source: str, hunks: Sequence[tuple]) -> Optional[list]:
    """The node ids of `rel`'s tests whose lines `hunks` (new-side `(start, count)`) changed. None, meaning run
    the whole file, when a hunk is a pure deletion or changes a code line outside every test (a fixture, helper,
    import or class body), or when the source does not parse. Blank and comment lines outside a test are inert."""
    spans = _test_spans(source)
    if spans is None or not hunks:
        return None
    lines = source.splitlines()
    chosen: dict = {}
    for start, count in hunks:
        if count == 0:
            return None
        for ln in range(start, start + count):
            hit = next((s for s in spans if s[1] <= ln <= s[2]), None)
            if hit is not None:
                chosen[hit[0]] = None
            elif ln <= len(lines) and lines[ln - 1].strip() and not lines[ln - 1].lstrip().startswith("#"):
                return None
    return [f"{rel}::{name}" for name, _a, _b in spans if name in chosen]


def narrow_touched_tests(root: Path, dest: Path, base: Optional[str], post: str, targets: Sequence[str], touched: Sequence[str]) -> tuple:
    """`targets` with each touched test file replaced by the node ids of the tests the `base`..`post` diff changed
    (one `git diff -U0` for all of them). A file whose diff is unknown or reaches code outside a test, and every
    target not touched, stays whole; an unavailable base or failed diff narrows nothing."""
    rels = {t.replace("\\", "/") for t in touched}
    files = [t for t in dict.fromkeys(targets) if t in rels and _is_test_path(t) and t.endswith(".py")]
    if not files or not base or base == post:
        return tuple(targets)
    out = _run([_git_exe(), "diff", "-U0", "--no-color", "--no-ext-diff", base, post, "--", *files], root, export_step_timeout_s(len(files)))
    if out.returncode != 0:
        return tuple(targets)
    per_file: dict = {}
    for chunk in re.split(r"(?m)^diff --git ", out.stdout)[1:]:
        name = _DIFF_FILE_RE.search(chunk)
        if name:
            per_file[name.group(1)] = [(int(a), int(b) if b else 1) for a, b in _HUNK_RE.findall(chunk)]
    narrowed: dict = {}
    for rel in files:
        try:
            source = (dest / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        ids = _changed_test_ids(rel, source, per_file.get(rel, ()))
        if ids:
            narrowed[rel] = ids
    return tuple(dict.fromkeys(i for t in targets for i in narrowed.get(t, (t,))))


def tier_deselected_only(proc: subprocess.CompletedProcess) -> bool:
    """True when pytest exited 5 because the tier markers deselected every test it collected."""
    return proc.returncode == 5 and "deselected" in proc.stdout + proc.stderr


def _rerun_tier_deselected(
    root: Path, dest: Path, basetemp: Path, targets: Sequence[str], env: dict, proc: subprocess.CompletedProcess,
    marker: Optional[str] = None,
) -> tuple:
    """`(proc, argv)`: `proc` unchanged (argv None) unless every collected test was marker-deselected, else the
    same `targets` re-run once with `marker` (the empty string, selecting every marker, when None) in place of
    the tier expression, the re-run's own outcome prefixed with the tier note."""
    if not tier_deselected_only(proc):
        return proc, None
    argv = _pytest_argv(root, dest, basetemp, targets, "" if marker is None else marker)
    again = _run_pytest_retrying(argv, dest, env)
    note = f"every collected test is in an excluded tier ({', '.join(TIER_EXCLUDED)}); re-ran with -m {marker or 'none'!r}:\n"
    return subprocess.CompletedProcess(again.args, again.returncode, note + again.stdout, again.stderr), argv


def _tier_empty_as_green(proc: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
    """`proc` as a clean exit when `tier_deselected_only`: a land touching only slow-tier tests has nothing a
    land verify may gate on, and its detail says so."""
    if not tier_deselected_only(proc):
        return proc
    return subprocess.CompletedProcess(
        proc.args, 0, f"every collected test is in an excluded tier ({', '.join(TIER_EXCLUDED)}): {proc.stdout.strip()[-200:]}\n", proc.stderr,
    )


def _settle_empty_collection(
    root: Path, dest: Path, basetemp: Path, run_seeds: Sequence[str], seeds: Sequence[str], env: dict, proc: subprocess.CompletedProcess,
) -> subprocess.CompletedProcess:
    """`proc` unchanged unless it is an exit 5 whose output names no deselection: the run is
    repeated over the whole `seeds` instead of the narrowed `run_seeds`. An exit 5 that survives returns with
    `_EMPTY_CAUSE` appended to its output."""
    if proc.returncode != _PYTEST_NO_TESTS or "deselected" in proc.stdout + proc.stderr:
        return proc
    if tuple(run_seeds) != tuple(seeds):
        proc = _run_pytest_retrying(_pytest_argv(root, dest, basetemp, seeds), dest, env)
        if proc.returncode != _PYTEST_NO_TESTS or "deselected" in proc.stdout + proc.stderr:
            return proc
    return subprocess.CompletedProcess(proc.args, proc.returncode, proc.stdout + _EMPTY_CAUSE, proc.stderr)


def _real_tree_verdict(
    root: Path, run_id: str, seeds: Sequence[str], marker: Optional[str], source: str, launched: Sequence[str],
) -> Optional[OverlayVerdict]:
    """The verdict of `seeds` run in the live worktree `root`, for an overlay that ran zero tests because it
    could not import `source`, a peer's uncommitted module (never carried into the mirror or the commit). Reds
    are classified by `retest_targeted.classify_real_tree_reds`: green when none, "red" naming the run's own
    reds, "pre_existing" (the landing's non-blocking token) when every red is attributed to a peer: a foreign
    verdict by attribution, not measured at base, and its detail says so. `None` when the real tree ran no
    test either (a genuine infra failure keeps the overlay's own verdict) or could not be launched."""
    from coordinator_core.ops.dispatch_emit import retest_targeted

    run_holder, basetemp, tmp = _claim_run_dirs(root, run_id)
    try:
        argv = _pytest_argv(root, root, basetemp, seeds, marker)
        ran = _run_pytest_retrying(argv, root, _pytest_env(tmp, root, seeds))
        text = ran.stdout + ran.stderr
        if not completed_a_test(text):
            return None
        log = tmp / "real-tree.log"
        log.write_text(text, encoding="utf-8", errors="replace", newline="\n")
        own, foreign = retest_targeted.classify_real_tree_reds(
            log, root, sorted({*_own_write_set(root, run_id), *_printed_writes(root, run_id)}),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        run_scratch.finish(run_holder)
    argv_all = (*launched, shlex.join(argv))
    lead = f"real-tree run: the overlay could not import {source} (a peer's uncommitted hunk)"
    tail = text.strip()[-_OUTPUT_TAIL:]
    if ran.returncode == 0 or hygiene_only_exit(ran.returncode, text):
        return OverlayVerdict("green", f"{lead}; every selected test passed in the real tree: {tail}", tuple(seeds), argv=argv_all)
    if own or not foreign:
        return OverlayVerdict(
            "red", f"{lead}; own red(s): {', '.join(own) or 'pytest exit ' + str(ran.returncode)}; foreign red(s): "
            f"{', '.join(foreign) or 'none'}: {tail}", tuple(seeds), failing=tuple(own), argv=argv_all,
        )
    return OverlayVerdict(
        "pre_existing", f"{lead}; foreign: every red is attributed to a peer's dirt, none names the run's writes, "
        f"and none was measured at base: {', '.join(foreign)}: {tail}", tuple(seeds),
        failing=tuple(foreign), argv=argv_all,
    )


def verify(
    root: Path, run_id: str, candidate_sha: str, seeds: Sequence[str], base_sha: Optional[str] = None,
    leg: LegHook = _no_leg, touched: Sequence[str] = (), marker: Optional[str] = None,
) -> OverlayVerdict:
    """Advance `root`'s persistent mirror to `candidate_sha`, run pytest over `seeds` there with `coordinator_core`
    resolved from the mirror (pytest runs the scoped files in sequence in one process, no workers, no grant), then a second pytest over the test files that mention a `touched` module (the
    importer shard), and hold the mirror lock across pytest and the base re-run. Each pytest run has no total
    wall: it continues while its output advances and ends after `PYTEST_STALL_S` of silence, then is re-run
    once under `stall_retry_window()`. An importer shard that stalls twice or does not complete is a note on the
    seeds' verdict; a seeds that stall twice are "inconclusive" with `timed_out` and a detail naming both
    attempts and the last progress point. A red whose
    every failing node id (both shards) also fails at `base_sha` (re-run in the same mirror, those ids only) is
    "pre_existing": the run did not cause it. A test file that fails to collect is red. Never raises: a setup
    failure, a busy mirror, a timeout or a run that collected nothing is "inconclusive". `leg(name)` is a
    context-manager factory entered around the "clone" (cold mirror only), "checkout", "pytest" and
    "base_rerun" legs and may yield a dict to receive `files_written` (see `overlay_verify_measure`); the
    default is a no-op."""
    seeds = tuple(seeds)
    if not seeds:
        return OverlayVerdict("inconclusive", "the landing set has no test seeds", seeds)
    pin = resolve_test_runner_python(root)
    if pin.command and pin.defect is not None:
        return OverlayVerdict(
            ENV_DEFECT, f"interpreter defect: the repo pins `{SOURCE_KEY}: {pin.command}` (from {pin.source}) but {pin.defect}", seeds,
        )
    run_dirs: list = []
    launched: list = []
    in_seeds = False
    foreign_src = ""
    try:
        with _mirror_lock(root, f"clean-overlay {run_id}") as mroot:
            sha = _commit_sha(root, candidate_sha)
            own_new = own_untracked_writes(root, run_id, sha) if run_id else []
            dest = _prepare_guarded(
                root, mroot, sha, leg, own_new, _closure_for(root, [*touched, *own_new], seeds), run_id=run_id,
            )
            picked, scan_note = _with_importers(dest, seeds, touched, sha)
            extras = picked[len(seeds):]
            if extras:
                _widen(root, dest, mroot, sha, extras)
            run_seeds = narrow_touched_tests(root, dest, base_sha, sha, seeds, touched)
            run_holder, basetemp, tmp = _claim_run_dirs(root, run_id)
            run_dirs.append(run_holder)
            env = _pytest_env(tmp, dest, [*run_seeds, *extras])
            shard_note = ""
            shard: Optional[subprocess.CompletedProcess] = None
            with leg("pytest"):
                in_seeds = True
                seeds_argv = _pytest_argv(root, dest, basetemp, run_seeds)
                launched.append(shlex.join(seeds_argv))
                ran = _run_pytest_retrying(seeds_argv, dest, env)
                ran = _settle_empty_collection(root, dest, basetemp, run_seeds, seeds, env, ran)
                in_seeds = False
                ran, again = _rerun_tier_deselected(root, dest, basetemp, run_seeds, env, ran, marker)
                if again:
                    launched.append(shlex.join(again))
                if extras:
                    shard_note = f"importer shard timed out: {len(extras)} file(s) unverified"
                    try:
                        shard_argv = _pytest_argv(root, dest, basetemp, extras)
                        launched.append(shlex.join(shard_argv))
                        shard, again = _rerun_tier_deselected(
                            root, dest, basetemp, extras, env, _run_pytest_retrying(shard_argv, dest, env), marker,
                        )
                        if again:
                            launched.append(shlex.join(again))
                        shard_note = ""
                    except PytestStalled as exc:
                        shard_note = f"importer shard stalled: {len(extras)} file(s) unverified ({exc})"
                    except subprocess.TimeoutExpired:
                        pass
                    except (OSError, subprocess.SubprocessError) as exc:
                        shard_note = f"importer shard did not complete ({type(exc).__name__}): {len(extras)} file(s) unverified"
            runs = [ran] if shard is None else [ran, shard]
            full = "".join(r.stdout + r.stderr for r in runs)
            tail = full.strip()[-_OUTPUT_TAIL:]
            if shard is not None and shard.returncode not in (0, _PYTEST_FAILED) and not (
                collection_error(shard.returncode, shard.stdout + shard.stderr)
                or hygiene_only_exit(shard.returncode, shard.stdout + shard.stderr)
            ):
                shard_note = f"importer shard did not complete (pytest exit {shard.returncode}): {len(extras)} file(s) unverified"
                runs = [ran]
                full = ran.stdout + ran.stderr
                tail = full.strip()[-_OUTPUT_TAIL:]
            notes = "; ".join(n for n in (_overlaid_note(own_new), scan_note, shard_note) if n)
            bad = [r for r in runs if r.returncode != 0 and not hygiene_only_exit(r.returncode, r.stdout + r.stderr)]
            if pin.source != SOURCE_KEY and any(_MODULE_MISSING in r.stdout + r.stderr for r in bad):
                notes = "; ".join(n for n in (f"ModuleNotFoundError under an undeclared interpreter: {REMEDY}", notes) if n)
            ids = tuple(dict.fromkeys(i for r in runs for i in failing_ids(r.stdout + r.stderr)))
            if notes:
                tail = f"{notes}: {tail}"
                if ids:
                    tail = f"failing: {', '.join(ids[:_NOTE_FAILING_IDS])}; {tail}"
            at_base: frozenset = frozenset()
            kept = kept_collection_traceback("".join(r.stdout + r.stderr for r in bad), tail)
            collected = any(collection_error(r.returncode, r.stdout + r.stderr) for r in bad)
            failed = collected or any(r.returncode == _PYTEST_FAILED for r in bad)
            owned = frozenset([*_own_write_set(root, run_id), *_printed_writes(root, run_id)]) if bad else frozenset()
            if bad and not completed_a_test(full):
                foreign_src = foreign_import_source(full, root, dest, owned)
            if ran.returncode == _PYTEST_NO_TESTS:
                return OverlayVerdict(
                    ENV_DEFECT,
                    f"engine fault: pytest exit 5 over {len(seeds)} non-empty seed(s) ({', '.join(seeds)}); "
                    f"{_EMPTY_CAUSE.strip()}; {process_facts(ran, dest, seeds)}: {tail}",
                    seeds, argv=tuple(launched),
                )
            if bad:
                absent = _missing_dependencies("".join(r.stdout + r.stderr for r in bad), root)
                if absent:
                    return OverlayVerdict(
                        ENV_DEFECT,
                        f"interpreter defect: {pin.command or 'the undeclared gate interpreter'} lacks module(s) {', '.join(absent)}"
                        f"{' (the repo declares no resolvable pin)' if not pin.command else ''}: {kept}{tail}",
                        seeds, argv=tuple(launched),
                    )
                if pin.source != SOURCE_KEY and any(guard_exit(r.returncode, r.stdout + r.stderr) for r in bad):
                    return OverlayVerdict(
                        ENV_DEFECT,
                        f"verify could not run: a repo guard ended pytest (exit {bad[0].returncode}) before any test ran under "
                        f"{pin.command or 'the undeclared gate interpreter'}; {REMEDY}; {process_facts(bad[0], dest, seeds)}: {kept}{tail}",
                        seeds, argv=tuple(launched),
                        guard_refusal="\n".join(s for s in (bad[0].stdout.strip(), bad[0].stderr.strip()) if s)[:1500],
                    )
            infra = infra_setup_error(full) if failed else ""
            if failed and not infra:
                from coordinator_core.ops.dispatch_emit import test_reach

                infra = mirror_gap(full, dest, test_reach._tracked(root), root, owned)
            if failed and ids and not infra and not foreign_src and isinstance(base_sha, str) and base_sha and base_sha != sha:
                at_base = _base_failing(dest, mroot, root, run_id, base_sha, ids, leg)
    except CoTenantDependency as exc:
        from coordinator_core.ops.dispatch_emit import co_tenant_owner

        resolved = co_tenant_owner.resolve(root, run_id, exc)
        return OverlayVerdict(
            "inconclusive", str(exc), seeds, argv=tuple(launched), cause=resolved.get("parked") or resolved.get("cause") or "",
        )
    except OwnDeltaMissing as exc:
        return OverlayVerdict("inconclusive", str(exc), seeds, argv=tuple(launched), cause="own_delta_missing")
    except ExportError as exc:
        return OverlayVerdict("inconclusive", str(exc), seeds, argv=tuple(launched))
    except subprocess.TimeoutExpired as exc:
        why = str(exc) if isinstance(exc, PytestStalled) else f"overlay verify timed out after {_timeout_text(exc)}: {type(exc).__name__}: {exc}"
        return OverlayVerdict("inconclusive", why, seeds, timed_out=in_seeds, argv=tuple(launched))
    except (OSError, subprocess.SubprocessError) as exc:
        return OverlayVerdict("inconclusive", f"overlay verify did not complete: {type(exc).__name__}: {exc}", seeds, argv=tuple(launched))
    finally:
        for run_dir in run_dirs:
            run_scratch.finish(run_dir)
    argv = tuple(launched)
    if foreign_src:
        fallback = _real_tree_verdict(root, run_id, seeds, marker, foreign_src, argv)
        if fallback is not None:
            return fallback
    if not bad:
        if any(r.returncode for r in runs):
            codes = ", ".join(str(r.returncode) for r in runs if r.returncode)
            residue = [ln for r in runs for ln in temp_residue_lines(r.stdout + r.stderr)]
            if residue:
                return OverlayVerdict(
                    "green", f"managed-temp residue after every test passed (exit {codes}): {'; '.join(residue)}: {tail}", seeds,
                    argv=argv,
                )
            return OverlayVerdict("green", f"pytest teardown error after every test passed (exit {codes}): {tail}", seeds, argv=argv)
        return OverlayVerdict("green", tail, seeds, argv=argv)
    if failed:
        if ids and at_base == frozenset(ids):
            return OverlayVerdict(
                "pre_existing", f"every failing test also fails at base {str(base_sha)[:12]}: {', '.join(ids)}: {tail}", seeds,
                failing=ids, argv=argv,
            )
        setup = infra or first_setup_error(full)
        body = f"test collection failed: {tail}" if collected else tail
        zero = not completed_a_test(full)
        facts = process_facts(bad[0], dest, seeds) if zero else ""
        own_path = ""
        if collected and zero and not infra:
            from coordinator_core.git.git_state import head_blobs

            created = [p for p in owned if p not in head_blobs(root, sorted(owned))]
            own_path = run_owned_collection_path(full, created)
        lead = f"run-owned collection failure: {own_path}" if own_path else ""
        return OverlayVerdict(
            "red", "; ".join(p for p in (lead, setup, facts, f"{kept}{body}") if p), seeds, failing=ids, argv=argv,
            cause=ZERO_RUN_TEST_INFRA if infra else ZERO_RUN_OWNED_COLLECTION if own_path else "",
        )
    if not completed_a_test(full):
        return OverlayVerdict(
            "red",
            "; ".join(p for p in ("pytest process error: no test ran", first_setup_error(full), process_facts(bad[0], dest, seeds), f"{kept}{tail}") if p),
            seeds, argv=argv, cause=ZERO_RUN_TEST_INFRA,
        )
    return OverlayVerdict(
        "inconclusive", f"pytest exit {bad[0].returncode}: {process_facts(bad[0], dest)}: {tail}", seeds, argv=argv,
    )


def overlay_candidate(measured: dict) -> Optional[str]:
    """The commit the overlay checks out: `overlay_sha` (declared paths carrying a peer's hunks split down to
    the run's own) when `measured_rev` recorded one, else the whole-file candidate `sha`."""
    sha = measured.get("overlay_sha") or measured.get("sha")
    return sha if isinstance(sha, str) else None


def name_peer_owners(outcome: OverlayVerdict, measured: dict) -> OverlayVerdict:
    """`outcome` with the peer hunks of the measured record named. A path whose peer hunks were split out is
    noted on any verdict; a path whose peer hunks could not be split turns a red into "inconclusive" (the
    red may be the peer's, so it is never a caused red) naming the owning run."""
    peers = measured.get("peer_hunks")
    if not isinstance(peers, dict) or not peers:
        return outcome
    kept = {p: v for p, v in peers.items() if isinstance(v, dict) and v.get("split")}
    left = {p: v for p, v in peers.items() if isinstance(v, dict) and not v.get("split")}
    def _named(group: dict) -> str:
        return "; ".join(f"{p} (run {v.get('owner')}, lines {v.get('lines')})" for p, v in sorted(group.items()))
    notes = []
    if kept:
        notes.append(f"peer hunks split out of the overlay candidate: {_named(kept)}")
    verdict = outcome.verdict
    if left:
        notes.append(f"peer hunks that could not be split out stay in the candidate: {_named(left)}")
        if outcome.red:
            verdict = "inconclusive"
            notes.append("the red may belong to that run's hunks, not this run: re-run once the owner lands or releases them")
    return OverlayVerdict(
        verdict, f"{'; '.join(notes)}: {outcome.detail}", outcome.seeds, outcome.timed_out, outcome.failing,
        outcome.argv, outcome.cause, outcome.guard_refusal,
    )


def keyed_components(measured: dict) -> dict:
    """What the overlay verdict is bound to, from a `measured_rev` record: `base_sha`, the landing set's
    path names, and the run's own content per path (`own_blobs` for a peer-split path, else the whole-file
    blob). A path in `peer_hunks` whose hunks could not be split carries the peer's bytes in its whole-file
    blob, so it is keyed by name only. Not keyed: peer edits to such a path, and the candidate `sha` (a
    function of these components)."""
    blobs, own, peers = (v if isinstance(v, dict) else {} for v in (measured.get("blobs"), measured.get("own_blobs"), measured.get("peer_hunks")))
    return {
        "base_sha": measured.get("base_sha"),
        "paths": sorted(blobs),
        "own_blobs": {p: own.get(p) or b for p, b in blobs.items() if p in own or p not in peers},
    }


def landing_key(measured: dict) -> str:
    """Digest of `keyed_components(measured)`: a change to the base sha, the landing set's paths, or the
    run's own content changes the key; peer churn confined to unsplittable shared paths does not."""
    payload = json.dumps(keyed_components(measured), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def verdict_path(root: Path, run_id: str) -> Path:
    return measured_rev_record_path(root, run_id).with_name(VERDICT_FILE)


def write_verdict(root: Path, run_id: str, measured: dict, outcome: OverlayVerdict) -> Path:
    path = verdict_path(root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "verdict": outcome.verdict, "detail": outcome.detail, "seeds": list(outcome.seeds),
                "timed_out": outcome.timed_out, "failing_ids": list(outcome.failing), "key": landing_key(measured), "base_sha": measured.get("base_sha"), "sha": measured.get("sha"),
                "keyed": keyed_components(measured), "argv": list(outcome.argv),
                **({"zero_run_cause": outcome.cause} if outcome.cause else {}),
                **({"guard_refusal": outcome.guard_refusal} if outcome.guard_refusal else {}),
            },
            indent=2, sort_keys=True,
        ),
        encoding="utf-8",
        newline="\n",
    )
    return path


def _touched_left(root: Path, run_id: str, measured: dict) -> list:
    """Run-touched paths the measured landing set (`blobs` plus `removed`) omits. The run's touched
    set is `measured["touched"]` when the record carries one, else `measured_rev`'s measured-paths
    file; neither (or an unreadable file) means unknown, and unknown is not leftover."""
    touched = measured.get("touched")
    if not isinstance(touched, list):
        from coordinator_core.ops.dispatch_emit.measured_rev import touched_record_path

        try:
            touched = json.loads(touched_record_path(root, run_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(touched, list):
            return []
    from coordinator_core.ops.dispatch_emit.run_leftovers import run_touched_left

    blobs = measured.get("blobs") if isinstance(measured.get("blobs"), dict) else {}
    removed = measured.get("removed") if isinstance(measured.get("removed"), list) else []
    return run_touched_left(touched, frozenset(touched), [*blobs, *removed], {})


def _base_advance_clear(git_root: Path, stored_base: object, current_base: object, watched: set) -> bool:
    """True when HEAD moved from the verified base to the current one without touching a path the verdict
    rests on (the landing set or a seed test file). One `git diff --name-only`; any failure is False."""
    if not (isinstance(stored_base, str) and isinstance(current_base, str) and stored_base and current_base):
        return False
    if stored_base == current_base:
        return True
    changed = _diff_names(git_root, stored_base, current_base)
    return changed is not None and not (changed & watched)


def _diff_names(git_root: Path, a: object, b: object) -> Optional[set]:
    """Paths differing between commits `a` and `b` (one `git diff --name-only`); None when unreadable."""
    if not (isinstance(a, str) and isinstance(b, str) and a and b):
        return None
    try:
        out = run_git(["diff", "--name-only", "-z", a, b], cwd=str(git_root))
    except OSError:
        return None
    return None if out.returncode else set(filter(None, out.stdout.split("\0")))


def legacy_landing_key(measured: dict) -> str:
    """The digest verdicts carried before they recorded `keyed` components: whole-file blobs, base sha and
    candidate sha (plus `own_blobs` when present). Used only to prove a stored `measured-rev.json` is the
    record a legacy verdict was keyed to."""
    blobs = measured.get("blobs") if isinstance(measured.get("blobs"), dict) else {}
    keyed = {"blobs": blobs, "base_sha": measured.get("base_sha"), "sha": measured.get("sha")}
    if isinstance(measured.get("own_blobs"), dict) and measured["own_blobs"]:
        keyed["own_blobs"] = measured["own_blobs"]
    return hashlib.sha256(json.dumps(keyed, sort_keys=True).encode("utf-8")).hexdigest()


def _run_landed_blobs(record_dir: Path) -> dict:
    """`{path: raw blob sha}` of the run's own latest bytes per path: `kept.json` chunk blobs, post-checkpoint blobs,
    then the latest write prints (later wins), then the halt's `evicted.json` blobs for paths none recorded."""
    from coordinator_core.ops.dispatch_emit import kept_work
    from coordinator_core.ops.dispatch_emit.own_hunk_candidate import printed_blobs

    kept = kept_work.read(record_dir)
    found: dict = {}
    for recorded in [*(e.get("paths") for e in kept["chunks"].values() if isinstance(e, dict)), kept[kept_work.POST_CHECKPOINT_KEY]]:
        for rel, blob in (recorded.items() if isinstance(recorded, dict) else ()):
            if isinstance(blob, str) and blob:
                found[rel] = blob
    found.update(printed_blobs(record_dir))
    try:
        evicted = json.loads((record_dir / "evicted.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        evicted = {}
    for rel, entry in (evicted.items() if isinstance(evicted, dict) else ()):
        if rel not in found and isinstance(entry, dict) and isinstance(entry.get("blob"), str):
            found[rel] = entry["blob"]
    return found


def _same_blob_content(git_root: Path, a: object, b: object) -> bool:
    """True when blobs `a` and `b` are one sha, or hold the same bytes up to CRLF/LF (a kept blob is the
    raw write, a verified blob the checked-in form). Reads the object store in process; any unreadable
    blob is False."""
    if not (isinstance(a, str) and isinstance(b, str) and a and b):
        return False
    if a == b:
        return True
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_objects import read_object

    try:
        gitdir = resolve_git_common_dir(git_root)
        objs = [read_object(gitdir, sha) for sha in (a, b)]
    except (OSError, ValueError):
        return False
    if any(o is None or o[0] != "blob" for o in objs):
        return False
    return objs[0][1].replace(b"\r\n", b"\n") == objs[1][1].replace(b"\r\n", b"\n")


def _legacy_clear(root: Path, git_root: Path, run_id: str, data: dict, measured: dict) -> bool:
    """True when a legacy verdict (no recorded components, so unkeyed) still holds for the run's declared
    paths. The verified blobs are the stored `measured-rev.json`'s, trusted only while it hashes to the
    verdict's `key`. Each declared path passes when its current blob is the verified one, or when the run's
    own landed bytes (`_run_landed_blobs`, compared in checkin form) are the verified content, whatever
    peer hunks the current worktree file carries; a path with no verified blob or no recorded landed
    bytes does not (unknown is not own). Paths the run never declared are never read. Zero spawns."""
    record = measured_rev_record_path(root, run_id)
    try:
        stored = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    declared = stored.get("paths") if isinstance(stored, dict) else None
    if not isinstance(declared, list) or legacy_landing_key(stored) != data.get("key"):
        return False
    verified = stored.get("blobs") if isinstance(stored.get("blobs"), dict) else {}
    now, own = (v if isinstance(v, dict) else {} for v in (measured.get("blobs"), measured.get("own_blobs")))
    landed = None
    for rel in declared:
        want = verified.get(rel)
        if not isinstance(want, str) or not want:
            return False
        if (own.get(rel) or now.get(rel)) == want:
            continue
        if landed is None:
            landed = _run_landed_blobs(record.parent)
        if not _same_blob_content(git_root, landed.get(rel), want):
            return False
    return True


def check_verdict(root: Path, run_id: str, measured: dict, git_root: Optional[Path] = None) -> OverlayVerdict:
    """Read the run's overlay verdict for the `measured` record. File reads, plus one `git diff --name-only`
    (in `git_root`, default `root`) only when the base sha advanced: a base advance that touches neither the
    landing set nor a seed test file keeps the verdict. A legacy verdict (no recorded components, unkeyed)
    is checked by `_legacy_clear` against the run's declared paths only, with no spawn. "green" or "red"
    (or "inconclusive", which land_run refuses unless `timed_out` is True, / "pre_existing", which it lands with an advisory) when a verdict
    keyed to exactly this landing set and base sha exists; otherwise "missing" (no file or an unreadable one,
    which land_run lands with an advisory) or
    "mismatch" (keyed to a different landing set or base sha; `detail` says which side moved). A run-touched
    path the `measured` landing set (`blobs` plus `removed`) omits is "mismatch" before any verdict is read
    (a verdict over half the run's work is not this run's verdict); no recorded touched set is never a
    mismatch on this account."""
    left = _touched_left(root, run_id, measured)
    if left:
        return OverlayVerdict("mismatch", f"the landing set omits run-touched path(s): {', '.join(left)}")
    try:
        data = json.loads(verdict_path(root, run_id).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return OverlayVerdict("missing", "no overlay verdict was written for this run")
    except (OSError, ValueError) as exc:
        return OverlayVerdict("missing", f"overlay verdict unreadable: {type(exc).__name__}: {exc}")
    if not isinstance(data, dict):
        return OverlayVerdict("missing", "overlay verdict unreadable: not an object")
    seeds = tuple(data.get("seeds") or ())
    current = keyed_components(measured)
    stored = data.get("keyed")
    moved = None
    if not isinstance(stored, dict):
        if not _legacy_clear(root, git_root or root, run_id, data, measured):
            moved = "keying (no recorded components)"
    elif stored.get("paths") != current["paths"]:
        moved = "landing set paths"
    elif stored.get("own_blobs") != current["own_blobs"]:
        moved = "landing set blobs"
    elif stored.get("base_sha") != current["base_sha"]:
        watched = set(current["paths"]) | {str(s).split("::", 1)[0] for s in seeds}
        if not _base_advance_clear(git_root or root, stored.get("base_sha"), current["base_sha"], watched):
            moved = "base sha"
    elif data.get("key") != landing_key(measured):
        moved = "landing set blobs"
    if moved:
        return OverlayVerdict("mismatch", f"the overlay verdict was keyed to a different {moved}", seeds)
    verdict = data.get("verdict")
    if verdict == "inconclusive":
        return OverlayVerdict(
            "inconclusive", str(data.get("detail", "")), seeds, data.get("timed_out") is True,
            argv=tuple(data.get("argv") or ()),
        )
    if verdict not in ("green", "red", "pre_existing", ENV_DEFECT):
        return OverlayVerdict("missing", f"overlay verdict was {verdict!r}: {data.get('detail', '')}", seeds)
    return OverlayVerdict(
        verdict, str(data.get("detail", "")), seeds, failing=tuple(data.get("failing_ids") or ()),
        argv=tuple(data.get("argv") or ()), cause=str(data.get("zero_run_cause") or ""),
        guard_refusal=str(data.get("guard_refusal") or ""),
    )


#: Step name and cap of the one verdict read plus detached spawn; `reverify_on_mismatch` ends in
#: `land_deadline.LandDeadlineExceeded(REVERIFY_STEP, step=True)` and the landing parks. The child
#: writes its verdict, which the re-sent landing reads.
REVERIFY_STEP = "clean_overlay_verify.reverify_on_mismatch"
REVERIFY_STEP_SECS = 8.0
#: Seconds a parked landing's resume entry waits before the drain re-sends it: the detached overlay
#: it launched is still running, and a re-send inside that window only parks again.
REVERIFY_RESUME_DELAY_SECS = 30.0


INFLIGHT_FILE = "overlay-inflight"
#: Path count the in-flight marker's TTL budgets for: a step of this many paths is the largest the marker must outlive.
INFLIGHT_PATH_BOUND = 5000
#: Load- and clock-independent upper bound (scale at its maximum), so an import under a quiet box never undercuts a later loaded step.
INFLIGHT_TTL_S = 3 * max(
    TEST_TIMEOUT_S * STALL_RETRY_SCALE_MAX + EXPORT_STEP_S_PER_PATH * INFLIGHT_PATH_BOUND,
    _positive_env_float(EXPORT_TIMEOUT_ENV),
)


def _inflight_path(root: Path, run_id: str) -> Path:
    return verdict_path(root, run_id).with_name(INFLIGHT_FILE)


#: Seconds a marker may carry no child pid before it is read as an unstarted child's: the child
#: records its pid as its first act, so a pidless marker past this window never had a child.
INFLIGHT_START_GRACE_S = 20.0


def _record_child_pid(root: Path, run_id: str) -> None:
    """The detached child writes its own pid into the claimed marker; a marker it no longer finds
    (released or replaced) is not recreated."""
    path = _inflight_path(root, run_id)
    try:
        if path.is_file():
            path.write_text(str(os.getpid()), encoding="ascii")
    except OSError:
        pass


def _inflight_held(path: Path) -> bool:
    """True when the marker proves a child that is running or still inside its start grace: a recorded
    pid that is alive, or no pid yet and younger than `INFLIGHT_START_GRACE_S`. A dead pid, a pidless
    marker past the grace, and any marker older than `INFLIGHT_TTL_S` (pid reuse bound) are not held."""
    age = time.time() - path.stat().st_mtime
    if age >= max(INFLIGHT_TTL_S, 3 * _positive_env_float(EXPORT_TIMEOUT_ENV)):
        return False
    try:
        pid = int(path.read_text(encoding="ascii").strip() or 0)
    except FileNotFoundError:
        raise
    except (OSError, ValueError):
        pid = 0
    if pid <= 0:
        return age < INFLIGHT_START_GRACE_S
    from coordinator_core.group_em.session_registry import pid_alive

    return pid_alive(pid)


def _claim_inflight(root: Path, run_id: str) -> bool:
    """Create the run's in-flight marker exclusively: False when a detached overlay that is provably
    alive (`_inflight_held`) already holds it; a dead or never-started child's marker is replaced."""
    path = _inflight_path(root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
            return True
        except FileExistsError:
            try:
                if _inflight_held(path):
                    return False
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                return False
        except OSError:
            return True
    return False


def _release_inflight(root: Path, run_id: str) -> None:
    try:
        _inflight_path(root, run_id).unlink()
    except OSError:
        pass


def reverify_on_mismatch(
    root: Path, run_id: str, measured: dict, stale: OverlayVerdict, *, spawn=None, git_root: Optional[Path] = None,
) -> OverlayVerdict:
    """Re-run the overlay for a "mismatch" `stale` verdict over the current `measured` candidate. Reads
    the verdict file once: a green, red or pre_existing verdict keyed to this landing is returned. With none,
    this module's CLI is launched detached (the overlay's git and pytest children never run inside the
    engine op) and the landing parks by raising `land_deadline.LandDeadlineExceeded(REVERIFY_STEP,
    step=True)`; the op never waits on the child. Any other `stale`, no recorded seeds, no candidate, an
    inconclusive fresh verdict or a failed launch returns `stale` unchanged. An `overlay-inflight` marker
    (claimed before the launch, pid-stamped and released by the child) keeps a re-sent landing from
    launching a second overlay while a live one runs: it parks without spawning. A marker whose pid is
    dead, or that carries none past `INFLIGHT_START_GRACE_S`, is an unstarted child's and is replaced."""
    if stale.verdict != "mismatch" or not stale.seeds or overlay_candidate(measured) is None:
        return stale
    if spawn is None:
        from coordinator_core.ops.ceremony.detached_spawn import spawn_detached as spawn
    from coordinator_core.ops.dispatch_emit import land_deadline

    engine = Path(__file__).resolve().parents[3]
    argv = ["--run-id", run_id, "--repo-root", str(root)]
    with land_deadline.step(REVERIFY_STEP, REVERIFY_STEP_SECS):
        fresh = check_verdict(root, run_id, measured, git_root)
        if fresh.verdict in ("green", "red", "pre_existing", ENV_DEFECT):
            return fresh
        if fresh.verdict == "inconclusive":
            return stale
        if _claim_inflight(root, run_id):
            if not spawn(str(engine), str(Path(__file__).resolve()), argv):
                _release_inflight(root, run_id)
                return stale
        raise land_deadline.LandDeadlineExceeded(
            REVERIFY_STEP, land_deadline.leg_costs(), land_deadline.BOUND_STEP, step=True
        )


def remedy_call(run_id: Optional[str], root: Optional[Path] = None) -> str:
    py = engine_python_command(root) if root is not None else Path(sys.executable).as_posix()
    return f"{py} -m coordinator_core.ops.dispatch_emit.clean_overlay_verify --run-id {run_id or '<run_id>'} --repo-root <repo_root>"


def _manifest_declared_paths(manifest: Path) -> Optional[list]:
    """The run manifest's `review_declared_paths` list, or None when the manifest is unreadable or carries no list."""
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    declared = data.get("review_declared_paths") if isinstance(data, dict) else None
    if not isinstance(declared, list) or not all(isinstance(p, str) for p in declared):
        return None
    return declared


def _own_written(root: Path, run_id: str) -> list:
    """The paths the run's own review agents wrote that `review_reverify` classes as own (`created`,
    `outside_declared`), read from the run's pre-review snapshots; [] when no session or snapshot answers."""
    from coordinator_core.ops import review_reverify
    from coordinator_core.ops.dispatch_emit import land_run
    from coordinator_core.ops.dispatch_emit.ask_fanout_contract import pinned_run_base
    from coordinator_core.session.core import resolve_session_id

    sessions = land_run._firing_sessions(root, run_id, resolve_session_id(str(root)) or None)
    return review_reverify.own_written_paths(root, run_id, pinned_run_base(root, run_id), sorted(sessions))


def _snapshot_scope(root: Path, run_id: str) -> list:
    """The paths the run's review-prep snapshots froze (their manifests' `files`, the reverify's `edited`
    source): a module the run created that no declared write names reaches the mirror only through them.
    [] when no session or snapshot answers; a path escaping the worktree is dropped."""
    from coordinator_core.ops import review_reverify
    from coordinator_core.ops.dispatch_emit import land_run, review_edits
    from coordinator_core.ops.review_slice_id import RUN_PREP_PLAN_STEM
    from coordinator_core.session.core import resolve_session_id
    from coordinator_core.session.machinery_paths import share_dir

    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", run_id)
    out: dict[str, None] = {}
    for session in sorted(s for s in land_run._firing_sessions(root, run_id, resolve_session_id(str(root)) or None) if s):
        if re.search(r"[\\/]|\.\.", session):
            continue
        share = Path(share_dir(str(root), session))
        for snap in share.glob(f"{RUN_PREP_PLAN_STEM}-{safe}-*-prep{review_reverify._PRE_REVIEW_SUFFIX}"):
            for raw in (review_reverify._read_manifest(snap) or {}).get("files", ()):
                path = review_edits.norm(raw)
                if path and not review_edits.out_of_worktree(path):
                    out[path] = None
    return list(out)


def own_hunk_overrides(root: Path, run_id: str, paths: Sequence[str]) -> dict:
    """`{path: bytes}` of HEAD plus only the run's own hunks, for each of `paths` whose worktree also carries a
    peer's hunks (`checkpoint_hunks.checkpoint_own_blobs`). A path it cannot split, or wholly the run's own, is
    absent. Never raises: an unreadable blob or attribution leaves the path out."""
    if not paths:
        return {}
    try:
        from coordinator_core.git.checkpoint_hunks import checkpoint_own_blobs
        from coordinator_core.git.git_dir import resolve_git_common_dir
        from coordinator_core.git.git_objects import read_object

        blobs, _withheld = checkpoint_own_blobs(root, run_id, list(paths))
        common = resolve_git_common_dir(root)
        out: dict = {}
        for rel, sha in blobs.items():
            obj = read_object(common, sha)
            if obj is not None and obj[0] == "blob":
                out[rel] = obj[1]
        try:
            heads = _head_sources(root, list(out)) if out else {}
        except Exception:  # noqa: BLE001 -- HEAD unreadable: keep every own blob rather than drop them all
            heads = {}
        # Own hunks that reduce a diffed path to HEAD's bytes would put the pre-edit source in the mirror.
        return {rel: data for rel, data in out.items() if heads.get(rel) != data}
    except Exception as exc:  # noqa: BLE001 -- an unsplit path falls back to its HEAD bytes
        print(f"[clean-export] advisory: own hunks not split ({type(exc).__name__}: {exc})", file=sys.stderr)
        return {}


def own_delta(root: Path, run_id: str, seed: Sequence[str]) -> tuple:
    """`(paths, overrides)`: `seed` plus the run's own delta it lacks (the manifest's `review_declared_paths`
    and `_own_written`), and `{path: bytes}` for each of those paths another live run declared or any run's record names (`peer_hunk_runs`, halted included), at HEAD
    plus only the run's own hunks (`own_hunk_overrides`). A shared path with no splittable own hunks (an
    untracked new file has no HEAD blob to split) stays in `paths` and reaches the mirror at its worktree
    bytes, peer hunks included; never HEAD or absent. A path only a peer declares is never added, so it stays
    at HEAD. Every path reaches the mirror at its
    worktree bytes, so a test the run created or a symbol it added is there when the command collects. Never
    raises: an own-write set that cannot be read leaves `seed` as it was, with an advisory. The run's whole
    recorded write set (`_own_write_set`: revise-widened and EM-row paths, untracked new modules included)
    joins the delta too. The mirror's git HEAD is the clone base and stays stale by design; `state.json`
    holds the advanced tree, so a HEAD that predates the run is not itself a missing-file cause."""
    from coordinator_core.ops.dispatch_emit import review_edits

    have = {review_edits.norm(p) for p in seed}

    def read(label: str, source) -> list:
        # One unreadable source never discards what the others name: the run's declared source must not drop to HEAD with it.
        try:
            return list(source())
        except Exception as exc:  # the own delta only widens the seed; an unreadable source never refuses the export
            print(f"[clean-export] advisory: run's own delta not read ({label}: {type(exc).__name__}: {exc}); that source skipped", file=sys.stderr)
            return []

    manifest = read("manifest", lambda: _manifest_declared_paths(measured_rev_record_path(root, run_id).with_name("manifest.json")) or [])
    owned = [
        *manifest, *read("write set", lambda: _own_write_set(root, run_id)),
        *read("own written", lambda: _own_written(root, run_id)),
        *read("write prints", lambda: _printed_writes(root, run_id)),
    ]
    snapshot = read("snapshot", lambda: _snapshot_scope(root, run_id))
    extra = [p for p in (*owned, *snapshot) if review_edits.norm(p) not in have]
    if not extra and not seed:
        return list(seed), {}
    try:
        sibling = {review_edits.norm(p) for p in review_edits.live_sibling_declared(root, run_id)}
    except Exception as exc:  # peers unknown: only paths the run itself owns join, never a snapshot-only path
        print(f"[clean-export] advisory: peer declarations not read ({type(exc).__name__}: {exc}); own paths only", file=sys.stderr)
        sibling = set()
        snapshot = []
        extra = [p for p in owned if review_edits.norm(p) not in have]
    # The review snapshot measures dirt, it does not declare ownership: a snapshot-only path a peer declared
    # is the peer's and stays at HEAD. A path the run itself declared or wrote stays even when shared.
    declared = {review_edits.norm(p) for p in owned}
    wanted = [p for p in dict.fromkeys(review_edits.norm(p) for p in extra) if p not in sibling or p in declared]
    from coordinator_core.ops.dispatch_emit import co_tenant_owner

    def peer_named(p: str) -> bool:
        if p in sibling:
            return True
        try:
            return bool(co_tenant_owner.peer_hunk_runs(root, p, exclude_run_id=run_id))
        except Exception:  # noqa: BLE001 -- peers unknown: the path is not split
            return False

    shared = [p for p in (*(review_edits.norm(s) for s in seed), *wanted) if peer_named(p)]
    split = own_hunk_overrides(root, run_id, list(dict.fromkeys(shared)))
    return [*seed, *wanted], {p: split[p] for p in dict.fromkeys(shared) if p in split}


def run_own_delta(root: Path, run_id: str, seed: Sequence[str]) -> list:
    """The paths of `own_delta`: `seed` plus the run's own delta it lacks."""
    return own_delta(root, run_id, seed)[0]


def live_seeds(root: Path, seeds: Sequence[str], base_sha: Optional[str]) -> list:
    """`seeds` without the files the run deleted (in `base_sha`'s tree, gone from `root`'s worktree, not
    renamed): a deleted test file is no seed. A seed absent for any other reason stays, so its absence is judged."""
    from coordinator_core.ops.dispatch_emit.landing_dropped_targets import run_deleted_targets_since

    absent = [s for s in seeds if not (root / s).exists()]
    if not absent or not base_sha:
        return list(seeds)
    try:
        gone = set(run_deleted_targets_since(root, _commit_sha(root, base_sha), absent))
    except (ExportError, OSError):
        return list(seeds)
    return [s for s in seeds if s not in gone]


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Verify-stage entry: read the run's `measured-rev.json`, run the overlay over the landing set's test
    seeds and write the verdict. Exit 0 on green, 1 on red, 2 when inconclusive or unmeasured.
    `--export-run -- <argv>` instead runs `argv` in the clean export (HEAD plus the run's own delta: its
    declared paths and the own-written paths `run_own_delta` adds, never a peer's declared path) under
    the mirror lock and exits with its status; a failed export or a timed-out command exits 2 with the cause
    on stderr."""
    from coordinator_core.ops.dispatch_emit.landing_reach import seeds_for_landing

    raw = list(sys.argv[1:] if argv is None else argv)
    command: list = []
    if "--" in raw:
        cut = raw.index("--")
        raw, command = raw[:cut], raw[cut + 1:]
    parser = argparse.ArgumentParser(prog="clean_overlay_verify")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument(
        "--export-run", action="store_true",
        help="run the command after `--` in the clean export (HEAD plus the run's declared paths)",
    )
    args = parser.parse_args(raw)
    if not safe_id(args.run_id):
        parser.error(f"--run-id {args.run_id!r} is not a plain run id")
    if args.export_run and not command:
        parser.error("--export-run needs the test command after `--`")
    if command and not args.export_run:
        parser.error("a command after `--` needs --export-run")
    root = Path(args.repo_root).resolve()
    record = measured_rev_record_path(root, args.run_id)
    try:
        measured = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        declared = _manifest_declared_paths(record.with_name("manifest.json")) if args.export_run else None
        if declared is None:
            print(
                f"no readable measured-rev record ({record}) and no manifest review_declared_paths "
                f"({record.with_name('manifest.json')}) for {args.run_id}: {exc}", file=sys.stderr,
            )
            return 2
        measured = {"paths": declared}
    paths = measured.get("paths") or []
    if args.export_run:
        try:
            delta, overrides = own_delta(root, args.run_id, paths)
            ran = run_in_export(root, args.run_id, delta, command, overrides)
        except CoTenantDependency as exc:
            from coordinator_core.ops.dispatch_emit import co_tenant_owner

            resolved = {**co_tenant_owner.resolve(root, args.run_id, exc), "peer": exc.peer}
            print(f"{CO_TENANT_LINE}: {json.dumps(resolved, sort_keys=True)}", file=sys.stderr)
            return 2
        except OwnDeltaMissing as exc:
            print(str(exc), file=sys.stderr)
            return 2
        except ExportError as exc:
            print(f"clean export failed: {exc}", file=sys.stderr)
            return 2
        except subprocess.TimeoutExpired as exc:
            print(f"clean export test command stalled: {exc} (window: {EXPORT_RUN_STALL_ENV})", file=sys.stderr)
            return 2
        except OSError as exc:
            print(f"clean export test command did not start: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        sys.stdout.write(ran.stdout or "")
        sys.stderr.write(ran.stderr or "")
        return ran.returncode
    sha = overlay_candidate(measured)
    if sha is None:
        print("measured-rev record carries no candidate sha", file=sys.stderr)
        return 2
    _record_child_pid(root, args.run_id)
    try:
        landing_seeds = seeds_for_landing(root, paths)
        live = live_seeds(root, landing_seeds, measured.get("base_sha"))
        if landing_seeds and not live:
            verdict = OverlayVerdict("green", "every test seed of the landing set was deleted by the run", ())
        else:
            verdict = verify(
                root, args.run_id, sha, live, base_sha=measured.get("base_sha"), touched=paths,
            )
        outcome = name_peer_owners(verdict, measured)
        written = write_verdict(root, args.run_id, measured, outcome)
    finally:
        _release_inflight(root, args.run_id)
    print(f"overlay verdict: {outcome.verdict}")
    if outcome.verdict != "green":
        print(f"detail: {written}")
    if outcome.verdict == ENV_DEFECT:
        print(outcome.detail, file=sys.stderr)
    return {"green": 0, "pre_existing": 0, ENV_DEFECT: 0, "red": 1}.get(outcome.verdict, 2)


if __name__ == "__main__":
    raise SystemExit(main())
