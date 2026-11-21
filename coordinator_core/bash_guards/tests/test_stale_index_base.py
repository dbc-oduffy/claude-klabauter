"""stale_base_reverted_paths: a scratch index built before a peer commit is flagged; a current one is not."""

from __future__ import annotations


from coordinator_core import deny_registry
import pytest

_IDENT = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def test_stale_temp_index_base_is_disruptive_class() -> None:
    assert deny_registry.GUARD_DENY_CLASS["stale-temp-index-base"] == deny_registry.DISRUPTIVE
