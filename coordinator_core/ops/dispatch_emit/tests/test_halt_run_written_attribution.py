"""A halting run names only paths attributable to it. The touch record is session-keyed, so with
another run live a path touched in the run's window but outside its own `write_baseline` stays put."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halt_tree_check, terminal_commit
from coordinator_core.ops.dispatch_emit.halted_record_eviction import _run_written, plan_tree_eviction
from coordinator_core.session import core, touch_record

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN_A, RUN_B = "run-a", "run-b"
A_FILE, B_FILE = "a_own.py", "b_dirty.py"
REC = f"state/warp/{RUN_A}"


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "root"
    (root / REC).mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    for rel in (A_FILE, B_FILE):
        (root / rel).write_text("X = 0\n", encoding="utf-8")
    subprocess.run(["git", "add", A_FILE, B_FILE], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "seed"], cwd=root, check=True,
    )
    (root / A_FILE).write_text("X = 1\n", encoding="utf-8")
    (root / B_FILE).write_text("X = 2\n", encoding="utf-8")
    (root / REC / "manifest.json").write_text(
        json.dumps({"rows": [], "write_baseline": {A_FILE: None}}), encoding="utf-8"
    )
    hub = tmp_path / "hub"
    monkeypatch.setattr(core, "sessions_dir", lambda *_a, **_k: str(hub))
    monkeypatch.setattr(terminal_commit, "_run_started_at", lambda *_a, **_k: 100.0)
    for path in (A_FILE, B_FILE):  # one session, so the record cannot say which run wrote which
        touch_record.append_event(
            touch_record.sink_path(hub / "sess-1"), session_id="sess-1", agent_id=None,
            verb=touch_record.VERB_TOUCH, path=path, timestamp=200.0, kind=touch_record.KIND_WRITE, name=None,
        )
    monkeypatch.setattr(halt_tree_check, "broken_paths", lambda r, own, ws: {})
    return root


def test_peer_runs_dirt_is_not_named_while_it_is_live(world: Path) -> None:
    assert _run_written(world, "sess-1", RUN_A, [RUN_A, RUN_B]) == [A_FILE]


def test_no_contention_keeps_the_whole_window(world: Path) -> None:
    assert _run_written(world, "sess-1", RUN_A, [RUN_A]) == [A_FILE, B_FILE]
    assert _run_written(world, "sess-1", RUN_A, None) == [A_FILE, B_FILE]


def test_halt_parks_own_declared_path_and_leaves_the_tree_untouched(world: Path) -> None:
    written = _run_written(world, "sess-1", RUN_A, [RUN_A, RUN_B])
    plan = plan_tree_eviction(world, REC, written, {A_FILE, B_FILE}, written, {}, [])
    assert plan is not None
    plan.apply()
    assert plan.evicted == [A_FILE]
    assert (world / A_FILE).read_text(encoding="utf-8") == "X = 1\n"
    assert (world / B_FILE).read_text(encoding="utf-8") == "X = 2\n"
