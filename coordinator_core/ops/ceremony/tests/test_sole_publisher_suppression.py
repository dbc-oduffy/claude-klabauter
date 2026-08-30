"""
coordinator_core.ops.ceremony.tests.test_sole_publisher_suppression

opro-01 C-01 (state/audits/2026-08-18-opro-01-where-the-push-outcome-is-known.md)
introduced a sole-publisher suppression axis (`git_native._sole_publisher_env`,
`_AUTO_PUSH_SUPPRESS_ENV`, `deferred_publisher_span()`) so a caller that
publishes a commit itself could stand the post-commit hook's own push down
for that one commit.

GRAVESTONED 2026-08-30 (overengineering-reviewer Finding 5,
docs/plans/2026-08-30-who-pushes-and-when.md): the axis's only reader was
`auto_push.main()` (itself gravestoned, Finding 4 -- the post-commit hook
stopped invoking `auto_push` at all once C6/C7 landed, so `git_hook_install.
ensure_post_commit_hook` no longer passes `skip_env` for post-commit either).
`deferred_publisher_span()`'s one named caller, `wsc_tail.
_deferred_publisher_backstop()`, does not exist in this tree (verified at
HEAD -- `wsc_tail.py` is gone). Both were dead by the diff's own action, not
merely orphaned by this wave.

`git_native._sole_publisher_env` survives as a no-op stub (always returns
None) because `commit_with_message_file`, `commit_with_message_file_
pathspec_scoped`, and `commit_scoped` still accept `suppress_post_commit_
auto_push` as a keyword from callers outside this dispatch's scope
(`consumed_handoff_stamp.py`, `post_commit_tail.py`) -- this file now pins
the no-op contract those three signatures still promise their callers:
whatever the flag's value, no env is built and `os.environ` is never
touched.

Spec backlink: state/handoffs/2026-08-18_190000_roadmap-opro-01.md (C-01)
               state/audits/2026-08-18-opro-01-where-the-push-outcome-is-known.md
"""

from __future__ import annotations

import os

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

from coordinator_core.ops.ceremony import git_native


def test_sole_publisher_env_is_always_none():
    """Gravestoned: neither argument value builds an env dict any more."""
    assert git_native._sole_publisher_env(False) is None
    assert git_native._sole_publisher_env(True) is None


def test_sole_publisher_env_never_touches_os_environ():
    before = dict(os.environ)
    git_native._sole_publisher_env(True)
    assert dict(os.environ) == before


def test_deferred_publisher_span_is_gone():
    """The widening span this axis grew for a caller (`wsc_tail`) that does
    not exist in this tree is deleted outright, not left as a no-op --
    unlike `_sole_publisher_env`, nothing outside this package calls it by
    keyword, so there is no compatibility surface to preserve.
    """
    assert not hasattr(git_native, "deferred_publisher_span")
    assert not hasattr(git_native, "_deferred_publisher_active")


def test_auto_push_suppress_env_constant_is_gone():
    """The write-side env var name (`_AUTO_PUSH_SUPPRESS_ENV`) is deleted
    along with the function that built it -- nothing reads it any more on
    either side (`auto_push.main()`, its only reader, is gone too)."""
    assert not hasattr(git_native, "_AUTO_PUSH_SUPPRESS_ENV")
