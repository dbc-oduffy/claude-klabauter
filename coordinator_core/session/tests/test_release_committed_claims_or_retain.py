"""`scope.release_committed_claims_or_retain` carries the two post-commit
release rules every commit route inherits: skip on a falsy sid, and never
let a release failure escape."""

from __future__ import annotations

import logging

import pytest

from coordinator_core.session import scope


@pytest.fixture
def calls(monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(
        scope,
        "release_committed_claims",
        lambda sid, paths, cwd=None: seen.append((sid, paths, cwd)),
    )
    return seen


def test_releases_under_the_given_sid(calls, tmp_path):
    scope.release_committed_claims_or_retain(tmp_path, iter(["a.md", "b.md"]), "s1", "x")
    assert calls == [("s1", ["a.md", "b.md"], str(tmp_path))]


@pytest.mark.parametrize("sid", [None, ""])
def test_falsy_sid_skips_without_releasing(calls, tmp_path, sid):
    scope.release_committed_claims_or_retain(tmp_path, ["a.md"], sid, "x")
    assert calls == []


def test_empty_paths_is_a_noop(calls, tmp_path):
    scope.release_committed_claims_or_retain(tmp_path, [], "s1", "x")
    assert calls == []


def test_release_failure_is_retained_and_logged(monkeypatch, tmp_path, caplog):
    def boom(sid, paths, cwd=None):
        raise RuntimeError("git gone")

    monkeypatch.setattr(scope, "release_committed_claims", boom)
    with caplog.at_level(logging.DEBUG, logger=scope.__name__):
        scope.release_committed_claims_or_retain(tmp_path, ["a.md"], "s1", "my_route")
    assert "my_route: release_committed_claims failed post-commit" in caplog.text


def test_path_generator_failure_is_retained(calls, tmp_path):
    def bad_paths():
        yield "a.md"
        raise ValueError("outside root")

    scope.release_committed_claims_or_retain(tmp_path, bad_paths(), "s1", "x")
    assert calls == []
