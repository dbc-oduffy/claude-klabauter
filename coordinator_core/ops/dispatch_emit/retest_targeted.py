"""Re-run a finished run's failing targeted tests and write the fresh verdict the landing gate reads.

Runs outside any engine op: ``python -m coordinator_core.ops.dispatch_emit.retest_targeted
--from-task-output FILE [--tests PATH ...]``. Exit 0 on pass, 1 on fail, 2 when nothing can run.

Trap: a recorded failure with no baseline names no test, so ``--tests`` alone decides what the
fresh pass covers; the gate trusts it as the run's whole verdict.
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence

from coordinator_core.ops.dispatch_emit import landing_integrity as li

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


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
    tests = [t.replace("\\", "/") for t in explicit]
    verdict = li.targeted_test_verdict(params)
    baseline = verdict.get("baseline") if isinstance(verdict, dict) else None
    if isinstance(baseline, dict):
        for node in baseline.get("caused") or []:
            tests.append(li._node_file(str(node)))
    return list(dict.fromkeys(t for t in tests if t))


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    run: Callable[[list[str], Path], int] = _run_argv,
    dirty: Callable[[Path], list[str]] = dirty_paths,
) -> int:
    from coordinator_core import diff_scoped_tests, resolve_validation_cmd
    from coordinator_core.git.git_state import head_sha
    from coordinator_core.ops.dispatch_emit import terminal_commit

    ap = argparse.ArgumentParser(prog="retest_targeted")
    ap.add_argument("--from-task-output", required=True)
    ap.add_argument("--tests", action="append", default=[])
    ap.add_argument("--repo-root")
    args = ap.parse_args(argv)

    try:
        params = terminal_commit._params_from_task_output(args.from_task_output)
        root = _repo_root(args.repo_root)
    except (ValueError, RuntimeError) as exc:
        print(f"retest_targeted: {exc}", file=sys.stderr)
        return 2
    run_id = terminal_commit._run_id_of(params, None, params.get("script_path") or "")

    tests = _tests_to_run(args.tests, params)
    if not tests:
        print("retest_targeted: no test to run; pass --tests PATH", file=sys.stderr)
        return 2

    resolved = resolve_validation_cmd.resolve_fast_test_cmd(str(root))
    if resolved.returncode != 0:
        print(f"retest_targeted: {resolved.stderr.strip()}", file=sys.stderr)
        return 2
    cmd = diff_scoped_tests.append_test_paths(resolved.stdout.strip(), tests)
    status = "pass" if run(shlex.split(cmd), root) == 0 else "fail"

    try:
        covered_paths = list(dict.fromkeys([*tests, *dirty(root)]))
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
    path = li.fresh_verdict_path(root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(f"{path} {status}")
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
