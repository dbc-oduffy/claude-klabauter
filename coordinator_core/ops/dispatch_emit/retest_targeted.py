"""Re-run a finished run's failing targeted tests and write the fresh verdict the landing gate reads.

Entered as ``python -m coordinator_core.ops.dispatch_emit.retest_targeted
--from-task-output FILE [--tests PATH ...]`` or, under the warm engine, through
``emit-dispatch-workflow --retest-targeted`` inside a pool worker. The entry validates, writes a
``running`` record, spawns a detached ``--detached-child`` and returns 0 at once (3 when a retest is
already running within its deadline); ``--poll`` reads the record: exit 0 pass, 1 fail, 2
error/stale/absent/abandoned, 3 running.

A failure with no baseline names its tests from ``--tests``, else the run's recorded targeted set;
with neither, a ``stale`` verdict with a ``note`` is written (the gate does not admit it).
Traps: the gate trusts a fresh pass as the run's whole verdict. The warm side never launches tests;
the child is bounded by its own timeout (``RETEST_CHILD_TIMEOUT_SECS``), reads no stdin, and logs to
a file beside the verdict. The record is the only handle: a child killed outright leaves ``running``
until its ``deadline`` passes.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Optional, Sequence

from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.ops._path_guard import contained_path, safe_id
from coordinator_core.ops.ceremony.detached_spawn import spawn_detached
from coordinator_core.ops.dispatch_emit import landing_retest
from coordinator_core.ops.dispatch_emit import landing_targeted_tests
from coordinator_core.ops.dispatch_emit import red_attribution
from coordinator_core.ops.review_test_blocker import ZERO_RAN_MESSAGE
from coordinator_core.telemetry import host_sampler
from coordinator_core.win_portability import no_console_creationflags

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run_id_of(params: dict) -> str:
    """``params.run_id``, else the script stem minus the ``.workflow`` an ask script's name carries."""
    rid = params.get("run_id")
    if isinstance(rid, str) and rid:
        return rid
    stem = PurePosixPath(str(params.get("script_path") or "").replace("\\", "/")).stem
    return stem.removesuffix(".workflow")


def _outside_own_records(run_id: str, paths: Sequence[str]) -> list[str]:
    """``paths`` minus ``state/warp/<run_id>/``: the landing rewrites those after the retest, so a
    fingerprint holding them reads stale."""
    from coordinator_core.session.record_homes import home_dir

    prefix = f"{PurePosixPath(home_dir('', 'warp').replace(chr(92), '/'))}/{run_id}/"
    return [p for p in paths if not str(p).replace("\\", "/").startswith(prefix)]


#: Bound on one detached targeted rerun, not on the pool: past it the child records ``error``.
RETEST_CHILD_TIMEOUT_SECS = 1800.0

#: Grace past the child timeout before a ``running`` record reads as abandoned.
_DEADLINE_GRACE_SECS = 60.0

#: Sleep between ``--poll --wait`` reads of the record.
_WAIT_INTERVAL_SECS = 0.5

#: Longest one warm-dispatched ``--poll --wait`` call may wait before returning 3 (still running);
#: the cold ``python -m`` entry waits unbounded. A warm pool slot is held for the whole call.
WARM_WAIT_BUDGET_SECS = 1.0

#: ``run`` return value for a launcher killed at the timeout.
LAUNCH_TIMED_OUT = "timed-out"

#: pytest exit code for "no tests collected", and the record status that names it.
NO_TESTS_COLLECTED = 5
NO_TESTS_STATUS = "no-tests"

#: Program names that are pytest itself (lower-cased basename).
_PYTEST_NAMES = frozenset({"pytest", "py.test", "pytest.exe", "py.test.exe"})

#: Options whose next token is a value, never a collection root.
_VALUE_OPTIONS = frozenset({
    "--rootdir", "-c", "-p", "--confcutdir", "-m", "-k", "--ignore", "--ignore-glob", "--deselect",
    "-o", "--override-ini", "--maxfail", "--tb", "--durations", "-n", "--basetemp",
})


def _is_export_root(cwd: Path) -> bool:
    """True when `cwd` sits in a shared clone (an export): the nearest `.git` dir carries `objects/info/alternates`.
    A live checkout or linked worktree has none."""
    for cand in (cwd, *cwd.parents):
        git = cand / ".git"
        if git.is_dir():
            return (git / "objects" / "info" / "alternates").is_file()
        if git.exists():
            return False
    return False


#: Shell-joined argv (with the per-run ``--basetemp``) of every session ``_run_argv`` launched in this process;
#: ``main`` clears it and the retest record carries it as ``argv``.
_LAUNCHED_ARGV: list[str] = []


def _run_argv(
    argv: list[str], cwd: Path, *, log: Optional[Path] = None, timeout: Optional[float] = None,
    append: bool = False,
) -> "int | str":
    """Exit code, or ``LAUNCH_TIMED_OUT``. The child runs in a capped job (a killpg session off
    Windows); the timeout and the normal exit both close it, so no grandchild survives. The log is a
    file so a surviving grandchild never blocks this call."""
    limit = RETEST_CHILD_TIMEOUT_SECS if timeout is None else timeout
    from coordinator_core.testing import spawn_depth

    refusal = spawn_depth.refusal_for_spawn(os.environ) or spawn_depth.selection_includes_spawner(
        argv[argv.index("pytest") + 1 :] if "pytest" in argv else argv, os.environ, str(cwd)
    )
    if refusal:
        raise OSError(refusal)
    from coordinator_core.bash_guards._heavy_command_class import (
        admit_before_spawn, register_spawn, release_spawn,
    )

    workers = next((int(argv[i + 1]) for i, t in enumerate(argv[:-1]) if t == "-n" and argv[i + 1].isdigit()), 1)
    admission = admit_before_spawn("retest-targeted-pytest", 0.75 + workers * 0.15)
    if not admission.admitted:
        raise OSError(f"admission: {admission.reason}")
    sink = open(log, "ab" if append else "wb") if log is not None else subprocess.DEVNULL
    from coordinator_core.testing import baseline_export, scoped_pytest

    # An export root (clone with .git) runs under the export env; only a live worktree keeps os.environ.
    from coordinator_core.diagnostics.contained_run import spawn_in_capped_job
    from coordinator_core.testing.nested_session_guard import guarded_child_env

    env = guarded_child_env(
        baseline_export.export_env(cwd) if _is_export_root(Path(cwd)) else os.environ
    )
    run_holder: Optional[Path] = None
    try:
        if "pytest" in argv and not scoped_pytest.has_basetemp(argv):
            run_holder, basetemp, tmp = scoped_pytest.per_run_dirs(cwd, scoped_pytest.new_run_slug())
            at = argv.index("pytest") + 1
            argv = [*argv[:at], "--basetemp", str(basetemp), *argv[at:]]
            env = scoped_pytest.tmp_env(env, tmp)
        _LAUNCHED_ARGV.append(shlex.join(argv))
        proc, close = spawn_in_capped_job(
            argv,
            popen_kwargs=dict(
                cwd=str(cwd), env=env, creationflags=_NO_WINDOW, stdin=subprocess.DEVNULL,
                stdout=sink, stderr=subprocess.STDOUT,
            ),
            background=True,
        )
        register_spawn(admission.key, admission.peak_gib, proc.pid)
        try:
            return proc.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            close()
            proc.wait()
            return LAUNCH_TIMED_OUT
        finally:
            close()
            release_spawn(admission.key, proc.pid)
    finally:
        if log is not None:
            sink.close()
        if run_holder is not None:
            shutil.rmtree(run_holder, ignore_errors=True)


#: ``main`` return code for a session never started because the shared deadline was already spent.
_NOT_LAUNCHED = "not-launched"


def _time_left(deadline: float) -> float:
    """Seconds left under the shared ``deadline`` (monotonic); ``<= 0`` means launch nothing."""
    return deadline - time.monotonic()


def _run_session(argv: list[str], run, root: Path, log: Path, deadline: float, append: bool):
    """``(code, error)`` for one session under the shared ``deadline`` (monotonic): ``_NOT_LAUNCHED``
    when no time remains, ``(None, message)`` when the launcher could not start. ``argv`` leads so a
    caller's per-session splice of its group is the first positional argument."""
    if _time_left(deadline) <= 0:
        return _NOT_LAUNCHED, None
    try:
        return run(argv, root, log=log, timeout=_time_left(deadline), append=append), None
    except OSError as exc:
        return None, str(exc)


def _strip_collection_roots(root: Path, cmd: str) -> list[str]:
    """``cmd`` split, minus positional tokens naming an existing path under ``root`` and the ``-m``
    marker filter (a named file must run whatever its markers). Other option values and the program
    token stay."""
    tokens = shlex.split(cmd)
    kept: list[str] = []
    prev_takes_value = False
    skip_next = False
    seen_pytest = False
    seen_runner = False
    for i, tok in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue
        if Path(tok).name.lower() in _PYTEST_NAMES:
            seen_pytest = True
        elif seen_pytest and tok == "-m":
            skip_next = True
            continue
        elif seen_pytest and tok.startswith("-m") and not tok.startswith("--"):
            continue
        if i == 0 or prev_takes_value or tok.startswith("-"):
            kept.append(tok)
        elif not seen_pytest and not seen_runner and tok.endswith(".py"):
            seen_runner = True
            kept.append(tok)
        elif not (root / tok.split("::")[0]).exists():
            kept.append(tok)
        prev_takes_value = tok in _VALUE_OPTIONS
    return kept


def dirty_paths(repo_root: Path) -> list[str]:
    """Repo-relative POSIX paths ``git status`` reports modified, added or untracked.
    Raises RuntimeError when git fails."""
    from coordinator_core.session.session_facts import _dirty_paths

    result = _dirty_paths(repo_root)
    if result["degraded"]:
        raise RuntimeError(result["evidence"])
    return sorted(result["value"]["paths"])


def covering_dirty_paths(repo_root: Path) -> list[str]:
    """``dirty_paths`` minus untracked files a test run cannot depend on: an untracked path stays only
    when it is a ``.py`` file outside any dot-directory (a new module the tests may import). Scratch
    trees such as ``.salvage-*`` therefore never enter the ``covered`` hash. Any path ending
    ``.evidence.yaml`` (tracked or new, at any depth) never enters it either: terminal_commit rewrites
    the plan sidecar after the retest. Trap: a test fixture with that suffix is excluded too.
    Raises RuntimeError when git fails."""
    from coordinator_core.session.session_facts import _dirty_paths

    result = _dirty_paths(repo_root)
    if result["degraded"]:
        raise RuntimeError(result["evidence"])
    kept = []
    for entry in result["value"]["entries"]:
        xy, path = entry[0], entry[1]
        if path.endswith(".evidence.yaml"):
            continue
        if xy.strip() == "??" and not (
            path.endswith(".py") and not any(seg.startswith(".") for seg in path.split("/"))
        ):
            continue
        kept.append(path)
    return sorted(set(kept))


def _run_repo(params: dict) -> Optional[str]:
    """The worktree root of the repo the run names: an existing-directory ``repo_root``/``target_root``
    param, else the repo holding an absolute ``script_path``. None when the run names none."""
    for key in ("repo_root", "target_root"):
        named = params.get(key)
        if isinstance(named, str) and named and Path(named).is_dir():
            return show_toplevel(named)
    script = params.get("script_path")
    if isinstance(script, str) and script and Path(script).is_absolute():
        holder = Path(script).parent
        while not holder.is_dir() and holder != holder.parent:
            holder = holder.parent
        return show_toplevel(str(holder))
    return None


def _repo_root(raw: Optional[str], params: Optional[dict] = None) -> Path:
    """``--repo-root`` wins, then the run's own repo, then the cwd's repo."""
    if raw:
        return Path(raw).resolve()
    named = _run_repo(params or {})
    if named is not None:
        return Path(named)
    top = show_toplevel()
    if top is None:
        raise RuntimeError("not in a git repo; pass --repo-root")
    return Path(top)


def _tests_to_run(explicit: Sequence[str], params: dict) -> list[str]:
    tests = [p.replace("\\", "/") for t in explicit for p in re.split(r"[,\s]+", t) if p]
    verdict = landing_targeted_tests.targeted_test_verdict(params)
    return landing_targeted_tests.targeted_set(params, verdict, tests)


def _with_invariants(
    root: Path, tests: list[str], dirty: Callable[[Path], list[str]],
    run_id: str = "", params: Optional[dict] = None,
) -> list[str]:
    """``tests`` plus every repo-wide invariant the landing set selects: the dirty paths, the run's
    landing paths (the run's own artifacts select none), through ``landing_integrity.landing_invariant_nodes``
    (the set the gate owes). A git failure adds none; the caller's own ``dirty`` call reports it."""
    from coordinator_core.ops.dispatch_emit import landing_integrity as li

    try:
        touched = dirty(root)
    except RuntimeError:
        return tests
    if run_id:
        writes, _source = _landing_paths(root, run_id, params or {})
        touched = [*touched, *writes]
    owed = li.landing_invariant_nodes(root, list(dict.fromkeys(touched)), run_id or None)
    return list(dict.fromkeys([*tests, *owed]))


