"""Shared fixtures for the frontmatter tests."""

from __future__ import annotations

import pytest

_TEST_GIT_TIMEOUT_SECS = 30.0


@pytest.fixture()
def widened_foreign_git_timeout(monkeypatch):
    """Widen the foreign-repo git timeout for one test.

    Production clamps these probes to `FOREIGN_REPO_GIT_TIMEOUT_SECONDS`
    (narrow-only), so the fixture rebinds every module-level copy of the
    constant and wraps `_probe_foreign_repo`, whose caller-supplied timeout is
    already clamped. For tests whose assertion is about real git behaviour on
    a fixture repo, not about the budget.
    """
    from coordinator_core import git_scope
    from coordinator_core.frontmatter import schema_validate

    real_probe = git_scope._probe_foreign_repo
    monkeypatch.setattr(git_scope, "FOREIGN_REPO_GIT_TIMEOUT_SECONDS", _TEST_GIT_TIMEOUT_SECS)
    monkeypatch.setattr(schema_validate, "FOREIGN_REPO_GIT_TIMEOUT_SECONDS", _TEST_GIT_TIMEOUT_SECS)
    monkeypatch.setattr(
        git_scope,
        "_probe_foreign_repo",
        lambda root, timeout: real_probe(root, max(timeout, _TEST_GIT_TIMEOUT_SECS)),
    )
