"""Clean-clone proof: run the tests covering a run's touched files in a `--shared` clone at its terminal sha.

`prove` records a `CleanCloneVerdict` and, when the clone is red, amends the close job's criterion to
not_met/red-at-sha with the failing ids and the shared-tree dirty paths the clone lacked. The close-background
leg only spawns this module (`RUNNER_MODULE`); the test run itself is bulk work outside the job budget.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Optional, Sequence

from coordinator_core.benchmarks.isolated_clone import (
    mkdtemp_for_clone,
    reap_processes_under,
    rmtree_or_raise,
)
from coordinator_core.bug_blitz import targeted_run
from coordinator_core.bug_blitz.contract import (
    OUTCOME_FAILED,
    OUTCOME_NO_RUNNER,
    OUTCOME_NOTHING_TO_RUN,
    OUTCOME_PASSED,
)
from coordinator_core.git.run import run_git
from coordinator_core.ops.clean_clone_proof_contract import (
    CleanCloneVerdict,
    criterion_for,
    verdict_path,
)
from coordinator_core.ops.dispatch_emit import run_close
from coordinator_core.testing import spawn_depth

_CLONE_PREFIX = "ccp-"
# The launcher's PYTHONPATH and `-p <plugin>` PYTEST_ADDOPTS would make the clone run import the shared tree's code.
_INHERITED_RUNNER_ENV = ("PYTHONPATH", "PYTEST_ADDOPTS", "PYTEST_PLUGINS")
_GIT_QUIET = ("-c", "core.fsmonitor=false", "-c", "core.longpaths=true")


class ProofInputError(RuntimeError):
    """The close job does not carry what the proof needs (record missing, or a sha absent)."""


def _close_range(root: Path, run_id: str) -> tuple[str, str]:
    """(run_base_sha, terminal_sha) of the run's close job. `read_close_job` returns legs only, so the record is read by its public path."""
    try:
        record = json.loads((root / run_close.close_job_path(run_id)).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProofInputError(f"close job for {run_id!r} unreadable: {exc}") from exc
    base, terminal = record.get("run_base_sha"), record.get("terminal_sha")
    if not base or not terminal:
        raise ProofInputError(f"close job for {run_id!r} lacks run_base_sha or terminal_sha")
    return str(base), str(terminal)


def _touched(root: Path, base: str, terminal: str) -> list[str]:
    proc = run_git(["diff", "--name-only", "-z", base, terminal], cwd=str(root))
    if not proc.ok:
        raise ProofInputError(f"git diff {base}..{terminal} failed: {proc.stderr.strip()}")
    return [p for p in proc.stdout.split("\0") if p]


def _masked_paths(root: Path) -> list[str]:
    """Shared-tree paths with uncommitted state: what a clone at the sha cannot see."""
    proc = run_git(["status", "--porcelain", "-z", "--untracked-files=all"], cwd=str(root))
    if not proc.ok:
        return []
    paths: list[str] = []
    entries = iter(proc.stdout.split("\0"))
    for entry in entries:
        if len(entry) < 4:
            continue
        paths.append(entry[3:])
        if entry[0] in "RC":
            next(entries, None)
    return sorted(set(paths))


def _covering_tests(dest: Path, touched: Sequence[str]) -> list[str]:
    """Test files in `touched` plus the covering tests of its Python sources, existence-checked in the clone."""
    from coordinator_core.source_test_map import map_changed_sources

    chosen: set[str] = set()
    py_sources: list[str] = []
    for rel in touched:
        if targeted_run.is_test_file(rel):
            chosen.add(rel)
        elif rel.endswith(".py"):
            py_sources.append(rel)
    if py_sources:
        chosen.update(map_changed_sources(py_sources, str(dest))[0])
    return sorted(rel for rel in chosen if (dest / rel).is_file())


def _plans(dest: Path, selected: Sequence[str]) -> Optional[list[tuple[str, list[str]]]]:
    plans: list[tuple[str, list[str]]] = []
    for group in (
        [f for f in selected if f.endswith(".py")],
        [f for f in selected if not f.endswith(".py")],
    ):
        if not group:
            continue
        head = targeted_run.head_for(dest, group)
        if head is None:
            return None
        plans.append((head[0], [*head[1], *group]))
    return plans


def _run_in_clone(dest: Path, selected: Sequence[str]) -> tuple[str, list[str]]:
    """(outcome, failing ids) of the selected tests run with cwd=dest and the launcher's PYTHONPATH/PYTEST_ADDOPTS/PYTEST_PLUGINS dropped."""
    from coordinator_core.testing import tier_t_slots

    plans = _plans(dest, selected)
    if plans is None:
        return OUTCOME_NO_RUNNER, []
    for _, argv in plans:
        if not targeted_run.admit(argv, dest):
            return OUTCOME_NO_RUNNER, []
        if targeted_run._is_pytest_argv(argv) and (
            spawn_depth.refusal_for_spawn(os.environ)
            or spawn_depth.selection_includes_spawner(argv[3:], os.environ, str(dest))
        ):
            return OUTCOME_NO_RUNNER, []
    cmd = shlex.join(tok for _, a in plans for tok in a)
    with tier_t_slots.held_slot(tier_t_slots.owner_id(), cmd, targeted_run.SLOT_WAIT_S) as slot:
        if slot is None:
            return OUTCOME_NO_RUNNER, []
        saved = {k: os.environ.pop(k) for k in _INHERITED_RUNNER_ENV if k in os.environ}
        try:
            worst, failing = targeted_run._run_plans(plans, dest, targeted_run.DEFAULT_TIMEOUT)
        finally:
            os.environ.update(saved)
    return (OUTCOME_PASSED if worst == 0 else OUTCOME_FAILED), failing


def _clone_at(root: Path, dest: Path, sha: str) -> None:
    clone = run_git(
        [*_GIT_QUIET, "clone", "-q", "--shared", "--no-checkout", str(root), str(dest)],
        cwd=str(dest.parent),
        remote=True,
    )
    if not clone.ok:
        raise RuntimeError(f"clean-clone-proof: git clone failed rc={clone.returncode}: {clone.stderr.strip()}")
    checkout = run_git(
        [*_GIT_QUIET, "-C", str(dest), "checkout", "-q", "--detach", sha], cwd=str(dest.parent), remote=True
    )
    if not checkout.ok:
        raise RuntimeError(f"clean-clone-proof: checkout {sha} failed: {checkout.stderr.strip()}")


def _write_verdict(root: Path, verdict: CleanCloneVerdict) -> None:
    path = root / verdict_path(verdict.sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(verdict.to_json(), indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def prove(repo_root: Path, run_id: str) -> CleanCloneVerdict:
    """Prove `run_id` from a clean clone at its terminal sha; write the verdict, amend the close job on red."""
    root = Path(repo_root)
    base, terminal = _close_range(root, run_id)
    touched = _touched(root, base, terminal)
    masked = _masked_paths(root)

    tmp = mkdtemp_for_clone(root, prefix=_CLONE_PREFIX)
    try:
        dest = tmp / "c"
        _clone_at(root, dest, terminal)
        selected = _covering_tests(dest, touched)
        outcome, failing = (OUTCOME_NOTHING_TO_RUN, []) if not selected else _run_in_clone(dest, selected)
    finally:
        reaped = reap_processes_under(tmp)
        rmtree_or_raise(tmp, label="clean-clone-proof", reaped=reaped)

    verdict = CleanCloneVerdict(
        sha=terminal,
        run_id=run_id,
        run_base_sha=base,
        selected=tuple(selected),
        outcome=outcome,
        failing_ids=tuple(failing),
        masked_paths=tuple(masked),
    )
    _write_verdict(root, verdict)
    criterion = criterion_for(verdict)
    if criterion is not None:
        run_close.amend_close_job(root, run_id, criterion=criterion)
    return verdict


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI: exit 0 unless the clone is red (1) or the close job cannot be proven (2)."""
    parser = argparse.ArgumentParser(prog="clean_clone_proof")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    try:
        verdict = prove(Path(args.repo).resolve(), args.run_id)
    except ProofInputError as exc:
        print(f"clean-clone-proof: {exc}", file=sys.stderr)
        return 2
    print(f"clean-clone-proof: {verdict.outcome} at {verdict.sha[:12]}", file=sys.stderr)
    return 1 if verdict.outcome == OUTCOME_FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
