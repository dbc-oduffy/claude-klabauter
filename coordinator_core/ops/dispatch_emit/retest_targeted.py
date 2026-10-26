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
from coordinator_core.ops.dispatch_emit import landing_integrity as li

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run_id_of(params: dict) -> str:
    """``params.run_id``, else the script stem (an ask script's stem is ``<run_id>.workflow``)."""
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
    sink = open(log, "ab" if append else "wb") if log is not None else subprocess.DEVNULL
    try:
        return subprocess.run(
            argv, cwd=str(cwd), creationflags=_NO_WINDOW, stdin=subprocess.DEVNULL,
            stdout=sink, stderr=subprocess.STDOUT, timeout=limit,
        ).returncode
    except subprocess.TimeoutExpired:
        return LAUNCH_TIMED_OUT
    finally:
        if log is not None:
            sink.close()


#: ``main`` return code for a session never started because the shared deadline was already spent.
_NOT_LAUNCHED = "not-launched"


def _time_left(deadline: float) -> float:
    """Seconds left under the shared ``deadline`` (monotonic); ``<= 0`` means launch nothing."""
    return deadline - time.monotonic()


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
    return li.targeted_set(params, li.targeted_test_verdict(params), tests)


def _with_invariants(root: Path, tests: list[str], dirty: Callable[[Path], list[str]]) -> list[str]:
    """``tests`` plus every repo-wide invariant whose measured set the dirty paths touch. A git
    failure adds none; the caller's own ``dirty`` call reports it."""
    from coordinator_core.ops.dispatch_emit import invariant_measured_paths as imp

    try:
        touched = dirty(root)
    except RuntimeError:
        return tests
    owed = imp.existing_invariants(root, imp.invariants_for_paths(touched, root))
    return list(dict.fromkeys([*tests, *owed]))


_recorded_targeted_set = li.recorded_targeted_set

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


def _scopes_conflict(root: Path, scopes: dict[str, list[str]]) -> bool:
    """True when two or more scopes each carry their own ``conftest.py`` (a bare ``from conftest import``
    would collide in one session)."""
    return sum(1 for s in scopes if (root / s / "conftest.py").is_file()) > 1


def _launch_plan(root: Path, tests: list[str], *, keep_tier: bool = False) -> "tuple[list[str], list[list[str]]] | str":
    """``(base, groups)``: the command prefix and the test groups it runs, one session per group. Both
    arms share one grouping: every node id in one group (input order) unless the scopes' conftests
    collide, then one group per scope. A declared ``targeted_test_launcher`` (pinned interpreter, else
    this one) supplies the prefix, else ``fast_test_cmd`` minus its collection roots and markers; its
    ``--tier`` option is dropped unless
    ``keep_tier`` (the fallback when a runner needs it). A string return is the reason no command could
    be built."""
    from coordinator_core import resolve_validation_cmd
    from coordinator_core.ops.dispatch_emit import test_interpreter as ti

    scopes: dict[str, list[str]] = {}
    for t in tests:
        scopes.setdefault(_scope_of(root, t), []).append(t)
    groups = list(scopes.values()) if _scopes_conflict(root, scopes) else [tests]

    declared = resolve_validation_cmd.read_local_md_key(str(root), ti.LAUNCHER_KEY).strip()
    if declared:
        script = declared.split()[0]
        if Path(script).is_absolute() or ".." in re.split(r"[\/]", script) or not (root / script).is_file():
            return f"{ti.LAUNCHER_KEY} {declared!r} is not a repo-relative file that exists"
        pin = ti.resolve_targeted_test_python(root)
        python = pin.command if pin.command and pin.defect is None else sys.executable
        return [python, *shlex.split(declared)], groups
    resolved = resolve_validation_cmd.resolve_fast_test_cmd(str(root))
    if resolved.returncode != 0:
        return f"fast_test_cmd unresolved: {resolved.stderr.strip()}"
    base = _strip_collection_roots(root, resolved.stdout.strip())
    return (base if keep_tier else _drop_tier_options(base)), groups


