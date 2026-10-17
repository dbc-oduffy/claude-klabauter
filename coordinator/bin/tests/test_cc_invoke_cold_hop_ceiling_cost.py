"""The cold spawn's kill ceiling resolves from a per-engine-root cache with no probe spawn.

Pins `cc_invoke._resolve_op_timeouts`'s cache: a hit costs neither an in-process engine
import nor a `--dump-op-timeouts` child, and the cached map carries the full key set.

Run: pytest coordinator/bin/tests/test_cc_invoke_cold_hop_ceiling_cost.py -v
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_LIB_DIR = Path(__file__).resolve().parent.parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import cc_invoke as _mod  # noqa: E402

pytestmark = pytest.mark.cadence

_ROOT = str(Path(__file__).resolve().parents[3])
_REQUIRED_KEYS = ("__default__", "__ceremony_budget__", "__warm_miss_wait__")


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv(_mod._ROUTE_UNREACHABLE_BASE_ENV, str(tmp_path))
    monkeypatch.delitem(sys.modules, "coordinator_core.ops._registry_map", raising=False)
    _mod._reset_op_timeout_cache()
    yield tmp_path
    _mod._reset_op_timeout_cache()


def _forbid_compute(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("map recomputed or probe spawned despite a valid cache entry")

    monkeypatch.setattr(_mod, "_dump_op_timeouts_in_process", _boom)
    monkeypatch.setattr(subprocess, "run", _boom)


def test_cache_hit_spawns_nothing_and_keeps_every_key(isolated, monkeypatch):
    _mod._resolve_op_timeouts(_ROOT, {}, 10)
    first = dict(_mod._OP_TIMEOUTS_MAP)
    assert _mod._OP_TIMEOUTS_STATE == "ok"
    assert list(isolated.rglob("op-timeouts-*.json"))

    _mod._reset_op_timeout_cache()
    _forbid_compute(monkeypatch)
    _mod._resolve_op_timeouts(_ROOT, {}, 10)

    from coordinator_core.invoke.__main__ import _dump_op_timeouts

    assert _mod._OP_TIMEOUTS_STATE == "ok"
    assert _mod._OP_TIMEOUTS_MAP == first
    assert set(first) == set(_dump_op_timeouts())
    for key in _REQUIRED_KEYS:
        assert key in first


def test_foreign_engine_map_is_cached_after_one_probe_spawn(isolated, monkeypatch):
    spawns = []
    payload = '{"__default__": 30.0, "__ceremony_budget__": 0.5, "__warm_miss_wait__": 3.0}'

    def _fake_run(argv, **kw):
        spawns.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=payload, stderr="")

    monkeypatch.setattr(_mod, "_dump_op_timeouts_in_process", lambda root: None)
    monkeypatch.setattr(subprocess, "run", _fake_run)
    _mod._resolve_op_timeouts(_ROOT, {}, 10)
    assert len(spawns) == 1 and _mod._OP_TIMEOUTS_MAP["__default__"] == 30.0

    _mod._reset_op_timeout_cache()
    _mod._resolve_op_timeouts(_ROOT, {}, 10)
    assert len(spawns) == 1
    assert _mod._OP_TIMEOUTS_STATE == "ok"


def test_failed_probe_is_never_cached(isolated, monkeypatch):
    monkeypatch.setattr(_mod, "_dump_op_timeouts_in_process", lambda root: None)
    monkeypatch.setattr(
        subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")
    )
    _mod._resolve_op_timeouts(_ROOT, {}, 10)
    assert _mod._OP_TIMEOUTS_STATE == "error"
    assert not list(isolated.rglob("op-timeouts-*.json"))


def test_env_knob_change_invalidates_the_cache(isolated, monkeypatch):
    monkeypatch.delenv("COORDINATOR_DISPATCH_TIMEOUT_SECS", raising=False)
    _mod._resolve_op_timeouts(_ROOT, {}, 10)
    wide = _mod._OP_TIMEOUTS_MAP["__default__"]
    _mod._reset_op_timeout_cache()
    monkeypatch.setenv("COORDINATOR_DISPATCH_TIMEOUT_SECS", "0.5")
    _mod._resolve_op_timeouts(_ROOT, {}, 10)
    assert wide != _mod._OP_TIMEOUTS_MAP["__default__"] == 0.5


def test_source_fingerprint_mismatch_is_a_miss(isolated):
    key = _mod._op_timeout_map_cache_key(_ROOT)
    path = _mod._op_timeout_map_cache_path(key)
    stale = [key[0], ["coordinator_core/ipc.py", 1, 1], *key[2:]]
    _mod._write_cached_op_timeout_map(path, stale, {"__default__": 1.0})
    assert _mod._read_cached_op_timeout_map(path, key) is None
    assert _mod._read_cached_op_timeout_map(path, stale) == {"__default__": 1.0}


def test_unwritable_cache_still_resolves(isolated, monkeypatch):
    blocker = isolated / "blocker"
    blocker.write_text("a file where the cache directory must go", encoding="utf-8")
    monkeypatch.setenv(_mod._ROUTE_UNREACHABLE_BASE_ENV, str(blocker))
    _mod._resolve_op_timeouts(_ROOT, {}, 10)
    assert _mod._OP_TIMEOUTS_STATE == "ok"
