"""The commit gate's ownership leg must not hard-deny without a verdict.

`block_subagent_commit`'s LEG 3 turned every `assert_paths_in_session_scope`
refusal into a hard deny for `coordinator:git-commit-agent`, including the
refusal that rests on no reading of the path at all: a claim-index walk that
aborted before answering (`ABORT_CAUSE_EMPTY_BASE`, the routine shape for a
session whose cwd resolves to no repo, plus `io_error` and `cap_exceeded`).
`scope_report.denial_is_wholly_indeterminate` existed for exactly that state
and named this guard as its consumer; it had no caller.

This file pins the verdicts that matter: no verdict allows and leaves a
record, a named live holder still denies, a determinate no-claimant still
denies (see `_ownership_leg_stand_down`'s docstring for why that one is a
ruling and not a predicate), and an ordinary allow is untouched. It also pins
the two message corrections that landed with it -- a rootless session's denial
naming its own leg, and the static text no longer implying a spent allowance
this guard does not keep.

Pure Python -- the scope helper is patched at the seam, so no repo, claim
ledger or hub on disk is required, and nothing here spawns a process.

Spec backlink: coordinator_core/bash_guards/_ownership_leg_stand_down.py
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import _ownership_leg_stand_down as stand_down
from coordinator_core.bash_guards import block_subagent_commit as guard
from coordinator_core.ops.session import scope_report as _scope_report

_SESSION = "sess-under-test"
_GIT_ROOT = "/fake/git-root"
_PATHS = ("state/handoffs/2026-08-19_210523_review-trail.md",)


def _reason(classification: str, path: str = _PATHS[0]) -> str:
    return (
        "path outside session %s scope: %r (%s); denied paths (1): %r (%s); "
        "no committable remainder (SC-DR-019)"
        % (_SESSION, path, classification, path, classification)
    )


_REASON_NO_VERDICT = _reason(_scope_report._CLASSIFICATION_INDETERMINATE)
_REASON_UNHELD = _reason(_scope_report._CLASSIFICATION_ORPHAN)
_REASON_HELD = _reason(
    "%slive session %s" % (_scope_report.CLAIMED_BY_PREFIX, "peer-session-id")
)


@pytest.fixture
def records(monkeypatch):
    from coordinator_core.bash_guards import _write_bump_stand_down

    written = []
    monkeypatch.setattr(
        _write_bump_stand_down,
        "log_environment_stand_down",
        lambda *a, **kw: written.append((a, kw)),
    )
    monkeypatch.setattr(_write_bump_stand_down, "stand_down_notice", lambda _r: None)
    return written


def _permit(reason: str):

    def helper(*_a, **_kw):
        return False, reason

    return guard._git_commit_agent_pathspec_permitted(
        list(_PATHS),
        False,
        _GIT_ROOT,
        _SESSION,
        None,
        assert_paths_in_session_scope=helper,
    )


def test_no_verdict_allows_and_records(records):
    allowed, reason = _permit(_REASON_NO_VERDICT)
    assert allowed is True, reason
    assert reason == ""
    assert len(records) == 1
    assert records[0][1]["marker"] == stand_down.STAND_DOWN_MARKER_NO_VERDICT


def test_an_audit_sink_that_raises_fails_closed(monkeypatch):
    """The stand-down is a DOWNGRADE to a recorded allow, so a record that
    cannot be made at all takes the allow with it.

    `log_environment_stand_down` swallows `OSError` itself and warns, which is
    the ordinary unwritable-sink path and leaves the allow standing. This pins
    the other direction: anything that escapes it reaches
    `block_subagent_commit._ownership_denial_stands_down`'s `except` arm, and
    a `CLASS = "hard-deny"` guard keeps the denial it already computed rather
    than granting an unrecorded commit.
    """
    from coordinator_core.bash_guards import _write_bump_stand_down

    def _raise(*_a, **_kw):
        raise RuntimeError("sink unavailable")

    monkeypatch.setattr(
        _write_bump_stand_down, "log_environment_stand_down", _raise
    )
    monkeypatch.setattr(_write_bump_stand_down, "stand_down_notice", lambda _r: None)
    allowed, _r = _permit(_REASON_NO_VERDICT)
    assert allowed is False


def test_a_named_live_holder_still_denies(records):
    allowed, reason = _permit(_REASON_HELD)
    assert allowed is False
    assert reason == _REASON_HELD
    assert records == []


def test_a_mixed_pathspec_with_one_held_path_denies_whole(records):
    mixed = _REASON_NO_VERDICT + "; additional: %s" % _REASON_HELD
    allowed, _reason = _permit(mixed)
    assert allowed is False
    assert records == []


def test_a_determinate_no_claimant_still_denies(records):
    allowed, reason = _permit(_REASON_UNHELD)
    assert allowed is False
    assert reason == _REASON_UNHELD
    assert records == []


def test_an_ordinary_allow_is_untouched(records):
    allowed, reason = guard._git_commit_agent_pathspec_permitted(
        list(_PATHS),
        False,
        _GIT_ROOT,
        _SESSION,
        None,
        assert_paths_in_session_scope=lambda *a, **kw: (True, ""),
    )
    assert allowed is True
    assert reason == ""
    assert records == []


def test_include_orphans_ask_never_reaches_the_stand_down(records):
    """SC-DR-022 refuses the ASK before the ownership leg runs, and the
    stand-down must not launder it: the `_LEG_*` sentinel is what comes back.
    """
    allowed, reason = guard._git_commit_agent_pathspec_permitted(
        list(_PATHS),
        True,
        _GIT_ROOT,
        _SESSION,
        None,
        assert_paths_in_session_scope=lambda *a, **kw: (True, ""),
    )
    assert allowed is False
    assert reason == guard._LEG_AGENT_ORPHAN_ADOPTION
    assert records == []


def test_already_clean_classification_does_not_stand_down(records):
    allowed, _r = _permit(_reason(_scope_report._CLASSIFICATION_ALREADY_CLEAN))
    assert allowed is False
    assert records == []


def test_unresolvable_repo_root_names_its_own_leg():
    allowed, reason = guard._git_commit_agent_may_commit(
        "git commit -m x -- a.py", None, _SESSION, None
    )
    assert allowed is False
    assert reason == guard._LEG_UNRESOLVABLE_GIT_ROOT
    message = guard._GIT_COMMIT_AGENT_LEG_MESSAGES[guard._LEG_UNRESOLVABLE_GIT_ROOT]
    assert "no repo root" in message
    assert "do not re-check" in message


def test_static_message_does_not_imply_a_spent_allowance():
    assert "Nothing is counted" in guard._GIT_COMMIT_AGENT_DENY_REASON
    assert "Used one already?" not in guard._GIT_COMMIT_AGENT_DENY_REASON


def test_the_ownership_leg_latches_nothing_across_attempts():
    denied_once = _permit(_REASON_HELD)
    denied_twice = _permit(_REASON_HELD)
    assert denied_once == denied_twice

    allowed, reason = guard._git_commit_agent_pathspec_permitted(
        list(_PATHS),
        False,
        _GIT_ROOT,
        _SESSION,
        None,
        assert_paths_in_session_scope=lambda *a, **kw: (True, ""),
    )
    assert allowed is True, "a prior refusal consumed something it must not"
    assert reason == ""

    again, reason_again = guard._git_commit_agent_pathspec_permitted(
        list(_PATHS),
        False,
        _GIT_ROOT,
        _SESSION,
        None,
        assert_paths_in_session_scope=lambda *a, **kw: (True, ""),
    )
    assert (again, reason_again) == (True, ""), (
        "a successful commit consumed something it must not"
    )
