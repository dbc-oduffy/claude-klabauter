import os
from pathlib import Path

import pytest

from coordinator_core.testing import fs_containment as fc


@pytest.fixture(autouse=True)
def _isolate_redirect_state(monkeypatch):
    import tempfile

    monkeypatch.setattr(fc, "_REDIRECT", None)
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
    temp.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp))
    monkeypatch.setattr(fc, "_SANCTIONED", [])
    monkeypatch.setattr(fc, "_WATCHED", [])
    repo = tmp_path / "myrepo"
    repo.mkdir()
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
