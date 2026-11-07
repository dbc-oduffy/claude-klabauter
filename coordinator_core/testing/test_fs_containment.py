import os
from pathlib import Path

import pytest

from coordinator_core.testing import fs_containment as fc

pytestmark = pytest.mark.complete


@pytest.fixture(autouse=True)
def _isolate_redirect_state(monkeypatch):
    import tempfile

    monkeypatch.setattr(fc, "_REDIRECT", None)
    monkeypatch.setattr(fc, "_INDEX_GUARD", {})
    monkeypatch.setattr(tempfile, "tempdir", None)
    for key in fc._TEMP_ENV:
        monkeypatch.setenv(key, os.environ.get(key, ""))


def test_gain_in_watched_root_is_reported(tmp_path, monkeypatch):
    watched = tmp_path / "home"
    watched.mkdir()
    monkeypatch.setattr(fc, "_WATCHED", [watched])
    monkeypatch.setattr(fc, "_SANCTIONED", [tmp_path / "repo"])
    before = fc._snapshot()
    (watched / ".stray").mkdir()
    after = fc._snapshot()
    assert after[watched] - before[watched] == {".stray"}


def test_entry_leading_into_sanctioned_root_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(fc, "_SANCTIONED", [tmp_path / "X" / "repo"])
    assert fc._leads_into_sanctioned(tmp_path, "X")
    assert not fc._leads_into_sanctioned(tmp_path, "fake")


def test_configure_does_not_watch_a_root_inside_the_repo():
    repo = Path("/a/b/repo")
    assert fc._contains(repo, repo / "sub")
    assert not fc._contains(repo / "sub", repo)


def test_ambient_markers_match_only_their_own_shapes():
    import fnmatch

    def ambient(name):
        return any(fnmatch.fnmatchcase(name, p) for p in fc._AMBIENT)

    assert ambient(".claude.json.lock")
    assert ambient(".claude.json.tmp.72694.8fded9339c65")
    assert ambient(".coordinator-claude.publish-staging-bokbz9l0")
    assert not ambient(".foo")
    assert not ambient("fake")
    assert not ambient("guard-message-corpus-doctrine-surface-x")


class _Opt:
    def __init__(self, basetemp=None):
        self.basetemp = basetemp


class _Cfg:
    def __init__(self, rootpath, basetemp=None, worker=False):
        self.rootpath = rootpath
        self.option = _Opt(basetemp)
        if worker:
            self.workerinput = {}


def _configure(monkeypatch, tmp_path, **kw):
    import tempfile

    temp = tmp_path / "Temp"
    temp.mkdir(exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp))
    monkeypatch.setattr(fc, "_SANCTIONED", [])
    monkeypatch.setattr(fc, "_WATCHED", [])
    repo = tmp_path / "myrepo"
    repo.mkdir(exist_ok=True)
    cfg = _Cfg(repo, **kw)
    fc.pytest_configure(cfg)
    return cfg, temp


def test_unset_basetemp_resolves_under_pytest_basetemp_root(tmp_path, monkeypatch):
    from coordinator_core import temp_layout

    cfg, temp = _configure(monkeypatch, tmp_path)
    base = Path(cfg.option.basetemp)
    assert base.parent == temp_layout.pytest_basetemp_root(tmp_path / "myrepo")
    assert base.parent.is_dir() and not base.exists()


def test_explicit_basetemp_is_respected(tmp_path, monkeypatch):
    cfg, _ = _configure(monkeypatch, tmp_path, basetemp="/explicit/x")
    assert cfg.option.basetemp == "/explicit/x"


def test_xdist_worker_is_left_alone(tmp_path, monkeypatch):
    cfg, temp = _configure(monkeypatch, tmp_path, worker=True)
    assert cfg.option.basetemp is None
    assert not (temp / "coordinator").exists()


def test_bare_temp_top_level_is_no_longer_sanctioned(tmp_path, monkeypatch):
    _, temp = _configure(monkeypatch, tmp_path)
    assert fc._leads_into_sanctioned(temp, "coordinator")
    assert not fc._leads_into_sanctioned(temp, "stray-file")


class _Sess:
    def __init__(self, config):
        self.config = config


def _minted(monkeypatch, tmp_path, **kw):
    cfg, _ = _configure(monkeypatch, tmp_path, **kw)
    base = Path(cfg.option.basetemp)
    base.mkdir()
    (base / "f.txt").write_text("x")
    return cfg, base