_recorded_targeted_set = landing_targeted_tests.recorded_targeted_set

#: pytest output that marks a collection failure (exit 2), which a ``--tier`` retry cannot fix.
_COLLECTION_FAILURE = re.compile(r"ERROR collecting |errors? during collection", re.IGNORECASE)


def _log_size(log: Path) -> int:
    try:
        return log.stat().st_size
    except OSError:
        return 0


def _collection_failed_since(log: Path, offset: int) -> bool:
    """True when ``log`` past byte ``offset`` shows pytest collection output."""
    try:
        with open(log, "rb") as handle:
            handle.seek(offset)
            return bool(_COLLECTION_FAILURE.search(handle.read().decode("utf-8", errors="replace")))
    except OSError:
        return False


_UNRECOGNIZED_ARGS = re.compile(r"unrecognized arguments:([^\n]*)", re.IGNORECASE)
_OPTION_PLUGINS = {"--timeout": "pytest-timeout", "--timeout-method": "pytest-timeout", "-n": "pytest-xdist",
                   "--numprocesses": "pytest-xdist", "--dist": "pytest-xdist"}


#: Importable top-level module of each plugin ``_OPTION_PLUGINS`` names.
_PLUGIN_MODULES = {"pytest-timeout": "pytest_timeout", "pytest-xdist": "xdist"}
_XDIST_SHORT = re.compile(r"-n(?:\d+|auto|logical)")


def _addopts_text(root: Path) -> str:
    """The ``addopts`` string of the config file pytest selects at ``root``: ``pytest.ini`` always, else the first
    of pyproject.toml (``[tool.pytest.ini_options]``), tox.ini (``[pytest]``), setup.cfg (``[tool:pytest]``) that
    holds its section, even without ``addopts``; ``""`` when none."""
    import configparser

    for name, section in (("pytest.ini", "pytest"), ("pyproject.toml", None), ("tox.ini", "pytest"), ("setup.cfg", "tool:pytest")):
        path = root / name
        try:
            if section is None:
                import tomllib

                table = tomllib.loads(path.read_text(encoding="utf-8")).get("tool", {}).get("pytest", {}).get("ini_options")
                if not isinstance(table, dict):
                    continue
                value = table.get("addopts")
            else:
                parser = configparser.ConfigParser(interpolation=None)
                parser.read_string(path.read_text(encoding="utf-8"))
                if name != "pytest.ini" and not parser.has_section(section):
                    continue
                value = parser.get(section, "addopts", fallback=None)
        except (OSError, ValueError, configparser.Error):
            continue
        if isinstance(value, list):
            value = " ".join(str(v) for v in value)
        return value if isinstance(value, str) else ""
    return ""


def addopts_plugins(root: Path) -> list[str]:
    """Sorted plugin names (``_OPTION_PLUGINS`` values) the repo's own ``addopts`` options need."""
    try:
        tokens = shlex.split(_addopts_text(root))
    except ValueError:
        return []
    return sorted({
        "pytest-xdist" if _XDIST_SHORT.fullmatch(t) else _OPTION_PLUGINS[t.split("=")[0]]
        for t in tokens if _XDIST_SHORT.fullmatch(t) or t.split("=")[0] in _OPTION_PLUGINS
    })


_IMPORT_PROBE = ("import importlib.util, sys\n"
                 "bad = []\n"
                 "for m in sys.argv[1:]:\n"
                 "    try:\n"
                 "        ok = importlib.util.find_spec(m) is not None\n"
                 "    except (ImportError, ValueError):\n"
                 "        ok = False\n"
                 "    if not ok:\n"
                 "        bad.append(m)\n"
                 "print(','.join(bad))\n")
_PROBE_TIMEOUT_S = 15


def _unimportable(interpreter: str, modules: Sequence[str]) -> Optional[list[str]]:
    """The ``modules`` ``interpreter`` cannot import, in order; ``None`` when that interpreter cannot be run.
    This process's own interpreter is answered in-process; any other costs one spawn."""
    import importlib.util

    if interpreter == sys.executable:
        bad = []
        for module in modules:
            try:
                found = importlib.util.find_spec(module) is not None
            except (ImportError, ValueError):
                found = False
            if not found:
                bad.append(module)
        return bad
    try:
        proc = subprocess.run([interpreter, "-c", _IMPORT_PROBE, *modules], capture_output=True, text=True,
                              timeout=_PROBE_TIMEOUT_S, **no_console_creationflags())
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return [m for m in proc.stdout.strip().split(",") if m]


def missing_addopts_plugins(root: Path, interpreter: Optional[str] = None) -> list[str]:
    """The plugins ``addopts_plugins(root)`` names that ``interpreter`` (default: this process's) cannot import;
    no spawn for this process's own interpreter, one for another. An interpreter that cannot be run lacks all.
    Consumed by `test_leg_run` (preflight before any pytest); the retest session path stays post-hoc on
    `_missing_plugin_defect` because its pytest runs in a detached session this module cannot preflight."""
    plugins = addopts_plugins(root)
    bad = _unimportable(interpreter or sys.executable, [_PLUGIN_MODULES[p] for p in plugins])
    if bad is None:
        return plugins
    return [p for p in plugins if _PLUGIN_MODULES[p] in bad]


_KNOWN_INTERPRETER_NAMES = ("python3.14", "python3.13", "python3.12", "python3.11", "python3")


def known_interpreters() -> list[str]:
    """Existing interpreters a leg may borrow, no venv created: ``~/.local/bin`` first, then ``PATH``, newest
    version first; this process's interpreter excluded, duplicates (same real path) dropped."""
    seen = {os.path.realpath(sys.executable)}
    found: list[str] = []
    for name in _KNOWN_INTERPRETER_NAMES:
        for path in (str(Path.home() / ".local" / "bin"), None):
            hit = shutil.which(name, path=path)
            if hit and os.path.realpath(hit) not in seen:
                seen.add(os.path.realpath(hit))
                found.append(hit)
    return found


def interpreter_with_plugins(plugins: Sequence[str], candidates: Optional[Sequence[str]] = None) -> Optional[str]:
    """The first of ``candidates`` (default ``known_interpreters()``) that imports pytest and every plugin in
    ``plugins``; ``None`` when none does."""
    modules = ["pytest", *(_PLUGIN_MODULES[p] for p in plugins)]
    for candidate in (known_interpreters() if candidates is None else candidates):
        if _unimportable(candidate, modules) == []:
            return candidate
    return None


def _declared_spec(root: Path, plugin: str) -> str:
    """The ``plugin`` requirement as ``pyproject.toml`` declares it in a ``[project.optional-dependencies]``
    extra (the pytest-timeout entry with its minimum-version bound), else the bare name."""
    try:
        import tomllib

        extras = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["optional-dependencies"]
        for specs in extras.values():
            for spec in specs:
                if re.split(r"[<>=!~\[; ]", spec, maxsplit=1)[0].lower() == plugin:
                    return spec
    except (OSError, KeyError, TypeError, ValueError):
        pass
    return plugin


def _missing_plugin_defect(log: Path, offset: int, root: Path) -> Optional[str]:
    """A provisioning-defect message when the session's output past ``offset`` shows pytest rejecting an
    option owned by a plugin that is not installed (exit 4, ``unrecognized arguments``); None otherwise."""
    try:
        with open(log, "rb") as handle:
            handle.seek(offset)
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    match = _UNRECOGNIZED_ARGS.search(text)
    if not match:
        return None
    plugins = sorted({_OPTION_PLUGINS[t.split("=")[0]] for t in match.group(1).split() if t.split("=")[0] in _OPTION_PLUGINS})
    if not plugins:
        return None
    specs = " ".join(f'"{_declared_spec(root, p)}"' for p in plugins)
    return (f"provisioning defect, not failed tests: pytest plugin(s) {', '.join(plugins)} not installed "
            f"(pytest rejected{match.group(1).rstrip()}); install with: {sys.executable} -m pip install {specs}")


def _scope_of(root: Path, test: str) -> str:
    """Nearest ancestor directory of the test file, inside ``root``, holding a ``conftest.py`` (repo-relative
    POSIX; ``""`` when none). One pytest session per scope keeps a bare ``from conftest import`` on its own."""
    rel = PurePosixPath(test.split("::")[0])
    for parent in rel.parents:
        if (root / parent / "conftest.py").is_file():
            return "" if str(parent) == "." else str(parent)
    return ""


def _drop_tier_options(tokens: list[str]) -> list[str]:
    """``tokens`` minus ``--tier X`` / ``--tier=X``: a runner's tier selection adds that tier's default
    collection to the named files."""
    out: list[str] = []
    skip = False
    for tok in tokens:
        if skip:
            skip = False
        elif tok == "--tier":
            skip = True
        elif not tok.startswith("--tier="):
            out.append(tok)
    return out


#: Named targets per pytest session. Measured: 277 nodes under ``-n 4`` peaked 574 MiB tree RSS.
RETEST_MAX_NODES = 100

#: Worker ceiling per session; 3 workers at the ~0.5 GiB/worker seen under a -n 7 engine run stay under
#: 2 GiB per invocation.
RETEST_MAX_WORKERS = 3


def worker_cap() -> int:
    """``min(physical_cores / 2, usable_RAM_GB * 1024 / 150MB, RETEST_MAX_WORKERS)``, at least 1."""
    import psutil

    cores = (psutil.cpu_count(logical=False) or 2) // 2
    avail = host_sampler.avail_mb()[0]
    ram = int(avail / 150) if avail is not None else RETEST_MAX_WORKERS
    return max(1, min(cores, ram, RETEST_MAX_WORKERS))


def _drop_marker_filter(tokens: list[str]) -> list[str]:
    """``tokens`` minus a ``-m <expr>`` / ``-m<expr>`` marker filter: every id the tier names runs whatever
    its markers (the declared-launcher arm; ``_strip_collection_roots`` does the same for ``fast_test_cmd``)."""
    out: list[str] = []
    skip = False
    for tok in tokens:
        if skip:
            skip = False
        elif tok == "-m":
            skip = True
        elif not (tok.startswith("-m") and not tok.startswith("--")):
            out.append(tok)
    return out


def _cap_workers(tokens: list[str], cap: int) -> list[str]:
    """``tokens`` with any ``-n``/``--numprocesses`` count clamped to ``cap`` (``auto`` becomes ``cap``)
    and ``--maxprocesses`` dropped; a command naming no worker option is unchanged."""
    out: list[str] = []
    skip = False
    for i, tok in enumerate(tokens):
        if skip:
            skip = False
        elif tok in ("-n", "--numprocesses") and i + 1 < len(tokens):
            out += [tok, _clamp(tokens[i + 1], cap)]
            skip = True
        elif tok.startswith("--numprocesses="):
            out.append("--numprocesses=" + _clamp(tok.split("=", 1)[1], cap))
        elif re.fullmatch(r"-n\w+", tok):
            out.append("-n" + _clamp(tok[2:], cap))
        elif tok == "--maxprocesses":
            skip = True
        elif not tok.startswith("--maxprocesses="):
            out.append(tok)
    return out


def _clamp(value: str, cap: int) -> str:
    return str(min(int(value), cap)) if value.isdigit() else str(cap)


def _chunks(group: list[str], size: int = RETEST_MAX_NODES) -> list[list[str]]:
    return [group[i:i + size] for i in range(0, len(group), size)] or [group]


def _scopes_conflict(root: Path, scopes: dict[str, list[str]]) -> bool:
    """True when two or more scopes each carry their own ``conftest.py`` (a bare ``from conftest import``
    would collide in one session)."""
    return sum(1 for s in scopes if (root / s / "conftest.py").is_file()) > 1


def _config_root(root: Path) -> Path:
    """The directory whose ``coordinator.local.md`` configures the test command for a pass at ``root``:
    ``root`` itself when it holds one, else the repo owning an isolated export
    (``<repo>/scratch/warp/<run>/export*``) when that repo does, else ``root``."""
    if (root / "coordinator.local.md").is_file():
        return root
    parts = root.parts
    for i in range(len(parts) - 3, 0, -1):
        if parts[i] == "scratch" and parts[i + 1] == "warp":
            repo = Path(*parts[:i])
            if (repo / "coordinator.local.md").is_file():
                return repo
            break
    return root


