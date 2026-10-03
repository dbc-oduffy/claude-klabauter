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
