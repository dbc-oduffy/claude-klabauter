"""Tests for the IBMDT item-6 fix in
``coordinator_core.bash_guards.block_subagent_commit``: a subagent's
stdin- or heredoc-fed Python interpreter whose body imports
``coordinator_core.git.commit`` now denies.

Before this fix, ``check()`` ran every matcher against ``cmd_for_scan`` --
``cmd`` with heredoc BODIES already discarded by ``_strip_heredoc_bodies``
(a step that exists for an unrelated false-positive reason: heredoc prose
that merely mentions ``commit`` should not deny). That discard made an
actual ``python3 <<'PYEOF'`` body invisible to every matcher, including
the cheap ``_prefilter_mentions_commit`` pre-filter that gates them all --
confirmed live (this planning session's own probe): a heredoc importing
``commit_paths`` passed the served guard while an equivalent ``python3 -c``
payload was denied.

Pure Python -- no shell spawns, no filesystem writes. Identity resolution
is monkeypatched directly onto the guard module object, the same
seam-patching pattern ``test_block_subagent_commit.py`` uses.

Spec backlink: coordinator_core/bash_guards/block_subagent_commit.py
(part 22, `_has_python_heredoc_or_stdin_git_commit_import`)
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from coordinator_core.bash_guards import block_subagent_commit as guard

_SUBAGENT_TYPE = "coordinator:executor"
_GIT_COMMIT_AGENT_TYPE = "coordinator:git-commit-agent"


def _payload(command, agent_id="deadbeef0123", agent_type=None, session_id="sess1"):
    p: Dict[str, Any] = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": session_id,
        "cwd": None,
    }
    if agent_id is not None:
        p["agent_id"] = agent_id
    if agent_type is not None:
        p["agent_type"] = agent_type
    return p


def _subagent(monkeypatch, subagent_type=_SUBAGENT_TYPE):
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/git-root")
    monkeypatch.setattr(
        guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123"
    )
    monkeypatch.setattr(
        guard,
        "_read_backpointer_subagent_type",
        lambda git_root, agent_id: subagent_type,
    )


def _denies(monkeypatch, cmd):
    _subagent(monkeypatch)
    result = guard.check(_payload(cmd, agent_type=_SUBAGENT_TYPE))
    assert result is not None, f"expected DENY for: {cmd!r}"
    assert (
        result["hookSpecificOutput"]["permissionDecision"] == "deny"
    ), f"expected DENY for: {cmd!r}"
    return result


def _allows(monkeypatch, cmd, agent_type=_SUBAGENT_TYPE):
    _subagent(monkeypatch)
    result = guard.check(_payload(cmd, agent_type=agent_type))
    assert result is None, f"expected ALLOW for: {cmd!r}, got {result!r}"


# ---------------------------------------------------------------------------
# The confirmed bypass, and its fix
# ---------------------------------------------------------------------------


_MEMO_HEREDOC = (
    "python3 - <<'PYEOF'\n"
    "from coordinator_core.git.commit import commit_paths\n"
    "commit_paths('sess1', ['a.py'], message='msg')\n"
    "PYEOF"
)


def test_memo_shape_heredoc_import_denies(monkeypatch):
    """The memo's exact shape -- a stdin-fed (``python3 -``) heredoc
    whose body imports ``commit_paths`` from ``coordinator_core.git.
    commit`` -- previously passed the served guard. It must now deny.
    """
    _denies(monkeypatch, _MEMO_HEREDOC)


def test_same_heredoc_without_import_allows(monkeypatch):
    """The same heredoc shape, body swapped for text with no import and
    no other commit-shaped signal, must still allow -- the fix is scoped
    to the import, not to every ``python3 <<EOF`` heredoc.
    """
    cmd = (
        "python3 - <<'PYEOF'\n"
        "print('hello world')\n"
        "PYEOF"
    )
    _allows(monkeypatch, cmd)


def test_bare_python_heredoc_without_stdin_dash_denies(monkeypatch):
    """``python3 <<'PYEOF'`` (no leading ``-`` argv, heredoc feeds stdin
    implicitly) with the same import body.
    """
    cmd = (
        "python3 <<'PYEOF'\n"
        "from coordinator_core.git.commit import commit_paths\n"
        "commit_paths('sess1', ['a.py'])\n"
        "PYEOF"
    )
    _denies(monkeypatch, cmd)


def test_import_statement_form_denies(monkeypatch):
    """``import coordinator_core.git.commit`` (module import, not a
    ``from ... import`` name) is also a matched shape.
    """
    cmd = (
        "python3 - <<'PYEOF'\n"
        "import coordinator_core.git.commit as gc\n"
        "gc.commit_paths('sess1', ['a.py'])\n"
        "PYEOF"
    )
    _denies(monkeypatch, cmd)


def test_from_git_import_commit_form_denies(monkeypatch):
    """``from coordinator_core.git import commit`` (submodule import via
    the package, third recognized spelling).
    """
    cmd = (
        "python3 - <<'PYEOF'\n"
        "from coordinator_core.git import commit\n"
        "commit.commit_paths('sess1', ['a.py'])\n"
        "PYEOF"
    )
    _denies(monkeypatch, cmd)


def test_piped_stdin_python_c_script_literal_denies(monkeypatch):
    """No heredoc at all -- the script text is a quoted literal piped
    into ``python3`` via stdin (``echo '<script>' | python3``).
    """
    cmd = (
        "echo 'from coordinator_core.git.commit import commit_paths; "
        "commit_paths(\"sess1\", [\"a.py\"])' | python3"
    )
    _denies(monkeypatch, cmd)


def test_piped_stdin_without_import_allows(monkeypatch):
    cmd = "echo 'print(1)' | python3"
    _allows(monkeypatch, cmd)


def test_heredoc_piped_into_python_denies(monkeypatch):
    """Heredoc attached to ``cat``, piped into ``python3`` on the same
    line -- the interpreter is not the token immediately before ``<<``.
    """
    cmd = (
        "cat <<'PYEOF' | python3\n"
        "from coordinator_core.git.commit import commit_paths\n"
        "commit_paths('sess1', ['a.py'])\n"
        "PYEOF"
    )
    _denies(monkeypatch, cmd)


def test_heredoc_fed_to_non_python_interpreter_allows(monkeypatch):
    """A heredoc that mentions the module path but is fed to ``bash``,
    not a Python interpreter, is out of this matcher's scope (it may
    still be caught by unrelated matchers, but this shape carries no
    other commit-shaped signal either).
    """
    cmd = (
        "bash - <<'SHEOF'\n"
        "echo coordinator_core.git.commit\n"
        "SHEOF"
    )
    _allows(monkeypatch, cmd)


# ---------------------------------------------------------------------------
# Regression: the sanctioned route still allows
# ---------------------------------------------------------------------------


def test_ceremony_commit_v2_op_door_still_allows(monkeypatch):
    """``ceremony.commit_v2`` through the op door, for the one identity
    permitted to use it, must still allow -- this fix's new matcher never
    fires on it (no heredoc, no stdin-piped python, no import), and must
    not regress the existing ownership-gated allow path.
    """
    _subagent(monkeypatch, subagent_type=_GIT_COMMIT_AGENT_TYPE)
    monkeypatch.setattr(
        guard,
        "_git_commit_agent_may_commit",
        lambda cmd_for_scan, git_root, session_id, cwd: (True, ""),
    )
    cmd = (
        "python3 -m coordinator_core.invoke ceremony.commit_v2 "
        "'{\"worktree_root\": \"/repo\", \"paths\": [\"src/foo.py\"], \"message\": \"x\"}'"
    )
    result = guard.check(_payload(cmd, agent_type=_GIT_COMMIT_AGENT_TYPE))
    assert result is None, f"expected ALLOW for: {cmd!r}, got {result!r}"


def test_ordinary_python_dash_m_pytest_allows(monkeypatch):
    """A plain, common dispatched-agent command -- must not regress."""
    _allows(monkeypatch, "python3 -m pytest coordinator_core/tests/test_x.py")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
