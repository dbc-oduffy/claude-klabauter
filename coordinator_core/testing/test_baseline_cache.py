"""baseline_cache: one persistent tree moved per sha, lease, publish by rename, drift."""
import os
import stat

import pytest

from coordinator_core.testing import baseline_cache as bc
from coordinator_core.testing import baseline_export as be
from coordinator_core.testing.tmp_git_repo import init_tmp_git_repo, tmp_git
from pathlib import Path

DEAD = 999_999_991


def _sha(n: int) -> str:
    return f"{n:040x}"


@pytest.fixture
def top(tmp_path: Path) -> Path:
    t = tmp_path / "top"
    t.mkdir()
    return t


class Extractor:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, partial) -> None:
        self.calls += 1
        (partial / "pkg" / "sub").mkdir(parents=True)
        (partial / "pkg" / "sub" / "mod.py").write_text("x = 1\n")
        (partial / "top.txt").write_text("t\n")


def _build(top, n, extractor=None) -> tuple[Path, bool]:
    return bc.ensure_entry(top, _sha(n), extractor or Extractor())


def _trees(top) -> list[str]:
    root = bc.cache_root(top)
    return sorted(n for n in os.listdir(root) if bc._SHA_RE.fullmatch(n))


def test_second_ensure_calls_the_extractor_zero_times(top) -> None:
    ex = Extractor()
    entry, built = bc.ensure_entry(top, _sha(1), ex)
    assert built is True and entry == bc.entry_path(top, _sha(1))
    entry2, built2 = bc.ensure_entry(top, _sha(1), ex)
    assert (entry2, built2) == (entry, False)
    assert ex.calls == 1


def test_entry_files_are_read_only(top) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root bypasses file permissions")
    entry, _ = _build(top, 1)
    with pytest.raises(PermissionError):
        open(entry / "pkg" / "sub" / "mod.py", "w", encoding="utf-8")
    assert not os.stat(entry / "top.txt").st_mode & stat.S_IWUSR


def test_at_most_one_tree_remains_after_three_distinct_shas(top) -> None:
    for n in (1, 2, 3):
        _build(top, n)
        assert _trees(top) == [_sha(n)]
    root = bc.cache_root(top)
    assert not list(root.glob("*.partial-*")) and not list(root.glob("*.evicting-*"))
    assert sorted(os.listdir(root)) == sorted([_sha(3), f"{_sha(3)}.dirs", f"{_sha(3)}.used"])


def test_leased_tree_is_never_evicted_and_a_second_tree_is_built(top) -> None:
    _build(top, 1)
    with bc.lease(bc.entry_path(top, _sha(1))):
        _build(top, 2)
        assert _trees(top) == [_sha(1), _sha(2)]
        _build(top, 3)
        assert _trees(top) == [_sha(1), _sha(3)]
    _build(top, 3)
    assert _trees(top) == [_sha(3)]
    assert not list(bc.cache_root(top).glob("*.lease-*"))


def test_hit_retires_trees_a_lease_pinned(top) -> None:
    _build(top, 1)
    with bc.lease(bc.entry_path(top, _sha(1))):
        _build(top, 2)
    ex = Extractor()
    bc.ensure_entry(top, _sha(2), ex)
    assert ex.calls == 0 and _trees(top) == [_sha(2)]


def test_evict_never_removes_keep_or_a_leased_tree(top) -> None:
    _build(top, 1)
    with bc.lease(bc.entry_path(top, _sha(1))):
        _build(top, 2)
        assert bc.evict(top, keep=_sha(2)) == []
    assert bc.evict(top, keep=_sha(2)) == [_sha(1)]
    assert bc.evict(top, keep=_sha(2)) == []


def test_dead_pid_lease_is_reclaimed(top, monkeypatch: pytest.MonkeyPatch) -> None:
    _build(top, 1)
    root = bc.cache_root(top)
    stale = root / f"{_sha(1)}.lease-{DEAD}"
    stale.touch()
    monkeypatch.setattr(bc, "_pid_alive", lambda pid: False if pid == DEAD else True)
    _build(top, 2)
    assert not stale.exists()
    assert _trees(top) == [_sha(2)]


def test_live_foreign_lease_blocks_eviction(top, monkeypatch: pytest.MonkeyPatch) -> None:
    _build(top, 1)
    root = bc.cache_root(top)
    (root / f"{_sha(1)}.lease-{DEAD}").touch()
    monkeypatch.setattr(bc, "_pid_alive", lambda pid: True)
    _build(top, 2)
    assert _trees(top) == [_sha(1), _sha(2)]


def test_dead_pid_partial_is_removed(top, monkeypatch: pytest.MonkeyPatch) -> None:
    root = bc.cache_root(top)
    stale = root / f"{_sha(9)}.partial-{DEAD}"
    (stale / "d").mkdir(parents=True)
    f = stale / "d" / "ro.txt"
    f.write_text("x")
    os.chmod(f, stat.S_IREAD)
    monkeypatch.setattr(bc, "_pid_alive", lambda pid: False if pid == DEAD else True)
    _build(top, 1)
    assert not stale.exists()


