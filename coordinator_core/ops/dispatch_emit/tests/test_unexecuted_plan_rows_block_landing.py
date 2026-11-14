"""A staged plan row that never executed blocks both landing paths from carrying the executed rows' code."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import run_artifacts, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    RECORD_DIR_ROOT,
    RUN_DIR_ROOT,
    ManifestRow,
    StageManifest,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401
    LEDGER,
    PASSING,
    RUN,
    _call,
    _git,
    _landed,
    _write_record,
    repo,
)
from coordinator_core.ops.dispatch_emit.unexecuted_plan_rows import (
    WITHHOLD_REASON,
    message,
    unexecuted_plan_rows,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REVIEWED = reviewed()
EXECUTED = ("C1", "C2")
GUARDS = ("C4", "C5")
PRODUCT = {"C1": "c1.py", "C2": "c2.py", "C4": "c4.py", "C5": "c5.py"}
NAMES_GUARDS = message(GUARDS)


def _row(row_id: str, wave: int) -> ManifestRow:
    return ManifestRow(
        id=row_id, agent_type="coordinator:executor", model="sonnet", brief_path=f"briefs/{row_id}.md",
        writes=(PRODUCT.get(row_id, f"{row_id}.py"),), wave=wave,
    )


def _plan_rows() -> tuple[ManifestRow, ...]:
    return (_row("C1", 0), _row("C2", 0), _row("C4", 1), _row("C5", 1))


# -- the helper -------------------------------------------------------------------------------


def test_helper_names_staged_rows_after_the_executed_wave():
    assert unexecuted_plan_rows(_plan_rows(), ["C1", "C2"]) == ("C4", "C5")


def test_helper_counts_a_row_alongside_the_executed_wave():
    rows = (_row("C1", 0), _row("C2", 0), _row("C3", 0))
    assert unexecuted_plan_rows(rows, ["C1"]) == ("C2", "C3")


def test_helper_ignores_an_unexecuted_row_earlier_than_every_executed_row():
    rows = (_row("C1", 0), _row("C2", 1), _row("C3", 2))
    assert unexecuted_plan_rows(rows, ["C2"]) == ("C3",)


def test_helper_returns_nothing_when_no_row_executed():
    assert unexecuted_plan_rows(_plan_rows(), []) == ()


def test_helper_returns_nothing_when_every_row_executed():
    assert unexecuted_plan_rows(_plan_rows(), ["C1", "C2", "C4", "C5"]) == ()


def test_helper_treats_a_ran_and_stopped_row_as_executed():
    # Callers add halted["stopped_rows"] to the report-backed ids: a row that ran and stopped is not unexecuted.
    assert unexecuted_plan_rows(_plan_rows(), ["C1", "C2", "C4"]) == ("C5",)


def test_helper_ids_are_sorted_and_the_message_names_them():
    rows = (_row("C5", 1), _row("C1", 0), _row("C4", 1))
    assert unexecuted_plan_rows(rows, ["C1"]) == ("C4", "C5")
    assert message(("C4", "C5")) == "unexecuted plan rows: C4, C5"
    assert WITHHOLD_REASON == "unexecuted-plan-rows"


# -- normal land ------------------------------------------------------------------------------


def _stage_normal(root: Path, executed: tuple[str, ...]) -> str:
    run_dir = root / RUN_DIR_ROOT / "run1"
    run_dir.mkdir(parents=True)
    (root / "reports").mkdir()
    chunks = []
    for cid in ("C1", "C2", "C4", "C5"):
        if cid in executed:
            (root / PRODUCT[cid]).write_text(f"{cid} = 1\n", encoding="utf-8")
            (root / "reports" / f"{cid}.md").write_text(f"DONE: {cid}\n", encoding="utf-8")
        chunks.append(ChunkCommit(id=cid, title=cid, paths=(PRODUCT[cid],), report=f"reports/{cid}.md"))
    request = CommitRequest(chunks=tuple(chunks))
    (run_dir / "commit-request.txt").write_text(render_marker(request) + "\n", encoding="utf-8")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/run1",
        rows=_plan_rows(),
        review_declared_paths=tuple(PRODUCT.values()),
        marker_path=f"{RUN_DIR_ROOT}/run1/commit-request.txt",
        write_baseline={p: None for p in PRODUCT.values()},
    )
    (run_dir / "manifest.json").write_text(json.dumps(manifest.to_json()), encoding="utf-8")
    return f"{RUN_DIR_ROOT}/run1/manifest.json"


def _land_normal(root: Path, executed: tuple[str, ...]) -> dict:
    manifest = _stage_normal(root, executed)
    (root / "run.mjs").write_text(f"{ASK_MANIFEST_MARKER}{manifest}\n", encoding="utf-8")
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED}, repo_root=root / ".git"
    )


def _paths_committed_since_seed(root: Path) -> set:
    seed = _git(["rev-list", "--max-parents=0", "HEAD"], root).split()[0]
    out = _git(["log", "--name-only", "--format=", f"{seed}..HEAD"], root)
    return {line for line in out.splitlines() if line}


def test_normal_land_withholds_executed_rows_code_while_guard_rows_are_unexecuted(repo):
    out = _land_normal(repo, EXECUTED)

    assert not ({"c1.py", "c2.py"} & _paths_committed_since_seed(repo)), out
    assert NAMES_GUARDS in json.dumps(out), out
    assert (repo / "c1.py").exists() and (repo / "c2.py").exists()


def test_normal_land_with_every_row_executed_lands_the_code(repo):
    out = _land_normal(repo, EXECUTED + GUARDS)

    assert out["committed"] is True, out
    assert {"c1.py", "c2.py", "c4.py", "c5.py"} <= _paths_committed_since_seed(repo)
    assert "unexecuted plan rows" not in json.dumps(out)


# -- halt commit ------------------------------------------------------------------------------


def _stage_halted(root: Path, executed: tuple[str, ...]) -> None:
    from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import _marker_script

    rec = _write_record(root, RUN)
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}",
        rows=_plan_rows(),
        review_declared_paths=tuple(PRODUCT.values()),
        marker_path=f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt",
        write_baseline={p: None for p in PRODUCT.values()},
    )
    (rec / "manifest.json").write_text(json.dumps(manifest.to_json()), encoding="utf-8")
    reports = {}
    for cid in executed:
        (root / PRODUCT[cid]).write_text(f"{cid} = 1\n", encoding="utf-8")
        reports[cid] = f"DONE: {cid}\n"
    _marker_script(root, [(cid, [PRODUCT[cid]]) for cid in ("C1", "C2", "C4", "C5")], reports)


def test_halt_commit_withholds_executed_rows_code_and_names_the_unexecuted_rows(repo):
    _stage_halted(repo, EXECUTED)

    out = _call(repo, LEDGER, **PASSING)

    assert out["committed"] is True, out
    names = _landed(repo, out, "--name-only")
    assert run_artifacts.outcome_rel(RUN) in names
    assert "c1.py" not in names and "c2.py" not in names
    for path in ("c1.py", "c2.py"):
        assert NAMES_GUARDS in out["withheld"][path], out["withheld"]


def test_halt_commit_with_every_row_executed_lands_the_code(repo):
    _stage_halted(repo, EXECUTED + GUARDS)

    out = _call(repo, LEDGER, **PASSING)

    assert out["committed"] is True, out
    names = _landed(repo, out, "--name-only")
    assert {"c1.py", "c2.py", "c4.py", "c5.py"} <= set(names)
    assert not any("unexecuted plan rows" in str(v) for v in out.get("withheld", {}).values())
