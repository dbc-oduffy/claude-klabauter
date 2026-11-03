"""publish.py quiesces the destination's warm pair only across the windows that change engine files.

One window, a marker hold plus a warm stop: the land into the engine root, entered only after every
pre-sync and end-of-run gate passed and no row was refused. A refusal takes no marker and stops
nothing. The destination refresh is a second window only when the fetch shows the root must move
(`env.moving`); a level root is never stopped before the gates. Live runs are read with the marker
held and logged; they never refuse the publish. Every test drives `publish_mod.main(["row-a"])` at
default arguments through the real `_run_round` and the real `land_diff`.

Run: python -m pytest coordinator/bin/tests/test_publish_quiesces_warm_pair.py -q
"""
from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_publish_lock_denies_fast as _h  # noqa: E402

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

GATES = (
    "dispatch_end_of_run_identity_check",
    "dispatch_end_of_run_install_doc_payload_check",
    "dispatch_end_of_run_unscanned_published_check",
    "dispatch_end_of_run_function_gate",
    "dispatch_end_of_run_entrypoint_gate",
    "dispatch_end_of_run_live_identifier_gate",
    "dispatch_end_of_run_repo_key_literal_gate",
    "dispatch_end_of_run_argv_parity_gate",
    "dispatch_end_of_run_plugin_payload_gate",
    "dispatch_end_of_run_plugin_provenance_gate",
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
         "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false", *args],
        check=True,
        capture_output=True,
        creationflags=_NO_WINDOW,
    )


def _init_real_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    (root / ".gitkeep").write_text("", encoding="utf-8")
    _git(root, "add", ".gitkeep")
    _git(root, "commit", "-q", "-m", "chore: init")
    _git(root, "remote", "add", "origin", str(root))
    _git(root, "fetch", "-q", "--no-tags", "origin")
    _git(root, "branch", "--set-upstream-to=origin/main", "main")


class _Env:
    """What a test needs after `_setup`: the module, the destination repo, and the ordered `events`
    log of (kind, marker_held_at_that_moment) the stubs append."""

    def __init__(self, publish_mod, repo, breadcrumb, pair_stop):
        self.publish_mod = publish_mod
        self.repo = repo
        self.bc = breadcrumb
        self.ps = pair_stop
        self.events: list = []
        self.live_runs: list = []
        self.stop_results: list = []
        self.pre_results: list = []
        self.moving = False
        self.refused: set = set()
        self.hold_calls: list = []

    def held(self) -> bool:
        return self.bc.publish_marker_held(self.bc.svc_dir(self.repo))

    def ok(self):
        return self.ps.PairStopResult(stopped=(), survivors=())

    def survivor_result(self):
        surv = self.ps.PairProcess(pid=4242, role=self.ps.ROLE_SERVER, cmdline="python server.py")
        return self.ps.PairStopResult(stopped=(), survivors=(surv,))

    def names(self, *kinds: str) -> list:
        return [e for e in self.events if e[0] in kinds]

    def gate_events(self) -> list:
        return [(i, e) for i, e in enumerate(self.events) if e[0] in GATES]