def _write_stale(root: Path, run_id: str, dirty: Callable[[Path], list[str]], head_sha: Callable) -> int:
    """Write a ``stale`` verdict (non-blocking to the landing gate) with a named note when no
    targeted set is recorded and no ``--tests`` was given."""
    try:
        covered_paths = _outside_own_records(run_id, dirty(root))
    except RuntimeError as exc:
        print(f"retest_targeted: {exc}", file=sys.stderr)
        return 2
    note = "no targeted test set recorded for this run and no --tests given; recorded failure treated as stale"
    record = {
        "run_id": run_id,
        "head": head_sha(root),
        "tests": [],
        "covered": li.landing_fingerprint(root, covered_paths),
        "status": "stale",
        "note": note,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    path = li.fresh_verdict_path(root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(f"{path} stale: {note}")
    return 0


def _write_no_tests(root, path, run_id, dropped, note, dirty, head_sha) -> int:
    """Record ``no-tests`` (tests_run 0, non-blocking, not ``error``) when every named target was
    dropped as nonexistent. Nothing is launched."""
    try:
        covered_paths = _outside_own_records(run_id, dirty(root))
    except RuntimeError as exc:
        print(f"retest_targeted: {exc}", file=sys.stderr)
        return 2
    reason = note or "no tests to run"
    _write_record(path, {
        "run_id": run_id, "head": head_sha(root), "tests": [], "tests_run": 0, "dropped": dropped,
        "covered": li.landing_fingerprint(root, covered_paths), "status": NO_TESTS_STATUS,
        "note": reason, "at": datetime.now(timezone.utc).isoformat(),
    })
    print(f"{path} {NO_TESTS_STATUS}: {reason}")
    return 0


def _failed_node_ids(log: Path) -> list[str]:
    """Node ids from pytest ``FAILED``/``ERROR`` summary lines in ``log``."""
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found = re.findall(r"^(?:FAILED|ERROR) (\S+::\S+)", text, flags=re.MULTILINE)
    return list(dict.fromkeys(found))


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


def _poll_command(task_output: str) -> str:
    return f"{li.RETEST_LAUNCHER} --poll --from-task-output {json.dumps(task_output) if ' ' in task_output else task_output}"


def _poll(path: Path, task_output: str) -> int:
    """Print the record's status line; 0 pass, 1 fail, 2 error/stale/absent/abandoned, 3 running."""
    record = _read_record(path)
    if record is None:
        print(f"{path} absent: no retest record; start one with {li.RETEST_LAUNCHER} --tests <files>")
        return 2
    status = record.get("status")
    if status == li.RETEST_RUNNING:
        if _past_deadline(record):
            print(f"{path} error: retest child abandoned past deadline")
            return 2
        print(f"{path} running since {record.get('at')}; poll: {_poll_command(task_output)}")
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
    poll = _poll_command(args.from_task_output)
    existing = _read_record(path)
    if existing and existing.get("status") == li.RETEST_RUNNING and not _past_deadline(existing):
        print(f"{path} running since {existing.get('at')}; poll: {poll}")
        return 3
    started = datetime.now(timezone.utc)
    _write_record(path, {
        "run_id": run_id,
        "status": li.RETEST_RUNNING,
        "tests": tests,
        "log": str(log_path),
        "at": started.isoformat(),
        "deadline": (started + timedelta(seconds=RETEST_CHILD_TIMEOUT_SECS + _DEADLINE_GRACE_SECS)).isoformat(),
    })
    engine = Path(__file__).resolve().parents[3]
    child_argv = [
        "--from-task-output", str(Path(args.from_task_output).resolve()),
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
    plan = _launch_plan(root, tests)
    if isinstance(plan, str):
        return "error", plan, [], False
    base, groups = plan
    budget = RETEST_CHILD_TIMEOUT_SECS
    codes: list = []
    failed_cmds: list[str] = []
    broken_msgs: list[str] = []
    kept: "tuple[list[str], list[list[str]]] | str | None" = None
    # One session per conftest-bearing scope when two or more collide, else exactly one; the
    # partition is the conftest-collision floor, so nothing further batches.
    sessions = [[*base, *group] for group in groups]
    for n, (argv, group) in enumerate(zip(sessions, groups)):
        offset = _log_size(log_path) if n > 0 else 0
        rc, err = _NOT_LAUNCHED, None
        if (left := _time_left(deadline)) > 0:
            try:
                rc = run(argv, root, log=log_path, timeout=left, append=n > 0)
            except OSError as exc:
                rc, err = None, str(exc)
        cmd_base = base
        if rc == 2 and not _collection_failed_since(log_path, offset):
            if kept is None:
                kept = _launch_plan(root, tests, keep_tier=True)
            if not isinstance(kept, str) and kept[0] != base:
                cmd_base = kept[0]
                rc, err = _NOT_LAUNCHED, None
                if (left := _time_left(deadline)) > 0:
                    try:
                        rc = run([*cmd_base, *group], root, log=log_path, timeout=left, append=True)
                    except OSError as exc:
                        rc, err = None, str(exc)
        codes.append(rc)
        cmd = shlex.join([*cmd_base, *group])
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


def _isolated_child(
    root: Path, path: Path, log_path: Path, run_id: str, params: dict, tests: list[str],
    dropped: list[str], drop_note: str, run, dirty, head_sha,
) -> int:
    """``--isolated-export`` child: build ``scratch/warp/<run>/export`` (HEAD plus the run write set), run
    ``tests`` there then in the live tree under one shared deadline, and record both. Top-level ``status``
    is the export's. A failed export build is ``error``, never a live-only run. A passing or
    no-tests export is removed; any other keeps its path in the record."""
    from coordinator_core.ops.dispatch_emit import landing_export as le
    from coordinator_core.testing import baseline_export

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
        write_set, source = _run_write_set(root, params, dirty)
        export_root = le.run_export_root(root, run_id)
        meta = {"root": export_root.as_posix(), "write_set": write_set, "write_set_source": source}
        meta["head"] = le.build_head_export(root, export_root, write_set).head_sha
    except RuntimeError as exc:
        return _fail(str(exc), meta)
    except le.LandingExportError as exc:
        return _fail(f"isolated export failed: {exc}", meta)

    export_log = path.with_suffix(".export.log")
    status = "error"
    try:
        status, reason, codes, export_log_used = _run_pass(run, export_root, tests, export_log, deadline)
        live_status, live_reason, live_codes, live_log_used = _run_pass(run, root, tests, log_path, deadline)
        return _write_dual(
            root, path, log_path, export_log, run_id, tests, dropped, drop_note, meta, dirty, head_sha,
            (status, reason, codes, export_log_used), (live_status, live_reason, live_codes, live_log_used), _fail,
        )
    finally:
        if status in ("pass", NO_TESTS_STATUS):
            baseline_export.remove(export_root)


def _write_dual(
    root, path, log_path, export_log, run_id, tests, dropped, drop_note, meta, dirty, head_sha,
    export_pass, live_pass, fail,
) -> int:
    """Write the dual record of ``_isolated_child``; top-level ``status`` is the export pass's."""
    now = lambda: datetime.now(timezone.utc).isoformat()  # noqa: E731
    status, reason, codes, export_log_used = export_pass
    live_status, live_reason, live_codes, live_log_used = live_pass
    export_failed = _failed_node_ids(export_log) if export_log_used and 1 in codes else []
    live_failed = _failed_node_ids(log_path) if live_log_used and 1 in live_codes else []

    test_files = [t.split("::")[0] for t in tests]
    try:
        covered_paths = list(dict.fromkeys([*test_files, *dirty(root)]))
    except RuntimeError as exc:
        return fail(str(exc), meta)
    record = {
        "run_id": run_id,
        "head": head_sha(root),
        "tests": tests,
        "covered": li.landing_fingerprint(root, covered_paths),
        "status": status,
        "at": now(),
        "isolated_export": meta,
        "export": {"status": status, "failed_tests": export_failed},
        "live": {"status": live_status, "failed_tests": live_failed},
    }
    if live_reason:
        record["live"]["reason"] = live_reason
    if reason:
        record["reason"] = reason
    if status == NO_TESTS_STATUS:
        record["tests_run"] = 0
    if status in ("fail", "error") and export_failed:
        record[li.FAILED_TESTS_PARAM] = export_failed
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
    from coordinator_core.ops.dispatch_emit.land_run import params_from_task_output

    ap = argparse.ArgumentParser(prog="retest_targeted")
    ap.add_argument("--from-task-output", required=True)
    ap.add_argument("--tests", action="append", default=[])
    ap.add_argument("--repo-root")
    ap.add_argument("--dropped", action="append", default=[], help=argparse.SUPPRESS)
    ap.add_argument("--detached-child", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--poll", action="store_true")
    ap.add_argument("--isolated-export", action="store_true")
    args = ap.parse_args(argv)

    try:
        params = params_from_task_output(args.from_task_output)
        root = _repo_root(args.repo_root)
    except (ValueError, RuntimeError) as exc:
        print(f"retest_targeted: {exc}", file=sys.stderr)
        return 2
    run_id = _run_id_of(params)
    if not safe_id(run_id):
        print(f"retest_targeted: run id {run_id!r} is not a single safe path segment", file=sys.stderr)
        return 2

    path = li.fresh_verdict_path(root, run_id)
    if args.poll:
        return _poll(path, args.from_task_output)

    tests = _tests_to_run(args.tests, params)
    if not tests:
        return _write_stale(root, run_id, dirty, head_sha)
    tests = _with_invariants(root, tests, dirty)

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
        return _write_no_tests(root, path, run_id, dropped, drop_note, dirty, head_sha)
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
        covered_paths = _outside_own_records(run_id, list(dict.fromkeys([*test_files, *dirty(root)])))
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
        "covered": li.landing_fingerprint(root, covered_paths),
        "status": status,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    if reason:
        record["reason"] = reason
    if status == NO_TESTS_STATUS:
        record["tests_run"] = 0
    if log_used and status in ("fail", "error") and 1 in codes:
        record[li.FAILED_TESTS_PARAM] = _failed_node_ids(log_path)
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
