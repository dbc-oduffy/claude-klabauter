"""publish.py main quiesces the destination's warm pair under a publish marker.

Run: python -m pytest coordinator/bin/tests/test_publish_quiesces_warm_pair.py -q
"""
from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_publish_lock_denies_fast as _h  # noqa: E402

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _setup(tmp_path, monkeypatch, engine_root: bool):
    from coordinator_core.warm import breadcrumb, engine_root as er, pair_stop

    monkeypatch.setenv("COORDINATOR_WARM_RUNTIME_BASE", str(tmp_path / "rt"))
    monkeypatch.delenv("COORDINATOR_ALLOW_PERCOLATE_QUEUE", raising=False)
    publish_mod = _h._load_publish_module()
    dest = tmp_path / "dest"
    dest.mkdir()
    _h._init_git_repo(dest)
    _h._wire_common_fakes(publish_mod, monkeypatch, tmp_path, {"row-a": dest})
    monkeypatch.setattr(er, "is_engine_root", lambda p: engine_root)
    monkeypatch.setattr(
        publish_mod,
        "_stale_index_lock",
        lambda root: None,
    )
    calls: list = []
    monkeypatch.setattr(
        publish_mod,
        "_run_round",
        lambda *a, **k: calls.append(("round", breadcrumb.publish_marker_held(breadcrumb.svc_dir(dest))))
        or (_ for _ in ()).throw(_Stop()),
    )
    return publish_mod, dest, calls, breadcrumb, pair_stop


class _Stop(Exception):
    pass


def _run(publish_mod):
    err = io.StringIO()
    rc = None
    with contextlib.redirect_stderr(err):
        try:
            rc = publish_mod.main(["row-a"])
        except _Stop:
            rc = "round-reached"
    return rc, err.getvalue()


def _refresh_ok(monkeypatch):
    import percolate.dest_refresh as dr

    monkeypatch.setattr(
        dr,
        "refresh_dest_from_origin",
        lambda *a, **k: type("R", (), {"ok": True, "reason": ""})(),
    )


def test_survivor_refuses_copy_and_releases_marker(tmp_path, monkeypatch):
    publish_mod, dest, calls, bc, ps = _setup(tmp_path, monkeypatch, True)
    surv = ps.PairProcess(pid=4242, role=ps.ROLE_SERVER, cmdline="python server.py")
    seen = []
    monkeypatch.setattr(
        ps,
        "stop_warm_pair",
        lambda root, **k: seen.append(bc.publish_marker_held(bc.svc_dir(dest)))
        or ps.PairStopResult(stopped=(), survivors=(surv,)),
    )
    rc, err = _run(publish_mod)
    assert rc == 1
    assert calls == []
    assert "pid=4242" in err and "role=server" in err
    assert seen == [True]
    assert not bc.publish_marker_held(bc.svc_dir(dest))


def _exc_cases():
    from coordinator_core.session.core import MissingPsutilError, _WinLivenessAmbiguous

    return [MissingPsutilError, _WinLivenessAmbiguous, OSError]


@pytest.mark.parametrize("where", ["hold", "stop"])
@pytest.mark.parametrize("exc_index", [0, 1, 2])
def test_marker_or_stop_exception_is_fatal_exit_1_and_releases_locks(
    tmp_path, monkeypatch, where, exc_index
):
    exc_type = _exc_cases()[exc_index]
    publish_mod, dest, calls, bc, ps = _setup(tmp_path, monkeypatch, True)
    _refresh_ok(monkeypatch)

    def boom(*a, **k):
        raise exc_type("induced")

    ok = lambda root, **k: ps.PairStopResult(stopped=(), survivors=())  # noqa: E731
    if where == "hold":
        real_hold = bc.hold_publish_marker
        monkeypatch.setattr(bc, "hold_publish_marker", boom)
        monkeypatch.setattr(ps, "stop_warm_pair", ok)
    else:
        monkeypatch.setattr(ps, "stop_warm_pair", boom)

    rc, err = _run(publish_mod)
    assert rc == 1
    assert "FATAL" in err and exc_type.__name__ in err
    assert calls == []
    assert not bc.publish_marker_held(bc.svc_dir(dest))

    if where == "hold":
        monkeypatch.setattr(bc, "hold_publish_marker", real_hold)
    else:
        monkeypatch.setattr(ps, "stop_warm_pair", ok)
    rc2, _ = _run(publish_mod)
    assert rc2 == "round-reached"


def test_ok_stop_runs_round_with_marker_held_then_releases(tmp_path, monkeypatch):
    publish_mod, dest, calls, bc, ps = _setup(tmp_path, monkeypatch, True)
    _refresh_ok(monkeypatch)
    order = []
    monkeypatch.setattr(
        ps,
        "stop_warm_pair",
        lambda root, **k: order.append(bc.publish_marker_held(bc.svc_dir(dest)))
        or ps.PairStopResult(stopped=(), survivors=()),
    )
    rc, _ = _run(publish_mod)
    assert rc == "round-reached"
    assert order == [True]
    assert calls == [("round", True)]
    assert not bc.publish_marker_held(bc.svc_dir(dest))


def test_nested_parent_marker_survives_publish_main(tmp_path, monkeypatch):
    import json
    import os
    import time

    publish_mod, dest, calls, bc, ps = _setup(tmp_path, monkeypatch, True)
    _refresh_ok(monkeypatch)
    parent_pid = 2**22 + 7
    monkeypatch.setattr(bc, "stable_pid_alive", lambda *a, **k: True)
    monkeypatch.setattr(bc, "_is_ancestor_pid", lambda p: p == parent_pid)
    marker = bc.publish_marker_path(dest)
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
    monkeypatch.setattr(
        ps, "stop_warm_pair", lambda root, **k: ps.PairStopResult(stopped=(), survivors=())
    )
    rc, _ = _run(publish_mod)
    assert rc == "round-reached"
    assert calls == [("round", True)]
    assert marker.read_bytes() == before
    assert os.getpid() != parent_pid


def test_non_engine_root_dest_neither_marks_nor_stops(tmp_path, monkeypatch):
    publish_mod, dest, calls, bc, ps = _setup(tmp_path, monkeypatch, False)
    _refresh_ok(monkeypatch)

    def boom(*a, **k):
        raise AssertionError("stop must not run")

    monkeypatch.setattr(ps, "stop_warm_pair", boom)
    rc, _ = _run(publish_mod)
    assert rc == "round-reached"
    assert calls == [("round", False)]