def _launch_plan(root: Path, tests: list[str], *, keep_tier: bool = False) -> "tuple[list[str], list[list[str]]] | str":
    """``(base, groups)``: the command prefix and the test groups it runs, one session per group. Both
    arms share one grouping: every node id in one group (input order) unless the scopes' conftests
    collide, then one group per scope; each group is cut into sessions of at most ``RETEST_MAX_NODES``
    ids and the command's worker count is clamped to ``worker_cap()``. A declared
    ``targeted_test_launcher`` (pinned interpreter, else this one) supplies the prefix, else
    ``fast_test_cmd`` minus its collection roots and markers; its
    ``--tier`` option is dropped unless
    ``keep_tier`` (the fallback when a runner needs it). A string return is the reason no command could
    be built."""
    from coordinator_core import resolve_validation_cmd
    from coordinator_core.ops.dispatch_emit import interpreter_pin as ti

    scopes: dict[str, list[str]] = {}
    for t in tests:
        scopes.setdefault(_scope_of(root, t), []).append(t)
    groups = list(scopes.values()) if _scopes_conflict(root, scopes) else [tests]
    groups = [chunk for group in groups for chunk in _chunks(group)]
    cap = worker_cap()

    config = _config_root(root)
    declared = resolve_validation_cmd.read_local_md_key(str(config), ti.LAUNCHER_KEY).strip()
    if declared:
        script = declared.split()[0]
        if Path(script).is_absolute() or ".." in re.split(r"[\/]", script) or not (root / script).is_file():
            return f"{ti.LAUNCHER_KEY} {declared!r} is not a repo-relative file that exists"
        pin = ti.resolve_test_runner_python(root)
        python = pin.command if pin.command and pin.defect is None else sys.executable
        return [python, *_cap_workers(_drop_marker_filter(shlex.split(declared)), cap)], groups
    resolved = resolve_validation_cmd.resolve_fast_test_cmd(str(config))
    if resolved.returncode != 0:
        return f"fast_test_cmd unresolved (read {config.as_posix()}/coordinator.local.md): {resolved.stderr.strip()}"
    base = _cap_workers(_strip_collection_roots(root, resolved.stdout.strip()), cap)
    return (base if keep_tier else _drop_tier_options(base)), groups


def _write_stale(
    root: Path, run_id: str, dirty: Callable[[Path], list[str]], head_sha: Callable, params: dict,
) -> int:
    """Write a ``stale`` verdict (non-blocking to the landing gate) with a named note when no
    targeted set is recorded and no ``--tests`` was given."""
    try:
        covered_paths, covered_source = _covered_set(root, run_id, params, dirty, [])
    except RuntimeError as exc:
        print(f"retest_targeted: {exc}", file=sys.stderr)
        return 2
    note = "no targeted test set recorded for this run and no --tests given; recorded failure treated as stale"
    record = {
        "run_id": run_id,
        "head": head_sha(root),
        "tests": [],
        "covered": landing_retest.landing_fingerprint(root, covered_paths),
        "covered_source": covered_source,
        "status": "stale",
        "note": note,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    path = landing_retest.fresh_verdict_path(root, run_id)
    kept = _read_record(path)
    if kept and kept.get("status") in ("pass", "fail", NO_TESTS_STATUS):
        print(f"{path} {kept['status']}: kept; a stale verdict never replaces a measured one")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2), encoding="utf-8", newline="\n")
    print(f"{path} stale: {note}")
    return 0


def _write_no_tests(root, path, run_id, dropped, note, dirty, head_sha, params) -> int:
    """Record ``no-tests`` (tests_run 0, non-blocking, not ``error``) when every named target was
    dropped as nonexistent. Nothing is launched."""
    try:
        covered_paths, covered_source = _covered_set(root, run_id, params, dirty, [])
    except RuntimeError as exc:
        print(f"retest_targeted: {exc}", file=sys.stderr)
        return 2
    reason = note or "no tests to run"
    _write_record(path, {
        "run_id": run_id, "head": head_sha(root), "tests": [], "tests_run": 0, "dropped": dropped,
        "covered": landing_retest.landing_fingerprint(root, covered_paths), "covered_source": covered_source,
        "status": NO_TESTS_STATUS, "note": reason, "at": datetime.now(timezone.utc).isoformat(),
        landing_targeted_tests.ZERO_RUN_CAUSE_KEY: landing_targeted_tests.ZERO_RUN_EMPTY,
    })
    print(f"{path} {NO_TESTS_STATUS}: {reason}")
    return 0


def _normalise_node_id(raw: str, rootdir: Path, root: Path) -> Optional[str]:
    """``raw`` (a pytest node id whose file part is relative to ``rootdir``) as a ``root``-relative POSIX
    id; None when its file lands outside ``root``. Pure path arithmetic, no suffix matching."""
    file_part, sep, rest = raw.partition("::")
    try:
        rel = Path(os.path.normpath(rootdir / file_part)).relative_to(os.path.normpath(root))
    except ValueError:
        return None
    return rel.as_posix() + sep + rest


def _failed_node_ids(log: Path, root: Path) -> list[str]:
    """``root``-relative node ids from pytest ``FAILED``/``ERROR`` summary lines in ``log`` (the short
    summary prints them relative to the invocation dir, which is ``root``); a parametrised id keeps its
    spaces. Trap: an id containing `` - `` is cut there, the summary's message separator. An id outside
    ``root`` stays raw: it never equals an in-root HEAD-red id, but two raw ids with the same text
    (e.g. ``../x.py::t``) match each other."""
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found = re.findall(r"^(?:FAILED|ERROR) (\S+::.+?)(?: - .*)?$", text, flags=re.MULTILINE)
    ids = [f.rstrip() for f in found]
    return list(dict.fromkeys(_normalise_node_id(n, root, root) or n for n in ids))


_COLLECTED_ID = re.compile(r"^(\S+?\.py::.*\S)\s*$", re.MULTILINE)
#: pytest's header is one line, ``rootdir: <dir>, configfile: pytest.ini, testpaths: ...``.
_ROOTDIR_LINE = re.compile(r"^rootdir: (.+?)(?:, (?:configfile|inifile|testpaths|plugins): .*)?\s*$", re.MULTILINE)

_QUIET_FLAGS = frozenset({"-q", "-qq", "--quiet"})

#: ``_collect_ids`` reason for a session that exits other than 0/5/2-with-errors or cannot be built.
_COLLECT_UNAVAILABLE = "collection unavailable"
_COLLECT_ERROR = re.compile(r"ERROR collecting (\S+\.py)")
#: pytest's closing line, ``3 passed, 1 failed, 2 skipped in 0.4s`` (optionally ``=``-framed).
_SUMMARY_LINE = re.compile(r"^\W*((?:\d+ [a-z]+(?:, )?)+) in [\d.]+s", re.MULTILINE)
_RAN_KINDS = frozenset({"passed", "failed", "error", "errors", "skipped", "xfailed", "xpassed"})


def _collect_ids(
    run, root: Path, tests: list[str], log: Path, deadline: float, marker: Optional[str] = None,
) -> "tuple[set[str], list[str]] | str":
    """``(ids, errored_files)``: the ``root``-relative node ids ``--collect-only`` finds for ``tests`` under
    ``root``, and the ``root``-relative files with a collection ERROR; a str reason when a session is
    broken or the printed ids cannot be mapped. ``-q`` collect-only prints no header, so each session is
    pinned with ``--rootdir=<root>`` and its ids come out ``root``-relative; a ``rootdir:`` line in the
    log, when present, overrides that base. Fail closed: more than one distinct rootdir, one outside
    ``root``, or an id whose file is not under ``root`` is a reason. Exit 2 is accepted only when the log
    names the ``ERROR collecting`` file."""
    plan = _launch_plan(root, tests)
    if isinstance(plan, str):
        return plan
    base, groups = plan
    quiet_free = [t for t in base if t not in _QUIET_FLAGS]  # a second -q prints ``path: N``, not node ids
    if marker is not None:
        quiet_free += ["-m", marker]
    log.write_bytes(b"")
    rcs: list = []
    for n, group in enumerate(groups):
        rc, err = _run_session(
            [*quiet_free, *group, "--collect-only", "-q", f"--rootdir={root}"], run, root, log, deadline, n > 0,
        )
        if err is not None or rc not in (0, NO_TESTS_COLLECTED, 2):
            return _COLLECT_UNAVAILABLE
        rcs.append(rc)
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _COLLECT_UNAVAILABLE
    rootdirs = set(_ROOTDIR_LINE.findall(text))
    if len(rootdirs) > 1:
        return "collect logs name more than one rootdir"
    rootdir = Path(next(iter(rootdirs))) if rootdirs else root
    errored = list(dict.fromkeys(
        (_normalise_node_id(f, rootdir, root) or f) for f in _COLLECT_ERROR.findall(text)
    ))
    if 2 in rcs and not errored:
        return _COLLECT_UNAVAILABLE
    ids: set[str] = set()
    for raw in _COLLECTED_ID.findall(text):
        rel = _normalise_node_id(raw, rootdir, root)
        if rel is None:
            return f"collect rootdir {rootdir.as_posix()} resolves outside {root.as_posix()}"
        if not (root / rel.split("::")[0]).is_file():
            return f"collected id {raw!r} does not resolve to a file under {root.as_posix()} (rootdir missing or wrong)"
        ids.add(rel)
    return ids, errored


def _ran_total(log: Path) -> Optional[int]:
    """Sum of passed+failed+error+skipped+xfailed+xpassed over every pytest summary line in ``log``;
    None when the log has no summary line."""
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    total, seen = 0, False
    for body in _SUMMARY_LINE.findall(text):
        for count, kind in re.findall(r"(\d+) ([a-z]+)", body):
            if kind in _RAN_KINDS:
                total += int(count)
                seen = True
    return total if seen else None


def _deselected_total(log: Path, offset: int = 0) -> int:
    """Sum of the ``N deselected`` counts over every pytest summary line in ``log`` past byte ``offset``;
    0 when none."""
    try:
        with open(log, "rb") as handle:
            handle.seek(offset)
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return 0
    return sum(
        int(count) for body in _SUMMARY_LINE.findall(text)
        for count, kind in re.findall(r"(\d+) ([a-z]+)", body) if kind == "deselected"
    )


def _zero_ran_owned(
    run, root: Path, tests: list[str], log: Path, deadline: float, marker: Optional[str] = None,
) -> list[str]:
    """The named node ids in ``tests`` that ran 0 times. Only a log reporting deselection triggers a
    ``--collect-only`` session (under ``-m marker`` when a marker re-run happened); an id with no
    collected match (itself, ``id[...]`` or ``id::...``) was deselected. Fail closed: when collection is
    unavailable every named node id is unproven."""
    owned = [t for t in tests if "::" in t or t.endswith(".py")]
    if not owned or not _deselected_total(log):
        return []
    collected = _collect_ids(run, root, tests, log.with_suffix(".collect.log"), deadline, marker)
    if isinstance(collected, str):
        return owned
    ids = collected[0]
    return [t for t in owned if not any(i == t or i.startswith((t + "[", t + "::")) for i in ids)]


def _read_record(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def retest_abandoned(record: dict) -> bool:
    """True when a ``running`` record's ``deadline`` (child timeout plus grace) has passed; an
    unparseable one counts as past. The one deadline rule for every reader of the record."""
    try:
        return datetime.now(timezone.utc) >= datetime.fromisoformat(str(record["deadline"]))
    except (KeyError, ValueError, TypeError):
        return True


def _write_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2), encoding="utf-8", newline="\n")


def _input_pair(args: argparse.Namespace) -> tuple[str, str]:
    """``(flag, value)`` of the params input the caller gave: ``--params-file`` or ``--from-task-output``."""
    if args.params_file:
        return "--params-file", args.params_file
    return "--from-task-output", args.from_task_output