def test_failed_extract_leaves_no_partial_and_no_entry(top) -> None:
    def boom(partial):
        (partial / "f").write_text("x")
        raise be.ExportError("nope")

    with pytest.raises(be.ExportError):
        bc.ensure_entry(top, _sha(1), boom)
    assert os.listdir(bc.cache_root(top)) == []


def test_lost_publish_race_leaves_one_entry(top) -> None:
    sha = _sha(1)
    root = bc.cache_root(top)

    def racing(partial) -> None:
        (partial / "mine").write_text("loser")
        winner = root / sha
        winner.mkdir()
        (winner / "winner").write_text("w")

    entry, built = bc.ensure_entry(top, sha, racing)
    assert built is False
    assert sorted(os.listdir(entry)) == ["winner"]
    assert not list(root.glob("*.partial-*"))
    assert [n for n in os.listdir(root) if n.startswith(sha) and "." not in n] == [sha]


def test_read_only_marking_never_alters_a_file_behind_a_data_dirs_link(top, tmp_path: Path) -> None:
    live = tmp_path / "live"
    (live / "state").mkdir(parents=True)
    keep = live / "state" / "live.txt"
    keep.write_text("live")
    before = os.stat(keep).st_mode

    def with_link(partial) -> None:
        (partial / "src.py").write_text("x")
        be.link(str(live), partial, ("state",))

    entry, _ = bc.ensure_entry(top, _sha(1), with_link)
    assert os.stat(keep).st_mode == before
    keep.write_text("still writable")
    assert not os.stat(entry / "src.py").st_mode & stat.S_IWUSR

    _build(top, 2)
    assert not entry.exists()
    assert keep.read_text() == "still writable"
    assert os.stat(keep).st_mode == before


def test_check_drift_reports_a_new_file_two_directories_deep(top) -> None:
    entry, _ = _build(top, 1)
    assert bc.check_drift(entry) is False
    (entry / "pkg" / "sub" / "polluted.pyc").write_text("junk")
    assert bc.check_drift(entry) is True


def test_check_drift_reports_a_removed_file_and_a_new_top_level_file(top) -> None:
    entry, _ = _build(top, 1)
    (entry / "top-new.txt").write_text("n")
    assert bc.check_drift(entry) is True
    entry2, _ = _build(top, 2)
    f = entry2 / "pkg" / "sub" / "mod.py"
    os.chmod(f, stat.S_IWRITE | stat.S_IREAD)
    f.unlink()
    assert bc.check_drift(entry2) is True


class GitExtractor(Extractor):
    def __call__(self, partial) -> None:
        super().__call__(partial)
        (partial / ".git" / "objects").mkdir(parents=True)
        (partial / ".git" / "HEAD").write_text("ref\n")


def test_top_level_dot_git_is_unsealed_and_never_drift(top) -> None:
    entry, _ = bc.ensure_entry(top, _sha(1), GitExtractor())
    head = entry / ".git" / "HEAD"
    assert os.stat(head).st_mode & stat.S_IWUSR
    head.write_text("refreshed\n")
    (entry / ".git" / "index").write_text("idx")
    assert bc.check_drift(entry) is False
    (entry / "pkg" / "sub" / "polluted.pyc").write_text("junk")
    assert bc.check_drift(entry) is True


def test_evicting_an_entry_removes_its_dot_git(top) -> None:
    entry, _ = bc.ensure_entry(top, _sha(1), GitExtractor())
    assert bc._evict_one(bc.cache_root(top), _sha(1)) is True
    assert not entry.exists()


def test_check_drift_is_true_when_unverifiable(top) -> None:
    entry, _ = _build(top, 1)
    bc._manifest(entry).unlink()
    assert bc.check_drift(entry) is True


def test_lease_is_reentrant_within_a_process(top) -> None:
    entry = bc.entry_path(top, _sha(1))
    with bc.lease(entry) as path:
        with bc.lease(entry):
            assert path.exists()
        assert path.exists()
    assert not path.exists()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, list[str]]:
    """A real repo with three commits touching root files, a nested file and a tracked DATA_DIRS file."""
    root = tmp_path / "repo"
    root.mkdir()
    init_tmp_git_repo(root)
    for rel, text in {"keep.txt": "keep\n", "a.txt": "a1\n", "gone.txt": "g\n", "d/n.txt": "n1\n", "state/s.txt": "s1\n"}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    tmp_git(root, "add", "-A")
    tmp_git(root, "commit", "-q", "-m", "c1")
    (root / "a.txt").write_text("a2\n", encoding="utf-8")
    (root / "gone.txt").unlink()
    (root / "new.txt").write_text("new\n", encoding="utf-8")
    (root / "d" / "n.txt").write_text("n2\n", encoding="utf-8")
    (root / "state" / "s.txt").write_text("s2\n", encoding="utf-8")
    tmp_git(root, "add", "-A")
    tmp_git(root, "commit", "-q", "-m", "c2")
    (root / "a.txt").write_text("a3\n", encoding="utf-8")
    tmp_git(root, "add", "-A")
    tmp_git(root, "commit", "-q", "-m", "c3")
    shas = tmp_git(root, "log", "--format=%H", "--reverse").stdout.split()
    return Path(os.path.realpath(root)), shas[-3:]


