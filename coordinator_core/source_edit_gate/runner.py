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
import subprocess
import sys
import tempfile
from pathlib import Path

from coordinator_core.win_portability import no_console_creationflags

from .gate_report import parse_junitxml, parse_vitest_json

__all__ = ["detect_runner", "find_runner_root", "run_selected"]

_XDIST_MB_PER_WORKER = 150


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


def run_selected(
    repo_root: str, test_files: list, runner: str, *, parallel: bool = True
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
            if parallel and importlib.util.find_spec("xdist") is not None:
                argv += ["-n", str(_xdist_worker_count()), "-p", "no:cacheprovider"]
            subprocess.run(
                argv,
                cwd=repo_root,
                capture_output=True,
                **no_console_creationflags(),
            )
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
            subprocess.run(
                argv,
                cwd=repo_root,
                capture_output=True,
                **no_console_creationflags(),
            )
            try:
                raw = out_path.read_text(encoding="utf-8")
            except OSError:
                raw = ""
            return parse_vitest_json(raw)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    return None
