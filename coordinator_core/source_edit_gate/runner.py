"""coordinator_core.source_edit_gate.runner -- detect and invoke a test runner
for the gate's computed source-edit tier, with no per-repo declaration.

`detect_runner(repo_root)` returns `"pytest"`, `"vitest"`, or `None` (no
recognized runner) by looking at what the repo root itself already carries:

  - `"pytest"` -- `pyproject.toml` with a `[tool.pytest.ini_options]` table,
    OR a `pytest.ini`, OR a `conftest.py` at that directory.
  - `"vitest"` -- `package.json` listing `vitest` under `dependencies` or
    `devDependencies`.

`find_runner_root(repo_root, test_rel)` answers the same question per
SELECTED TEST FILE rather than once for the whole repo: a monorepo's own
pytest/vitest config often lives one or more directories below `repo_root`
(a pnpm-workspace subpackage's own `package.json`+`vitest`, a Python
subproject's own `pyproject.toml`), and running that file from `repo_root`
against `repo_root`'s config/`node_modules` can silently under-collect --
a well-formed, empty report that looks identical to "nothing to verify". It
walks up from the test file's own directory to `repo_root` (inclusive),
checking the same markers `detect_runner` checks, and returns the FIRST
(nearest) directory that carries one, plus that directory's runner. No
marker found anywhere between the test file and `repo_root` -> `(None,
None)`, same indeterminate contract as `detect_runner` returning `None`.

`run_selected(repo_root, test_files, runner, *, parallel=True)` runs ONLY the
selected files (never the whole suite) and returns a `TestReport` (see
`gate.py`) using the repo's own config (pytest picks up
`pyproject.toml`/`pytest.ini`/`conftest.py` automatically via cwd; vitest
picks up its own config the same way) -- `None` on a crashed/unparseable run,
same contract as `gate._run_declared_command` before it.

Process-group cleanup: the runner child is started in its own session/process
group (POSIX `start_new_session=True`; Windows
`CREATE_NEW_PROCESS_GROUP`, via `_popen_group_kwargs()`) so a test that itself
spawns a grandchild (a daemon supervisor, a `sleep_forever.py`) is reachable
for cleanup even after the immediate child exits or is killed. A `finally`
block always terminates then kills the whole group -- `_terminate_group` sends
SIGTERM to the process group (POSIX, via `os.killpg`) or `taskkill /T /F`
(Windows) first, gives it `_GROUP_KILL_GRACE_S` seconds, then force-kills
anything left (`SIGKILL`/`os.killpg` again, or a second `taskkill /T /F`,
which is already forceful) -- this runs whether the pytest/vitest subprocess
returned normally, was killed by the per-test-file timeout below, or raised.

A per-group wall-clock budget (`group_timeout_s`, default
`_DEFAULT_GROUP_TIMEOUT_S`) bounds the whole `subprocess.run` call: exceeding
it raises `subprocess.TimeoutExpired`, which `run_selected` catches and turns
into `None` (the pre-existing crashed/unparseable-run contract) after the
`finally` cleanup above has already reaped the group -- `gate.py`'s
`_run_groups` already treats a `None` report as a failed group, so a group
that hangs past budget surfaces the same way a crash does, with the group's
own `(runner, root)` naming it in `detail`, rather than hanging the whole gate
run.

A per-TEST timeout (as opposed to the per-GROUP wall clock above) is applied
via `pytest-timeout` (`--timeout=<N>`, `N` = `pytest_timeout_s`, default
`_DEFAULT_PYTEST_TIMEOUT_S`) whenever the `pytest_timeout` plugin is
importable in this interpreter -- a single test that hangs is then a FAILURE
of that test (pytest-timeout's own SIGALRM/thread-based enforcement), letting
`gate.py`'s existing base-vs-candidate diff and confirm-rerun logic treat it
like any other failing node id, rather than consuming the whole group's wall
budget. `pytest_timeout` not importable -> the flag is omitted outright (never
a synthetic/home-grown per-test timeout); the per-group wall-clock budget
above is the only bound in that case. No `-p pytest_timeout` is ever passed:
an importable `pytest_timeout` auto-registers itself as an installed plugin,
and an explicit `-p` for it double-registers and pytest refuses to start.

The pytest leg runs the selected groups under `xdist` (`-n <N>`,
`-p no:cacheprovider`) whenever `xdist` is importable in this interpreter AND
`parallel=True` (the default) -- `N = max(1, min(cpu_count // 2, usable_RAM_GB
* 1024 // 150))` (`_xdist_worker_count`), never unbounded `-n auto`. `xdist`
not importable, or `parallel=False` (the gate's own confirmation re-run) ->
serial.

Interpreter resolution: `sys.executable` for pytest (the interpreter this
process is already running under -- never a bare `python`/`python3` argv0,
which does not resolve via `CreateProcess` on Windows the way a POSIX exec
search does); `shutil.which("pnpm")` then `shutil.which("npx")` for vitest.
Neither resolving -> `None` (indeterminate), never a `shell=True` fallback.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from coordinator_core.win_portability import no_console_creationflags

from .gate_report import parse_junitxml, parse_vitest_json

__all__ = ["detect_runner", "find_runner_root", "run_selected", "GroupTimeout"]


class GroupTimeout(Exception):
    """Raised by `run_selected` when the group's `subprocess.run` call
    exceeds `group_timeout_s` -- distinct from the pre-existing `None`
    (crashed/unparseable) return so `gate.py` can report the machine-readable
    `"group-timeout"` indeterminate reason instead of folding a hang into the
    same bucket as a genuine crash."""

_XDIST_MB_PER_WORKER = 150

# Per-group wall-clock budget (seconds) -- bounds one `subprocess.run` call
# (a whole `(runner, root)` group) so a hang cannot stall the gate forever.
_DEFAULT_GROUP_TIMEOUT_S = 900
# Per-test timeout (seconds) handed to `pytest-timeout`, when importable.
_DEFAULT_PYTEST_TIMEOUT_S = 120
# Grace period between SIGTERM/soft-taskkill and the follow-up force-kill.
_GROUP_KILL_GRACE_S = 5

_IS_WINDOWS = os.name == "nt"


def _popen_group_kwargs() -> dict:
    """Kwargs that start the child in its own session/process group, so its
    own descendants (a daemon supervisor, a `sleep_forever.py` grandchild)
    stay reachable for cleanup as a unit even after the immediate child exits
    or is killed. POSIX: `start_new_session=True` (new session + process
    group, survives the immediate child dying). Windows:
    `CREATE_NEW_PROCESS_GROUP` (the taskkill `/T` tree-kill below is what
    actually reaches descendants there -- process groups on Windows don't map
    onto job-object-style descendant tracking the way POSIX pgids do, but the
    flag is still required for `_terminate_group`'s Windows leg to target the
    right console group)."""
    if _IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _terminate_group(proc: subprocess.Popen) -> None:
    """Terminate then kill the whole process group `proc` heads -- always
    called from a `finally`, whether `proc` already exited normally, was
    killed by the per-group timeout, or the caller raised. POSIX:
    `os.killpg(pgid, SIGTERM)`, a grace period, then `os.killpg(pgid,
    SIGKILL)` for anything still alive. Windows: `taskkill /PID <pid> /T /F`
    -- already forceful (no separate soft-kill leg; `taskkill` without `/F`
    can hang waiting on a console prompt), so it is issued once, tolerantly
    (a process that already exited makes `taskkill` fail harmlessly)."""
    if proc.poll() is not None:
        return
    if _IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            **no_console_creationflags(),
        )
        return
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=_GROUP_KILL_GRACE_S)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _usable_ram_gb() -> float:
    """Best-effort available-RAM estimate, cross-platform: `psutil` if
    installed (most accurate -- available, not total), else POSIX
    `os.sysconf` (`SC_PAGE_SIZE` * `SC_PHYS_PAGES`, unavailable on Windows),
    else a conservative fixed fallback so a worker-count computation never
    raises on a platform/environment neither path covers."""
    try:
        import psutil

        return psutil.virtual_memory().available / (1024 ** 3)
    except ImportError:
        pass
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        phys_pages = os.sysconf("SC_PHYS_PAGES")
        return (page_size * phys_pages) / (1024 ** 3)
    except (ValueError, AttributeError, OSError):
        return 4.0


def _xdist_worker_count() -> int:
    """`max(1, min(cpu_count // 2, usable_RAM_GB * 1024 // 150))` -- caps
    parallelism by both core count and a per-worker memory budget so xdist
    never oversubscribes a small/loaded box."""
    cpu = os.cpu_count() or 1
    ram_gb = _usable_ram_gb()
    return max(1, min(cpu // 2, int(ram_gb * 1024 // _XDIST_MB_PER_WORKER)))


def _detect_runner_at(root: Path) -> str | None:
    pytest_ini = root / "pytest.ini"
    conftest = root / "conftest.py"
    pyproject = root / "pyproject.toml"
    if pytest_ini.is_file() or conftest.is_file():
        return "pytest"
    if pyproject.is_file():
        try:
            text = pyproject.read_text(encoding="utf-8")
        except OSError:
            text = ""
        if "[tool.pytest.ini_options]" in text:
            return "pytest"

    package_json = root / "package.json"
    if package_json.is_file():
        try:
            payload = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
        deps = {}
        deps.update(payload.get("dependencies", {}) or {})
        deps.update(payload.get("devDependencies", {}) or {})
        if "vitest" in deps:
            return "vitest"

    return None


def detect_runner(repo_root: str) -> str | None:
    return _detect_runner_at(Path(repo_root))


def find_runner_root(repo_root: str, test_rel: str) -> tuple:
    """Walk from `test_rel`'s own directory up to `repo_root` (inclusive),
    returning `(runner, root_dir_str)` for the nearest directory carrying a
    runner marker, or `(None, None)` if none is found by the time `repo_root`
    itself has been checked."""
    root = Path(repo_root).resolve()
    cur = (root / test_rel).resolve().parent
    while True:
        runner = _detect_runner_at(cur)
        if runner is not None:
            return runner, str(cur)
        if cur == root or cur.parent == cur:
            return None, None
        cur = cur.parent


def _run_with_group_cleanup(argv: list, *, cwd: str, timeout_s: float) -> bool:
    """Runs `argv` as a `Popen` started in its own session/process group
    (`_popen_group_kwargs`), waits up to `timeout_s`, and ALWAYS terminates
    the whole group in a `finally` (`_terminate_group`) -- whether the run
    completed, timed out, or raised. Returns `True` on a completed run
    (whatever its exit code -- the caller parses the report file either way,
    matching the pre-existing `subprocess.run` contract of never raising on a
    non-zero test-runner exit), `False` on a timeout (the caller's cue to
    treat this the same as a crashed/unparseable run)."""
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **_popen_group_kwargs(),
        **no_console_creationflags(),
    )
    try:
        proc.wait(timeout=timeout_s)
        return True
    except subprocess.TimeoutExpired:
        return False
    finally:
        _terminate_group(proc)


def run_selected(
    repo_root: str,
    test_files: list,
    runner: str,
    *,
    parallel: bool = True,
    group_timeout_s: float = _DEFAULT_GROUP_TIMEOUT_S,
    pytest_timeout_s: float = _DEFAULT_PYTEST_TIMEOUT_S,
) -> dict | None:
    if runner == "pytest":
        tmp_dir = tempfile.mkdtemp(prefix="source-edit-gate-pytest-")
        try:
            junit_path = Path(tmp_dir) / "source-edit-gate-report.xml"
            basetemp_path = Path(tmp_dir) / "basetemp"
            argv = [
                sys.executable,
                "-m",
                "pytest",
                *test_files,
                f"--junitxml={junit_path}",
                f"--basetemp={basetemp_path}",
            ]
            if importlib.util.find_spec("pytest_timeout") is not None:
                # `pytest_timeout` auto-registers itself as an installed
                # plugin the moment it's importable -- an explicit
                # `-p pytest_timeout` double-registers it and pytest refuses
                # to start (`ValueError: Plugin already registered`), so only
                # the `--timeout=` flag is passed, never a `-p` for it.
                argv += [f"--timeout={pytest_timeout_s}"]
            if parallel and importlib.util.find_spec("xdist") is not None:
                argv += ["-n", str(_xdist_worker_count()), "-p", "no:cacheprovider"]
            completed = _run_with_group_cleanup(argv, cwd=repo_root, timeout_s=group_timeout_s)
            if not completed:
                raise GroupTimeout(f"pytest group exceeded {group_timeout_s}s: {repo_root}")
            return parse_junitxml(junit_path)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    if runner == "vitest":
        vitest_bin = shutil.which("pnpm") or shutil.which("npx")
        if not vitest_bin:
            return None
        tmp_dir = tempfile.mkdtemp(prefix="source-edit-gate-vitest-")
        try:
            out_path = Path(tmp_dir) / "source-edit-gate-report.json"
            argv = [
                vitest_bin,
                "vitest",
                "run",
                *test_files,
                "--reporter=json",
                f"--outputFile={out_path}",
            ]
            completed = _run_with_group_cleanup(argv, cwd=repo_root, timeout_s=group_timeout_s)
            if not completed:
                raise GroupTimeout(f"vitest group exceeded {group_timeout_s}s: {repo_root}")
            try:
                raw = out_path.read_text(encoding="utf-8")
            except OSError:
                raw = ""
            return parse_vitest_json(raw)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    return None
