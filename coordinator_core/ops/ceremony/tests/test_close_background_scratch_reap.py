"""The scratch-reap close leg, driven through run_background with the default runners."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from coordinator_core import run_scratch as run_scratch_mod
from coordinator_core.ops.ceremony import close_background as cb
from coordinator_core.ops.dispatch_emit import run_close, run_scratch

RUN = "ask-20261109T000000-aaaaaa"
PRED = "ask-20261108T000000-cccccc"
PEER = "ask-20261109T000001-bbbbbb"

pytestmark = pytest.mark.complete


DEAD_PID = 2_000_000_000


@pytest.fixture(autouse=True)
def box_roots(tmp_path, monkeypatch):
    scratch, build = tmp_path / "box-scratch", tmp_path / "box-build"
    monkeypatch.setenv("MACHINE_LOCAL_PATHS_SCRATCH_ROOT", str(scratch))
    monkeypatch.setenv("MACHINE_LOCAL_PATHS_BUILD_ROOT", str(build))
    return scratch, build


def _tree(root: Path, rel: str) -> Path:
    p = root / rel
    p.mkdir(parents=True, exist_ok=True)
    (p / "f.txt").write_text("x", encoding="utf-8")
    return p


def _marker(root: Path, run_id: str, status: str) -> None:
    rec = root / "state" / "warp" / run_id
    rec.mkdir(parents=True, exist_ok=True)
    (rec / "halted.json").write_text(json.dumps({"status": status}), encoding="utf-8")


def _run(root: Path, run_id: str = RUN, **record_extra):
    (root / ".git").mkdir(exist_ok=True)
    (root / ".git" / "index").write_bytes(b"DIRC" + (2).to_bytes(4, "big"))
    run_close.write_close_job(
        root, run_id, ("scratch-reap",), datetime.now().astimezone() + timedelta(minutes=10)
    )
    if record_extra:
        path = root / run_close.close_job_path(run_id)
        record = json.loads(path.read_text(encoding="utf-8"))
        record.update(record_extra)
        path.write_text(json.dumps(record), encoding="utf-8")
    ctx = cb.JobContext(root, root / ".git", run_id, (), None)
    return cb.run_background(ctx)["scratch-reap"]


def _gone(root: Path, rel: str) -> bool:
    return not (root / rel).exists()


def test_landed_run_scratch_reaped(tmp_path):
    _tree(tmp_path, f"scratch/warp/{RUN}")
    _tree(tmp_path, "scratch/land-x")
    _tree(tmp_path, "scratch/probe-y")
    run_scratch.record_paths(tmp_path, RUN, ["scratch/land-x", "scratch/probe-y"])
    _tree(tmp_path, "scratch/land-unlisted")

    result = _run(tmp_path)

    assert result.status == "ran" and "removed 3" in result.detail
    assert all(_gone(tmp_path, r) for r in (f"scratch/warp/{RUN}", "scratch/land-x", "scratch/probe-y"))
    assert (tmp_path / "scratch/land-unlisted").is_dir()
    assert len(list((tmp_path / "scratch" / ".deleted").iterdir())) == 3


def test_halted_run_keeps_only_resume_inputs_until_landed(tmp_path):
    warp = _tree(tmp_path, f"scratch/warp/{RUN}")
    (warp / "run-base").write_text("sha", encoding="utf-8")
    _tree(tmp_path, f"scratch/warp/{RUN}/export-tree")
    _marker(tmp_path, RUN, "halted")

    result = _run(tmp_path)
    assert result.status == "ran" and "held" not in result.detail
    assert (warp / "run-base").is_file()
    assert _gone(tmp_path, f"scratch/warp/{RUN}/export-tree")
    assert _gone(tmp_path, f"scratch/warp/{RUN}/f.txt")

    _marker(tmp_path, RUN, "landed")
    _run(tmp_path)
    assert _gone(tmp_path, f"scratch/warp/{RUN}")


def test_unexpired_keep_survives_the_leg(tmp_path):
    _tree(tmp_path, "scratch/probe-keep")
    run_scratch.declare_keep(tmp_path, RUN, "scratch/probe-keep", "baseline for the next run", 24)

    result = _run(tmp_path)

    assert "kept 1" in result.detail
    assert (tmp_path / "scratch/probe-keep").is_dir()


def test_resumed_predecessor_scratch_is_reaped_with_the_run(tmp_path):
    _tree(tmp_path, f"scratch/warp/{PRED}")
    _marker(tmp_path, PRED, "superseded")

    result = _run(tmp_path, resume={"run_id": PRED, "sizing": "s", "halted": "parked", "stage": {}})

    assert result.status == "ran"
    assert _gone(tmp_path, f"scratch/warp/{PRED}")


def test_resume_halt_sha_never_names_a_run(tmp_path):
    sha = "a" * 40
    _tree(tmp_path, f"scratch/warp/{sha}")

    result = _run(tmp_path, inputs={"resume_halt_sha": sha})

    assert result.status == "not-applicable"
    assert (tmp_path / "scratch/warp" / sha).is_dir()


def test_peer_ledger_path_is_untouched(tmp_path):
    _tree(tmp_path, f"scratch/warp/{PEER}")
    _tree(tmp_path, "scratch/land-peer")
    run_scratch.record_paths(tmp_path, PEER, ["scratch/land-peer"])

    result = _run(tmp_path)

    assert result.status == "not-applicable"
    assert (tmp_path / "scratch/land-peer").is_dir() and (tmp_path / "scratch/warp" / PEER).is_dir()


def test_run_with_no_scratch_is_not_applicable(tmp_path):
    assert _run(tmp_path).status == "not-applicable"


def test_close_leg_sweeps_dead_pid_claims_and_keeps_live_ones(tmp_path):
    live = run_scratch_mod.claim("claude-klabauter", "land", "y", "scratch")
    dead = run_scratch_mod.claim("claude-klabauter", "land", "x", "scratch", pid=DEAD_PID)
    dead_build = run_scratch_mod.claim("claude-klabauter", "clone", "z", "build", pid=DEAD_PID)

    result = _run(tmp_path)

    assert result.status == "ran" and "run-scratch swept 2" in result.detail
    assert not dead.exists() and not dead_build.exists()
    assert live.is_dir()


def test_close_leg_prunes_complete_builds_beyond_keep_two(tmp_path):
    paths = [run_scratch_mod.claim("claude-klabauter", "clone", f"b{i}", "build") for i in range(3)]
    for path in paths:
        run_scratch_mod.finish(path, keep=99)

    result = _run(tmp_path)

    assert "run-scratch swept 1" in result.detail
    assert sum(p.exists() for p in paths) == 2


def test_close_leg_with_only_live_claims_stays_not_applicable(tmp_path):
    live = run_scratch_mod.claim("claude-klabauter", "land", "y", "scratch")

    assert _run(tmp_path).status == "not-applicable"
    assert live.is_dir()


def test_leg_is_registered_and_ordered_after_close_commit():
    assert "scratch-reap" in cb.DEFAULT_RUNNERS and "scratch-reap" in run_close.RUN_BOUND_LEGS
    assert cb.BACKGROUND_LEGS.index("scratch-reap") > cb.BACKGROUND_LEGS.index("close-commit")
