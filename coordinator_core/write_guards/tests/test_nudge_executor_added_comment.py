"""Behavioral tests for coordinator_core.write_guards.nudge_executor_added_comment."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.write_guards import engine
from coordinator_core.write_guards import nudge_executor_added_comment as guard

_SESSION = "em-session-abc123"
_AGENT = "a1b2c3d4e5f60718"
_REL = "tools/mod_x.py"
_COMMENT = "# keep this because the test was flaky"


def _seed(root: Path, subagent_type: str = "coordinator:executor", em_sid: str = _SESSION) -> None:
    hub = root / ".coordinator-local" / "coordinator-sessions"
    (hub / ".agents" / _AGENT).mkdir(parents=True)
    (hub / ".agents" / _AGENT / "em-session-id.txt").write_text(em_sid + "\n", encoding="utf-8")
    (hub / _SESSION).mkdir(parents=True, exist_ok=True)
    (hub / _SESSION / "dispatched-agents.txt").write_text(
        f"{_AGENT}\tx\t{subagent_type}\n", encoding="utf-8"
    )


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(
        "coordinator_core.write_guards._repo_root.resolve_repo_root",
        lambda cwd=None: str(tmp_path),
    )

    def _boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    return tmp_path


def _payload(root: Path, content: str, *, agent: str = _AGENT, rel: str = _REL) -> dict:
    p = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(root / rel), "content": content},
        "session_id": _SESSION,
        "cwd": str(root),
    }
    if agent:
        p["agent_id"] = agent
    return p


def _context(out: dict) -> str:
    return out["hookSpecificOutput"]["additionalContext"]


def test_executor_added_comment_advises_through_engine(root: Path) -> None:
    _seed(root)
    out = engine.evaluate(_payload(root, f"x = 1\n{_COMMENT}\n"))
    ctx = _context(out)
    assert f"{_REL}:2" in ctx
    assert "keep this because the test was flaky" in ctx
    assert "major finding" in ctx
    assert "trap:" in ctx
    assert "WARNING" in ctx
    assert "justify the keep in your report" in ctx
    assert "permissionDecision" not in out["hookSpecificOutput"]


def test_no_subagent_identity_gets_none(root: Path) -> None:
    _seed(root)
    assert guard.check(_payload(root, f"x = 1\n{_COMMENT}\n", agent="")) is None
    assert engine.evaluate(_payload(root, f"x = 1\n{_COMMENT}\n", agent="")) is None


def test_non_executor_type_gets_none(root: Path) -> None:
    _seed(root, subagent_type="coordinator:code-reviewer")
    assert guard.check(_payload(root, f"x = 1\n{_COMMENT}\n")) is None


def test_unresolvable_backpointer_gets_none(root: Path) -> None:
    assert guard.check(_payload(root, f"x = 1\n{_COMMENT}\n")) is None


def test_backpointer_naming_other_session_gets_none(root: Path) -> None:
    _seed(root, em_sid="some-other-session")
    assert guard.check(_payload(root, f"x = 1\n{_COMMENT}\n")) is None


def test_tagged_comment_gets_none(root: Path) -> None:
    _seed(root)
    assert guard.check(_payload(root, "x = 1\n# trap: order matters\n")) is None


def test_preexisting_comment_gets_none(root: Path) -> None:
    _seed(root)
    (root / _REL).parent.mkdir(parents=True)
    (root / _REL).write_text(f"{_COMMENT}\nx = 1\n", encoding="utf-8")
    assert guard.check(_payload(root, f"{_COMMENT}\nx = 2\n")) is None


def test_markdown_gets_none(root: Path) -> None:
    _seed(root)
    assert guard.check(_payload(root, "<!-- note -->\n# heading\n", rel="docs/a.md")) is None


def test_edit_reconstructs_after(root: Path) -> None:
    _seed(root)
    (root / _REL).parent.mkdir(parents=True)
    (root / _REL).write_text("x = 1\n", encoding="utf-8")
    payload = {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": str(root / _REL),
            "old_string": "x = 1\n",
            "new_string": f"x = 1\n{_COMMENT}\n",
        },
        "session_id": _SESSION,
        "agent_id": _AGENT,
        "cwd": str(root),
    }
    assert guard.check(payload) is not None
    payload["tool_input"]["old_string"] = "absent"
    assert guard.check(payload) is None


def test_cap_at_five(root: Path) -> None:
    _seed(root)
    body = "".join(f"# note number {i} here\n" for i in range(8))
    ctx = _context(guard.check(_payload(root, body)))
    assert ctx.count("added comment") == 5
    assert "(+3 more)" in ctx


def test_excerpt_is_truncated(root: Path) -> None:
    _seed(root)
    long_text = "word " * 60
    ctx = _context(guard.check(_payload(root, f"x = 1\n# {long_text}\n")))
    assert "..." in ctx
    assert long_text.strip() not in ctx


def test_oversized_before_file_gets_none(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(root)
    (root / _REL).parent.mkdir(parents=True)
    (root / _REL).write_text("x = 1\n" * 10, encoding="utf-8")
    monkeypatch.setattr(guard, "_MAX_BEFORE_BYTES", 8)
    assert guard.check(_payload(root, f"x = 1\n{_COMMENT}\n")) is None


def test_exception_fails_open(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(root)
    monkeypatch.setattr(guard, "_read_before", lambda p: (_ for _ in ()).throw(RuntimeError("x")))
    assert guard.check(_payload(root, f"x = 1\n{_COMMENT}\n")) is None
