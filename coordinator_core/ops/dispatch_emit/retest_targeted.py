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
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Optional, Sequence

from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.ops._path_guard import contained_path, safe_id
from coordinator_core.ops.ceremony.detached_spawn import spawn_detached
from coordinator_core.ops.dispatch_emit import landing_retest
from coordinator_core.ops.dispatch_emit import landing_targeted_tests

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


def _run_argv(
    argv: list[str], cwd: Path, *, log: Optional[Path] = None, timeout: Optional[float] = None,
    append: bool = False,
):
    """Exit code, or ``LAUNCH_TIMED_OUT``. Trap: the timeout kills only the direct child; the log is a
    file so a surviving grandchild never blocks this call."""
    limit = RETEST_CHILD_TIMEOUT_SECS if timeout is None else timeout
    from coordinator_core.bash_guards._heavy_command_class import admit_before_spawn, register_spawn

    workers = next((int(argv[i + 1]) for i, t in enumerate(argv[:-1]) if t == "-n" and argv[i + 1].isdigit()), 1)
    admission = admit_before_spawn("retest-targeted-pytest", 0.75 + workers * 0.15)
    if not admission.admitted:
        raise OSError(f"admission: {admission.reason}")
    sink = open(log, "ab" if append else "wb") if log is not None else subprocess.DEVNULL
    from coordinator_core.testing import baseline_export

    # An export carries no .git of its own: confine git there so it cannot adopt an enclosing repo.
    env = None if (Path(cwd) / ".git").exists() else baseline_export.export_env(cwd)
    try:
        proc = subprocess.Popen(
            argv, cwd=str(cwd), env=env, creationflags=_NO_WINDOW, stdin=subprocess.DEVNULL,
            stdout=sink, stderr=subprocess.STDOUT,
        )
        register_spawn(admission.key, admission.peak_gib, proc.pid)
        try:
            return proc.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return LAUNCH_TIMED_OUT
    finally:
        if log is not None:
            sink.close()


#: ``main`` return code for a session never started because the shared deadline was already spent.
_NOT_LAUNCHED = "not-launched"


def _time_left(deadline: float) -> float:
    """Seconds left under the shared ``deadline`` (monotonic); ``<= 0`` means launch nothing."""
    return deadline - time.monotonic()


def _run_session(run, argv: list[str], root: Path, log: Path, deadline: float, append: bool):
    """``(code, error)`` for one session under the shared ``deadline`` (monotonic): ``_NOT_LAUNCHED``
    when no time remains, ``(None, message)`` when the launcher could not start."""
    if _time_left(deadline) <= 0:
        return _NOT_LAUNCHED, None
    try:
        return run(argv, root, log=log, timeout=_time_left(deadline), append=append), None
    except OSError as exc:
        return None, str(exc)


def _launch_argv(root: Path, tests: list[str], *, keep_tier: bool = False) -> "list[list[str]] | str":
    """One argv per session of ``_launch_plan`` (prefix plus group); a str is the reason none could be built."""
    plan = _launch_plan(root, tests, keep_tier=keep_tier)
    if isinstance(plan, str):
        return plan
    base, groups = plan
    return [[*base, *group] for group in groups]


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


def _repo_root(raw: Optional[str]) -> Path:
    if raw:
        return Path(raw).resolve()
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
    ram = int(psutil.virtual_memory().available / 2**20 / 150)
    return max(1, min(cores, ram, RETEST_MAX_WORKERS))


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
    from coordinator_core.ops.dispatch_emit import test_interpreter as ti

    scopes: dict[str, list[str]] = {}
    for t in tests:
        scopes.setdefault(_scope_of(root, t), []).append(t)
    groups = list(scopes.values()) if _scopes_conflict(root, scopes) else [tests]
    groups = [chunk for group in groups for chunk in _chunks(group)]
    cap = worker_cap()

    declared = resolve_validation_cmd.read_local_md_key(str(root), ti.LAUNCHER_KEY).strip()
    if declared:
        script = declared.split()[0]
        if Path(script).is_absolute() or ".." in re.split(r"[\/]", script) or not (root / script).is_file():
            return f"{ti.LAUNCHER_KEY} {declared!r} is not a repo-relative file that exists"
        pin = ti.resolve_targeted_test_python(root)
        python = pin.command if pin.command and pin.defect is None else sys.executable
        return [python, *_cap_workers(shlex.split(declared), cap)], groups
    resolved = resolve_validation_cmd.resolve_fast_test_cmd(str(root))
    if resolved.returncode != 0:
        return f"fast_test_cmd unresolved: {resolved.stderr.strip()}"
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
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


