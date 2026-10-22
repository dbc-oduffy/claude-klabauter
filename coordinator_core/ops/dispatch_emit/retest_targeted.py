"""Re-run a finished run's failing targeted tests and write the fresh verdict the landing gate reads.

Runs outside any engine op: ``python -m coordinator_core.ops.dispatch_emit.retest_targeted
--from-task-output FILE [--tests PATH ...]``. Exit 0 on pass or stale, 1 on fail, 2 on a read error or an ``error`` verdict (launcher
denied, missing or exited without test results; the record carries ``reason``).

A failure with no baseline names its tests from ``--tests``, else the run's recorded targeted set;
with neither, a ``stale`` verdict with a ``note`` is written and the gate reads it as non-blocking.
Trap: the gate trusts a fresh pass as the run's whole verdict.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Optional, Sequence

from coordinator_core.ops._path_guard import contained_path, safe_id
from coordinator_core.ops.dispatch_emit import landing_integrity as li

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run_id_of(params: dict) -> str:
    """``params.run_id``, else the script stem (an ask script's stem is ``<run_id>.workflow``)."""
    rid = params.get("run_id")
    if isinstance(rid, str) and rid:
        return rid
    return PurePosixPath(str(params.get("script_path") or "").replace("\\", "/")).stem


def _run_argv(argv: list[str], cwd: Path) -> int:
    return subprocess.run(argv, cwd=str(cwd), creationflags=_NO_WINDOW).returncode


def dirty_paths(repo_root: Path) -> list[str]:
    """Repo-relative POSIX paths ``git status`` reports modified, added or untracked.
    Raises RuntimeError when git fails."""
    from coordinator_core.session.session_facts import _dirty_paths

    result = _dirty_paths(repo_root)
    if result["degraded"]:
        raise RuntimeError(result["evidence"])
    return sorted(result["value"]["paths"])


def _repo_root(raw: Optional[str]) -> Path:
    if raw:
        return Path(raw).resolve()
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, creationflags=_NO_WINDOW,
    )
    if proc.returncode != 0:
        raise RuntimeError("not in a git repo; pass --repo-root")
    return Path(proc.stdout.strip())


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
    return shlex.split(diff_scoped_tests.append_test_paths(resolved.stdout.strip(), tests))


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


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    run: Callable[[list[str], Path], int] = _run_argv,
    dirty: Callable[[Path], list[str]] = dirty_paths,
) -> int:
    from coordinator_core.git.git_state import head_sha
    from coordinator_core.ops.dispatch_emit.land_run import params_from_task_output

    ap = argparse.ArgumentParser(prog="retest_targeted")
    ap.add_argument("--from-task-output", required=True)
    ap.add_argument("--tests", action="append", default=[])
    ap.add_argument("--repo-root")
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

    status, reason = "pass", ""
    argv_or_err = _launch_argv(root, tests)
    if isinstance(argv_or_err, str):
        status, reason = "error", argv_or_err
    else:
        try:
            rc = run(argv_or_err, root)
        except OSError as exc:
            rc, reason = None, f"launcher could not start: {exc}"
        if rc is None:
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
    path = li.fresh_verdict_path(root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(f"{path} {status}" + (f": {reason}" if reason else ""))
    return {"pass": 0, "fail": 1}.get(status, 2)


if __name__ == "__main__":
    sys.exit(main())
