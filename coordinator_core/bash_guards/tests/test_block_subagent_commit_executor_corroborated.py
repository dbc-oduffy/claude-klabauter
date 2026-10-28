"""The commit agent adopts orphan paths an executor run-report names.

Drives `_git_commit_agent_pathspec_permitted` with a scope check that denies
the way `assert_paths_in_session_scope` does for an unverified session.
"""
from __future__ import annotations

import pytest

from coordinator_core.bash_guards import block_subagent_commit as guard
from coordinator_core.ops.session import scope_report

SESSION = "sess-lead-1"


def _reason(entries):
    parts = ["%r (%s)" % (p, c) for p, c in entries]
    first_p, first_c = entries[0]
    return "path outside session %s scope: %r (%s); denied paths (%d): %s" % (
        SESSION,
        first_p,
        first_c,
        len(entries),
        ", ".join(parts),
    )


_IGNORED = scope_report._CLASSIFICATION_INCLUDE_ORPHANS_IGNORED
_ORPHAN = scope_report._CLASSIFICATION_ORPHAN
_PEER = "claimed by live session other-sess"


def _permitted(tmp_path, entries):
    def _scope(session_id, paths, cwd=None, *, allow_orphans=False):
        return False, _reason(entries)

    return guard._git_commit_agent_pathspec_permitted(
        [p for p, _ in entries],
        False,
        str(tmp_path),
        SESSION,
        str(tmp_path),
        assert_paths_in_session_scope=_scope,
    )


def _sidecar(tmp_path, name, agent_type, body):
    d = tmp_path / ".coordinator-local" / "subagent-share" / SESSION
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(
        "---\nstatus: open\nagent_type: %s\n---\n\n%s\n" % (agent_type, body),
        encoding="utf-8",
    )


@pytest.mark.parametrize("classification", [_IGNORED, _ORPHAN])
def test_orphan_named_in_executor_report_is_adopted(tmp_path, classification):
    _sidecar(tmp_path, "ex.md", "coordinator:executor", "Files changed:\n- src/a.py\n")
    allowed, reason = _permitted(tmp_path, [("src/a.py", classification)])
    assert allowed, reason


def test_orphan_absent_from_executor_report_is_denied(tmp_path):
    _sidecar(tmp_path, "ex.md", "coordinator:executor", "Files changed:\n- src/a.py\n")
    allowed, _ = _permitted(tmp_path, [("src/b.py", _IGNORED)])
    assert not allowed


def test_prefix_of_a_reported_path_is_not_corroboration(tmp_path):
    _sidecar(tmp_path, "ex.md", "coordinator:executor", "- src/a.py.bak\n- src/a.pyc\n")
    allowed, _ = _permitted(tmp_path, [("src/a.py", _IGNORED)])
    assert not allowed


def test_one_uncorroborated_orphan_denies_the_whole_pathspec(tmp_path):
    _sidecar(tmp_path, "ex.md", "coordinator:executor", "- src/a.py\n")
    allowed, _ = _permitted(
        tmp_path, [("src/a.py", _IGNORED), ("src/b.py", _IGNORED)]
    )
    assert not allowed


def test_peer_claimed_path_denies_even_when_report_names_it(tmp_path):
    _sidecar(tmp_path, "ex.md", "coordinator:executor", "- src/a.py\n- src/p.py\n")
    allowed, _ = _permitted(
        tmp_path, [("src/a.py", _IGNORED), ("src/p.py", _PEER)]
    )
    assert not allowed


def test_report_from_a_non_executor_does_not_corroborate(tmp_path):
    _sidecar(
        tmp_path, "gc.md", "coordinator:git-commit-agent", "- src/a.py\n"
    )
    allowed, _ = _permitted(tmp_path, [("src/a.py", _IGNORED)])
    assert not allowed


def test_no_report_directory_denies(tmp_path):
    allowed, _ = _permitted(tmp_path, [("src/a.py", _IGNORED)])
    assert not allowed


def test_unsafe_session_id_reads_nothing(tmp_path):
    assert guard._executor_report_texts(str(tmp_path), "../x") == []