def _collect_ids(run, root: Path, tests: list[str], log: Path, deadline: float) -> "tuple[set[str], list[str]] | str":
    """``(ids, errored_files)``: the ``root``-relative node ids ``--collect-only`` finds for ``tests`` under
    ``root``, and the ``root``-relative files with a collection ERROR; a str reason when a session is
    broken or the printed ids cannot be mapped. ``-q`` collect-only prints no header, so each session is
    pinned with ``--rootdir=<root>`` and its ids come out ``root``-relative; a ``rootdir:`` line in the
    log, when present, overrides that base. Fail closed: more than one distinct rootdir, one outside
    ``root``, or an id whose file is not under ``root`` is a reason. Exit 2 is accepted only when the log
    names the ``ERROR collecting`` file."""
    argvs = _launch_argv(root, tests)
    if isinstance(argvs, str):
        return argvs
    log.write_bytes(b"")
    rcs: list = []
    for n, argv in enumerate(argvs):
        quiet_free = [t for t in argv if t not in _QUIET_FLAGS]  # a second -q prints ``path: N``, not node ids
        rc, err = _run_session(
            run, [*quiet_free, "--collect-only", "-q", f"--rootdir={root}"], root, log, deadline, n > 0,
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


def _read_record(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _past_deadline(record: dict) -> bool:
    """True when a ``running`` record's ``deadline`` has passed; an unparseable one counts as past."""
    try:
        return datetime.now(timezone.utc) >= datetime.fromisoformat(str(record["deadline"]))
    except (KeyError, ValueError, TypeError):
        return True


def _write_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")


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


def _poll(path: Path, flag: str, value: str) -> int:
    """Print the record's status line; 0 pass, 1 fail, 2 error/stale/absent/abandoned, 3 running."""
    record = _read_record(path)
    if record is None:
        print(f"{path} absent: no retest record; start one with {landing_targeted_tests.RETEST_LAUNCHER} --tests <files>")
        return 2
    status = record.get("status")
    if status == landing_retest.RETEST_RUNNING:
        if _past_deadline(record):
            print(f"{path} error: retest child abandoned past deadline")
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
    if existing and existing.get("status") == landing_retest.RETEST_RUNNING and not _past_deadline(existing):
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


def _run_pass(run, root: Path, tests: list[str], log_path: Path, deadline: float):
    """One full pass of ``tests`` with cwd ``root`` under the shared ``deadline`` (monotonic):
    ``(status, reason, codes, log_used)``."""
    argv_or_err = _launch_argv(root, tests)
    if isinstance(argv_or_err, str):
        return "error", argv_or_err, [], False
    budget = RETEST_CHILD_TIMEOUT_SECS
    codes: list = []
    failed_cmds: list[str] = []
    broken_msgs: list[str] = []
    kept: "list[list[str]] | str | None" = None  # the keep-tier fallback plan, built once for every session
    for n, argv in enumerate(argv_or_err):
        offset = _log_size(log_path) if n > 0 else 0
        rc, err = _run_session(run, argv, root, log_path, deadline, n > 0)
        if rc == 2 and not _collection_failed_since(log_path, offset):
            if kept is None:
                kept = _launch_argv(root, tests, keep_tier=True)
            if isinstance(kept, list) and len(kept) == len(argv_or_err) and kept[n] != argv:
                argv = kept[n]
                rc, err = _run_session(run, argv, root, log_path, deadline, True)
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
    elif all(c == NO_TESTS_COLLECTED for c in codes):
        status = NO_TESTS_STATUS
        reason = f"launcher collected no tests from: {', '.join(tests)}"
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


def _isolated_body(
    root: Path, path: Path, log_path: Path, run_id: str, params: dict, tests: list[str],
    dropped: list[str], drop_note: str, run, dirty, head_sha,
) -> int:
    """Build ``scratch/warp/<run>/export`` (HEAD plus exactly the landing paths, never dirty paths), run
    ``tests`` there then in the live tree under one shared deadline, and record both. Top-level ``status``
    is the export's. A failed export build is ``error``, never a live-only run."""
    from coordinator_core.ops.dispatch_emit import landing_export as le

    deadline = time.monotonic() + RETEST_CHILD_TIMEOUT_SECS
    path.parent.mkdir(parents=True, exist_ok=True)
    now = lambda: datetime.now(timezone.utc).isoformat()  # noqa: E731

    def _fail(reason: str, meta: dict) -> int:
        _write_record(path, {
            "run_id": run_id, "status": "error", "tests": tests, "reason": reason,
            "isolated_export": meta, "at": now(),
        })
        print(f"{path} error: {reason}")
        return 2

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
        meta["head"] = le.build_head_export(root, export_root, write_set).head_sha
    except RuntimeError as exc:
        return _fail(str(exc), meta)
    except le.LandingExportError as exc:
        return _fail(f"isolated export failed: {exc}", meta)

    export_log = path.with_suffix(".export.log")
    status, reason, codes, export_log_used = _run_pass(run, export_root, tests, export_log, deadline)
    live_status, live_reason, live_codes, live_log_used = _run_pass(run, root, tests, log_path, deadline)
    baseline: dict = {}
    if status == "fail" and export_log_used and 1 in codes and _failed_node_ids(export_log, export_root):
        baseline = _head_baseline(
            root, run, tests, export_root, export_log, path.with_suffix(".head.log"), deadline,
        )
    return _write_dual(
        root, path, log_path, export_log, run_id, tests, dropped, drop_note, meta, dirty, head_sha,
        (status, reason, codes, export_log_used), (live_status, live_reason, live_codes, live_log_used), _fail,
        baseline,
    )


def _head_baseline(
    root: Path, run, tests: list[str], export_root: Path, export_log: Path, head_log: Path,
    deadline: float,
) -> dict:
    """``{"baseline": {method, caused, foreign, evidence}}``: ``caused`` is the export pass's failed ids
    absent from a HEAD-only export run (classify's ``union - base``), per node id; ``foreign`` those
    also red there; ``evidence`` the sorted HEAD-red ids. An id new at HEAD is never HEAD-red, so it is caused when
    red in the export; an id removed since HEAD is ignored. Fail closed with ``{"baseline_skipped":
    {reason, files}}`` (the gate stays fail-closed) when the HEAD export cannot be built, no targeted file
    exists at HEAD, HEAD collection is unavailable, errors in a file (named), or yields 0 items overall or
    for a file present at HEAD (named), the HEAD pass is not pass/fail, or the HEAD run's summary totals
    (passed+failed+error+skipped+xfailed+xpassed, the log carries no per-id results) are below the HEAD
    collected count. Collection runs before the HEAD pass so a skip avoids the costly run. The HEAD export
    is always removed.

    Traps: the ran-vs-collected check is an aggregate lower bound (a teardown error counts twice, a
    shortfall in one session can hide behind an excess in another). A file present at HEAD that collects
    0 for a legitimate reason (module-level skip, platform gate) also skips the baseline. Collect-only
    and the run are separate invocations; verify their counts agree on a live tree before trusting a
    baseline. A node-id target absent at HEAD makes HEAD collect-only exit 4 (a skip, not caused).
    Ratified fail-closed: unbuildable argv, collect/run timeout, zero collected, a named collection
    error, an unresolvable id or rootdir, and a missing summary line each write ``baseline_skipped``."""
    from coordinator_core.ops.dispatch_emit import landing_export as le
    from coordinator_core.testing import baseline_export

    head_root = export_root.with_name("export-head")
    files: dict[str, dict] = {}

    def _skip(reason: str) -> dict:
        return {"baseline_skipped": {"reason": reason, "files": files}}

    try:
        le.build_head_export(root, head_root, [])
        head_tests = [t for t in tests if (head_root / t.split("::")[0]).exists()]
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
            return _skip("HEAD collection error in: " + ", ".join(errored))
        if not head_ids:
            return _skip("HEAD pass collected 0 items")
        empty = [f for f, c in files.items() if not c["head"]]
        if empty:
            return _skip("HEAD pass collected 0 items in: " + ", ".join(empty))
        status, _, codes, used = _run_pass(run, head_root, head_tests, head_log, deadline)
        if status not in ("pass", "fail", NO_TESTS_STATUS):
            return _skip(f"HEAD pass {status}")
        ran = _ran_total(head_log) if used else None
        if ran is None or ran < len(head_ids):
            return _skip(f"HEAD run did not run what it collected: ran {ran}, collected {len(head_ids)}")
        red_at_head = set(_failed_node_ids(head_log, head_root)) if used and 1 in codes else set()
    except (le.LandingExportError, RuntimeError) as exc:
        return _skip(f"HEAD export failed: {exc}")
    finally:
        baseline_export.remove(head_root)
    export_reds = _failed_node_ids(export_log, export_root)
    return {"baseline": {
        "method": "isolated-export",
        "caused": [n for n in export_reds if n not in red_at_head],
        "foreign": [n for n in export_reds if n in red_at_head],
        "evidence": sorted(red_at_head),
    }}


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

    test_files = [t.split("::")[0] for t in tests]
    covered_paths = list(dict.fromkeys([*landing_retest._import_closure(root, test_files), *meta["write_set"]]))
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
    if live_reason:
        record["live"]["reason"] = live_reason
    if reason:
        record["reason"] = reason
    if status == NO_TESTS_STATUS:
        record["tests_run"] = 0
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
) -> int:
    from coordinator_core.git.git_state import head_sha
    ap = argparse.ArgumentParser(prog="retest_targeted")
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--from-task-output")
    source.add_argument("--params-file")
    ap.add_argument("--tests", action="append", default=[])
    ap.add_argument("--repo-root")
    ap.add_argument("--dropped", action="append", default=[], help=argparse.SUPPRESS)
    ap.add_argument("--detached-child", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--poll", action="store_true")
    ap.add_argument("--isolated-export", action="store_true")
    args = ap.parse_args(argv)

    try:
        params = _load_params(args)
        root = _repo_root(args.repo_root)
    except (ValueError, RuntimeError) as exc:
        print(f"retest_targeted: {exc}", file=sys.stderr)
        return 2
    run_id = _run_id_of(params)
    if not safe_id(run_id):
        print(f"retest_targeted: run id {run_id!r} is not a single safe path segment", file=sys.stderr)
        return 2

    path = landing_retest.fresh_verdict_path(root, run_id)
    if args.poll:
        return _poll(path, *_input_pair(args))

    tests = _tests_to_run(args.tests, params)
    if not tests:
        return _write_stale(root, run_id, dirty, head_sha, params)
    tests = _with_invariants(root, tests, dirty, run_id, params)

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

    if args.isolated_export:
        return _isolated_child(root, path, log_path, run_id, params, tests, dropped, drop_note, run, dirty, head_sha)

    path.parent.mkdir(parents=True, exist_ok=True)
    status, reason, codes, log_used = _run_pass(
        run, root, tests, log_path, time.monotonic() + RETEST_CHILD_TIMEOUT_SECS,
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
    if log_used and status in ("fail", "error") and 1 in codes:
        record[landing_retest.FAILED_TESTS_PARAM] = _failed_node_ids(log_path, root)
    if dropped:
        record["dropped"] = dropped
        record["note"] = drop_note
    if log_used:
        record["log"] = str(log_path)
    _write_record(path, record)
    print(f"{path} {status}" + (f": {reason}" if reason else ""))
    return {"pass": 0, "fail": 1, NO_TESTS_STATUS: 0}.get(status, 2)


if __name__ == "__main__":
    sys.exit(main())
