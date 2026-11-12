"""`reverify-delivery record` with no --repo-root resolves the enclosing checkout, not the bare cwd."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import reverify_delivery as rd


def test_subdirectory_cwd_resolves_the_enclosing_checkout(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    sub = repo / "state" / "cross-repo" / "inbox"
    sub.mkdir(parents=True)
    (repo / ".git").mkdir()
    monkeypatch.chdir(sub)
    # A basetemp inside a real checkout would let the record walk escape tmp_path and find that
    # checkout's .git; confine existence checks to tmp_path so the fallback-to-cwd leg is measured.
    root = tmp_path.resolve()
    real_exists = rd.Path.exists
    monkeypatch.setattr(rd.Path, "exists", lambda self: (self == root or root in self.parents) and real_exists(self))
    assert rd._repo_of_cwd() == repo.resolve()
    assert rd._repo_of_record(tmp_path / "elsewhere" / "run.md") == repo.resolve()


def test_cwd_outside_any_checkout_is_returned_unchanged(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(rd.Path, "exists", lambda self: False)
    assert rd._repo_of_cwd() == tmp_path.resolve()