def _setup(tmp_path, monkeypatch, engine_root: bool, rows: tuple = ("row-a",)) -> _Env:
    from coordinator_core.warm import breadcrumb, engine_root as er, live_runs, pair_stop

    monkeypatch.setenv("COORDINATOR_WARM_RUNTIME_BASE", str(tmp_path / "rt"))
    monkeypatch.delenv("COORDINATOR_ALLOW_PERCOLATE_QUEUE", raising=False)
    publish_mod = _h._load_publish_module()
    repo = tmp_path / "dest"
    _init_real_repo(repo)
    _h._wire_common_fakes(
        publish_mod, monkeypatch, tmp_path, {name: repo / f"sub-{name[-1]}" for name in rows}
    )
    env = _Env(publish_mod, repo, breadcrumb, pair_stop)

    real_hold = breadcrumb.hold_publish_marker
    monkeypatch.setattr(
        breadcrumb, "hold_publish_marker", lambda *a, **k: (env.hold_calls.append(1), real_hold(*a, **k))[1]
    )

    monkeypatch.setattr(er, "is_engine_root", lambda p: engine_root)
    monkeypatch.setattr(publish_mod, "_stale_index_lock", lambda root: None)
    monkeypatch.setattr(publish_mod, "write_publish_provenance_record", lambda **kw: None)
    publish_mod._bootstrap_engine()

    def find(*a, **k):
        env.events.append(("find_live_runs", env.held()))
        return list(env.live_runs)

    monkeypatch.setattr(live_runs, "find_live_runs", find)

    def stop(root, **k):
        if not env.held():
            env.events.append(("prestop", False))
            return env.pre_results.pop(0) if env.pre_results else env.ok()
        env.events.append(("stop", True))
        return env.stop_results.pop(0) if env.stop_results else env.ok()

    monkeypatch.setattr(pair_stop, "stop_warm_pair", stop)

    import percolate.dest_refresh as dr
    from percolate import diff_commit

    def fetch(repo_root, **k):
        env.events.append(("fetch", env.held()))
        return dr.RefreshResult(
            repo_root=repo_root,
            ok=True,
            behind=1 if env.moving is True else 0,
            pending_abort=env.moving == "pending_abort",
            subsumed=("x",) if env.moving == "subsumed" else (),
        )

    def fast_forward(result, **k):
        env.events.append(("refresh", env.held()))
        return result

    monkeypatch.setattr(dr, "fetch_dest_upstream", fetch)
    monkeypatch.setattr(dr, "fast_forward_dest", fast_forward)

    for gate in GATES:
        monkeypatch.setattr(
            publish_mod, gate,
            lambda *a, _n=gate, **k: (env.events.append((_n, env.held())), True)[1],
        )

    real_land = diff_commit.land_diff

    def land(*a, **k):
        env.events.append(("land_diff", env.held()))
        return real_land(*a, **k)

    monkeypatch.setattr(diff_commit, "land_diff", land)

    def process_target(target, setup_dir, totals, **kwargs):
        if target.name in env.refused:
            return None
        totals.processed += 1
        staging = Path(
            tempfile.mkdtemp(prefix=f".{target.dest_dir.name}.publish-staging-", dir=str(target.dest_dir.parent))
        )
        (staging / "payload.txt").write_text("published\n", encoding="utf-8", newline="\n")
        return publish_mod.StagedRowResult(
            staging_dir=staging,
            row_visited=set(),
            row_changed_files=None,
            row_removed_files=set(),
            row_published_files={Path("payload.txt")},
            report_text="",
            synced=1,
            deleted=0,
        )

    monkeypatch.setattr(publish_mod, "process_target", process_target)
    return env


def _run(env: _Env, *names: str):
    err = io.StringIO()
    out = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
        rc = env.publish_mod.main([",".join(names) or "row-a"])
    return rc, err.getvalue()


def _window_shape(env: _Env) -> list:
    return env.names("stop", "refresh", "land_diff")


def _nothing_quiesced(env: _Env) -> None:
    assert env.names("prestop", "stop", "land_diff", "successor") == []
    assert all(held is False for _, held in env.names("refresh"))
    assert env.hold_calls == []
    assert not env.held()


def test_gates_run_unquiesced_and_only_the_land_is_stopped(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch, True)
    rc, err = _run(env)
    assert rc == 0, err
    assert _window_shape(env) == [("refresh", False), ("stop", True), ("land_diff", True)]
    gates = env.gate_events()
    assert {e[0] for _, e in gates} == set(GATES)
    assert all(held is False for _, (_, held) in gates), gates
    stop_idx = env.events.index(("stop", True))
    assert max(i for i, _ in gates) < stop_idx < env.events.index(("land_diff", True))
    assert env.hold_calls == [1]
    assert not env.held()


