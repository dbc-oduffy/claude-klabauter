from pathlib import Path

from coordinator_core.testing import fs_containment as fc


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
