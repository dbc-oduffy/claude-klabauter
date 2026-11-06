"""publish.py leaves no handover-pending breadcrumb on any exit and holds it only across stop-and-copy.

Every test drives `publish_mod.main(["row-a"])` through the real `_run_round` and `_main`, reusing the
fixture of `test_publish_quiesces_warm_pair`. AC1: each exit (success, FATAL refusal, BUSY 75, slot-gate
exit, raised exception) reads `handover_pending_remaining_secs() == 0.0` for the shared svc dir and the
engine root, with any raised exception still referenced so a garbage-collected generator cannot mask a
leak. AC3: the breadcrumb is absent while the fetch and the staging run, and present
only inside `_quiesced_engine_roots` (the mid-stage drain, the stop and the land).

Run: python -m pytest coordinator/bin/tests/test_publish_handover_hold_release.py -q
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_publish_quiesces_warm_pair as _q  # noqa: E402

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _remaining(env) -> "list[float]":
    """Remaining hold secs for the shared svc dir (`None`) and the engine root."""
    return [env.bc.handover_pending_remaining_secs(root) for root in (None, env.repo)]


def _assert_released(env) -> None:
    assert _remaining(env) == [0.0, 0.0]


def test_success_leaves_no_handover_hold(tmp_path, monkeypatch):
    env = _q._setup(tmp_path, monkeypatch, True)
    rc, err = _q._run(env)
    assert rc == 0, err
    _assert_released(env)


def test_fatal_refusal_for_a_mid_stage_run_leaves_no_handover_hold(tmp_path, monkeypatch):
    from coordinator_core.warm import live_runs

    monkeypatch.setattr(live_runs, "HOLD_BOUND_S", 0.0)
    env = _q._setup(tmp_path, monkeypatch, True)
    env.live_runs[:] = [live_runs.LiveRun("example-game-repo", "ask-be779d", "implement", "2026-10-31T22:46:00Z")]
    env.leg_in_flight = True
    rc, err = _q._run(env)
    assert rc == 1 and "not copying" in err, err
    _assert_released(env)


def test_busy_75_leaves_no_handover_hold(tmp_path, monkeypatch):
    import contextlib
    import io

    import coordinator_core.locked_write as locked_write

    monkeypatch.delenv(locked_write.CONTENDED_LOCK_WAIT_ENV, raising=False)
    env = _q._setup(tmp_path, monkeypatch, True)
    err = io.StringIO()
    with locked_write.held_lock(Path(env.repo), holder_label="third-party-holder"):
        with contextlib.redirect_stderr(err):
            rc = env.publish_mod.main(["row-a"])
    assert rc == 75, err.getvalue()
    _assert_released(env)


def test_slot_gate_exit_leaves_no_handover_hold(tmp_path, monkeypatch):
    from coordinator_core.percolate import publish_slot

    env = _q._setup(tmp_path, monkeypatch, True)

    def held(*_a, **_k):
        raise publish_slot.PublishSlotHeldError("peer", time.time() + 60, "mirror")

    monkeypatch.setattr(publish_slot, "slot_remote", lambda root: "origin")
    monkeypatch.setattr(publish_slot, "mirror_name", lambda root: "mirror")
    monkeypatch.setattr(publish_slot, "make_holder", lambda session_id: object())
    monkeypatch.setattr(publish_slot, "acquire_slot", held)
    rc, err = _q._run(env)
    assert rc == 75, err
    _assert_released(env)


@pytest.mark.parametrize("where", ["fetch", "stale_index_lock", "refresh", "gate"])
def test_a_raised_exception_leaves_no_handover_hold(tmp_path, monkeypatch, where):
    env = _q._setup(tmp_path, monkeypatch, True)
    import percolate.dest_refresh as dr  # importable only after `_setup` bootstraps the engine path

    env.moving = where == "refresh"

    def boom(*_a, **_k):
        raise RuntimeError("induced")

    if where == "fetch":
        monkeypatch.setattr(dr, "fetch_dest_upstream", boom)
    elif where == "stale_index_lock":
        monkeypatch.setattr(env.publish_mod, "_stale_index_lock", boom)
    elif where == "refresh":
        monkeypatch.setattr(dr, "fast_forward_dest", boom)
    else:
        monkeypatch.setattr(env.publish_mod, _q.GATES[0], boom)

    with pytest.raises(RuntimeError, match="induced") as excinfo:
        _q._run(env)
    assert excinfo.value is not None  # holds the traceback frames, so a leaked stack stays reachable
    _assert_released(env)


def test_the_breadcrumb_is_held_only_inside_stop_and_copy(tmp_path, monkeypatch):
    from coordinator_core.warm import live_runs, pair_stop

    env = _q._setup(tmp_path, monkeypatch, True)
    import percolate.dest_refresh as dr  # importable only after `_setup` bootstraps the engine path
    from percolate import diff_commit

    seen: "list[tuple[str, bool]]" = []

    def watch(module, attr, label):
        inner = getattr(module, attr)

        def wrapper(*a, **k):
            seen.append((label, env.bc.handover_pending_held(env.repo)))
            return inner(*a, **k)

        monkeypatch.setattr(module, attr, wrapper)

    watch(live_runs, "find_live_runs", "probe")
    watch(dr, "fetch_dest_upstream", "fetch")
    watch(env.publish_mod, "process_target", "stage")
    watch(env.publish_mod, _q.GATES[3], "gate")
    watch(pair_stop, "stop_warm_pair", "stop")
    watch(diff_commit, "land_diff", "land")

    rc, err = _q._run(env)
    assert rc == 0, err
    held_at = {label: [h for lab, h in seen if lab == label] for label, _ in seen}
    assert held_at["fetch"] == [False]
    assert held_at["stage"] == [False]
    assert held_at["gate"] == [False]
    assert held_at["land"] == [True]
    # The first stop is the unmarked pre-stop and the second runs under the marker; both sit inside
    # `_quiesced_engine_roots`, so both read the breadcrumb held.
    assert held_at["stop"] and all(held_at["stop"])
    # Every live-run read sits inside `_quiesced_engine_roots`, so the breadcrumb is held for each.
    assert held_at["probe"] and all(held_at["probe"])
    _assert_released(env)


def test_a_mid_stage_run_still_refuses_before_anything_is_copied(tmp_path, monkeypatch):
    from coordinator_core.warm import live_runs

    monkeypatch.setattr(live_runs, "HOLD_BOUND_S", 0.0)
    env = _q._setup(tmp_path, monkeypatch, True)
    env.live_runs[:] = [live_runs.LiveRun("example-game-repo", "ask-be779d", "implement", "2026-10-31T22:46:00Z")]
    env.leg_in_flight = True
    rc, err = _q._run(env)
    assert rc == 1 and "example-game-repo ask-be779d" in err
    assert env.names("land_diff", "stop") == []
    _assert_released(env)


def _spy_slot(env, monkeypatch):
    """Record the tokens `acquire_slot` hands out so a test proves a slot was really taken."""
    from coordinator_core.percolate import publish_slot

    taken: "list[str]" = []
    real = publish_slot.acquire_slot

    def acquire(*a, **k):
        token = real(*a, **k)
        taken.append(token)
        return token

    monkeypatch.setattr(publish_slot, "acquire_slot", acquire)
    return taken


def test_warm_handover_refusal_releases_the_publish_slot(tmp_path, monkeypatch):
    from coordinator_core.percolate import publish_lease, publish_slot
    from coordinator_core.warm import live_runs

    monkeypatch.setattr(live_runs, "HOLD_BOUND_S", 0.0)
    env = _q._setup(tmp_path, monkeypatch, True)
    taken = _spy_slot(env, monkeypatch)
    env.live_runs[:] = [live_runs.LiveRun("example-game-repo", "ask-be779d", "implement", "2026-10-31T22:46:00Z")]
    env.leg_in_flight = True
    rc, err = _q._run(env)
    assert rc == 1 and "not copying" in err, err
    assert len(taken) == 1
    assert publish_lease.read(env.repo, publish_slot.SLOT_REMOTE) is None


def test_a_release_that_returns_false_is_reported(tmp_path, monkeypatch):
    from coordinator_core.percolate import publish_slot

    env = _q._setup(tmp_path, monkeypatch, True)
    taken = _spy_slot(env, monkeypatch)
    monkeypatch.setattr(publish_slot, "release_slot", lambda root, token: False)
    rc, err = _q._run(env)
    assert rc == 0, err
    assert taken and f"publish slot {taken[0]}" in err and "not released" in err


def test_a_release_that_raises_is_reported_and_keeps_the_exit_code(tmp_path, monkeypatch):
    from coordinator_core.percolate import publish_slot

    env = _q._setup(tmp_path, monkeypatch, True)
    taken = _spy_slot(env, monkeypatch)

    def boom(root, token):
        raise RuntimeError("push rejected")

    monkeypatch.setattr(publish_slot, "release_slot", boom)
    rc, err = _q._run(env)
    assert rc == 0, err
    assert taken and "not released" in err and "push rejected" in err