def test_moving_root_fast_forwards_in_its_own_window_before_the_gates(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch, True)
    env.moving = True
    rc, err = _run(env)
    assert rc == 0, err
    assert _window_shape(env) == [("stop", True), ("refresh", True), ("stop", True), ("land_diff", True)]
    gates = env.gate_events()
    assert env.events.index(("refresh", True)) < min(i for i, _ in gates)
    assert all(held is False for _, (_, held) in gates), gates
    assert not env.held()


@pytest.mark.parametrize("trigger", ["pending_abort", "subsumed"])
def test_a_worktree_write_without_behind_still_opens_the_refresh_window(tmp_path, monkeypatch, trigger):
    env = _setup(tmp_path, monkeypatch, True)
    env.moving = trigger
    rc, err = _run(env)
    assert rc == 0, err
    assert _window_shape(env) == [("stop", True), ("refresh", True), ("stop", True), ("land_diff", True)]


def test_pre_sync_refusal_takes_no_marker_and_stops_nothing(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch, True)
    env.refused.add("row-a")
    rc, err = _run(env)
    assert rc == 1, err
    _nothing_quiesced(env)


def test_a_refused_sibling_row_keeps_the_staged_row_out_of_the_engine_root(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch, True, rows=("row-a", "row-b"))
    env.refused.add("row-b")
    rc, err = _run(env, "row-a", "row-b")
    assert rc == 1, err
    assert "Not landing row-a into engine root" in err
    _nothing_quiesced(env)
    assert not (env.repo / "sub-a" / "payload.txt").exists()


def test_a_refused_sibling_row_still_lands_into_a_non_engine_root(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch, False, rows=("row-a", "row-b"))
    env.refused.add("row-b")
    rc, err = _run(env, "row-a", "row-b")
    assert rc == 1, err
    assert env.names("land_diff") == [("land_diff", False)]
    assert env.hold_calls == []


def test_end_of_run_gate_failure_takes_no_marker(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch, True)
    monkeypatch.setattr(
        env.publish_mod,
        "dispatch_end_of_run_function_gate",
        lambda *a, **k: (env.events.append(("dispatch_end_of_run_function_gate", env.held())), False)[1],
    )
    rc, err = _run(env)
    assert rc == 2, err
    assert ("dispatch_end_of_run_function_gate", False) in env.events
    _nothing_quiesced(env)


def test_survivor_at_land_stop_refuses_before_any_land_and_releases_marker(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch, True)
    env.stop_results[:] = [env.survivor_result()]
    rc, err = _run(env)
    assert rc == 1
    assert "pid=4242" in err and "role=server" in err
    assert env.names("land_diff") == []
    assert len(env.gate_events()) == len(GATES)
    assert env.names("stop") == [("stop", True)]
    assert not env.held()


def test_pair_that_cannot_be_stopped_never_takes_the_marker(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch, True)
    env.pre_results[:] = [env.survivor_result()]
    rc, err = _run(env)
    assert rc == 1 and "pid=4242" in err
    assert env.hold_calls == []
    assert env.names("prestop", "stop", "land_diff") == [("prestop", False)]
    assert not env.held()


def test_a_respawn_after_the_unmarked_stop_is_left_to_the_marked_stop(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch, True)
    respawn = env.ps.PairProcess(pid=43708, role=env.ps.ROLE_SERVER, cmdline="python server.py")
    env.pre_results[:] = [
        env.ps.PairStopResult(stopped=(), survivors=(respawn,), initial_pids=frozenset({6468})),
    ]
    rc, err = _run(env)
    assert rc == 0, err
    assert env.names("land_diff") == [("land_diff", True)]


def test_an_original_pid_surviving_the_unmarked_stop_still_refuses(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch, True)
    orig = env.ps.PairProcess(pid=6468, role=env.ps.ROLE_SERVER, cmdline="python server.py")
    env.pre_results[:] = [
        env.ps.PairStopResult(stopped=(), survivors=(orig,), initial_pids=frozenset({6468})),
    ]
    rc, err = _run(env)
    assert rc == 1 and "pid=6468" in err
    assert env.names("prestop", "stop", "land_diff") == [("prestop", False)]


