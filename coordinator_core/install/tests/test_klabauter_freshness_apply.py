"""Apply-path tests for klabauter freshness over tmp_path bare-origin + clone git fixtures."""

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.install import klabauter_freshness as kf
from coordinator_core.install import klabauter_freshness_apply as apply

pytestmark = pytest.mark.complete

_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def _legs(monkeypatch, record_pid, sup, door, own_pid=4242):
    from coordinator_core.warm import supervisor

    monkeypatch.setattr(supervisor, "read_discovery", lambda root=None: {"pid": record_pid})
    return apply.engine_leg_in_flight(
        Path("."), sup_count=lambda r: sup, door_count=lambda r: door, own_pid=own_pid
    )


def test_engine_leg_gate_discounts_own_request_when_served_by_the_supervisor(monkeypatch):
    assert _legs(monkeypatch, 4242, 1, 1) == 0
    assert _legs(monkeypatch, 4242, 1, 0) == 0
    assert _legs(monkeypatch, 4242, 2, 1) == 1


def test_engine_leg_gate_counts_every_request_when_not_served_by_the_supervisor(monkeypatch):
    assert _legs(monkeypatch, 1, 1, 0) == 1
    assert _legs(monkeypatch, 1, 0, 0) == 0


def test_engine_leg_gate_unanswerable_probe_reads_in_flight(monkeypatch):
    assert _legs(monkeypatch, 4242, None, 0) == 1
    assert _legs(monkeypatch, 4242, 1, None) == 1


def test_non_checkout_reports_without_raising(tmp_path):
    res = apply.apply_freshness(tmp_path, tmp_path, hostname="boxy", live_runs=lambda: 0, installers={})
    assert res.action == kf.ACTION_CURRENT and res.reason


_FAKE_INSTALLER = '''
VENDORED_SCRIPTS = ()
VENDORED_LIB = ("box_mux.py", "absent.py")


def script_closure(src, root="unimatrix-autolink.py"):
    return set()


def refresh_installed_copy(src, dest, say):
    import pathlib
    dest = pathlib.Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with open(dest / "refresh.log", "a") as fh:
        fh.write("refresh\\n")
    if (dest / "restart-fails").exists():
        raise RuntimeError("router restart refused")
    return {"copied": [], "router": {"stopped": "handshake", "old_pid": 1, "new_pid": 2}}


def router_runs_stale_code(dest):
    import pathlib
    return (pathlib.Path(dest) / "stale-flag").exists()


def router_is_live(dest):
    return True
'''

_ROUTER = {"stopped": "handshake", "old_pid": 1, "new_pid": 2}


@pytest.fixture
def box(tmp_path):
    """A checkout dir carrying the fake installer, and a settings home beside it."""
    checkout, home = tmp_path / "co", tmp_path / "home"
    (checkout / "coordinator" / "bin").mkdir(parents=True)
    (checkout / "coordinator" / "bin" / "unimatrix-install.py").write_text(_FAKE_INSTALLER, encoding="utf-8")
    home.mkdir()
    return checkout, home


def _roll(monkeypatch, box, changed=None):
    """apply_freshness over a stubbed git: a fast-forward touching `changed`, or current when None."""
    checkout, home = box
    behind = 0 if changed is None else 1
    monkeypatch.setattr(apply, "fetch", lambda _c: "")
    monkeypatch.setattr(apply, "probe", lambda _c: apply.Probe("a", "origin/main", 0, behind, False))
    monkeypatch.setattr(apply, "_rev", lambda _c, ref: "a" if behind == 0 else "b")

    def fake_git(_c, *args, remote=False):
        out = "\n".join(changed or []) if args[0] == "diff" else ""
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr(apply, "_git", fake_git)
    return apply.apply_freshness(
        checkout,
        home,
        hostname="boxy",
        live_runs=lambda: 0,
        installers={kf.INSTALLER_UNIMATRIX: apply._run_unimatrix},
    )


