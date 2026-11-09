"""A live run token stays redeemable through publish.py's warm-pair quiesce and a run_token module reload.

Run: python -m pytest coordinator_core/ops/dispatch_emit/tests/test_run_token_survives_warm_restart.py -q
"""
from __future__ import annotations

import contextlib
import importlib
import importlib.util
import io
import sys
from pathlib import Path

import pytest

_BIN_TESTS = Path(__file__).resolve().parents[4] / "coordinator" / "bin" / "tests"
_spec = importlib.util.spec_from_file_location("test_publish_lock_denies_fast", _BIN_TESTS / "test_publish_lock_denies_fast.py")
_h = importlib.util.module_from_spec(_spec)
sys.modules.setdefault(_spec.name, _h)
_spec.loader.exec_module(_h)

pytestmark = [pytest.mark.spawns_process]

_COORDINATOR_LIB = Path(__file__).resolve().parents[4] / "coordinator" / "lib"
_SCRIPT = "scratch/warp/example/run.js"


class _Stop(Exception):
    pass


def _publish_quiesce(tmp_path, monkeypatch):
    """Run publish.main against a git dest up to the round, with the real pair stop in place."""
    from coordinator_core.warm import breadcrumb, engine_root as er, live_runs, pair_stop
    monkeypatch.syspath_prepend(str(_COORDINATOR_LIB))
    import percolate.dest_refresh as dr

    monkeypatch.setattr(live_runs, "find_live_runs", lambda *a, **k: [])
    monkeypatch.setenv("COORDINATOR_WARM_RUNTIME_BASE", str(tmp_path / "rt"))
    monkeypatch.delenv("COORDINATOR_ALLOW_PERCOLATE_QUEUE", raising=False)
    publish_mod = _h._load_publish_module()
    dest = tmp_path / "dest"
    dest.mkdir()
    _h._init_git_repo(dest)
    _h._wire_common_fakes(publish_mod, monkeypatch, tmp_path, {"row-a": dest})
    monkeypatch.setattr(er, "is_engine_root", lambda p: True)
    monkeypatch.setattr(publish_mod, "_stale_index_lock", lambda root: None)
    # A behind destination is the moving root: only it is quiesced across its fast-forward.
    monkeypatch.setattr(
        dr, "fetch_dest_upstream", lambda root, **k: dr.RefreshResult(repo_root=root, ok=True, behind=1)
    )
    monkeypatch.setattr(dr, "fast_forward_dest", lambda result, **k: result)
    stops: list = []
    real_probe = pair_stop.probe_pair_terminable

    def stop(root, **k):
        stops.append(root)
        return real_probe(root, **k)

    monkeypatch.setattr(pair_stop, "probe_pair_terminable", stop)
    monkeypatch.setattr(
        publish_mod, "_run_round", lambda *a, **k: (_ for _ in ()).throw(_Stop())
    )
    return publish_mod, dest, stops, breadcrumb


def test_token_admits_after_publish_quiesce_and_module_reload(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import run_token

    publish_mod, dest, stops, _bc = _publish_quiesce(tmp_path, monkeypatch)
    run_id = run_token.new_ask_run_id()
    token = run_token.mint(dest, run_id, _SCRIPT)
    assert run_token.admits(dest, run_id, token, _SCRIPT)

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        with pytest.raises(_Stop):
            publish_mod.main(["row-a"])
    assert stops, "the publish quiesce path did not run"

    reloaded = importlib.reload(run_token)
    assert reloaded.is_live(dest, run_id)
    assert reloaded.admits(dest, run_id, token, _SCRIPT)
    assert reloaded.halt_admits(dest, run_id, token, _SCRIPT)
    assert not reloaded.admits(dest, run_id, "0" * 32, _SCRIPT)
    assert not reloaded.admits(dest, run_id, token, "scratch/other.js")


def test_halt_admits_after_quiesce_with_claimed_token(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import run_token

    publish_mod, dest, stops, _bc = _publish_quiesce(tmp_path, monkeypatch)
    run_id = run_token.new_ask_run_id()
    token = run_token.mint(dest, run_id, _SCRIPT)
    assert run_token.redeem(dest, run_id, token, _SCRIPT)

    with contextlib.redirect_stderr(io.StringIO()):
        with pytest.raises(_Stop):
            publish_mod.main(["row-a"])
    assert stops

    reloaded = importlib.reload(run_token)
    assert reloaded.halt_admits(dest, run_id, token, _SCRIPT)