def test_marker_is_taken_only_after_the_unmarked_stop_confirms_the_pair_gone(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch, True)
    rc, err = _run(env)
    assert rc == 0, err
    assert env.names("prestop", "stop") == [("prestop", False), ("stop", True)]


def test_survivor_at_refresh_stop_refuses_before_refresh_and_gates(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch, True)
    env.moving = True
    env.stop_results[:] = [env.survivor_result()]
    rc, err = _run(env)
    assert rc == 1 and "pid=4242" in err
    assert env.names("refresh", "land_diff") == [] and env.gate_events() == []
    assert not env.held()


def test_live_midstage_runs_refuse_the_handover_naming_each_run(tmp_path, monkeypatch):
    from coordinator_core.warm import live_runs

    monkeypatch.setattr(live_runs, "HOLD_BOUND_S", 0.0)
    env = _setup(tmp_path, monkeypatch, True)
    env.live_runs[:] = [
        live_runs.LiveRun("example-game-repo", "ask-be779d", "implement", "2026-10-31T22:46:00Z"),
        live_runs.LiveRun("claude-klabauter", "ask-aaaaaa", "staged", "2026-10-31T22:47:00Z"),
    ]
    rc, text = _run(env)
    assert rc == 1, text
    assert "example-game-repo ask-be779d" in text and "claude-klabauter ask-aaaaaa" in text
    assert "--allow" not in text and "--cut-run" not in text
    assert env.names("land_diff") == []
    assert env.names("stop") == []


def test_live_midstage_run_does_not_bypass_survivor_refusal(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch, True)
    env.stop_results[:] = [env.survivor_result()]
    rc, err = _run(env)
    assert rc == 1 and env.names("land_diff") == [] and "pid=4242" in err


def test_override_flags_are_retired(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch, True)
    err = io.StringIO()
    with contextlib.redirect_stderr(err), pytest.raises(SystemExit) as ei:
        env.publish_mod.main(["row-a", "--cut-run", "ask-be779d"])
    assert ei.value.code == 2


def test_live_midstage_run_ignored_for_non_engine_root(tmp_path, monkeypatch):
    from coordinator_core.warm import live_runs

    env = _setup(tmp_path, monkeypatch, False)
    env.live_runs[:] = [live_runs.LiveRun("example-game-repo", "ask-be779d", "implement", "2026-10-31T22:46:00Z")]
    rc, err = _run(env)
    assert rc == 0, err
    assert env.names("find_live_runs") == []


def _exc_cases():
    from coordinator_core.session.core import MissingPsutilError, _WinLivenessAmbiguous

    return [MissingPsutilError, _WinLivenessAmbiguous, OSError]


_WINDOWS = [(True, 1), (True, 2), (False, 1)]
_WINDOW_IDS = ["refresh-window", "land-window-after-refresh", "land-window"]


@pytest.mark.parametrize("moving,window", _WINDOWS, ids=_WINDOW_IDS)
@pytest.mark.parametrize("where", ["hold", "stop"])
@pytest.mark.parametrize("exc_index", [0, 1, 2])
def test_marker_or_stop_exception_is_fatal_exit_1_and_releases_locks(
    tmp_path, monkeypatch, where, exc_index, moving, window
):
    exc_type = _exc_cases()[exc_index]
    env = _setup(tmp_path, monkeypatch, True)
    env.moving = moving
    bc, ps = env.bc, env.ps
    target_attr = (bc, "hold_publish_marker") if where == "hold" else (ps, "stop_warm_pair")
    real = getattr(*target_attr)
    calls = []

    def maybe_boom(*a, **k):
        calls.append(1)
        # Each window calls stop_warm_pair twice (unmarked pre-stop, then under the marker);
        # the induced fault lands on the marker-held call.
        if len(calls) == (window if where == "hold" else 2 * window):
            raise exc_type("induced")
        return real(*a, **k)

    monkeypatch.setattr(*target_attr, maybe_boom)

    rc, err = _run(env)
    assert rc == 1
    assert "FATAL" in err and exc_type.__name__ in err
    assert env.names("land_diff") == []
    assert (env.gate_events() == []) == (moving and window == 1)
    assert not env.held()

    monkeypatch.setattr(*target_attr, real)
    rc2, err2 = _run(env)
    assert rc2 == 0, err2


