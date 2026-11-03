"""baseline_cache: one read-only entry per sha, lease, LRU eviction, publish by rename, drift."""
import os
import stat

import pytest

from coordinator_core.testing import baseline_cache as bc
from coordinator_core.testing import baseline_export as be

DEAD = 999_999_991


def _sha(n: int) -> str:
    return f"{n:040x}"


@pytest.fixture
def top(tmp_path):
    t = tmp_path / "top"
    t.mkdir()
    return t


class Extractor:
    def __init__(self):
        self.calls = 0

    def __call__(self, partial):
        self.calls += 1
        (partial / "pkg" / "sub").mkdir(parents=True)
        (partial / "pkg" / "sub" / "mod.py").write_text("x = 1\n")
        (partial / "top.txt").write_text("t\n")


def _build(top, n, extractor=None):
    return bc.ensure_entry(top, _sha(n), extractor or Extractor())


def test_second_ensure_calls_the_extractor_zero_times(top):
    ex = Extractor()
    entry, built = bc.ensure_entry(top, _sha(1), ex)
    assert built is True and entry == bc.entry_path(top, _sha(1))
    entry2, built2 = bc.ensure_entry(top, _sha(1), ex)
    assert (entry2, built2) == (entry, False)
    assert ex.calls == 1


def test_entry_files_are_read_only(top):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root bypasses file permissions")
    entry, _ = _build(top, 1)
    with pytest.raises(PermissionError):
        open(entry / "pkg" / "sub" / "mod.py", "w")
    assert not os.stat(entry / "top.txt").st_mode & stat.S_IWUSR


def test_fourth_sha_evicts_lru_unleased_never_a_leased_one(top):
    for n in (1, 2, 3):
        _build(top, n)
    with bc.lease(bc.entry_path(top, _sha(1))):
        _build(top, 4)
        root = bc.cache_root(top)
        assert (root / _sha(1)).is_dir()
        assert not (root / _sha(2)).exists()
        assert (root / _sha(3)).is_dir() and (root / _sha(4)).is_dir()
        assert not (root / f"{_sha(2)}.used").exists()
    assert not list(bc.cache_root(top).glob("*.lease-*"))


def test_hit_refreshes_recency(top):
    for n in (1, 2, 3):
        _build(top, n)
    _build(top, 1)
    _build(top, 4)
    root = bc.cache_root(top)
    assert (root / _sha(1)).is_dir() and not (root / _sha(2)).exists()


def test_evict_never_removes_keep(top):
    for n in (1, 2, 3, 4):
        _build(top, n)
    assert bc.evict(top, keep=_sha(4)) == []
    assert bc.evict(top, keep=_sha(1)) == []


def test_dead_pid_lease_is_reclaimed(top, monkeypatch):
    for n in (1, 2, 3):
        _build(top, n)
    root = bc.cache_root(top)
    stale = root / f"{_sha(1)}.lease-{DEAD}"
    stale.touch()
    monkeypatch.setattr(bc, "_pid_alive", lambda pid: False if pid == DEAD else True)
    _build(top, 4)
    assert not stale.exists()
    assert not (root / _sha(1)).exists()


def test_live_foreign_lease_blocks_eviction(top, monkeypatch):
    for n in (1, 2, 3):
        _build(top, n)
    root = bc.cache_root(top)
    (root / f"{_sha(1)}.lease-{DEAD}").touch()
    monkeypatch.setattr(bc, "_pid_alive", lambda pid: True)
    _build(top, 4)
    assert (root / _sha(1)).is_dir() and not (root / _sha(2)).exists()


def test_dead_pid_partial_is_removed(top, monkeypatch):
    root = bc.cache_root(top)
    stale = root / f"{_sha(9)}.partial-{DEAD}"
    (stale / "d").mkdir(parents=True)
    f = stale / "d" / "ro.txt"
    f.write_text("x")
    os.chmod(f, stat.S_IREAD)
    monkeypatch.setattr(bc, "_pid_alive", lambda pid: False if pid == DEAD else True)
    _build(top, 1)
    assert not stale.exists()


def test_failed_extract_leaves_no_partial_and_no_entry(top):
    def boom(partial):
        (partial / "f").write_text("x")
        raise be.ExportError("nope")

    with pytest.raises(be.ExportError):
        bc.ensure_entry(top, _sha(1), boom)
    assert os.listdir(bc.cache_root(top)) == []


def test_lost_publish_race_leaves_one_entry(top):
    sha = _sha(1)
    root = bc.cache_root(top)

    def racing(partial):
        (partial / "mine").write_text("loser")
        winner = root / sha
        winner.mkdir()
        (winner / "winner").write_text("w")

    entry, built = bc.ensure_entry(top, sha, racing)
    assert built is False
    assert sorted(os.listdir(entry)) == ["winner"]
    assert not list(root.glob("*.partial-*"))
    assert [n for n in os.listdir(root) if n.startswith(sha) and "." not in n] == [sha]


def test_read_only_marking_never_alters_a_file_behind_a_data_dirs_link(top, tmp_path):
    live = tmp_path / "live"
    (live / "state").mkdir(parents=True)
    keep = live / "state" / "live.txt"
    keep.write_text("live")
    before = os.stat(keep).st_mode

    def with_link(partial):
        (partial / "src.py").write_text("x")
        be.link(str(live), partial, ("state",))

    entry, _ = bc.ensure_entry(top, _sha(1), with_link)
    assert os.stat(keep).st_mode == before
    keep.write_text("still writable")
    assert not os.stat(entry / "src.py").st_mode & stat.S_IWUSR

    for n in (2, 3, 4):
        _build(top, n)
    assert not entry.exists()
    assert keep.read_text() == "still writable"
    assert os.stat(keep).st_mode == before


def test_check_drift_reports_a_new_file_two_directories_deep(top):
    entry, _ = _build(top, 1)
    assert bc.check_drift(entry) is False
    (entry / "pkg" / "sub" / "polluted.pyc").write_text("junk")
    assert bc.check_drift(entry) is True


def test_check_drift_reports_a_removed_file_and_a_new_top_level_file(top):
    entry, _ = _build(top, 1)
    (entry / "top-new.txt").write_text("n")
    assert bc.check_drift(entry) is True
    entry2, _ = _build(top, 2)
    f = entry2 / "pkg" / "sub" / "mod.py"
    os.chmod(f, stat.S_IWRITE | stat.S_IREAD)
    f.unlink()
    assert bc.check_drift(entry2) is True


def test_check_drift_is_true_when_unverifiable(top):
    entry, _ = _build(top, 1)
    bc._manifest(entry).unlink()
    assert bc.check_drift(entry) is True


def test_lease_is_reentrant_within_a_process(top):
    entry = bc.entry_path(top, _sha(1))
    with bc.lease(entry) as path:
        with bc.lease(entry):
            assert path.exists()
        assert path.exists()
    assert not path.exists()
