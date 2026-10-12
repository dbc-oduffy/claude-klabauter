"""`reverify-delivery record` with no --repo-root resolves the enclosing checkout, not the bare cwd."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import reverify_delivery as rd


def test_subdirectory_cwd_resolves_the_enclosing_checkout(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    sub = repo / "state" / "cross-repo" / "inbox"
    sub.mkdir(parents=True)
    (repo / ".git").mkdir()
    monkeypatch.chdir(sub)
    assert rd._repo_of_cwd() == repo.resolve()
    assert rd._repo_of_record(tmp_path / "elsewhere" / "run.md") == repo.resolve()


def test_cwd_outside_any_checkout_is_returned_unchanged(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(rd.Path, "exists", lambda self: False)
    assert rd._repo_of_cwd() == tmp_path.resolve()