def test_nested_parent_marker_survives_publish_main(tmp_path, monkeypatch):
    import json
    import os
    import time

    env = _setup(tmp_path, monkeypatch, True)
    bc = env.bc
    parent_pid = 2**22 + 7
    monkeypatch.setattr(bc, "stable_pid_alive", lambda *a, **k: True)
    monkeypatch.setattr(bc, "_is_ancestor_pid", lambda p: p == parent_pid)
    marker = bc.publish_marker_path(env.repo)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "pid": parent_pid,
                "stable_pid_start_epoch": 1,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "holder": "percolate-round:abc123",
            }
        ),
        encoding="utf-8",
    )
    before = marker.read_bytes()
    rc, err = _run(env)
    assert rc == 0, err
    assert env.names("land_diff") == [("land_diff", True)]
    assert marker.read_bytes() == before
    assert os.getpid() != parent_pid


def test_non_engine_root_dest_neither_marks_nor_stops(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch, False)

    def boom(*a, **k):
        raise AssertionError("must not run")

    monkeypatch.setattr(env.ps, "stop_warm_pair", boom)
    monkeypatch.setattr(env.bc, "hold_publish_marker", boom)
    rc, err = _run(env)
    assert rc == 0, err
    assert env.names("land_diff") == [("land_diff", False)]
    assert all(held is False for _, held in env.events)


@pytest.mark.parametrize("moving,window", _WINDOWS, ids=_WINDOW_IDS)
def test_unsignalable_pair_pid_refuses_before_that_windows_stop(tmp_path, monkeypatch, moving, window):
    env = _setup(tmp_path, monkeypatch, True)
    env.moving = moving
    probes = []

    def probe(root, **k):
        probes.append(1)
        return [(777, "server", "access denied")] if len(probes) == window else []

    monkeypatch.setattr(env.ps, "probe_pair_terminable", probe)
    rc, err = _run(env)
    assert rc == 1 and env.names("land_diff") == []
    assert len(env.names("stop")) == window - 1
    assert "pid=777" in err and "access denied" in err
    assert not env.held()


def _record_successor(env: _Env, monkeypatch) -> None:
    from coordinator_core.percolate import round as round_mod

    def start(root, **k):
        env.events.append(("successor", env.held()))
        return True

    monkeypatch.setattr(round_mod, "start_successor_pair_in_hold", start)


def test_successor_starts_inside_the_window_after_the_body_and_before_release(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch, True)
    _record_successor(env, monkeypatch)
    rc, err = _run(env)
    assert rc == 0, err
    assert env.names("stop", "refresh", "successor", "land_diff") == [
        ("refresh", False),
        ("stop", True),
        ("land_diff", True),
        ("successor", True),
    ]
    assert not env.held()


def test_successor_starts_inside_each_window_of_a_moving_root(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch, True)
    env.moving = True
    _record_successor(env, monkeypatch)
    rc, err = _run(env)
    assert rc == 0, err
    assert env.names("stop", "refresh", "successor", "land_diff") == [
        ("stop", True),
        ("refresh", True),
        ("successor", True),
        ("stop", True),
        ("land_diff", True),
        ("successor", True),
    ]
    assert not env.held()


def test_survivor_at_stop_starts_no_successor(tmp_path, monkeypatch):
    env = _setup(tmp_path, monkeypatch, True)
    _record_successor(env, monkeypatch)
    env.stop_results[:] = [env.survivor_result()]
    rc, _ = _run(env)
    assert rc == 1
    assert env.names("successor") == []
    assert not env.held()
