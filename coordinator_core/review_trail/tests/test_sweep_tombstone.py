import os

from coordinator_core.review_trail import sweep_tombstone as st
from coordinator_core.session import machinery_paths as mp


def test_tombstone_path_is_outside_both_share_roots(tmp_path):
    p = mp.tombstone_path(str(tmp_path), "sid1")
    for root in mp.share_roots(str(tmp_path)):
        assert not p.startswith(root + os.sep)
    assert p.endswith(os.path.join("sweep-tombstones", "sid1.jsonl"))


def test_round_trip(tmp_path):
    root = str(tmp_path)
    abs_p = os.path.join(root, ".coordinator-local", "subagent-share", "s1", "r.md")
    st.record_swept(root, "s1", [abs_p, "state/subagent-share/s1/x.md"], "reap", 1.0)
    recs = st.swept_sidecars(root, "s1", now=2.0)
    assert [r["path"] for r in recs] == [
        ".coordinator-local/subagent-share/s1/r.md",
        "state/subagent-share/s1/x.md",
    ]
    assert recs[0]["sweeper"] == "reap"
    assert recs[0]["swept_at"] == "1970-01-01T00:00:01Z"
    assert recs[0]["reason"]


def test_missing_and_unreadable_give_empty(tmp_path):
    root = str(tmp_path)
    assert st.swept_sidecars(root, "nope") == ()
    target = mp.tombstone_path(root, "bad")
    os.makedirs(target)  # a directory where the file should be: unreadable
    assert st.swept_sidecars(root, "bad") == ()


def test_two_sweepers_both_read_back(tmp_path):
    root = str(tmp_path)
    st.record_swept(root, "s", ["a.md"], "reap", 1.0)
    st.record_swept(root, "s", ["b.md"], "prune", 2.0)
    assert {r["sweeper"] for r in st.swept_sidecars(root, "s", now=3.0)} == {"reap", "prune"}


def test_age_bound_drops_old_records_but_keeps_recent(tmp_path):
    root = str(tmp_path)
    st.record_swept(root, "s", ["old.md"], "reap", 1000.0)
    st.record_swept(root, "s", ["new.md"], "reap", 1000.0 + st.MAX_AGE_SECONDS)
    now = 1000.0 + st.MAX_AGE_SECONDS + 10
    assert [r["path"] for r in st.swept_sidecars(root, "s", now=now)] == ["new.md"]
    assert len(st.swept_sidecars(root, "s", now=1001.0)) == 2


def test_session_reuse_bound_drops_prior_incarnation(tmp_path):
    root = str(tmp_path)
    st.record_swept(root, "s", ["prev.md"], "reap", 100.0)
    st.record_swept(root, "s", ["cur.md"], "reap", 500.0)
    got = st.swept_sidecars(root, "s", now=600.0, since=300.0)
    assert [r["path"] for r in got] == ["cur.md"]
    assert len(st.swept_sidecars(root, "s", now=600.0)) == 2


def test_expire_tombstones_removes_only_stale_files(tmp_path):
    root = str(tmp_path)
    st.record_swept(root, "old", ["a.md"], "reap", 1.0)
    st.record_swept(root, "fresh", ["b.md"], "reap", 1.0)
    old, fresh = mp.tombstone_path(root, "old"), mp.tombstone_path(root, "fresh")
    os.utime(old, (1000.0, 1000.0))
    os.utime(fresh, (1000.0 + st.MAX_AGE_SECONDS, 1000.0 + st.MAX_AGE_SECONDS))
    assert st.expire_tombstones(root, 1000.0 + st.MAX_AGE_SECONDS + 5) == 1
    assert not os.path.exists(old) and os.path.exists(fresh)
    assert st.expire_tombstones(str(tmp_path / "none"), 1.0) == 0


def test_record_never_raises_and_refuses_unsafe_id(tmp_path):
    root = str(tmp_path)
    os.makedirs(mp.tombstone_path(root, "x"))
    st.record_swept(root, "x", ["a.md"], "reap", 1.0)
    st.record_swept(root, "../evil", ["a.md"], "reap", 1.0)
    assert st.swept_sidecars(root, "../evil") == ()
    assert not os.path.exists(os.path.join(str(tmp_path.parent), "evil.jsonl"))
