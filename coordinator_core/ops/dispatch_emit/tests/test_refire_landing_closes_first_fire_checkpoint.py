"""A halted run that is refired and lands closes the first fire's run-open checkpoint too.

Real git repo; the first fire's plan path differs from the refire's (a moved plan).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_contract, halted_run
from coordinator_core.ops.dispatch_emit.halted_run_close_commit import commit_close_records
from coordinator_core.ops.dispatch_emit.landing_integrity import closing_bases
from coordinator_core.percolate.source_preflight import _open_checkpoints
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "warp-x-20261026T000000-abcdef"
FIRST_PLAN = "docs/plans/2026-11/2026-11-16-fix-4467aa.md"
LIVE_PLAN = "docs/plans/2026-11-16-fix-4467aa.md"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
         "-c", "maintenance.auto=false", *args],
        cwd=str(repo), check=True, capture_output=True, text=True, **no_console_creationflags(),
    )
    return done.stdout.strip()


def _commit(repo: Path, files: dict, message: str) -> str:
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", "--", *files)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _checkpoint(repo: Path, plan: str, base: str, withheld: str, tag: str) -> str:
    message = (
        f"checkpoint(wave 0): 1 rows — C{tag}\n\nCheckpoint-Plan: {plan}\nCheckpoint-Base: {base}\n"
        f"Checkpoint-Not-Landed: run-open\nCheckpoint-Withheld: {withheld}\n"
    )
    return _commit(repo, {plan: f"plan {tag}"}, message)


@pytest.fixture
def scene(tmp_path):
    """B0, first-fire checkpoint (base B1, withholds a.py), refire checkpoint (base B2, withholds b.py)."""
    _git(tmp_path, "init", "-q")
    b0 = _commit(tmp_path, {"seed.txt": "s"}, "seed")
    b1 = _commit(tmp_path, {"x1.txt": "1"}, "first fire base")
    _checkpoint(tmp_path, FIRST_PLAN, b1, "a.py", "1")
    b2 = _commit(tmp_path, {"x2.txt": "2"}, "refire base")
    _checkpoint(tmp_path, LIVE_PLAN, b2, "b.py", "2")
    record = ask_contract.record_dir(tmp_path, RUN)
    halted_run.write_halt_marker(record, halted=RUN, sizing_rel=None, run_base_sha=b2)
    record.joinpath("manifest.json").write_text(json.dumps({"head_sha": b2}), encoding="utf-8")
    record.joinpath("resume.json").write_text(json.dumps({"resume_plan_rel": LIVE_PLAN}), encoding="utf-8")
    return tmp_path, record, b0, b1, b2


def test_close_leg_closes_first_fire_checkpoint_after_landing(scene):
    root, record, b0, b1, b2 = scene
    landing = _commit(root, {"a.py": "a", "b.py": "b"}, "landing")
    halted_run.mark_landed(record, landing)
    assert len(_open_checkpoints(root, b0)) == 2

    out = commit_close_records(root, RUN, record, "close")

    assert "records_commit" in out, out
    assert _open_checkpoints(root, b0) == []


def test_close_leg_leaves_checkpoint_whose_paths_never_landed(scene):
    root, record, b0, b1, b2 = scene
    landing = _commit(root, {"b.py": "b"}, "landing covers only the refire's path")
    halted_run.mark_landed(record, landing)

    out = commit_close_records(root, RUN, record, "close")

    assert "records_commit" in out, out
    assert [plan for _, plan in _open_checkpoints(root, b0)] == [FIRST_PLAN]


def test_landing_bases_include_sibling_only_when_paths_covered(scene):
    root, _, _, b1, b2 = scene
    params = {"plan_path": LIVE_PLAN}

    assert closing_bases(root, RUN, params) == (b2,)
    assert closing_bases(root, RUN, params, landed_paths=["b.py"]) == (b2,)
    assert closing_bases(root, RUN, params, landed_paths=["a.py", "b.py"]) == (b2, b1)
    assert closing_bases(root, RUN, {}, landed_paths=["a.py", "b.py"]) == (b2, b1)  # plan name read from resume.json
    assert closing_bases(root, "warp-no-record", {}, landed_paths=["a.py", "b.py"]) == ()


def test_other_plan_checkpoint_is_not_closed(scene):
    root, _, _, b1, b2 = scene
    other = _commit(root, {"x3.txt": "3"}, "other base")
    _checkpoint(root, "docs/plans/2026-11/unrelated-aaaaaa.md", other, "c.py", "3")

    assert closing_bases(root, RUN, {"plan_path": LIVE_PLAN}, landed_paths=["a.py", "b.py", "c.py"]) == (b2, b1)


def test_checkpoint_older_than_scan_cap_stays_open(scene, monkeypatch, caplog):
    from coordinator_core.ops.dispatch_emit import landing_integrity

    root, _, _, b1, b2 = scene
    for i in range(5):
        _commit(root, {f"pad{i}.txt": "p"}, f"pad {i}")
    _commit(root, {"a.py": "a", "b.py": "b"}, "landing")
    params = {"plan_path": LIVE_PLAN}

    monkeypatch.setattr(landing_integrity, "_SIBLING_LOG_CAP", 400)
    assert closing_bases(root, RUN, params, landed_paths=["a.py"]) == (b2, b1)
    monkeypatch.setattr(landing_integrity, "_SIBLING_LOG_CAP", 7)  # first-fire checkpoint is 9 commits deep
    with caplog.at_level("WARNING"):
        assert closing_bases(root, RUN, params, landed_paths=["a.py"]) == (b2,)
    assert "scan hit the 7-commit cap" in caplog.text


def test_checkpoint_with_no_withheld_paths_is_not_closed_by_any_landing(scene):
    root, _, _, b1, b2 = scene
    empty_base = _commit(root, {"x4.txt": "4"}, "empty base")
    _checkpoint(root, FIRST_PLAN, empty_base, "", "4")

    assert closing_bases(root, RUN, {"plan_path": LIVE_PLAN}, landed_paths=["a.py", "b.py"]) == (b2, b1)


def test_close_leg_ignores_a_later_commit_by_another_run(scene):
    root, record, b0, _, b2 = scene
    _commit(root, {"a.py": "other run"}, "unrelated commit touching the first fire's withheld path")
    landing = _commit(root, {"b.py": "b"}, "the run's own landing")
    halted_run.mark_landed(record, landing)

    commit_close_records(root, RUN, record, "close")

    assert [plan for _, plan in _open_checkpoints(root, b0)] == [FIRST_PLAN]


def test_unlanded_close_closes_only_own_bases(scene):
    root, record, b0, _, b2 = scene
    _commit(root, {"a.py": "a", "b.py": "b"}, "later commit covering both paths")

    commit_close_records(root, RUN, record, "close")

    assert [plan for _, plan in _open_checkpoints(root, b0)] == [FIRST_PLAN]


def test_landing_commit_trailers_close_first_fire_checkpoint(scene):
    """The landing commit's own trailer builder passes its committed paths, so the first fire's base closes."""
    from coordinator_core.ops.dispatch_emit.land_run import _run_closed_lines

    root, _, _, b1, b2 = scene
    params = {"plan_path": LIVE_PLAN}

    assert _run_closed_lines(root, RUN, params, ["a.py", "b.py"]) == [
        f"Checkpoint-Run-Closed: {b2}", f"Checkpoint-Run-Closed: {b1}",
    ]
    assert _run_closed_lines(root, RUN, params, ["b.py"]) == [f"Checkpoint-Run-Closed: {b2}"]