def _load_params(args: argparse.Namespace) -> dict:
    """The run's landing params from whichever input was given. A ``--params-file`` is the commit stage's
    raw ``_nextAction.params`` JSON; one carrying ``task_output_path`` is expanded as ``land_run`` does
    (task-output fields first, the file's keys win). Raises ValueError naming the file."""
    from coordinator_core.ops.dispatch_emit.land_run import params_from_task_output
    from coordinator_core.ops.dispatch_emit.terminal_commit_params_channel import missing_file_diagnosis

    if not args.params_file:
        return params_from_task_output(args.from_task_output)
    try:
        data = json.loads(Path(args.params_file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(missing_file_diagnosis(args.params_file) or f"cannot read params file {args.params_file!r}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"params file {args.params_file!r} does not hold a JSON object")
    if isinstance(data.get("task_output_path"), str):
        return {**params_from_task_output(data["task_output_path"]), **{k: v for k, v in data.items() if k != "task_output_path"}}
    return data


def _poll_command(flag: str, value: str) -> str:
    return f"{landing_targeted_tests.RETEST_LAUNCHER} --poll {flag} {json.dumps(value) if ' ' in value else value}"


def _pid_alive(pid: object) -> bool:
    """True when ``pid`` names a live process; a missing or malformed pid reads dead."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    import psutil

    return psutil.pid_exists(pid)


def _stamp_pid(path: Path) -> None:
    """The detached child records its own pid in the ``running`` record, so a poll can tell a dead child."""
    record = _read_record(path)
    if record and record.get("status") == landing_retest.RETEST_RUNNING:
        record["pid"] = os.getpid()
        _write_record(path, record)


def _seal_if_running(path: Path, run_id: str, tests: list[str], reason: str) -> None:
    """Replace a record still ``running`` with a terminal ``error`` carrying ``reason`` (the child's last act)."""
    record = _read_record(path)
    if record is None or record.get("status") != landing_retest.RETEST_RUNNING:
        return
    sealed = {"run_id": run_id, "status": "error", "tests": tests, "reason": reason,
              "at": datetime.now(timezone.utc).isoformat()}
    if record.get("log"):
        sealed["log"] = record["log"]
    _write_record(path, sealed)


#: Record key mapping each failing node id to ``caused``, ``foreign`` or ``pre_existing``.
NODE_VERDICTS_KEY = "node_verdicts"


def _head_red_ids(run, root: Path, ids: Sequence[str], log: Path, deadline: float) -> set[str]:
    """Of ``ids``, those red at a pure-HEAD export of ``root``. Any build or measure failure returns the empty
    set, so every id reads ``foreign`` (not ``pre_existing``) in ``_classify_reds``: still non-blocking."""
    from coordinator_core.ops.dispatch_emit import landing_export as le
    from coordinator_core.testing import baseline_export

    head_root = le.run_export_root(root, log.stem).with_name("export-head")
    try:
        le.build_head_export(root, head_root, [], None)
        measured = _measure(run, head_root, list(ids), log.with_suffix(".head.log"), deadline)
        return set(measured[1]) if measured else set()
    except (le.LandingExportError, RuntimeError, OSError):
        return set()
    finally:
        baseline_export.remove(head_root)


def _classify_reds(
    root: Path, run_id: str, params: dict, log: Path, ids: Sequence[str],
    head_red: Optional[Callable[[Sequence[str]], set[str]]] = None,
) -> dict:
    """``{"baseline": ..., NODE_VERDICTS_KEY: ...}`` for the failing ``ids`` of a non-isolated retest, or ``{}``
    when no verdict can be told. A node whose failure text names none of the run's landing paths and whose file
    the run did not write is not the run's: ``pre_existing`` when ``head_red`` finds it red at HEAD, else
    ``foreign`` (``undeclared-dirt``). Both are evidenced, so the landing reads them non-blocking; the rest stay
    ``caused``. No landing path set (nothing to attribute against) classifies nothing."""
    writes, _source = _landing_paths(root, run_id, params)
    if not ids or not writes:
        return {}
    own = _names_own_path(log, ids, writes)
    other = [i for i in ids if i not in own]
    if not other:
        return {}
    at_head = head_red(other) if head_red is not None else set()
    pre_existing = [i for i in other if i in at_head]
    foreign = [i for i in other if i not in at_head]
    caused = [i for i in ids if i in own]
    verdicts = {**{i: "caused" for i in caused}, **{i: "foreign" for i in foreign},
                **{i: "pre_existing" for i in pre_existing}}
    baseline = {
        "method": "retest-log", "caused": caused, "foreign": foreign, "pre_existing": pre_existing,
        "foreign_reason": {i: landing_targeted_tests.UNDECLARED_DIRT for i in foreign},
        "evidence": sorted([*foreign, *pre_existing]),
    }
    return {"baseline": baseline, NODE_VERDICTS_KEY: verdicts}


def _summary_failures(log: Path) -> int:
    """Failed plus errored counts over every pytest summary line of ``log``."""
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return sum(
        int(n) for body in _SUMMARY_LINE.findall(text)
        for n, kind in re.findall(r"(\d+) ([a-z]+)", body) if kind in ("failed", "error", "errors")
    )


def _ran_count(log: Path) -> Optional[int]:
    """``_ran_total``, with pytest's ``no tests ran in Ns`` closing line read as 0; None with no closing line."""
    ran = _ran_total(log)
    if ran is not None:
        return ran
    try:
        return 0 if re.search(r"^\W*no tests ran in [\d.]+s", log.read_text(encoding="utf-8", errors="replace"), re.MULTILINE) else None
    except OSError:
        return None


def _empty_chunks(log: Path) -> int:
    """Count of pytest ``no tests ran in Ns`` closing lines in ``log``: sessions that collected 0 items."""
    try:
        return len(re.findall(r"^\W*no tests ran in [\d.]+s", log.read_text(encoding="utf-8", errors="replace"), re.MULTILINE))
    except OSError:
        return 0


def _log_complete(log: Path) -> bool:
    """True when ``log`` holds a pytest closing summary line."""
    return _ran_count(log) is not None


def _finalise_from_log(path: Path, record: dict, root: Path, run_id: str, params: dict) -> Optional[int]:
    """Replace a ``running`` ``record`` whose child exited or died with a terminal verdict read from its log
    (per-node ids from the FAILED/ERROR lines; no JUnit file is written by the launcher). ``fail`` for any
    failing node, ``pass`` for a log that ran tests, ``error`` for no summary (child died) or zero items ran
    (typed ``test_infra_failure``, never a pass). Returns the poll exit code, or None when the record was
    already terminal or cannot be finalised (git unreadable)."""
    from coordinator_core.git.git_state import head_sha

    log = Path(str(record.get("log") or path.with_suffix(".log")))
    tests = [str(t) for t in record.get("tests") or []]
    ran = _ran_count(log)
    ids = _failed_node_ids(log, root)
    status, reason = "pass", ""
    if ran is None:
        status, reason = "error", "retest child exited without a pytest summary (died before finishing)"
    elif ids or _summary_failures(log):
        status = "fail"
    elif ran == 0:
        status, reason = "error", "retest ran 0 items"
    elif empty := _empty_chunks(log):
        status, reason = "error", f"{empty} retest chunk(s) collected 0 items (test_infra_failure)"
    try:
        covered, covered_source = _covered_set(root, run_id, params, covering_dirty_paths, [t.split("::")[0] for t in tests])
    except RuntimeError:
        return None
    out = {
        "run_id": run_id, "head": head_sha(root), "tests": tests,
        "covered": landing_retest.landing_fingerprint(root, covered), "covered_source": covered_source,
        "status": status, "at": datetime.now(timezone.utc).isoformat(), "log": str(log),
        "finalised_by": "poll",
    }
    if reason:
        out["reason"] = reason
    if status == "error" and (ran == 0 or ran is not None and reason.endswith("(test_infra_failure)")):
        from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov

        out[landing_targeted_tests.ZERO_RUN_CAUSE_KEY] = cov.ZERO_RUN_TEST_INFRA
    if status == "fail":
        out[landing_retest.FAILED_TESTS_PARAM] = ids
        out.update(_classify_reds(root, run_id, params, log, ids))
    latest = _read_record(path)
    if latest is not None and latest.get("status") != landing_retest.RETEST_RUNNING:
        return None
    _write_record(path, out)
    print(f"{path} {status}" + (f": {reason}" if reason else ""))
    return {"pass": 0, "fail": 1}.get(status, 2)


def _poll(
    path: Path, flag: str, value: str, root: Optional[Path] = None, run_id: str = "",
    params: Optional[dict] = None,
) -> int:
    """Print the record's status line; 0 pass, 1 fail, 2 error/stale/absent/abandoned, 3 running. A
    ``running`` record whose child pid is dead, or whose log is complete with no live child, is finalised
    from the log first (``root`` given)."""
    record = _read_record(path)
    if record is None:
        print(f"{path} absent: no retest record; start one with {landing_targeted_tests.RETEST_LAUNCHER} --tests <files>")
        return 2
    status = record.get("status")
    if status == landing_retest.RETEST_RUNNING and root is not None and not _pid_alive(record.get("pid")):
        log = Path(str(record.get("log") or path.with_suffix(".log")))
        if record.get("pid") is not None or _log_complete(log):
            code = _finalise_from_log(path, record, root, run_id, params or {})
            if code is not None:
                return code
            record = _read_record(path) or record
            status = record.get("status")
    if status == landing_retest.RETEST_RUNNING:
        if retest_abandoned(record):
            print(f"{path} {landing_retest.RETEST_ABANDONED}: retest child abandoned past deadline")
            return 2
        print(f"{path} running since {record.get('at')}; poll: {_poll_command(flag, value)}")
        return 3
    reason = record.get("reason") or record.get("note")
    print(f"{path} {status}" + (f": {reason}" if reason else ""))
    return {"pass": 0, "fail": 1}.get(status, 2)


def _start_detached(
    args: argparse.Namespace, root: Path, path: Path, log_path: Path, run_id: str, tests: list[str],
    spawn: Callable[..., bool], dropped: Sequence[str] = (),
) -> int:
    """Warm path: write the ``running`` record, spawn the child, return. Launches no tests and reads
    no interpreter pin, so it stays inside the warm budget."""
    flag, value = _input_pair(args)
    poll = _poll_command(flag, value)
    existing = _read_record(path)
    if existing and existing.get("status") == landing_retest.RETEST_RUNNING and not retest_abandoned(existing):
        print(f"{path} running since {existing.get('at')}; poll: {poll}")
        return 3
    started = datetime.now(timezone.utc)
    _write_record(path, {
        "run_id": run_id,
        "status": landing_retest.RETEST_RUNNING,
        "tests": tests,
        "log": str(log_path),
        "at": started.isoformat(),
        "deadline": (started + timedelta(seconds=RETEST_CHILD_TIMEOUT_SECS + _DEADLINE_GRACE_SECS)).isoformat(),
    })
    engine = Path(__file__).resolve().parents[3]
    child_argv = [
        flag, str(Path(value).resolve()),
        "--repo-root", str(root),
        *[x for t in tests for x in ("--tests", t)],
        *[x for t in dropped for x in ("--dropped", t)],
        *(["--isolated-export"] if args.isolated_export else []),
        "--detached-child",
    ]
    if not spawn(str(engine), str(Path(__file__).resolve()), child_argv, site_free=True):
        _write_record(path, {
            "run_id": run_id, "status": "error", "tests": tests,
            "reason": "retest child failed to spawn (see state/housekeeping-failures.log)",
            "at": datetime.now(timezone.utc).isoformat(),
        })
        print(f"{path} error: retest child failed to spawn", file=sys.stderr)
        return 2
    print(f"{path} running; poll: {poll}")
    return 0


def _zero_run_cause(status: str, codes: list, log: Path, log_used: bool) -> Optional[str]:
    """Why a pass executed zero tests, or None when it ran some or failed for another reason: a
    ``no-tests`` pass is an empty selection; an ``error`` pass with no session that finished (exit 0/1)
    is budget exhaustion (session not launched or killed at the timeout) or a pytest collection failure."""
    if status == NO_TESTS_STATUS:
        return landing_targeted_tests.ZERO_RUN_EMPTY
    if status == "error" and NO_TESTS_COLLECTED in codes and (
        all(c == NO_TESTS_COLLECTED for c in codes) or any(c in (0, 1) for c in codes)
    ):
        from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov

        return cov.ZERO_RUN_TEST_INFRA
    if status != "error" or any(c in (0, 1) for c in codes):
        return None
    if any(c in (_NOT_LAUNCHED, LAUNCH_TIMED_OUT) for c in codes):
        return landing_targeted_tests.ZERO_RUN_BUDGET
    if log_used and 2 in codes and _collection_failed_since(log, 0):
        return landing_targeted_tests.ZERO_RUN_COLLECTION
    return None


#: Launcher exits typed ``test_infra_failure`` when no test ran: 4 is pytest's usage error (a launcher base
#: that is not a git sha, a bad argv); 120 is CPython's shutdown flush failure.
INFRA_EXITS = (4, 120)

#: ``{cwd, argv, rc, detail}`` of every INFRA_EXITS session ``_run_pass`` saw in this process.
_INFRA_SESSIONS: list[dict] = []

def __getattr__(name: str):
    """``TEST_INFRA_RETRY_LIMIT``: re-runs of a pass whose outcome is a 0-test ``test_infra_failure``, the one
    limit ``clean_overlay_verify`` holds for this module and ``test_leg_run``; a second such outcome is reported."""
    if name == "TEST_INFRA_RETRY_LIMIT":
        from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov

        return cov.TEST_INFRA_RETRY_LIMIT
    raise AttributeError(name)

#: ``str(log)`` -> ``{limit, first_reason}`` of every pass ``_run_pass_retrying`` re-ran in this process.
_RETRIED: dict[str, dict] = {}


def _infra_exit_detail(rc: int, log: Path, offset: int) -> str:
    """The cause named in the reason of an INFRA_EXITS session. Exit 120 reads the session's log tail
    for the stream failure (cp1252 encode vs a pipe closed early). Exit 4 names the session's own first setup
    or conftest-load error when its log holds one (a usage error is not always a bad launcher base)."""
    try:
        with log.open("rb") as fh:
            fh.seek(max(offset, 0))
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        text = ""
    if rc == 4:
        from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov

        named = cov.first_setup_error(text)
        return f"test_infra_failure: pytest usage error, {named}" if named else (
            "test_infra_failure: pytest usage error, launcher base or argv unusable"
        )
    low = text.lower()
    if "unicodeencodeerror" in low or "cp1252" in low or "charmap" in low:
        stream = "cp1252 encode failure"
    elif "brokenpipeerror" in low or "broken pipe" in low or "errno 32" in low or "closed file" in low:
        stream = "pipe closed early"
    else:
        stream = "stream cause unrecorded (pipe closed early or cp1252)"
    return f"test_infra_failure: shutdown flush failure, {stream}"


def _stamp_infra_exits(record: dict, codes: list, cwd: Path) -> None:
    """For a pass with no finished session (no exit 0/1) holding an INFRA_EXITS session of ``cwd``: type the
    record ``test_infra_failure`` and stamp the session's ``argv`` and ``cwd`` as ``infra_session``."""
    if record.get(landing_targeted_tests.ZERO_RUN_CAUSE_KEY) or any(c in (0, 1) for c in codes):
        return
    from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov

    mine = [s for s in _INFRA_SESSIONS if s["cwd"] == str(cwd)]
    if mine:
        record[landing_targeted_tests.ZERO_RUN_CAUSE_KEY] = cov.ZERO_RUN_TEST_INFRA
        record["infra_session"] = mine[0]


def _stamp_session_facts(record: dict, log: Path, log_used: bool, codes: list, cwd: Optional[Path] = None) -> None:
    """Stamp the launched ``argv``; type an INFRA_EXITS pass; and, when a pytest session of ``log`` finished
    (exit 1) having completed no test and reported only setup errors, ``zero_run_cause`` ``test_infra_failure``
    plus the first setup error's text as ``first_setup_error`` (and as ``reason`` when the record has none)."""
    record["argv"] = list(_LAUNCHED_ARGV)
    if cwd is not None:
        _stamp_infra_exits(record, codes, cwd)
    if not (log_used and 1 in codes) or record.get(landing_targeted_tests.ZERO_RUN_CAUSE_KEY):
        return
    from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov

    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    infra = cov.infra_setup_error(text)
    if infra:
        record[landing_targeted_tests.ZERO_RUN_CAUSE_KEY] = cov.ZERO_RUN_TEST_INFRA
        record["first_setup_error"] = infra
        record.setdefault("reason", f"ran zero tests ({cov.ZERO_RUN_TEST_INFRA}): {infra}")


def _is_zero_test_infra(pass_result: "tuple[str, str, list, bool]", log: Path, cwd: Path) -> bool:
    """True when ``pass_result`` would be recorded with ``zero_run_cause`` ``test_infra_failure`` (the record's own
    typing, so the retry and the record never disagree)."""
    from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov

    status, _reason, codes, log_used = pass_result
    probe: dict = {}
    cause = _zero_run_cause(status, codes, log, log_used)
    if cause:
        probe[landing_targeted_tests.ZERO_RUN_CAUSE_KEY] = cause
    _stamp_session_facts(probe, log, log_used, codes, cwd)
    return probe.get(landing_targeted_tests.ZERO_RUN_CAUSE_KEY) == cov.ZERO_RUN_TEST_INFRA


def _is_deterministic_usage_error(root: Path, log: Path) -> bool:
    """True when this pass's only INFRA_EXITS session exited 4 and its log names no setup or conftest error:
    a launcher base or argv the pytest rejects, which a re-run repeats. A named collection or conftest error
    (a peer's file mid-edit) may clear, so it stays retryable."""
    from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov

    mine = [s for s in _INFRA_SESSIONS if s["cwd"] == str(root)]
    if not mine or any(s["rc"] != 4 for s in mine):
        return False
    try:
        return not cov.first_setup_error(log.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return True


def _run_pass_retrying(
    run, root: Path, tests: list[str], log_path: Path, deadline: float, marker: str = "",
) -> "tuple[str, str, list, bool]":
    """``_run_pass``, re-run up to ``TEST_INFRA_RETRY_LIMIT`` times while its outcome is a 0-test
    ``test_infra_failure`` that is not a deterministic usage error, and the shared ``deadline`` has time left. A re-run overwrites ``log_path``; the
    first outcome's reason is kept in ``_RETRIED`` for ``_stamp_retry``."""
    from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov

    limit = cov.TEST_INFRA_RETRY_LIMIT
    result = _run_pass(run, root, tests, log_path, deadline, marker)
    for _ in range(limit):
        if (time.monotonic() >= deadline or not _is_zero_test_infra(result, log_path, root)
                or _is_deterministic_usage_error(root, log_path)):
            break
        entry = _RETRIED.setdefault(str(log_path), {"limit": limit, "first_reason": result[1]})
        _INFRA_SESSIONS[:] = [s for s in _INFRA_SESSIONS if s["cwd"] != str(root)]
        result = _run_pass(run, root, tests, log_path, deadline, marker)
        entry["attempts"] = entry.get("attempts", 0) + 1
    return result


def _stamp_retry(record: dict, log: Path) -> None:
    """``retried: true`` plus the retry limit and the first attempt's reason on ``record`` when ``log``'s pass
    was re-run; ``zero_run_cause`` is stamped by the caller from the final outcome and stays."""
    entry = _RETRIED.get(str(log))
    if entry:
        record["retried"] = True
        record["retry_limit"] = entry["limit"]
        record["retry_first_reason"] = entry["first_reason"]


def _row_marker(params: Mapping) -> str:
    """The ``-m`` expression a plan row names in ``params["test_marker"]``; ``""`` (no marker filter) when none."""
    named = params.get("test_marker")
    return named.strip() if isinstance(named, str) else ""


def _foreign_rootdir(log: Path, offset: int, root: Path) -> bool:
    """True when the session output past ``offset`` names a pytest ``rootdir:`` other than ``root`` (a
    sub-directory config root such as ``coordinator_core/pytest.ini``)."""
    try:
        with open(log, "rb") as handle:
            handle.seek(offset)
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return False
    found = _ROOTDIR_LINE.findall(text)
    return bool(found) and os.path.normcase(os.path.realpath(found[-1])) != os.path.normcase(os.path.realpath(root))


def _absolute_ids(root: Path, group: list[str]) -> list[str]:
    """``group`` with each repo-root-relative file part made absolute: an absolute path collects under any
    rootdir pytest picks, where a relative one is only as good as the invocation dir."""
    out = []
    for t in group:
        file_part, sep, rest = t.partition("::")
        out.append((root / file_part).as_posix() + sep + rest)
    return out


def _run_pass(
    run, root: Path, tests: list[str], log_path: Path, deadline: float, marker: str = "",
) -> "tuple[str, str, list, bool]":
    """One full pass of ``tests`` with cwd ``root`` under the shared ``deadline`` (monotonic):
    ``(status, reason, codes, log_used)``. A session that exits 5 with its log reporting deselection
    (every collected id deselected by an ini ``addopts`` ``-m``) is re-run once with ``-m <marker>``,
    which overrides that filter (``""`` selects every marker); a session collecting nothing stays 5."""
    plan = _launch_plan(root, tests)
    if isinstance(plan, str):
        return "error", plan, [], False
    base, groups = plan
    budget = RETEST_CHILD_TIMEOUT_SECS
    reran = False
    codes: list = []
    failed_cmds: list[str] = []
    broken_msgs: list[str] = []
    kept: "tuple[list[str], list[list[str]]] | str | None" = None  # keep-tier fallback plan, built once
    for n, group in enumerate(groups):
        offset = _log_size(log_path) if n > 0 else 0
        argv = [*base, *group]
        rc, err = _run_session(argv, run, root, log_path, deadline, n > 0)
        if rc == 2 and not _collection_failed_since(log_path, offset):
            if kept is None:
                kept = _launch_plan(root, tests, keep_tier=True)
            if not isinstance(kept, str) and kept[1] == groups and kept[0] != base:
                argv = [*kept[0], *group]
                rc, err = _run_session([*kept[0], *group], run, root, log_path, deadline, True)
        if rc == NO_TESTS_COLLECTED and err is None and _deselected_total(log_path, offset):
            offset = _log_size(log_path)
            argv = [*base, "-m", marker, *group]
            rc, err = _run_session(argv, run, root, log_path, deadline, True)
            reran = True
        if rc == NO_TESTS_COLLECTED and err is None and not _deselected_total(log_path, offset) \
                and _foreign_rootdir(log_path, offset, root):
            offset = _log_size(log_path)
            argv = [*base, *_absolute_ids(root, group)]
            rc, err = _run_session(argv, run, root, log_path, deadline, True)
        codes.append(rc)
        cmd = shlex.join(argv)
        if err is not None:
            broken_msgs.append(f"launcher could not start: {err}: {cmd}")
        elif rc == _NOT_LAUNCHED:
            broken_msgs.append(f"launcher not started, the {budget:g}s budget was spent: {cmd}")
            break
        elif rc == LAUNCH_TIMED_OUT:
            broken_msgs.append(f"launcher exceeded {budget:g}s: {cmd}")
            break
        elif rc == 1:
            failed_cmds.append(cmd)
        elif rc == 4 and (defect := _missing_plugin_defect(log_path, offset, root)):
            broken_msgs.append(f"{defect}: {cmd}")
        elif rc in INFRA_EXITS:
            detail = _infra_exit_detail(rc, log_path, offset)
            _INFRA_SESSIONS.append({"cwd": str(root), "argv": cmd, "rc": rc, "detail": detail})
            broken_msgs.append(f"launcher exited {rc} with no test results ({detail}) in cwd {root}: {cmd}")
        elif rc not in (0, NO_TESTS_COLLECTED):
            broken_msgs.append(f"launcher exited {rc} with no test results (denied, missing or crashed launcher): {cmd}")
    status, reason = "pass", ""
    if broken_msgs:
        status = "error"
        reason = "; ".join(broken_msgs)
        if failed_cmds:
            reason += "; failing session(s): " + ", ".join(failed_cmds)
    elif failed_cmds:
        status = "fail"
    elif all(c == NO_TESTS_COLLECTED for c in codes) and any("::" in t for t in tests) and _foreign_rootdir(log_path, 0, root):
        status = "error"
        reason = f"every retest session collected 0 items from named node id(s) (test_infra_failure): {', '.join(tests)}"
    elif all(c == NO_TESTS_COLLECTED for c in codes):
        status = NO_TESTS_STATUS
        reason = f"launcher collected no tests from: {', '.join(tests)}"
    elif NO_TESTS_COLLECTED in codes:
        status = "error"
        reason = f"{codes.count(NO_TESTS_COLLECTED)} of {len(codes)} retest chunk(s) collected 0 items (test_infra_failure)"
    if status in ("pass", NO_TESTS_STATUS):
        unran = _zero_ran_owned(run, root, tests, log_path, deadline, marker if reran else None)
        if unran:
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write("".join(f"\nFAILED {t} - {ZERO_RAN_MESSAGE}" for t in unran) + "\n")
            codes.append(1)
            status, reason = "fail", f"{ZERO_RAN_MESSAGE}: {', '.join(unran)}"
    return status, reason, codes, True


def _run_write_set(root: Path, params: dict, dirty: Callable[[Path], list[str]]) -> tuple[list[str], str]:
    """``(paths, source)``: the union of the run manifest's ``rows[].writes`` (``"manifest"``), the manifest
    located as ``land_run`` locates it; ``covering_dirty_paths`` (``"covering_dirty_paths"``) when none is
    reachable. Raises RuntimeError when that fallback's git call fails."""
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_DIR_ROOT
    from coordinator_core.ops.dispatch_emit.commit_request import MalformedCommitRequestError, parse_manifest_marker

    script_path = params.get("script_path")
    try:
        script = contained_path(root / script_path, [root]) if isinstance(script_path, str) and script_path else None
        manifest_rel = parse_manifest_marker(script.read_text(encoding="utf-8")) if script else None
        manifest_abs = (
            contained_path(root / manifest_rel, [root / RECORD_DIR_ROOT, root / RUN_DIR_ROOT]) if manifest_rel else None
        )
        data = json.loads(manifest_abs.read_text(encoding="utf-8")) if manifest_abs else None
    except (OSError, ValueError, MalformedCommitRequestError):
        data = None
    rows = data.get("rows") if isinstance(data, dict) else None
    if isinstance(rows, list):
        writes = [w.replace("\\", "/") for r in rows if isinstance(r, dict)
                  for w in r.get("writes") or () if isinstance(w, str) and w]
        return sorted(set(writes)), "manifest"
    return dirty(root), "covering_dirty_paths"


def _covered_set(
    root: Path, run_id: str, params: dict, dirty: Callable[[Path], list[str]], test_files: Sequence[str],
) -> tuple[list[str], str]:
    """``(paths, source)`` a record's ``covered`` fingerprints: the run's landing set (``_landing_paths``)
    plus ``test_files`` and their import closure. The dirty tree enters only when no landing set is
    derivable (source ``covering_dirty_paths``). Raises RuntimeError when that fallback's git call fails."""
    writes, source = _landing_paths(root, run_id, params)
    if not writes:
        writes, source = _run_write_set(root, params, dirty)
    tests = [t.replace("\\", "/") for t in test_files]
    return _outside_own_records(run_id, list(dict.fromkeys([*landing_retest._import_closure(root, tests), *writes]))), source


def _landing_paths(root: Path, run_id: str, params: dict) -> tuple[list[str], str]:
    """``(paths, source)``: the paths the run's terminal commit lands, never the dirty tree. The run
    manifest's ``rows[].writes`` (``"manifest"``), else ``run_write_set`` (``"run_write_set"``); empty
    when neither is readable."""
    writes, source = _run_write_set(root, params, lambda _root: [])
    if source == "manifest":
        return writes, source
    from coordinator_core.ops.dispatch_emit.run_write_set import run_write_set

    try:
        return list(run_write_set(root, run_id, "")), "run_write_set"
    except (ValueError, OSError):
        return [], "run_write_set"


class _ChildInterrupted(BaseException):
    """A handled termination signal unwinding the isolated child to its cleanup."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


def _isolated_child(
    root: Path, path: Path, log_path: Path, run_id: str, params: dict, tests: list[str],
    dropped: list[str], drop_note: str, run, dirty, head_sha,
) -> int:
    """``--isolated-export`` child: sweep orphan exports, then run ``_isolated_body`` with SIGTERM
    (SIGBREAK on Windows) unwinding to cleanup. Every export under ``scratch/warp/<run>`` is removed on
    every exit path; the record keeps the path and log. SIGKILL is left to the next start's sweep."""
    import signal
    import threading

    from coordinator_core.ops.dispatch_emit import export_lifecycle, landing_export as le
    from coordinator_core.testing import baseline_export

    export_root = le.run_export_root(root, run_id)
    export_lifecycle.sweep_orphan_exports(root, keep_run=run_id)
    sigs = baseline_export._handled_signals()
    previous: dict[int, object] = {}
    if threading.current_thread() is threading.main_thread():
        def _on_signal(signum, _frame):
            for s in sigs:
                signal.signal(s, signal.SIG_IGN)
            raise _ChildInterrupted(signum)

        for s in sigs:
            previous[s] = signal.signal(s, _on_signal)
    try:
        return _isolated_body(root, path, log_path, run_id, params, tests, dropped, drop_note, run, dirty, head_sha)
    except _ChildInterrupted as exc:
        reason = f"retest interrupted by signal {exc.signum}"
        _write_record(path, {
            "run_id": run_id, "status": "error", "tests": tests, "reason": reason,
            "isolated_export": {"root": export_root.as_posix()},
            "log": str(path.with_suffix(".export.log")), "live_log": str(log_path),
            "at": datetime.now(timezone.utc).isoformat(),
        })
        print(f"{path} error: {reason}")
        return 2
    finally:
        for s in sigs:
            if s in previous:
                signal.signal(s, signal.SIG_IGN)
        for name in export_lifecycle.EXPORT_DIR_NAMES:
            baseline_export.remove(export_root.with_name(name))
        for s, handler in previous.items():
            signal.signal(s, handler)


def _overlay_sha(root: Path, run_id: str) -> Optional[str]:
    """The run's measured-rev ``overlay_sha`` (declared paths carrying a peer's hunks split down to the run's
    own), or None when the record is absent, unreadable or names none; the export then overlays worktree bytes."""
    from coordinator_core.ops.dispatch_emit.ask_contract import measured_rev_record_path

    try:
        data = json.loads(measured_rev_record_path(root, run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    sha = data.get("overlay_sha") if isinstance(data, dict) else None
    return sha if isinstance(sha, str) and sha else None


def _untracked_at_overlay(root: Path, write_set: Sequence[str], overlay: str) -> list[str]:
    """The ``write_set`` paths present as files in the worktree but absent from ``overlay``'s tree (the run's
    own new files, never committed into the measured rev). Paths tracked at ``overlay`` stay off the list so
    its split blobs are not replaced by peer-carrying worktree bytes. Object-store reads only; no git spawn."""
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.ops.dispatch_emit import landing_export as le

    common = resolve_git_common_dir(root)
    tree = le._commit_tree(common, overlay)
    out: list[str] = []
    for raw in write_set:
        try:
            rel = le._norm_landing(root, raw)
        except le.LandingExportError:
            continue
        if (root / rel).is_file() and not le._tracked_at(common, tree, rel):
            out.append(rel)
    return out


def _resolve_run_base(root: Path, run_id: str, params: dict) -> "tuple[Optional[str], Optional[str]]":
    """``(base, refusal)``: the run's recorded base as a sha the launcher can clone, or ``(raw, reason)`` when
    it cannot. A sha-shaped base passes as is; any other value (a run-suffixed ref) is resolved to its commit
    sha by one ``git rev-parse``; one that names no commit, or leads with ``-``, is refused with the cwd. No
    recorded base is ``(None, None)``: the export then clones HEAD."""
    from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov

    raw = landing_retest.run_base_for(root, run_id, params) or None
    if raw is None or landing_retest._SHA_RE.match(raw):
        return raw, None
    try:
        if raw.startswith("-"):
            raise cov.ExportError(f"{raw!r} is an option, not a ref")
        return cov._commit_sha(root, raw), None
    except (cov.ExportError, OSError) as exc:
        return raw, f"run base {raw!r} is not a git sha and does not resolve to one ({exc}); refusing the launch in cwd {root}"


def _isolated_body(
    root: Path, path: Path, log_path: Path, run_id: str, params: dict, tests: list[str],
    dropped: list[str], drop_note: str, run, dirty, head_sha,
) -> int:
    """Build ``scratch/warp/<run>/export`` (HEAD plus exactly the landing paths, never dirty paths), run
    ``tests`` there then in the live tree under one shared deadline, and record both. Top-level ``status``
    is the export's. A failed export build is ``error``, never a live-only run."""
    from coordinator_core.ops.dispatch_emit import landing_export as le

    _LAUNCHED_ARGV.clear()
    _INFRA_SESSIONS.clear()
    _RETRIED.clear()
    deadline = time.monotonic() + RETEST_CHILD_TIMEOUT_SECS
    path.parent.mkdir(parents=True, exist_ok=True)
    now = lambda: datetime.now(timezone.utc).isoformat()  # noqa: E731

    def _fail(reason: str, meta: dict, cause: Optional[str] = None) -> int:
        record = {
            "run_id": run_id, "status": "error", "tests": tests, "reason": reason,
            "isolated_export": meta, "at": now(),
        }
        if cause:
            record[landing_targeted_tests.ZERO_RUN_CAUSE_KEY] = cause
        _write_record(path, record)
        print(f"{path} error: {reason}")
        return 2

    run_base, refusal = _resolve_run_base(root, run_id, params)
    if refusal is not None:
        from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov

        return _fail(refusal, {"run_base": run_base, "cwd": str(root)}, cov.ZERO_RUN_TEST_INFRA)
    meta: dict = {}
    try:
        write_set, source = _landing_paths(root, run_id, params)
        if not write_set:
            reason = "landing path set not derivable (no readable run record write set)"
            meta = {"files": []}
            _write_record(path, {
                "run_id": run_id, "status": "error", "tests": tests, "reason": reason,
                "baseline_skipped": {"reason": reason, "files": {}},
                "isolated_export": meta, "at": now(),
            })
            print(f"{path} error: {reason}")
            return 2
        export_root = le.run_export_root(root, run_id)
        meta = {"root": export_root.as_posix(), "write_set": write_set, "files": write_set,
                "write_set_source": source}
        overlay = _overlay_sha(root, run_id)
        if overlay:
            meta["overlay_sha"] = overlay
            untracked = _untracked_at_overlay(root, write_set, overlay)
            if untracked:
                meta["untracked_overlaid"] = untracked
            meta["head"] = le.build_head_export(root, export_root, untracked, overlay).head_sha
        else:
            meta["head"] = le.build_head_export(root, export_root, write_set).head_sha
    except RuntimeError as exc:
        return _fail(str(exc), meta)
    except le.LandingExportError as exc:
        return _fail(f"isolated export failed: {exc}", meta)

    export_log = path.with_suffix(".export.log")
    marker = _row_marker(params)
    status, reason, codes, export_log_used = _run_pass_retrying(run, export_root, tests, export_log, deadline, marker)
    live_status, live_reason, live_codes, live_log_used = _run_pass_retrying(run, root, tests, log_path, deadline, marker)
    baseline: dict = {}
    reverify_ids = _reverify_failing_ids(params)
    export_red = status == "fail" and export_log_used and 1 in codes and bool(_failed_node_ids(export_log, export_root))
    if export_red or (status == "pass" and reverify_ids):
        baseline = _head_baseline(
            root, run, tests, export_root, export_log, path.with_suffix(".head.log"), deadline,
            run_base, meta["head"], write_set,
            reverify_ids, export_passed=not export_red,
        )
        if export_red:
            baseline.update(_guard_hits(root, export_root, export_log, path.with_suffix(".head.log"), baseline, write_set))
    return _write_dual(
        root, path, log_path, export_log, run_id, tests, dropped, drop_note, meta, dirty, head_sha,
        (status, reason, codes, export_log_used), (live_status, live_reason, live_codes, live_log_used), _fail,
        baseline,
    )


def _guard_hits(
    root: Path, export_root: Path, export_log: Path, head_log: Path, baseline: Mapping,
    touched: Sequence[str],
) -> dict:
    """``{GUARD_HITS_KEY: {guard: {"tree": {path: n}, "head": {path: n}}}}`` for each declared always-run
    guard red in the export pass, counted from the export log and the HEAD log ``_head_baseline`` already
    wrote (``head_log``, or its ``.node.log`` on the head-node-measure path): no new pytest session. ``{}``
    when no guard is red, or when the baseline was skipped or its HEAD log is absent: the gate then reads
    the guard as unmeasured, never as baseline-only. Trap: a whole-file HEAD log also carries the file's
    other tests' output, so a path those tests name counts on both sides."""
    from coordinator_core.ops.dispatch_emit import always_run_guards as arg

    base = baseline.get("baseline")
    if not isinstance(base, Mapping) or not touched:
        return {}
    failed = _failed_node_ids(export_log, export_root)
    red = [g for g in arg.declared_guards(root) if any(f == g or f.startswith((g + "::", g + "[")) for f in failed)]
    if not red:
        return {}
    log = head_log.with_suffix(".node.log") if base.get("method") == "head-node-measure" else head_log
    try:
        tree_text = export_log.read_text(encoding="utf-8", errors="replace")
        head_text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    tree, head = arg.touched_hits(tree_text, touched), arg.touched_hits(head_text, touched)
    return {arg.GUARD_HITS_KEY: {g: {"tree": tree, "head": head} for g in red}}


def _head_baseline(
    root: Path, run, tests: list[str], export_root: Path, export_log: Path, head_log: Path,
    deadline: float, base_sha: Optional[str] = None, export_sha: Optional[str] = None,
    write_set: Sequence[str] = (), reverify_ids: Sequence[str] = (), export_passed: bool = False,
) -> dict:
    """``{"baseline": {method, base_sha, caused, foreign, evidence}}`` plus the ``_landing_checks`` keys
    (``head_red``, ``owed_cleared``, ``head_sha``); ``export_sha`` the sha of ``export_root``. ``base_sha`` is the sha measured
    (the run base when given, else HEAD); the "HEAD" below reads as that sha. ``caused`` is the export
    pass's failed ids absent from a HEAD-only export run (classify's ``union - base``), per node id; ``foreign`` those
    also red there; ``evidence`` the sorted HEAD-red ids. An id new at HEAD is never HEAD-red, so it is caused when
    red in the export; an id removed since HEAD is ignored. Fail closed with ``{"baseline_skipped":
    {reason, files}}`` (the gate stays fail-closed) when the HEAD export cannot be built, no targeted file
    exists at HEAD, HEAD collection is unavailable, errors in a file (named), or yields 0 items overall or
    for a file present at HEAD (named), the HEAD pass is not pass/fail, or the HEAD run's summary totals
    (passed+failed+error+skipped+xfailed+xpassed, the log carries no per-id results) are below the HEAD
    collected count. Collection runs before the HEAD pass so a skip avoids the costly run. The HEAD export
    is always removed. ``export_passed`` (a green export carrying ``reverify_ids``): no HEAD pass, an empty
    ``caused``/``foreign`` baseline, and only the ``_landing_checks`` measurements of ``reverify_ids``.

    Traps: the ran-vs-collected check is an aggregate lower bound (a teardown error counts twice, a
    shortfall in one session can hide behind an excess in another). A file present at HEAD that collects
    0 for a legitimate reason (module-level skip, platform gate) also skips the baseline. Collect-only
    and the run are separate invocations; verify their counts agree on a live tree before trusting a
    baseline. A node-id target is run at HEAD as its whole file: an id absent at HEAD would make HEAD
    collect-only exit 4 and drop the baseline, un-attributing every run-uncaused red in the file.
    Ratified fail-closed: unbuildable argv, collect/run timeout, zero collected, a named collection
    error, an unresolvable id or rootdir, and a missing summary line each write ``baseline_skipped``."""
    from coordinator_core.ops.dispatch_emit import landing_export as le
    from coordinator_core.testing import baseline_export

    head_root = export_root.with_name("export-head")
    files: dict[str, dict] = {}

    def _skip(reason: str) -> dict:
        return {"baseline_skipped": {"reason": reason, "files": files}}

    def _node_or_skip(reason: str) -> dict:
        """Whole-file HEAD pass unusable: measure only the export-red ids at HEAD. Unmeasured, or no id
        collectable at HEAD, keeps the fail-closed skip."""
        export_reds = _failed_node_ids(export_log, export_root)
        measured = _measure(run, head_root, export_reds, head_log.with_suffix(".node.log"), deadline)
        if measured is None or not measured[0]:
            return _skip(reason)
        _, failed = measured
        baseline = {
            "method": "head-node-measure",
            "base_sha": measured_sha,
            "caused": [n for n in export_reds if n not in failed],
            "foreign": [n for n in export_reds if n in failed],
            "evidence": sorted(failed),
            "skipped_whole_file": reason,
        }
        baseline.update(_landing_checks(
            root, run, baseline, export_root, head_root, head_log, deadline, measured_sha, base_sha, export_sha,
            write_set, reverify_ids,
        ))
        return {"baseline": baseline}

    try:
        measured_sha = le.build_head_export(root, head_root, [], base_sha).head_sha
        if export_passed:
            baseline = {"method": "reverify-export-measure", "base_sha": measured_sha, "caused": [], "foreign": [], "evidence": []}
            baseline.update(_landing_checks(
                root, run, baseline, export_root, head_root, head_log, deadline, measured_sha, base_sha, export_sha,
                write_set, reverify_ids,
            ))
            return {"baseline": baseline}
        head_tests = list(dict.fromkeys(f for f in (t.split("::")[0] for t in tests) if (head_root / f).exists()))
        if not head_tests:
            return _skip("no targeted test file exists at HEAD")
        collected = _collect_ids(run, head_root, head_tests, head_log.with_suffix(".collect.log"), deadline)
        if isinstance(collected, str):
            return _skip(collected)
        head_ids, errored = collected
        for f in dict.fromkeys(t.split("::")[0] for t in head_tests):
            files[f] = {"head": sum(1 for i in head_ids if i.split("::")[0] == f)}
        if errored:
            for f in errored:
                files[f] = {**files.get(f, {"head": 0}), "collection_error": True}
            return _node_or_skip("HEAD collection error in: " + ", ".join(errored))
        if not head_ids:
            return _node_or_skip("HEAD pass collected 0 items")
        empty = [f for f, c in files.items() if not c["head"]]
        if empty:
            return _node_or_skip("HEAD pass collected 0 items in: " + ", ".join(empty))
        status, _, codes, used = _run_pass(run, head_root, head_tests, head_log, deadline)
        if status not in ("pass", "fail", NO_TESTS_STATUS):
            return _node_or_skip(f"HEAD pass {status}")
        ran = _ran_total(head_log) if used else None
        if ran is None or ran < len(head_ids):
            return _node_or_skip(f"HEAD run did not run what it collected: ran {ran}, collected {len(head_ids)}")
        red_at_head = set(_failed_node_ids(head_log, head_root)) if used and 1 in codes else set()
        export_reds = _failed_node_ids(export_log, export_root)
        baseline = {
            "method": "isolated-export",
            "base_sha": measured_sha,
            "caused": [n for n in export_reds if n not in red_at_head],
            "foreign": [n for n in export_reds if n in red_at_head],
            "evidence": sorted(red_at_head),
        }
        baseline.update(_landing_checks(
            root, run, baseline, export_root, head_root, head_log, deadline, measured_sha, base_sha, export_sha,
            write_set, reverify_ids,
        ))
    except (le.LandingExportError, RuntimeError) as exc:
        return _skip(f"HEAD export failed: {exc}")
    finally:
        baseline_export.remove(head_root)
    return {"baseline": baseline}


def _measure(run, root: Path, ids: Sequence[str], log: Path, deadline: float) -> Optional[tuple[set[str], set[str]]]:
    """``(present, failed)``: of ``ids``, the ones ``--collect-only`` finds under ``root`` and the ones
    red there. An id absent under ``root`` is in neither. None (unmeasured) when a collection or run is
    broken or ran fewer tests than it named."""
    files = list(dict.fromkeys(f for f in (i.split("::")[0] for i in ids) if (root / f).is_file()))
    if not files:
        return set(), set()
    collected = _collect_ids(run, root, files, log.with_suffix(".collect.log"), deadline)
    if isinstance(collected, str) or collected[1]:
        return None
    present = [i for i in dict.fromkeys(ids) if i in collected[0]]
    if not present:
        return set(), set()
    status, _, codes, used = _run_pass(run, root, present, log, deadline)
    if status not in ("pass", "fail") or not used or (_ran_total(log) or 0) < len(present):
        return None
    return set(present), (set(_failed_node_ids(log, root)) & set(present)) if 1 in codes else set()


def _head_red_not_the_runs(
    root: Path, base_sha: Optional[str], head: str, write_set: Sequence[str], red: set[str],
) -> Optional[set[str]]:
    """``red`` (the caused ids red at pure HEAD) minus what the run's own commits could have made red,
    or None when HEAD cannot be told from the run's work. A checkpoint commit leaves the run's blobs in
    HEAD, so a path of ``write_set`` changed between ``base_sha`` and ``head`` taints every id (None); an
    id whose test file changed in that range stays caused. A range git cannot read (an unresolvable sha)
    taints nothing: there is no run commit to find. Zero-spawn."""
    from coordinator_core.git.git_state import diff_commit_trees

    changes = diff_commit_trees(root, base_sha, head) if base_sha else None
    if changes is None:
        return red
    changed = {c.path for c in changes}
    if changed & {str(w).replace("\\", "/") for w in write_set}:
        return None
    return {i for i in red if i.split("::")[0].replace("\\", "/") not in changed}


def _landing_checks(
    root: Path, run, baseline: dict, export_root: Path, head_root: Path, head_log: Path, deadline: float,
    measured_sha: str, base_sha: Optional[str], export_sha: Optional[str], write_set: Sequence[str] = (),
    reverify_ids: Sequence[str] = (),
) -> dict:
    """The landing base checks, so ``dispatch.land_run`` only reads them: ``head_red`` the ``caused`` and
    ``reverify_ids`` red at a pure-HEAD export (``head_root`` itself when ``measured_sha`` is HEAD, where
    ``caused`` is green by construction), ``owed_cleared`` the owed ids (``unevidenced_ids(baseline)``) and
    ``reverify_ids`` green at ``export_root`` (HEAD plus the landing paths), ``reverify_export_red`` the
    ``reverify_ids`` red there (omitted without ``reverify_ids``), and ``head_sha`` the sha ``head_red`` was
    measured at. ``head_red`` drops what ``_head_red_not_the_runs`` taints. A key whose measurement is
    broken or ran fewer tests than named is omitted: the landing keeps those ids caused. Rebuilds
    ``head_root`` at HEAD when the run base differs and ``caused`` or ``reverify_ids`` is non-empty (at
    most that one build); the caller removes it."""
    from coordinator_core.git.git_state import head_sha as current_head
    from coordinator_core.ops.dispatch_emit import landing_export as le

    out: dict = {}
    caused = baseline["caused"]
    reverify = list(dict.fromkeys(reverify_ids))
    head, red = measured_sha, set()
    try:
        current = measured_sha if base_sha is None else current_head(root, fresh=True)
        if (caused or reverify) and current != measured_sha:
            head = le.build_head_export(root, head_root, [], None).head_sha
        probe = reverify if head == measured_sha else list(dict.fromkeys([*caused, *reverify]))
        if probe:
            measured = _measure(run, head_root, probe, head_log.with_suffix(".red.log"), deadline)
            red = None if measured is None else measured[1]
            if red and head != measured_sha:
                red = _head_red_not_the_runs(root, base_sha, head, write_set, red)
    except (le.LandingExportError, RuntimeError):
        red = None
    if red is not None:
        out[landing_targeted_tests.HEAD_RED_KEY] = sorted(red)
        out[landing_targeted_tests.HEAD_SHA_KEY] = head
    owed = landing_targeted_tests.unevidenced_ids(baseline)
    wanted = list(dict.fromkeys([*owed, *reverify]))
    owed_run = (set(), set()) if not wanted else _measure(run, export_root, wanted, head_log.with_suffix(".owed.log"), deadline)
    if owed_run is not None:
        out[landing_targeted_tests.OWED_CLEARED_KEY] = sorted(owed_run[0] - owed_run[1])
        if reverify:
            out[landing_targeted_tests.REVERIFY_EXPORT_RED_KEY] = sorted(owed_run[1] & set(reverify))
        out.setdefault(landing_targeted_tests.HEAD_SHA_KEY, export_sha or measured_sha)
    return out


def _reverify_failing_ids(params: dict) -> list[str]:
    """The review re-verify record's ``failing`` node ids, posix-normalised, minus the ids the record
    names red at the run base (``BASE_RED_KEYS``). Empty when the run carries no re-verify record."""
    return landing_targeted_tests.reverify_live_failing(landing_targeted_tests.reverify_record(params))


def _names_own_path(log: Path, nodes: Sequence[str], write_set: Sequence[str]) -> set[str]:
    """``red_attribution.names_own_path`` over the captured ``log``, read once; no spawn."""
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    return red_attribution.names_own_path(text, nodes, write_set)


def classify_real_tree_reds(
    log: Path, root: Path, write_set: Sequence[str],
) -> "tuple[list[str], list[str]]":
    """``(own, foreign)`` node ids of the FAILED/ERROR lines in a real-tree pytest ``log``: an id in a
    ``write_set`` test file or whose failure text names a ``write_set`` path is the run's own; any other red is a
    peer's dirt (the rule ``_write_dual`` applies to a live-only red). An empty ``write_set`` names no
    owner, so every red stays own: ownership is never granted away without a record."""
    ids = _failed_node_ids(log, root)
    if not write_set:
        return ids, []
    mine = _names_own_path(log, ids, write_set)
    return [i for i in ids if i in mine], [i for i in ids if i not in mine]


_SECTION_HEAD = re.compile(r"^_{2,} (.+?) _{2,}$", re.MULTILINE)
_SUMMARY_HEAD = re.compile(r"^=+ short test summary info =+$", re.MULTILINE)
_PATH_TOKEN = re.compile(r"(?<![\w./\\-])((?:[\w.-]+/)+[\w.-]+\.\w+)")
_ASSERT_LINE = re.compile(r"^E\s.*$", re.MULTILINE)
_MAX_NAMED_PATHS = 200


def _named_paths(log: Path, strip: Sequence[Path], ids: Sequence[str]) -> dict[str, set[str]]:
    """``{id: repo-relative POSIX paths its failure text names}`` from the ``E`` assertion lines of the pytest
    FAILURES section headed by the id's test name (traceback frames excluded) and the id's ``FAILED`` summary
    message in ``log``; an absolute ``strip`` root prefix is cut
    first. An id with no text maps to an empty set."""
    try:
        text = log.read_text(encoding="utf-8", errors="replace").replace("\\", "/")
    except OSError:
        return {i: set() for i in ids}
    for base in strip:
        text = text.replace(base.as_posix() + "/", "")
    cut = _SUMMARY_HEAD.search(text)
    body, summary = (text[:cut.start()], text[cut.start():]) if cut else (text, "")
    heads = list(_SECTION_HEAD.finditer(body))
    sections: dict[str, str] = {}
    for n, m in enumerate(heads):
        end = heads[n + 1].start() if n + 1 < len(heads) else len(body)
        sections[m.group(1)] = sections.get(m.group(1), "") + body[m.end():end]
    named: dict[str, set[str]] = {}
    for i in ids:
        test = i.replace("\\", "/").partition("::")[2].replace("::", ".")
        message = "".join(re.findall(rf"^FAILED {re.escape(i)} - (.*)$", summary, flags=re.MULTILINE))
        named[i] = set(_PATH_TOKEN.findall("\n".join(_ASSERT_LINE.findall(sections.get(test, ""))) + "\n" + message))
    return named


def _peer_held_reds(
    root: Path, export_root: Path, export_log: Path, run_id: str, write_set: Sequence[str], ids: Sequence[str],
) -> dict[str, list[str]]:
    """``{id: [peer-held paths]}`` for each of ``ids`` whose failure text names a path outside ``write_set``
    that a live peer run holds (one claim lookup over every named path), or whose test file or named path is a
    declared write of a run parked behind ``run_id`` (``_parked_waiter_paths``), and names none of ``write_set``:
    a red on the run's own paths stays the run's. Empty when nothing is named or held. No git spawn per id.
    Trap: only ``E`` lines and the FAILED message name paths; a peer-held path only in an assertion message with
    no own path named reads foreign."""
    from coordinator_core.ops.dispatch_emit.run_write_set import _split_peer_held

    own = {str(w).replace("\\", "/") for w in write_set}
    named = _named_paths(export_log, (export_root, root), ids)
    candidates = sorted({p for paths in named.values() for p in paths} - own)[:_MAX_NAMED_PATHS]
    held = _split_peer_held(root, candidates, "", run_id)[1] if candidates else {}
    waiting_all = _parked_waiter_paths(root, run_id)
    waiting = waiting_all - own
    ranges_by_file: dict[str, str] = {}
    out: dict[str, list[str]] = {}
    for i, paths in named.items():
        test_file = i.replace("\\", "/").partition("::")[0]
        if test_file in own and test_file in waiting_all and not (paths & own) - {test_file}:
            if _red_on_unproduced_hunk(root, run_id, test_file, i, ranges_by_file):
                out[i] = [test_file]
                continue
        if paths & own:
            continue
        hit = {p for p in paths if p in held} | {p for p in {*paths, test_file} if p in waiting}
        if hit:
            out[i] = sorted(hit)
    return out


def _red_on_unproduced_hunk(
    root: Path, run_id: str, test_file: str, node_id: str, ranges_by_file: dict[str, str],
) -> bool:
    """True when ``node_id``'s parameter id (else ``def <test name>``) sits in a hunk of the shared ``test_file``
    that ``run_id`` did not produce (``hunk_provenance._unproduced``): the parked waiter's hunk, not the run's.
    False when provenance is unreadable. Trap: attribution is textual, so a param id repeated in the run's own
    hunk of the same file reads as the waiter's only when the unproduced lines carry it."""
    from coordinator_core.ops.dispatch_emit import hunk_provenance

    try:
        if test_file not in ranges_by_file:
            with hunk_provenance.invariants_scope(root, run_id):
                ranges_by_file[test_file] = hunk_provenance._unproduced(root, run_id, test_file)[0]
        ranges = ranges_by_file[test_file]
        lines = (root / test_file).read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:  # noqa: BLE001 -- unreadable provenance never reclassifies
        return False
    text = ""
    for part in ranges.split(","):
        first, _dash, last = part.strip().partition("-")
        if first.isdigit():
            text += "\n".join(lines[int(first) - 1:int(last or first)]) + "\n"
    leaf = node_id.replace("\\", "/").partition("::")[2].rpartition("::")[2]
    name, bracket, param = leaf.partition("[")
    token = param[:-1] if bracket and param.endswith("]") else f"def {name}"
    return bool(token) and (token in text if bracket else re.search(rf"{re.escape(token)}\(", text) is not None)


def _parked_waiter_paths(root: Path, run_id: str) -> set[str]:
    """Declared write paths of the runs parked behind ``run_id`` (``halted_hold_release._parked_behind``): a
    waiter minted after the run holds nothing against it, so its attributed hunk is not the run's. Empty when
    nothing waits. One write-set read per waiter."""
    from coordinator_core.ops.dispatch_emit import halted_hold_release
    from coordinator_core.ops.dispatch_emit.run_write_set import run_write_set_full

    out: set[str] = set()
    for waiter in halted_hold_release._parked_behind(root, run_id):
        if waiter != run_id:
            out.update(str(p).replace("\\", "/") for p in run_write_set_full(root, waiter, "")[0])
    return out


def _write_dual(
    root, path, log_path, export_log, run_id, tests, dropped, drop_note, meta, dirty, head_sha,
    export_pass, live_pass, fail, baseline=None,
) -> int:
    """Write the dual record of ``_isolated_child``; top-level ``status`` is the export pass's, and a
    ``baseline`` dict (``baseline`` or ``baseline_skipped``) is merged into the record."""
    now = lambda: datetime.now(timezone.utc).isoformat()  # noqa: E731
    status, reason, codes, export_log_used = export_pass
    live_status, live_reason, live_codes, live_log_used = live_pass
    export_failed = _failed_node_ids(export_log, Path(meta["root"])) if export_log_used and 1 in codes else []
    live_failed = _failed_node_ids(log_path, root) if live_log_used and 1 in live_codes else []

    covered_paths = list(dict.fromkeys(meta["write_set"]))
    record = {
        "run_id": run_id,
        "head": head_sha(root),
        "tests": tests,
        "covered": landing_retest.landing_fingerprint(root, covered_paths),
        "status": status,
        "at": now(),
        "isolated_export": meta,
        "export": {"status": status, "failed_tests": export_failed},
        "live": {"status": live_status, "failed_tests": live_failed},
    }
    record.update(baseline or {})
    measured_baseline = record.get("baseline")
    if isinstance(measured_baseline, dict) and measured_baseline.get("caused") and export_log_used:
        peer = _peer_held_reds(
            root, Path(meta["root"]), export_log, run_id, meta["write_set"], measured_baseline["caused"],
        )
        if peer:
            measured_baseline["caused"] = [n for n in measured_baseline["caused"] if n not in peer]
            measured_baseline["foreign"] = [*measured_baseline.get("foreign", []), *peer]
            measured_baseline["foreign_reason"] = {
                **measured_baseline.get("foreign_reason", {}), **{n: landing_targeted_tests.PEER_HELD_PATH for n in peer},
            }
            measured_baseline["evidence"] = sorted({*measured_baseline.get("evidence", []), *peer})
    if isinstance(measured_baseline, dict) and export_log_used and _ran_total(export_log) is not None:
        live_only = [n for n in live_failed if n not in export_failed and n not in measured_baseline.get("foreign", [])]
        own = _names_own_path(log_path, live_only, meta["write_set"])
        dirt = [n for n in live_only if n not in own]
        if own:
            measured_baseline["caused"] = [*measured_baseline.get("caused", []), *own]
        if dirt:
            measured_baseline["foreign"] = [*measured_baseline.get("foreign", []), *dirt]
            measured_baseline["foreign_reason"] = {
                **measured_baseline.get("foreign_reason", {}), **{n: landing_targeted_tests.UNDECLARED_DIRT for n in dirt},
            }
            # Trap: the landing's invariant gate credits a foreign id only through ``baseline.evidence``.
            measured_baseline["evidence"] = sorted({*measured_baseline.get("evidence", []), *dirt})
    if live_reason:
        record["live"]["reason"] = live_reason
    if reason:
        record["reason"] = reason
    if status == NO_TESTS_STATUS:
        record["tests_run"] = 0
    cause = _zero_run_cause(status, codes, export_log, export_log_used)
    if cause:
        record[landing_targeted_tests.ZERO_RUN_CAUSE_KEY] = cause
    _stamp_session_facts(record, export_log, export_log_used, codes, Path(meta["root"]))
    _stamp_retry(record, export_log)
    _stamp_retry(record["live"], log_path)
    if status in ("fail", "error") and export_failed:
        record[landing_retest.FAILED_TESTS_PARAM] = export_failed
    if dropped:
        record["dropped"] = dropped
        record["note"] = drop_note
    record["log"] = str(export_log)
    record["live_log"] = str(log_path)
    _write_record(path, record)
    print(f"{path} {status}" + (f": {reason}" if reason else ""))
    return {"pass": 0, "fail": 1, NO_TESTS_STATUS: 0}.get(status, 2)


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    run: Callable[..., "int | str | None"] = _run_argv,
    dirty: Callable[[Path], list[str]] = covering_dirty_paths,
    spawn: Optional[Callable[..., bool]] = None,
    wait_budget_secs: Optional[float] = WARM_WAIT_BUDGET_SECS,
) -> int:
    """Entry for both the cold ``python -m`` and the warm ``--retest-targeted`` route. With ``--poll
    --wait`` the wait is capped at ``wait_budget_secs`` (None: unbounded, cold only); a capped wait
    that sees ``running`` returns 3 so the caller re-polls."""
    from coordinator_core.git.git_state import head_sha
    _LAUNCHED_ARGV.clear()
    _INFRA_SESSIONS.clear()
    _RETRIED.clear()
    ap = argparse.ArgumentParser(prog="retest_targeted")
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--from-task-output")
    source.add_argument("--params-file")
    ap.add_argument("--tests", action="append", default=[])
    ap.add_argument("--repo-root")
    ap.add_argument("--dropped", action="append", default=[], help=argparse.SUPPRESS)
    ap.add_argument("--detached-child", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--poll", action="store_true")
    ap.add_argument("--wait", action="store_true", help="with --poll: block until a terminal verdict")
    ap.add_argument("--isolated-export", action="store_true")
    args = ap.parse_args(argv)
    if args.wait and not args.poll:
        ap.error("--wait requires --poll")

    try:
        params = _load_params(args)
        root = _repo_root(args.repo_root, params)
    except (ValueError, RuntimeError) as exc:
        print(f"retest_targeted: {exc}", file=sys.stderr)
        return 2
    run_id = _run_id_of(params)
    if not safe_id(run_id):
        print(f"retest_targeted: run id {run_id!r} is not a single safe path segment", file=sys.stderr)
        return 2

    path = landing_retest.fresh_verdict_path(root, run_id)
    if args.poll:
        code = _poll(path, *_input_pair(args), root=root, run_id=run_id, params=params)
        limit = None if wait_budget_secs is None else time.monotonic() + wait_budget_secs
        while args.wait and code == 3:
            step = _WAIT_INTERVAL_SECS
            if limit is not None:
                step = min(step, limit - time.monotonic())
                if step <= 0:
                    break
            time.sleep(step)
            code = _poll(path, *_input_pair(args), root=root, run_id=run_id, params=params)
        return code

    tests = _with_invariants(root, _tests_to_run(args.tests, params), dirty, run_id, params)
    if not tests:
        return _write_stale(root, run_id, dirty, head_sha, params)

    escaping = [t for t in tests if contained_path(root / t.split("::")[0], [root]) is None]
    if escaping:
        print("retest_targeted: test path escapes the repo: " + ", ".join(escaping), file=sys.stderr)
        return 2
    dropped = list(dict.fromkeys([*args.dropped, *(t for t in tests if not (root / t.split("::")[0]).exists())]))
    tests = [t for t in tests if t not in dropped]
    drop_note = ("dropped nonexistent target(s): " + ", ".join(dropped)) if dropped else ""
    if drop_note:
        print(f"retest_targeted: {drop_note}", file=sys.stderr)

    log_path = path.with_suffix(".log")
    if not tests:
        return _write_no_tests(root, path, run_id, dropped, drop_note, dirty, head_sha, params)
    if not args.detached_child:
        return _start_detached(args, root, path, log_path, run_id, tests, spawn or spawn_detached, dropped)

    _stamp_pid(path)
    try:
        code = _child(args, root, path, log_path, run_id, params, tests, dropped, drop_note, run, dirty, head_sha)
    except BaseException as exc:
        _seal_if_running(path, run_id, tests, f"retest child died: {type(exc).__name__}: {exc}")
        raise
    _seal_if_running(path, run_id, tests, "retest child exited without writing a verdict")
    return code


def _child(args, root, path, log_path, run_id, params, tests, dropped, drop_note, run, dirty, head_sha) -> int:
    """The detached child's work: run ``tests`` and write the terminal record (isolated or live-tree)."""
    if args.isolated_export:
        return _isolated_child(root, path, log_path, run_id, params, tests, dropped, drop_note, run, dirty, head_sha)

    path.parent.mkdir(parents=True, exist_ok=True)
    status, reason, codes, log_used = _run_pass_retrying(
        run, root, tests, log_path, time.monotonic() + RETEST_CHILD_TIMEOUT_SECS, _row_marker(params),
    )

    test_files = [t.split("::")[0] for t in tests]
    try:
        covered_paths, covered_source = _covered_set(root, run_id, params, dirty, test_files)
    except RuntimeError as exc:
        print(f"retest_targeted: {exc}", file=sys.stderr)
        _write_record(path, {
            "run_id": run_id, "status": "error", "tests": tests, "reason": str(exc),
            "at": datetime.now(timezone.utc).isoformat(),
        })
        return 2
    record = {
        "run_id": run_id,
        "head": head_sha(root),
        "tests": tests,
        "covered": landing_retest.landing_fingerprint(root, covered_paths),
        "covered_source": covered_source,
        "status": status,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    if reason:
        record["reason"] = reason
    if status == NO_TESTS_STATUS:
        record["tests_run"] = 0
    cause = _zero_run_cause(status, codes, log_path, log_used)
    if cause:
        record[landing_targeted_tests.ZERO_RUN_CAUSE_KEY] = cause
    _stamp_session_facts(record, log_path, log_used, codes, root)
    _stamp_retry(record, log_path)
    if log_used and status in ("fail", "error") and 1 in codes:
        failed = _failed_node_ids(log_path, root)
        record[landing_retest.FAILED_TESTS_PARAM] = failed
        if status == "fail":
            deadline = time.monotonic() + RETEST_CHILD_TIMEOUT_SECS
            record.update(_classify_reds(
                root, run_id, params, log_path, failed,
                lambda ids: _head_red_ids(run, root, ids, log_path, deadline),
            ))
    if dropped:
        record["dropped"] = dropped
        record["note"] = drop_note
    if log_used:
        record["log"] = str(log_path)
    _write_record(path, record)
    print(f"{path} {status}" + (f": {reason}" if reason else ""))
    return {"pass": 0, "fail": 1, NO_TESTS_STATUS: 0}.get(status, 2)


if __name__ == "__main__":
    sys.exit(main(wait_budget_secs=None))
