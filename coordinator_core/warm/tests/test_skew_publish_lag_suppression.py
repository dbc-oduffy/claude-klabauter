"""Regression for the C3 `publish_lag_message` repo-name suppression (probe row 21).

Spec backlink: docs/plans/2026-08-30-the-engine-stops-naming-its-own-repo.md § C3.

`publish_lag_message` surfaces broadly (engine floor, cross-repo) regardless
of the reader's own repo, but its remedy (`percolate-round.py claude-klabauter`)
belongs to the engine/publish owner, not a reader working in some third repo
who cannot run it -- same shape as `cc_invoke._announce_engine_cli_split`.
`_reader_owns_engine_repo` gates only the repo-naming portion; the lag fact
itself always renders regardless.

All git interaction is monkeypatched; no `git` process is spawned, so this
stays on the fast tier (matches `test_publish_lag.py`'s own convention).
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.warm import skew


def _make_lag(behind: int = 3, age_minutes: float = 90.0) -> skew.PublishLag:
    return skew.PublishLag(
        stamp_sha="abc123",
        engine_commits_behind=behind,
        oldest_unpublished_iso="2026-08-30T00:00:00+00:00",
        age_minutes=age_minutes,
    )


def test_repo_name_present_for_a_reader_who_owns_the_engine_repo(monkeypatch):
    monkeypatch.setattr(skew, "_reader_owns_engine_repo", lambda: True)
    message = skew.publish_lag_message(_make_lag())
    assert message is not None
    assert "claude-klabauter" in message
    assert "3 commit(s)" in message


def test_repo_name_absent_for_a_third_repo_reader(monkeypatch):
    monkeypatch.setattr(skew, "_reader_owns_engine_repo", lambda: False)
    message = skew.publish_lag_message(_make_lag())
    assert message is not None
    assert "claude-klabauter" not in message
    # The lag fact itself is preserved even though the remedy's repo name is not.
    assert "3 commit(s)" in message
    assert "Publish:" in message


def test_reader_owns_engine_repo_true_when_cwd_matches_current_engine_clone(monkeypatch):
    from coordinator_core.warm import engine_root as engine_root_mod

    monkeypatch.setattr(
        "coordinator_core.git.repo_root.show_toplevel",
        lambda: str(engine_root_mod.current_engine_clone()),
    )
    assert skew._reader_owns_engine_repo() is True


def test_reader_owns_engine_repo_false_for_a_third_repo_cwd(tmp_path: Path, monkeypatch):
    third_repo = tmp_path / "some-other-repo"
    third_repo.mkdir()
    monkeypatch.setattr(
        "coordinator_core.git.repo_root.show_toplevel",
        lambda: str(third_repo),
    )
    assert skew._reader_owns_engine_repo() is False


def test_reader_owns_engine_repo_fails_open_when_unresolvable(monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.git.repo_root.show_toplevel",
        lambda: None,
    )
    assert skew._reader_owns_engine_repo() is True
