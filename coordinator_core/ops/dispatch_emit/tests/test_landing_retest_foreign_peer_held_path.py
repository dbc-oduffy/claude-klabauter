"""A landing retest red whose failure names a path outside the run's writes that a live peer run holds is
recorded foreign ``peer-held-path`` and not refused; a red naming the run's own path, or a path no live run
holds, stays caused. Stub runner and exports stand in for pytest/git; the claim lookup is stubbed at its seam."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run, landing_export as le
from coordinator_core.ops.dispatch_emit import landing_retest, landing_targeted_tests, retest_targeted

RUN_ID = "run-peer-held"
INV = "test_a.py::t0"
OWN = "src/own.py"
PEER = "coordinator_core/distill/launch_args.py"
UNHELD = "coordinator_core/other/nobody.py"


def _retest(tmp_path: Path, monkeypatch, *, names: list[str], held: set[str]) -> dict:
    """The record the isolated retest child writes when ``INV`` is red in the export with a failure text
    naming ``names`` and green at HEAD; ``held`` are the paths a live peer run holds."""
    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", "pytest -q")
    root = tmp_path / "repo"
    rec = root / "state" / "warp" / RUN_ID
    rec.mkdir(parents=True)
    (rec / "manifest.json").write_text(json.dumps({"review_declared_paths": [OWN]}), encoding="utf-8")
    (root / "test_a.py").write_text("", encoding="utf-8")
    (root / OWN).parent.mkdir(parents=True)
    (root / OWN).write_text("x", encoding="utf-8")
    out = tmp_path / "task.output"
    out.write_text(json.dumps({"next_action": {"params": {"run_id": RUN_ID, "targeted_test_paths": ["test_a.py"]}}}), encoding="utf-8")

    def _build(worktree, out_dir, paths, sha=None):
        shutil.rmtree(out_dir, ignore_errors=True)
        Path(out_dir).mkdir(parents=True)
        (Path(out_dir) / "test_a.py").write_text("", encoding="utf-8")
        return le.LandingExport(Path(out_dir).as_posix(), sha or "h" * 40, (), ())

    monkeypatch.setattr(le, "build_head_export", _build)
    monkeypatch.setattr(
        land_run, "_peer_held", lambda _root, paths, *a, **k: {p: ["peer-session"] for p in paths if p in held},
    )
    detail = "AssertionError: undispositioned writes: " + ", ".join(names)

    def _run(argv, cwd, *, log=None, timeout=None, append=False):
        cwd = Path(cwd)
        if "--collect-only" in argv:
            with open(log, "ab" if append else "wb") as sink:
                sink.write(b"test_a.py::t0\ntest_a.py::t1\n")
            return 0
        red = cwd.name == "export" or cwd == root
        with open(log, "ab" if append else "wb") as sink:
            if red:
                sink.write(
                    f"=== FAILURES ===\n___ t0 ___\nE   {detail}\n\n=== short test summary info ===\n"
                    f"FAILED {INV} - {detail}\n1 failed, 1 passed in 0.1s\n".encode()
                )
            else:
                sink.write(b"2 passed in 0.1s\n")
        return 1 if red else 0

    retest_targeted.main(
        ["--from-task-output", str(out), "--repo-root", str(root), "--isolated-export", "--detached-child"],
        run=_run, dirty=lambda r: [],
    )
    return json.loads(landing_retest.fresh_verdict_path(root, RUN_ID).read_text(encoding="utf-8"))


def test_a_red_naming_a_peer_held_undeclared_path_is_foreign_and_not_refused(tmp_path, monkeypatch):
    record = _retest(tmp_path, monkeypatch, names=[PEER], held={PEER})

    baseline = record["baseline"]
    assert baseline["caused"] == [] and baseline["foreign"] == [INV]
    assert baseline["foreign_reason"] == {INV: landing_targeted_tests.PEER_HELD_PATH}
    assert landing_targeted_tests.unevidenced_ids(baseline) == []
    assert landing_targeted_tests.failed_targeted_tests(record) == ([], "pre-existing-only")


def test_a_red_naming_a_path_no_live_run_holds_stays_caused(tmp_path, monkeypatch):
    record = _retest(tmp_path, monkeypatch, names=[UNHELD], held={PEER})

    assert record["baseline"]["caused"] == [INV]
    assert "foreign_reason" not in record["baseline"]
    assert landing_targeted_tests.failed_targeted_tests(record)[1] == "failed"


def test_a_red_naming_the_runs_own_path_stays_caused_even_beside_a_peer_held_one(tmp_path, monkeypatch):
    record = _retest(tmp_path, monkeypatch, names=[OWN, PEER], held={PEER})

    assert record["baseline"]["caused"] == [INV]
    assert landing_targeted_tests.failed_targeted_tests(record)[1] == "failed"


@pytest.mark.parametrize("held", [set(), {PEER}])
def test_a_red_naming_no_path_stays_caused(tmp_path, monkeypatch, held):
    record = _retest(tmp_path, monkeypatch, names=["the gate is red"], held=held)

    assert record["baseline"]["caused"] == [INV]
