"""Tests for the heavy-admission lease store with a stubbed ProcessPrimitives."""

import threading

from coordinator_core.bash_guards import _heavy_lease_store as store
from coordinator_core.bash_guards._heavy_admission_contract import (
    LEASE_ATTRIBUTION_TTL_S,
    LeaseRecord,
)


class _Prims:
    def __init__(self, live):
        self.live = set(live)

    def alive(self, pid, ctime):
        return (pid, ctime) in self.live


def _rec(holder=(10, 100), session=(10, 100), at=1000.0, cls="build"):
    return LeaseRecord(holder[0], holder[1], session[0], session[1], cls, at)


def test_write_lease_roundtrip(tmp_path):
    path = store.write_lease(_rec(), tmp_path)
    assert path.parent == tmp_path
    assert store.live_leases(tmp_path) == [_rec()]
    assert not [p for p in tmp_path.iterdir() if p.suffix != ".json"]


def test_reap_dead_pid(tmp_path):
    store.write_lease(_rec(holder=(10, 100), session=(5, 50)), tmp_path)
    assert store.reap(_Prims([]), tmp_path, now=lambda: 1001.0) == 1
    assert store.live_leases(tmp_path) == []


def test_reap_pid_reuse_ctime_mismatch(tmp_path):
    store.write_lease(_rec(holder=(10, 100), session=(5, 50)), tmp_path)
    assert store.reap(_Prims([(10, 999)]), tmp_path, now=lambda: 1001.0) == 1


def test_live_attributed_lease_survives_past_ttl(tmp_path):
    store.write_lease(_rec(holder=(10, 100), session=(5, 50)), tmp_path)
    assert store.reap(_Prims([(10, 100)]), tmp_path, now=lambda: 1000.0 + 10 * LEASE_ATTRIBUTION_TTL_S) == 0


def test_unattributed_ttl(tmp_path):
    store.write_lease(_rec(), tmp_path)
    prims = _Prims([(10, 100)])
    assert store.reap(prims, tmp_path, now=lambda: 1000.0 + LEASE_ATTRIBUTION_TTL_S) == 0
    assert store.unattributed_count(tmp_path, now=lambda: 1000.0 + 1) == 1
    assert store.reap(prims, tmp_path, now=lambda: 1000.0 + LEASE_ATTRIBUTION_TTL_S + 1) == 1
    assert store.unattributed_count(tmp_path) == 0


def test_corrupt_file_skipped_and_removed(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert store.live_leases(tmp_path) == []
    assert store.holders(tmp_path) == []
    assert store.reap(_Prims([]), tmp_path, now=lambda: 0.0) == 1
    assert not bad.exists()


def test_holders_names_pid_and_class(tmp_path):
    store.write_lease(_rec(holder=(77, 1), session=(5, 50), cls="typecheck"), tmp_path)
    (line,) = store.holders(tmp_path)
    assert "77" in line and "typecheck" in line and "unattributed" not in line


def test_attribute_narrows_only_matching_session(tmp_path):
    store.write_lease(_rec(), tmp_path)
    store.write_lease(_rec(holder=(20, 200), session=(20, 200)), tmp_path)
    assert store.attribute(10, 100, 33, 330, tmp_path) == 1
    got = {(r.holder_pid, r.session_pid) for r in store.live_leases(tmp_path)}
    assert got == {(33, 10), (20, 20)}
    assert store.attribute(10, 100, 44, 440, tmp_path) == 0


def test_two_writers_racing(tmp_path):
    def w(i):
        store.write_lease(_rec(holder=(10, 100), session=(10, 100), at=float(i)), tmp_path)

    threads = [threading.Thread(target=w, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(store.live_leases(tmp_path)) == 16


def test_default_dir_under_settings_home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    store.write_lease(_rec())
    assert (tmp_path / "heavy-admission" / "leases").is_dir()
    assert len(store.live_leases()) == 1