def _dest(box):
    return box[1] / apply.UNIMATRIX_DEST_DIRNAME


def test_ac4_fast_forward_touching_unimatrix_package_refreshes_and_restarts_router(monkeypatch, box):
    res = _roll(monkeypatch, box, ["coordinator/bin/_unimatrix/adapters/agy.py"])
    assert res.moved and res.installers_run == [kf.INSTALLER_UNIMATRIX]
    assert (_dest(box) / "refresh.log").read_text() == "refresh\n"
    assert res.router == _ROUTER
    assert res.to_dict()["router"] == _ROUTER


def test_ac4_fast_forward_touching_vendored_lib_refreshes_and_restarts_router(monkeypatch, box):
    res = _roll(monkeypatch, box, ["coordinator/bin/lib/box_mux.py"])
    assert res.installers_run == [kf.INSTALLER_UNIMATRIX]
    assert (_dest(box) / "refresh.log").exists()


def test_ac4_unshipped_lib_change_does_not_restart_router(monkeypatch, box):
    res = _roll(monkeypatch, box, ["coordinator/bin/lib/other.py"])
    assert res.moved and res.installers_run == [] and res.router is None
    assert not (_dest(box) / "refresh.log").exists()


def test_router_restart_failure_lands_in_installer_errors(monkeypatch, box):
    _dest(box).mkdir()
    (_dest(box) / "restart-fails").write_text("x", encoding="utf-8")
    res = _roll(monkeypatch, box, ["coordinator/bin/_unimatrix/adapters/agy.py"])
    assert res.installers_run == []
    assert "router restart refused" in res.installer_errors[kf.INSTALLER_UNIMATRIX]
    assert res.router is None


def test_live_router_on_stale_code_is_queued_without_a_changed_path(monkeypatch, box):
    _dest(box).mkdir()
    res = _roll(monkeypatch, box)
    assert res.action == kf.ACTION_CURRENT and res.installers_run == []
    (_dest(box) / "stale-flag").write_text("x", encoding="utf-8")
    res = _roll(monkeypatch, box)
    assert res.installers_run == [kf.INSTALLER_UNIMATRIX]
    assert res.router == _ROUTER


def test_stale_router_is_not_queued_for_a_box_without_unimatrix(monkeypatch, box):
    res = _roll(monkeypatch, box)
    assert res.installers_run == [] and res.router is None


def test_unimatrix_stale_covers_package_and_vendored_lib(box):
    checkout, home = box
    src = checkout / "coordinator" / "bin"
    (src / "_unimatrix" / "adapters").mkdir(parents=True)
    (src / "lib").mkdir()
    (src / "_unimatrix" / "adapters" / "agy.py").write_text("new\n", encoding="utf-8")
    (src / "lib" / "box_mux.py").write_text("new\n", encoding="utf-8")
    dest = _dest(box)
    (dest / "_unimatrix" / "adapters").mkdir(parents=True)
    (dest / "lib").mkdir()
    (dest / "_unimatrix" / "adapters" / "agy.py").write_text("old\n", encoding="utf-8")
    (dest / "lib" / "box_mux.py").write_text("old\n", encoding="utf-8")
    assert apply.unimatrix_stale(checkout, home) == ("_unimatrix/adapters/agy.py", "lib/box_mux.py")
    (dest / "_unimatrix" / "adapters" / "agy.py").write_text("new\n", encoding="utf-8")
    (dest / "lib" / "box_mux.py").write_text("new\n", encoding="utf-8")
    assert apply.unimatrix_stale(checkout, home) == ()


def test_unimatrix_roll_pass_spawns_no_process_of_its_own(monkeypatch, box):
    _dest(box).mkdir()
    (_dest(box) / "stale-flag").write_text("x", encoding="utf-8")

    def no_spawn(*_a, **_k):
        raise AssertionError("unimatrix leg spawned a process")

    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    assert apply._router_stale(*box)
    assert apply.unimatrix_stale(*box) == ()
    apply._run_unimatrix(box[0], box[1], None)
