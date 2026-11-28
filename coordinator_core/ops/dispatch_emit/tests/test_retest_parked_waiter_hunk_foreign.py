"""A retest red whose test file is a declared write of a run parked behind the retesting run is recorded
foreign ``peer-held-path`` with ``baseline.evidence`` credit; a red on the run's own path, or on a path no
waiter declares, stays caused. Stub runner and exports stand in for pytest/git; the parked relation and the
waiter's write set are stubbed at their seams."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_hold_release, hunk_provenance, land_run, landing_export as le
from coordinator_core.ops.dispatch_emit import landing_retest, landing_targeted_tests, retest_targeted, run_write_set

RUN_ID = "run-blocker"
WAITER = "run-waiter"
INV = "test_a.py::t0"
OWN = "src/own.py"


def _retest(
    tmp_path: Path, monkeypatch, *, names: str, waiters: list[str], waiter_writes: list[str],
    own: tuple = (OWN,), unproduced: str = "",
) -> dict:
    """The record the isolated retest child writes when ``INV`` is red in the export with a failure text
    ``names`` and green at HEAD, while ``waiters`` are parked behind ``RUN_ID`` declaring ``waiter_writes``.
    ``own`` is the run's declared write set; ``unproduced`` the line ranges of ``test_a.py`` (two lines:
    line 1 the run's own hunk, line 2 the waiter's ``def t0``) that provenance reports as not the run's."""
    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", "pytest -q")
    root = tmp_path / "repo"
    rec = root / "state" / "warp" / RUN_ID
    rec.mkdir(parents=True)
    (rec / "manifest.json").write_text(json.dumps({"review_declared_paths": [*own]}), encoding="utf-8")
    (root / "test_a.py").write_text("# own hunk\ndef t0(): pass\n", encoding="utf-8")
    monkeypatch.setattr(hunk_provenance, "_unproduced", lambda _root, _run, _path: (unproduced, [], b""))
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
    monkeypatch.setattr(land_run, "_peer_held", lambda _root, paths, *a, **k: {})
    monkeypatch.setattr(
        halted_hold_release, "_parked_behind", lambda _root, blocker: list(waiters) if blocker == RUN_ID else [],
    )
    real = run_write_set.run_write_set_full
    monkeypatch.setattr(
        run_write_set, "run_write_set_full",
        lambda r, run, sid: (tuple(waiter_writes), {}) if run == WAITER else real(r, run, sid),
    )

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
                    f"=== FAILURES ===\n___ t0 ___\nE   {names}\n\n=== short test summary info ===\n"
                    f"FAILED {INV} - {names}\n1 failed, 1 passed in 0.1s\n".encode()
                )
            else:
                sink.write(b"2 passed in 0.1s\n")
        return 1 if red else 0

    retest_targeted.main(
        ["--from-task-output", str(out), "--repo-root", str(root), "--isolated-export", "--detached-child"],
        run=_run, dirty=lambda r: [],
    )
    return json.loads(landing_retest.fresh_verdict_path(root, RUN_ID).read_text(encoding="utf-8"))


def test_a_red_on_a_parked_waiters_test_hunk_is_foreign_credited_and_not_refused(tmp_path, monkeypatch):
    record = _retest(tmp_path, monkeypatch, names="the gate is red", waiters=[WAITER], waiter_writes=["test_a.py"])

    baseline = record["baseline"]
    assert baseline["caused"] == [] and baseline["foreign"] == [INV]
    assert baseline["foreign_reason"] == {INV: landing_targeted_tests.PEER_HELD_PATH}
    assert INV in baseline["evidence"]
    assert landing_targeted_tests.failed_targeted_tests(record) == ([], "pre-existing-only")


def test_a_red_on_a_path_no_waiter_declares_stays_caused(tmp_path, monkeypatch):
    record = _retest(tmp_path, monkeypatch, names="the gate is red", waiters=[WAITER], waiter_writes=["other_test.py"])

    assert record["baseline"]["caused"] == [INV]
    assert landing_targeted_tests.failed_targeted_tests(record)[1] == "failed"


def test_a_run_with_no_waiter_keeps_the_red_caused(tmp_path, monkeypatch):
    record = _retest(tmp_path, monkeypatch, names="the gate is red", waiters=[], waiter_writes=["test_a.py"])

    assert record["baseline"]["caused"] == [INV]
    assert "foreign_reason" not in record["baseline"]


def test_a_red_naming_the_runs_own_path_stays_caused_beside_a_waiters_hunk(tmp_path, monkeypatch):
    record = _retest(tmp_path, monkeypatch, names=f"bad {OWN}", waiters=[WAITER], waiter_writes=["test_a.py"])

    assert record["baseline"]["caused"] == [INV]
    assert landing_targeted_tests.failed_targeted_tests(record)[1] == "failed"


def test_a_red_on_the_waiters_hunk_of_a_test_file_both_runs_declare_is_foreign(tmp_path, monkeypatch):
    record = _retest(
        tmp_path, monkeypatch, names="the gate is red", waiters=[WAITER], waiter_writes=["test_a.py"],
        own=(OWN, "test_a.py"), unproduced="2",
    )

    baseline = record["baseline"]
    assert baseline["caused"] == [] and baseline["foreign"] == [INV]
    assert baseline["foreign_reason"] == {INV: landing_targeted_tests.PEER_HELD_PATH}
    assert landing_targeted_tests.failed_targeted_tests(record) == ([], "pre-existing-only")


def test_a_red_on_the_runs_own_hunk_of_a_shared_test_file_stays_caused(tmp_path, monkeypatch):
    record = _retest(
        tmp_path, monkeypatch, names="the gate is red", waiters=[WAITER], waiter_writes=["test_a.py"],
        own=(OWN, "test_a.py"), unproduced="1",
    )

    assert record["baseline"]["caused"] == [INV]
    assert landing_targeted_tests.failed_targeted_tests(record)[1] == "failed"


def test_reds_in_one_shared_test_file_cost_one_provenance_lookup(tmp_path, monkeypatch):
    (tmp_path / "test_a.py").write_text("# own hunk\ndef t0(): pass\ndef t1(): pass\n", encoding="utf-8")
    calls: list[str] = []

    def _unproduced(_root, _run, path):
        calls.append(path)
        return "2-3", [], b""

    monkeypatch.setattr(hunk_provenance, "_unproduced", _unproduced)
    monkeypatch.setattr(halted_hold_release, "_parked_behind", lambda _root, _blocker: [WAITER])
    monkeypatch.setattr(run_write_set, "run_write_set_full", lambda *_a: (("test_a.py",), {}))
    monkeypatch.setattr(retest_targeted, "_named_paths", lambda *_a: {"test_a.py::t0": set(), "test_a.py::t1": set()})
    monkeypatch.setattr(run_write_set, "_split_peer_held", lambda *_a: ([], {}))

    out = retest_targeted._peer_held_reds(
        tmp_path, tmp_path, tmp_path / "log", RUN_ID, ["test_a.py"], ["test_a.py::t0", "test_a.py::t1"],
    )

    assert out == {"test_a.py::t0": ["test_a.py"], "test_a.py::t1": ["test_a.py"]}
    assert calls == ["test_a.py"]


def test_a_shared_test_file_with_no_parked_waiter_keeps_the_red_caused(tmp_path, monkeypatch):
    record = _retest(
        tmp_path, monkeypatch, names="the gate is red", waiters=[], waiter_writes=["test_a.py"],
        own=(OWN, "test_a.py"), unproduced="2",
    )

    assert record["baseline"]["caused"] == [INV]
    assert "foreign_reason" not in record["baseline"]