def _real_extract(toplevel: Path, sha: str):
    def extract(partial: Path) -> None:
        be.clone_tree(str(toplevel), sha, partial)
        be.link(str(toplevel), partial, ("state",))

    return extract


def _count_git(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []
    real = be._git_popen

    def counting(args, cwd, **kw):
        calls.append(list(args))
        return real(args, cwd, **kw)

    monkeypatch.setattr(be, "_git_popen", counting)
    monkeypatch.setattr(bc, "_git_popen", counting)
    return calls


@pytest.mark.spawns_process
def test_move_rewrites_only_the_diff_and_costs_two_git_spawns(repo, top, monkeypatch: pytest.MonkeyPatch) -> None:
    toplevel, (s1, s2, s3) = repo
    entry1, built = bc.ensure_entry(top, s1, _real_extract(toplevel, s1))
    assert built is True
    keep_before = os.stat(entry1 / "keep.txt").st_mtime_ns
    nested_before = os.stat(entry1 / "d" / "n.txt").st_mtime_ns
    calls = _count_git(monkeypatch)

    def no_extract(_partial) -> None:
        raise AssertionError("a move must not re-extract")

    entry2, built2 = bc.ensure_entry(top, s2, no_extract)
    assert built2 is False and entry2 == bc.entry_path(top, s2)
    assert [c[0] for c in calls] == ["diff", "checkout"]
    assert _trees(top) == [s2]
    assert (entry2 / "a.txt").read_text() == "a2\n" and (entry2 / "new.txt").read_text() == "new\n"
    assert (entry2 / "d" / "n.txt").read_text() == "n2\n"
    assert not (entry2 / "gone.txt").exists()
    assert os.stat(entry2 / "keep.txt").st_mtime_ns == keep_before
    assert os.stat(entry2 / "d" / "n.txt").st_mtime_ns != nested_before
    assert be._head_is(entry2, s2)
    assert bc.check_drift(entry2) is False
    if not (hasattr(os, "geteuid") and os.geteuid() == 0):
        for rel in ("a.txt", "new.txt", "d/n.txt", "keep.txt"):
            assert not os.stat(entry2 / rel).st_mode & stat.S_IWUSR

    calls.clear()
    entry3, _ = bc.ensure_entry(top, s3, no_extract)
    assert [c[0] for c in calls] == ["diff", "checkout"]
    assert _trees(top) == [s3] and (entry3 / "a.txt").read_text() == "a3\n"
    assert os.stat(entry3 / "keep.txt").st_mtime_ns == keep_before


@pytest.mark.spawns_process
def test_move_never_writes_through_the_data_dir_link(repo, top) -> None:
    toplevel, (s1, s2, _s3) = repo
    entry1, _ = bc.ensure_entry(top, s1, _real_extract(toplevel, s1))
    live = toplevel / "state" / "s.txt"
    live.write_text("live edit\n", encoding="utf-8")
    entry2, _ = bc.ensure_entry(top, s2, _real_extract(toplevel, s2))
    assert entry2.is_dir() and not entry1.exists()
    assert live.read_text(encoding="utf-8") == "live edit\n"
    assert be._is_link(str(entry2 / "state"))


@pytest.mark.spawns_process
def test_move_refused_by_a_lease_builds_a_second_tree(repo, top) -> None:
    toplevel, (s1, s2, _s3) = repo
    entry1, _ = bc.ensure_entry(top, s1, _real_extract(toplevel, s1))
    with bc.lease(entry1):
        entry2, built = bc.ensure_entry(top, s2, _real_extract(toplevel, s2))
        assert built is True and _trees(top) == sorted([s1, s2])
        assert (entry1 / "a.txt").read_text() == "a1\n" and (entry2 / "a.txt").read_text() == "a2\n"


@pytest.mark.spawns_process
def test_move_of_a_tree_whose_head_drifted_falls_back_to_extract(repo, top) -> None:
    toplevel, (s1, s2, _s3) = repo
    entry1, _ = bc.ensure_entry(top, s1, _real_extract(toplevel, s1))
    (entry1 / ".git" / "HEAD").write_text(f"{s2}\n", encoding="utf-8")
    entry2, built = bc.ensure_entry(top, s2, _real_extract(toplevel, s2))
    assert built is True and _trees(top) == [s2]
    assert (entry2 / "a.txt").read_text() == "a2\n"
