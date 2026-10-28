"""Claude temp root retention: idle past max_age_hours and not live is deleted."""

import os
import time

import pytest

from coordinator_core.ops import prune_claude_temp as pct

HOUR = 3600


@pytest.fixture
def troot(tmp_path, monkeypatch):
    root = tmp_path / "claude"
    root.mkdir()
    monkeypatch.setattr(pct, "claude_temp_root", lambda: root)
    monkeypatch.setattr(pct, "_claude_retention_days", lambda: 48 / 24)
    return root


def _session(root, sid, idle_hours):
    d = root / "proj" / sid
    d.mkdir(parents=True)
    f = d / "out.txt"
    f.write_text("x")
    t = time.time() - idle_hours * HOUR
    os.utime(f, (t, t))
    os.utime(d, (t, t))
    return d


def test_dead_session_idle_49h_deleted(troot):
    d = _session(troot, "dead", 49)
    assert pct.prune(roots=[(troot, 2, 7)], live_session_ids=frozenset()) == 1
    assert not d.exists()


def test_session_idle_47h_kept(troot):
    d = _session(troot, "recent", 47)
    assert pct.prune(roots=[(troot, 2, 7)], live_session_ids=frozenset()) == 0
    assert d.exists()


def test_live_noncurrent_session_idle_49h_kept(troot):
    live = _session(troot, "live-other", 49)
    dead = _session(troot, "dead", 49)
    assert pct.prune("cur", roots=[(troot, 2, 7)], live_session_ids=frozenset({"live-other"})) == 1
    assert live.exists()
    assert not dead.exists()


def test_liveness_exception_keeps_every_dir(troot):
    d = _session(troot, "dead", 49)
    assert pct.prune(roots=[(troot, 2, 7)], live_session_ids=None) == 0
    assert d.exists()


def test_main_liveness_exception_keeps_every_dir(troot, monkeypatch):
    import coordinator_core.session.liveness as liveness

    def boom(*_a, **_k):
        raise RuntimeError("no liveness")

    monkeypatch.setattr(liveness, "live_session_ids", boom)
    d = _session(troot, "dead", 49)
    assert pct.main(["prune", "", ""]) == 0
    assert d.exists()


def test_other_root_keeps_seven_day_retention(troot, tmp_path):
    other = tmp_path / "other"
    old = other / "p" / "s"
    old.mkdir(parents=True)
    t = time.time() - 49 * HOUR
    os.utime(old, (t, t))
    assert pct.prune(roots=[(other, 2, 7)], live_session_ids=frozenset()) == 0
    assert old.exists()


def test_default_roots_claude_retention_reads_max_age_hours(monkeypatch, tmp_path):
    import coordinator_core.machine_resolver as mr

    monkeypatch.setattr(mr, "registry_get", lambda key: "24")
    assert pct._claude_retention_days() == 1.0
    monkeypatch.setattr(mr, "registry_get", lambda key: None)
    assert pct._claude_retention_days() == 2.0


def test_long_path_tree_deleted(troot):
    d = _session(troot, "deep", 49)
    t = time.time() - 49 * HOUR
    cur = d
    chain = []
    while len(str(cur)) < 330:
        cur = cur / ("n" * 40)
        chain.append(cur)
    os.makedirs(pct._long(str(cur)))
    for c in reversed(chain):
        os.utime(pct._long(str(c)), (t, t))
    os.utime(pct._long(str(d)), (t, t))
    assert pct.prune(roots=[(troot, 2, 7)], live_session_ids=frozenset()) == 1
    assert not os.path.exists(pct._long(str(d)))


def test_readonly_file_removed_by_chmod_retry(troot):
    import stat

    d = _session(troot, "ro", 49)
    f = d / "out.txt"
    os.chmod(f, stat.S_IREAD)
    pct._rmtree_tolerant(str(d))
    assert not d.exists()


def test_vanished_path_is_not_fresh(troot):
    assert pct._has_fresh_entry(str(troot / "missing-dir"), time.time()) is False


def test_live_session_scratch_children_pruned_after_48h(troot):
    live = _session(troot, "live", 1)
    old = live / "scratchpad" / "old"
    new = live / "scratchpad" / "new"
    old.mkdir(parents=True)
    new.mkdir()
    t = time.time() - 49 * HOUR
    os.utime(old, (t, t))
    pct.prune(roots=[(troot, 2, 7)], live_session_ids=frozenset({"live"}))
    assert live.exists() and new.exists() and not old.exists()