@pytest.mark.parametrize("status", [0, 5])
def test_pass_and_no_tests_delete_the_minted_dir(tmp_path, monkeypatch, status):
    cfg, base = _minted(monkeypatch, tmp_path)
    fc.pytest_sessionfinish(_Sess(cfg), status)
    assert not base.exists()


def test_failing_run_is_kept_marked_and_capped_at_three(tmp_path, monkeypatch):
    import os

    cfg, base = _minted(monkeypatch, tmp_path)
    parent = base.parent
    for i in range(4):
        old = parent / f"{100 + i}-aaaaa{i}"
        old.mkdir()
        marker = old / fc._KEPT_MARKER
        marker.touch()
        os.utime(marker, (1000 + i, 1000 + i))
    unmarked = parent / "999-bbbbbb"
    unmarked.mkdir()
    fc.pytest_sessionfinish(_Sess(cfg), 1)
    assert (base / fc._KEPT_MARKER).is_file()
    assert unmarked.is_dir()
    marked = sorted(p.name for p in parent.iterdir() if (p / fc._KEPT_MARKER).exists())
    assert marked == sorted([base.name, "103-aaaaa3", "102-aaaaa2"])


def test_explicit_basetemp_is_never_deleted(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    (explicit / "f").write_text("x")
    cfg, _ = _configure(monkeypatch, tmp_path, basetemp=str(explicit))
    fc.pytest_sessionfinish(_Sess(cfg), 0)
    assert (explicit / "f").is_file()


def test_worker_never_deletes_even_a_minted_dir(tmp_path, monkeypatch):
    cfg, base = _minted(monkeypatch, tmp_path)
    cfg.workerinput = {}
    fc.pytest_sessionfinish(_Sess(cfg), 0)
    assert base.is_dir()


def test_marker_literal_mirrors_temp_layout():
    from coordinator_core import temp_layout

    assert fc._KEPT_MARKER == temp_layout.PYTEST_KEPT_MARKER


def _redirected(monkeypatch, tmp_path, **kw):
    cfg, temp = _configure(monkeypatch, tmp_path, **kw)
    return cfg, temp, fc._REDIRECT


def test_run_temp_literal_mirrors_temp_layout():
    from coordinator_core import temp_layout

    assert fc._RUN_TEMP_DIRNAME == temp_layout.RUN_TEMP_DIRNAME


def test_controller_configure_redirects_every_temp_variable(tmp_path, monkeypatch):
    import tempfile

    _, temp, redirect = _redirected(monkeypatch, tmp_path)
    root = redirect.root
    assert root.is_dir()
    assert root.parent == temp / "coordinator" / "myrepo" / "run-temp"
    for key in ("TEMP", "TMP", "TMPDIR"):
        assert os.environ[key] == str(root)
    assert tempfile.tempdir == str(root)


def test_worker_configure_creates_no_redirect(tmp_path, monkeypatch):
    _, temp, redirect = _redirected(monkeypatch, tmp_path, worker=True)
    assert redirect is None
    assert not (temp / "coordinator").exists()


def test_nested_configure_does_not_nest_a_second_redirect(tmp_path, monkeypatch):
    import tempfile

    _, _, outer = _redirected(monkeypatch, tmp_path)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(outer.root))
    monkeypatch.setattr(fc, "_REDIRECT", None)
    fc.pytest_configure(_Cfg(tmp_path / "myrepo"))
    assert fc._REDIRECT is None
    assert not list(outer.root.rglob(fc._RUN_TEMP_DIRNAME))
    assert outer.root.resolve() not in fc._WATCHED


def test_redirect_root_is_not_watched_and_its_coordinator_dir_sanctioned(tmp_path, monkeypatch):
    _, _, redirect = _redirected(monkeypatch, tmp_path)
    assert redirect.root.resolve() not in fc._WATCHED
    assert fc._leads_into_sanctioned(redirect.root, "coordinator")
    assert not fc._leads_into_sanctioned(redirect.root, "stray")


def test_worker_inheriting_the_redirect_does_not_watch_it(tmp_path, monkeypatch):
    import tempfile

    _, _, redirect = _redirected(monkeypatch, tmp_path)
    root = redirect.root
    monkeypatch.setattr(fc, "_WATCHED", [])
    monkeypatch.setattr(fc, "_SANCTIONED", [])
    monkeypatch.setattr(fc, "_REDIRECT", None)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(root))
    fc.pytest_configure(_Cfg(tmp_path / "myrepo", worker=True))
    assert root.resolve() not in fc._WATCHED
    before = fc._snapshot()
    assert before
    (root / "stray").mkdir()
    after = fc._snapshot()
    assert all(after[r] - before[r] == set() for r in before)
    assert fc._leads_into_sanctioned(root, "coordinator")


