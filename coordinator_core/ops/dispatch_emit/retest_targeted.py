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
    return PurePosixPath(str(params.get("script_path") or "").replace("\\", "/")).stem


#: Bound on one detached targeted rerun, not on the pool: past it the child records ``error``.
RETEST_CHILD_TIMEOUT_SECS = 1800.0

#: Grace past the child timeout before a ``running`` record reads as abandoned.
_DEADLINE_GRACE_SECS = 60.0

#: ``run`` return value for a launcher killed at the timeout.
LAUNCH_TIMED_OUT = "timed-out"

#: Options whose next token is a value, never a collection root.
_VALUE_OPTIONS = frozenset({
    "--rootdir", "-c", "-p", "--confcutdir", "-m", "-k", "--ignore", "--ignore-glob", "--deselect",
    "-o", "--override-ini", "--maxfail", "--tb", "--durations", "-n", "--basetemp",
})


def _run_argv(argv: list[str], cwd: Path, *, log: Optional[Path] = None, timeout: Optional[float] = None):
    """Exit code, or ``LAUNCH_TIMED_OUT``. Trap: the timeout kills only the direct child; the log is a
    file so a surviving grandchild never blocks this call."""
    limit = RETEST_CHILD_TIMEOUT_SECS if timeout is None else timeout
    sink = open(log, "wb") if log is not None else subprocess.DEVNULL
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


def _strip_collection_roots(root: Path, cmd: str) -> list[str]:
    """``cmd`` split, minus positional tokens naming an existing path under ``root``. Option values
    and the program token stay."""
    tokens = shlex.split(cmd)
    kept: list[str] = []
    prev_takes_value = False
    for i, tok in enumerate(tokens):
        if i == 0 or prev_takes_value or tok.startswith("-"):
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
    trees such as ``.salvage-*`` therefore never enter the ``covered`` hash. Raises RuntimeError when
    git fails."""
    from coordinator_core.session.session_facts import _dirty_paths

    result = _dirty_paths(repo_root)
    if result["degraded"]:
        raise RuntimeError(result["evidence"])
    kept = []
    for entry in result["value"]["entries"]:
        xy, path = entry[0], entry[1]
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
    verdict = li.targeted_test_verdict(params)
    baseline = verdict.get("baseline") if isinstance(verdict, dict) else None
    if isinstance(baseline, dict):
        for node in baseline.get("caused") or []:
            tests.append(li._node_file(str(node)))
    if not tests:
        tests = _recorded_targeted_set(params, verdict)
    return list(dict.fromkeys(t for t in tests if t))


def _recorded_targeted_set(params: dict, verdict: object) -> list[str]:
    """Test paths the run recorded as its targeted set: ``tests`` or ``test_paths`` on the verdict,
    else ``params["targeted_test_paths"]``. Empty when none is recorded."""
    holders = [verdict if isinstance(verdict, dict) else {}, params]
    keys = (("tests", "test_paths"), ("targeted_test_paths",))
    for holder, names in zip(holders, keys):
        for name in names:
            raw = holder.get(name)
            if isinstance(raw, list):
                found = [li._node_file(str(n)) for n in raw if isinstance(n, str)]
                if any(found):
                    return found
    return []


def _launch_argv(root: Path, tests: list[str]) -> "list[str] | str":
    """The command that runs ``tests``: the repo's declared ``targeted_test_launcher`` under its
    pinned interpreter (else this one) with the node ids passed through, else ``fast_test_cmd``
    with the paths appended. A string return is the reason no command could be built."""
    from coordinator_core import diff_scoped_tests, resolve_validation_cmd
    from coordinator_core.ops.dispatch_emit import test_interpreter as ti

    declared = resolve_validation_cmd.read_local_md_key(str(root), ti.LAUNCHER_KEY).strip()
    if declared:
        script = declared.split()[0]
        if Path(script).is_absolute() or ".." in re.split(r"[\/]", script) or not (root / script).is_file():
            return f"{ti.LAUNCHER_KEY} {declared!r} is not a repo-relative file that exists"
        pin = ti.resolve_targeted_test_python(root)
        python = pin.command if pin.command and pin.defect is None else sys.executable
        return [python, *shlex.split(declared), *tests]
    resolved = resolve_validation_cmd.resolve_fast_test_cmd(str(root))
    if resolved.returncode != 0:
        return f"fast_test_cmd unresolved: {resolved.stderr.strip()}"
    stripped = shlex.join(_strip_collection_roots(root, resolved.stdout.strip()))
    return shlex.split(diff_scoped_tests.append_test_paths(stripped, tests))


def _write_stale(root: Path, run_id: str, dirty: Callable[[Path], list[str]], head_sha: Callable) -> int:
    """Write a ``stale`` verdict (non-blocking to the landing gate) with a named note when no
    targeted set is recorded and no ``--tests`` was given."""
    try:
        covered_paths = dirty(root)
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
    spawn: Callable[..., bool],
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
    ap.add_argument("--detached-child", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--poll", action="store_true")
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

    escaping = [t for t in tests if contained_path(root / t.split("::")[0], [root]) is None]
    if escaping:
        print("retest_targeted: test path escapes the repo: " + ", ".join(escaping), file=sys.stderr)
        return 2
    missing = [t for t in tests if not (root / t.split("::")[0]).exists()]
    if missing:
        print(
            "retest_targeted: test path does not exist (target absent, nothing run): " + ", ".join(missing),
            file=sys.stderr,
        )
        return 2

    log_path = path.with_suffix(".log")
    if not args.detached_child:
        return _start_detached(args, root, path, log_path, run_id, tests, spawn or spawn_detached)

    status, reason = "pass", ""
    log_used = False
    argv_or_err = _launch_argv(root, tests)
    if isinstance(argv_or_err, str):
        status, reason = "error", argv_or_err
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        log_used = True
        try:
            rc = run(argv_or_err, root, log=log_path, timeout=RETEST_CHILD_TIMEOUT_SECS)
        except OSError as exc:
            rc, reason = None, f"launcher could not start: {exc}"
        if rc == LAUNCH_TIMED_OUT:
            status = "error"
            reason = f"launcher exceeded {RETEST_CHILD_TIMEOUT_SECS:g}s: {shlex.join(argv_or_err)}"
        elif rc is None:
            status = "error"
        elif rc == 1:
            status = "fail"
        elif rc != 0:
            status = "error"
            reason = (
                f"launcher exited {rc} with no test results (denied, missing or crashed launcher): "
                + shlex.join(argv_or_err)
            )

    test_files = [t.split("::")[0] for t in tests]
    try:
        covered_paths = list(dict.fromkeys([*test_files, *dirty(root)]))
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
    if log_used:
        record["log"] = str(log_path)
    _write_record(path, record)
    print(f"{path} {status}" + (f": {reason}" if reason else ""))
    return {"pass": 0, "fail": 1}.get(status, 2)


if __name__ == "__main__":
    sys.exit(main())
