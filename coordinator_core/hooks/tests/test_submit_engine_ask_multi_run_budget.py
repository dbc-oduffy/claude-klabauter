"""The submit hook at default arguments, three runs starting plus a `{sizing}` refire released together,
answers every submission in-bar and never spools: the door's delivery deadline cannot fire on a hook that
holds no leg over the bar."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ask_spool import record
from coordinator_core.benchmarks import fixture_settings_home
from coordinator_core.benchmarks import submit_engine_ask_process_budget as B
from coordinator_core.hooks import submit_engine_ask as hook


#: The spool envelope's own reason text; the benchmark's `spooled` flag keys on a different phrase.
SPOOLED_TEXT = "spooled as"


def _spooled_reasons(res: dict) -> list:
    return [s["reason"] for rnd in res["round_reports"] for s in rnd["submissions"] if SPOOLED_TEXT in (s["reason"] or "")]


class _Unavailable(Exception):
    code = hook._UNAVAILABLE_CODE


@pytest.fixture(scope="module")
def burst(tmp_path_factory) -> tuple[Path, dict]:
    parent = tmp_path_factory.mktemp("three-run-burst")
    repo = parent / "repo"
    repo.mkdir()
    with fixture_settings_home(parent):
        B.build_fixture(repo, B.LAUNCH_STARTS)
        return repo, B.measure_multi_run_launch(repo, B.LAUNCH_STARTS, runs=0)


def test_every_submission_is_allowed_and_none_is_spooled(burst):
    _repo, res = burst
    assert res["errors"] == []
    assert res["decisions"] == ["allow"] * (B.LAUNCH_STARTS + 1)
    assert _spooled_reasons(res) == []


def test_no_spool_envelope_is_written(burst):
    repo, _res = burst
    assert record.list_pending(str(repo)) == []


def test_no_leg_passes_the_bar_and_no_process_is_spawned(burst):
    _repo, res = burst
    assert res["legs_over_bar"] == []
    assert res["spawns"] == 0


def test_a_hook_that_loses_the_engine_is_reported_spooled(tmp_path, monkeypatch):
    """The other side: a dispatch timeout spools, so the assertions above can fail."""

    def stalled(*a, **k):
        raise _Unavailable("no response within 8000ms of delivery")

    monkeypatch.setattr(hook, "_emit_within_budget", stalled)
    repo = tmp_path / "repo"
    repo.mkdir()
    with fixture_settings_home(tmp_path):
        B.build_fixture(repo, 1)
        res = B.measure_multi_run_launch(repo, 1, runs=0)
    assert len(_spooled_reasons(res)) == 2
    assert record.list_pending(str(repo)) != []


def _burst_with_prewarm_probe(tmp_path, monkeypatch, prewarm):
    from coordinator_core.warm import server as warm_server

    calls = []
    monkeypatch.setattr(warm_server, "_prewarm_emit_chain", lambda: calls.append(1))
    repo = tmp_path / "repo"
    repo.mkdir()
    with fixture_settings_home(tmp_path):
        B.build_fixture(repo, 1)
        res = B.measure_multi_run_launch(repo, 1, runs=0, prewarm=prewarm)
    return calls, res


def test_round_zero_prewarms_the_emit_chain_the_way_the_engine_worker_does(tmp_path, monkeypatch):
    calls, res = _burst_with_prewarm_probe(tmp_path, monkeypatch, True)
    assert calls == [1]
    assert res["prewarmed"] is True
    assert res["prewarm_ms"] is not None


def test_a_fresh_process_burst_skips_the_prewarm_and_says_so(tmp_path, monkeypatch):
    calls, res = _burst_with_prewarm_probe(tmp_path, monkeypatch, False)
    assert calls == []
    assert res["prewarmed"] is False
    assert res["prewarm_ms"] is None


class _FakePool:
    def __init__(self):
        self.submitted = []

    def submit(self, fn):
        import concurrent.futures

        self.submitted.append(fn)
        done = concurrent.futures.Future()
        done.set_result(None)
        return done


def _boot_ctx(guard_pool, dispatch_pool, explode=False):
    from types import SimpleNamespace

    from coordinator_core.warm import server as warm_server

    def ensure(pool):
        def _ensure():
            if explode:
                raise RuntimeError("pool build failed")
            return pool

        return _ensure

    ctx = SimpleNamespace(_ensure_guard_pool=ensure(guard_pool), _ensure_dispatch_pool=ensure(dispatch_pool))
    ctx._prewarm_pool = lambda guard, n=warm_server._HEAL_PREWARM_WORKERS: warm_server._ServerContext._prewarm_pool(ctx, guard, n)
    ctx._boot_prewarm_run = lambda: warm_server._ServerContext._boot_prewarm_run(ctx)
    return ctx


def test_boot_prewarm_starts_a_guard_worker_per_burst_request_and_a_dispatch_worker():
    from coordinator_core.warm import server as warm_server

    guard, dispatch = _FakePool(), _FakePool()
    _boot_ctx(guard, dispatch)._boot_prewarm_run()
    assert len(guard.submitted) == warm_server._BOOT_PREWARM_GUARD_WORKERS >= B.LAUNCH_STARTS + 1
    assert len(dispatch.submitted) == 1
    assert set(guard.submitted + dispatch.submitted) == {warm_server._prewarm_noop}


def test_boot_prewarm_failure_is_swallowed_so_the_first_request_builds_lazily():
    _boot_ctx(_FakePool(), _FakePool(), explode=True)._boot_prewarm_run()