def _finish(monkeypatch, tmp_path, status):
    cfg, _, redirect = _redirected(monkeypatch, tmp_path)
    sess = _Sess(cfg)
    sess.exitstatus = status
    return sess, redirect, cfg


def test_leftover_in_redirect_root_fails_a_passing_run(tmp_path, monkeypatch, capsys):
    sess, redirect, cfg = _finish(monkeypatch, tmp_path, 0)
    (redirect.root / "leaked.txt").write_text("x")
    (redirect.root / "coordinator").mkdir()
    fc.pytest_sessionfinish(sess, 0)
    assert sess.exitstatus == 1
    assert "leaked.txt" in capsys.readouterr().err


def test_clean_redirect_root_keeps_exit_status(tmp_path, monkeypatch):
    sess, redirect, _ = _finish(monkeypatch, tmp_path, 0)
    (redirect.root / "coordinator").mkdir()
    fc.pytest_sessionfinish(sess, 0)
    assert sess.exitstatus == 0


def test_failing_run_with_leftovers_keeps_its_status(tmp_path, monkeypatch):
    sess, redirect, _ = _finish(monkeypatch, tmp_path, 1)
    (redirect.root / "leaked").write_text("x")
    fc.pytest_sessionfinish(sess, 1)
    assert sess.exitstatus == 1


@pytest.mark.parametrize("leak", [False, True])
def test_redirect_root_is_deleted_after_sessionfinish(tmp_path, monkeypatch, leak):
    sess, redirect, _ = _finish(monkeypatch, tmp_path, 0)
    if leak:
        (redirect.root / "leaked").mkdir()
        (redirect.root / "leaked" / "f").write_text("x")
    fc.pytest_sessionfinish(sess, 0)
    assert not redirect.root.exists()
    assert fc._REDIRECT is None


def test_leftover_report_is_capped(tmp_path, monkeypatch, capsys):
    sess, redirect, _ = _finish(monkeypatch, tmp_path, 0)
    for i in range(25):
        (redirect.root / f"x{i:02d}").write_text("x")
    fc.pytest_sessionfinish(sess, 0)
    assert capsys.readouterr().err.count("left in run Temp") == 20


def test_sessionfinish_restores_the_temp_environment(tmp_path, monkeypatch):
    import tempfile

    monkeypatch.setenv("TMP", "before-tmp")
    monkeypatch.setattr(tempfile, "tempdir", "before-dir")
    sess, redirect, _ = _finish(monkeypatch, tmp_path, 0)
    fc.pytest_sessionfinish(sess, 0)
    assert os.environ["TMP"] == "before-tmp"
    assert tempfile.tempdir == "before-dir"


def test_counted_numbered_dir_never_scans_the_root(tmp_path, monkeypatch):
    (tmp_path / "work1").mkdir()
    fc._NEXT_NUMBER.clear()

    real_scandir = os.scandir

    def _no_scan(path=".", *a, **k):
        if isinstance(path, (str, os.PathLike)) and Path(path) == tmp_path:
            raise AssertionError("root was scanned")
        return real_scandir(path, *a, **k)

    monkeypatch.setattr(os, "scandir", _no_scan)
    made = [fc._counted_numbered_dir(tmp_path, "work").name for _ in range(3)]
    assert made == ["work0", "work2", "work3"]
    assert not (tmp_path / "workcurrent").exists()


def test_the_session_mktemp_uses_the_counted_maker():
    import _pytest.tmpdir as tmpdir_mod

    if hasattr(tmpdir_mod.TempPathFactory, "_make_counted_dir"):
        assert tmpdir_mod.TempPathFactory._make_counted_dir is fc._counted_make_counted_dir
    else:
        assert getattr(tmpdir_mod, "make_numbered_dir") is fc._counted_numbered_dir


def test_the_hooked_factory_maker_makes_unscanned_dirs_without_current_link(tmp_path):
    import _pytest.tmpdir as tmpdir_mod

    if not hasattr(tmpdir_mod.TempPathFactory, "_make_counted_dir"):
        pytest.skip("pytest without TempPathFactory._make_counted_dir")
    fc._NEXT_NUMBER.clear()
    factory = tmpdir_mod.TempPathFactory(None, 3, "all", None, _ispytest=True)
    factory._basetemp = tmp_path
    factory._trace = lambda *a, **k: None
    made = [factory.mktemp("w").name for _ in range(2)]
    assert made == ["w0", "w1"]
    assert not (tmp_path / "wcurrent").exists()


pytest_plugins = ["pytester"]

_GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]


def _git(repo, *args):
    import subprocess

    subprocess.run([*_GIT, *args], cwd=repo, check=True, capture_output=True)


def _tracked_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    (path / "tracked.txt").write_text("x")
    (path / "other.txt").write_text("y")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "seed")
    return path


def test_index_guard_names_a_new_untrack_and_ignores_an_unchanged_index(tmp_path):
    repo = _tracked_repo(tmp_path / "r")
    fc._arm_index_guard(repo)
    assert fc._INDEX_GUARD
    assert fc._index_changes() == []
    _git(repo, "rm", "-q", "--cached", "tracked.txt")
    assert fc._index_changes() == ["D tracked.txt"]
    assert fc._index_changes() == []


def test_index_guard_skips_the_git_spawn_while_the_index_stat_is_unchanged(tmp_path, monkeypatch):
    repo = _tracked_repo(tmp_path / "r")
    fc._arm_index_guard(repo)
    monkeypatch.setattr(fc, "_index_vs_head", lambda _r: pytest.fail("spawned"))
    assert fc._index_changes() == []


def test_index_guard_is_off_outside_a_git_repo(tmp_path):
    fc._arm_index_guard(tmp_path)
    assert not fc._INDEX_GUARD
    assert fc._index_changes() == []


def test_index_guard_ignores_a_preexisting_staged_difference(tmp_path):
    repo = _tracked_repo(tmp_path / "r")
    _git(repo, "rm", "-q", "--cached", "other.txt")
    fc._arm_index_guard(repo)
    assert fc._index_changes() == []


def _run_inner(pytester, tmp_path, body):
    repo = _tracked_repo(pytester.path)
    pytester.makepyfile(test_inner=body.format(repo=repr(str(repo))))
    pytester.makeini("[pytest]\n")
    env_path = str(Path(fc.__file__).resolve().parents[2])
    pytester._monkeypatch.setenv("PYTHONPATH", env_path)
    return pytester.runpytest_subprocess(
        "-p", "coordinator_core.testing.fs_containment", "-p", "no:cacheprovider",
        "--basetemp", str(tmp_path / "bt"),
    )


def test_session_names_the_test_that_untracks_a_repo_file(pytester, tmp_path):
    result = _run_inner(
        pytester,
        tmp_path,
        """
import subprocess

def test_untracker():
    subprocess.run(["git", "rm", "-q", "--cached", "tracked.txt"], cwd={repo}, check=True)

def test_innocent():
    pass
""",
    )
    out = result.stdout.str()
    assert "test_inner.py::test_untracker" in out
    assert "D tracked.txt" in out
    result.assert_outcomes(passed=2, errors=1)


def test_session_passes_a_test_that_only_touches_a_tmp_path_repo(pytester, tmp_path):
    result = _run_inner(
        pytester,
        tmp_path,
        """
import subprocess

def test_elsewhere(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "f").write_text("x")
    subprocess.run(["git", "add", "f"], cwd=tmp_path, check=True)
""",
    )
    result.assert_outcomes(passed=1)


def _dead_pid():
    import subprocess
    import sys

    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    proc.wait()
    return proc.pid


def _seed_root(monkeypatch, tmp_path):
    import tempfile

    from coordinator_core import temp_layout

    temp = tmp_path / "Temp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    root = temp_layout.pytest_basetemp_root(repo)
    root.mkdir(parents=True)
    return root


def test_configure_reaps_only_unmarked_dead_pid_run_dirs(tmp_path, monkeypatch):
    root = _seed_root(monkeypatch, tmp_path)
    dead = root / f"{_dead_pid()}-abcdef"
    import subprocess
    import sys

    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        live = root / f"{sleeper.pid}-abcdef"
        kept = root / f"{_dead_pid()}-fedcba"
        foreign = root / "not-a-run-dir"
        for d in (dead, live, kept, foreign):
            d.mkdir()
            (d / "f.txt").write_text("x")
        (kept / fc._KEPT_MARKER).touch()
        cfg, _ = _configure(monkeypatch, tmp_path)
        assert not dead.exists()
        assert live.is_dir() and kept.is_dir() and foreign.is_dir()
        assert Path(cfg.option.basetemp).parent == root
    finally:
        sleeper.kill()
        sleeper.wait()


def test_worker_configure_reaps_nothing(tmp_path, monkeypatch):
    root = _seed_root(monkeypatch, tmp_path)
    dead = root / f"{_dead_pid()}-abcdef"
    dead.mkdir()
    _configure(monkeypatch, tmp_path, worker=True)
    assert dead.is_dir()
