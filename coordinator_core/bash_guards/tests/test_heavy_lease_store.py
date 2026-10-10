"""Tests for the heavy-admission lease store with a stubbed ProcessPrimitives."""

import json
import threading
import time

from coordinator_core.bash_guards import _heavy_lease_store as store
from coordinator_core.bash_guards._heavy_lease_store import claim_orphans, live_leases, oldest_open_launch, write_lease
from coordinator_core.bash_guards._heavy_admission_contract import (
    LEASE_ATTRIBUTION_TTL_S,
    LeaseRecord,
    ProcRow,
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


def _tree(pid, ctime, heavy=("node",)):
    """An orphan tree under a bash root whose heavy processes start in the order given."""
    from coordinator_core.bash_guards._session_census import OrphanTree

    root = ProcRow(pid, 999, ctime, "bash.exe")
    rows = tuple(ProcRow(pid + 1 + i, pid, ctime + 1 + i, f"{stem}.exe") for i, stem in enumerate(heavy))
    return OrphanTree(root, rows)


def _open(launch, cls="typecheck", session=(1, 10), at=None):
    return LeaseRecord(session[0], session[1], session[0], session[1], cls, time.time() if at is None else at, launch)


class TestClaimOrphans:
    def test_a_lease_claims_the_tree_created_soonest_after_its_launch(self, tmp_path):
        path = write_lease(_open(launch=100), tmp_path)
        assert claim_orphans([_tree(50, 130), _tree(40, 110), _tree(30, 90)], window=60, directory=tmp_path) == 1
        rec = json.loads(path.read_text(encoding="utf-8"))
        assert (rec["holder_pid"], rec["holder_ctime"], rec["launch_ctime"]) == (40, 110, 100)

    def test_outside_the_window_or_before_the_launch_nothing_is_claimed(self, tmp_path):
        write_lease(_open(launch=100), tmp_path)
        assert claim_orphans([_tree(30, 99), _tree(40, 161)], window=60, directory=tmp_path) == 0

    def test_a_tree_without_an_image_of_the_class_is_not_claimed(self, tmp_path):
        write_lease(_open(launch=100, cls="typecheck"), tmp_path)
        assert claim_orphans([_tree(40, 110, heavy=("python",))], window=60, directory=tmp_path) == 0

    def test_a_tree_whose_command_is_another_class_is_not_claimed_for_a_later_image(self, tmp_path):
        # Live 2026-10-10: a typecheck lease whose tsc had exited claimed a peer's UE python run.
        write_lease(_open(launch=100, cls="typecheck"), tmp_path)
        assert claim_orphans([_tree(40, 110, heavy=("python", "node"))], window=60, directory=tmp_path) == 0

    def test_two_leases_take_two_trees_in_launch_order(self, tmp_path):
        a = write_lease(_open(launch=100, session=(1, 10)), tmp_path)
        b = write_lease(_open(launch=105, session=(2, 20)), tmp_path)
        assert claim_orphans([_tree(40, 103), _tree(50, 108)], window=60, directory=tmp_path) == 2
        assert json.loads(a.read_text(encoding="utf-8"))["holder_pid"] == 40
        assert json.loads(b.read_text(encoding="utf-8"))["holder_pid"] == 50

    def test_a_held_tree_is_never_claimed_twice(self, tmp_path):
        write_lease(LeaseRecord(40, 110, 1, 10, "typecheck", time.time(), 90), tmp_path)
        write_lease(_open(launch=100), tmp_path)
        assert claim_orphans([_tree(40, 110)], window=60, directory=tmp_path) == 0

    def test_a_lease_without_a_launch_mark_or_past_the_ttl_is_never_claimed(self, tmp_path):
        write_lease(_open(launch=0), tmp_path)
        write_lease(_open(launch=100, at=time.time() - 10_000), tmp_path)
        assert claim_orphans([_tree(40, 110)], window=60, directory=tmp_path) == 0
        assert oldest_open_launch(tmp_path) is None

    def test_an_old_lease_file_without_launch_ctime_still_reads(self, tmp_path):
        p = tmp_path / "1-10-x.json"
        p.write_text(json.dumps({"holder_pid": 1, "holder_ctime": 10, "session_pid": 1, "session_ctime": 10,
                                 "heavy_class": "build", "admitted_at": 1.0}), encoding="utf-8")
        assert live_leases(tmp_path)[0].launch_ctime == 0
