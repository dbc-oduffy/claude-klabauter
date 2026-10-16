"""bump_out_of_repo_tool_write renders the external-workspace remedy for a Write into a registered workspace."""

from __future__ import annotations

import pytest

from coordinator_core import machine_resolver as mr
from coordinator_core.bash_guards import _write_bump_applicability as applicability
from coordinator_core.bash_guards import _write_bump_message as message
from coordinator_core.tests.git_seed import seeded_repo
from coordinator_core.write_guards import bump_out_of_repo_tool_write as guard

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _reason(result) -> str:
    return result["hookSpecificOutput"]["permissionDecisionReason"]


def _setup(tmp_path, monkeypatch, register: bool):
    own = seeded_repo(tmp_path / "own", name="Test")
    ws = tmp_path / "ws"
    ws.mkdir()
    reg = tmp_path / "reg"
    reg.mkdir()
    text = f'[repos]\nown = "{own.as_posix()}"\n'
    if register:
        text += f'[external_workspaces.p4_x]\npath = "{ws.as_posix()}"\nvcs = "p4"\n'
    (reg / "registry.toml").write_text(text, encoding="utf-8")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(own))
    mr._external_ws_cache.clear()
    pinned = tmp_path / "tmproot"
    pinned.mkdir()
    monkeypatch.setattr(applicability.tempfile, "gettempdir", lambda: str(pinned))
    monkeypatch.setattr(applicability, "_posix_tmp_literal", lambda: str(pinned))
    for var in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.delenv(var, raising=False)
    return own, ws


def _payload(path, cwd, agent_id=""):
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": str(path)},
        "session_id": "sess-ext-ws",
        "cwd": str(cwd),
        "agent_id": agent_id,
    }


def test_write_into_non_git_workspace_gets_external_workspace_remedy(tmp_path, monkeypatch):
    own, ws = _setup(tmp_path, monkeypatch, register=True)
    result = guard.check(_payload(ws / "f.txt", own))
    assert result is not None, "a Write into a non-git workspace must reach render_bump_message"
    text = _reason(result)
    assert "p4_x" in text
    assert "someone else's code" in text
    assert "cross-repo-memo" not in text
    assert "touch" not in text


def test_unregistered_non_git_dir_keeps_old_copy(tmp_path, monkeypatch):
    own, ws = _setup(tmp_path, monkeypatch, register=False)
    result = guard.check(_payload(ws / "f.txt", own))
    assert result is None or "someone else's code" not in _reason(result)


def test_foreign_repo_target_still_renders_old_copy(tmp_path, monkeypatch):
    own, _ = _setup(tmp_path, monkeypatch, register=True)
    other = seeded_repo(tmp_path / "other", name="Test")
    reg = tmp_path / "reg"
    (reg / "registry.toml").write_text(
        f'[repos]\nown = "{own.as_posix()}"\nother = "{other.as_posix()}"\n', encoding="utf-8"
    )
    mr._external_ws_cache.clear()
    result = guard.check(_payload(other / "f.txt", own))
    assert result is not None
    assert "someone else's code" not in _reason(result)


def test_renderer_variants_name_key_and_no_memo():
    for agent in (message.AGENT_CLASS_EM, message.AGENT_CLASS_SUBAGENT):
        text = message.render_bump_message(
            agent_class=agent,
            target_repo="/w/ws",
            session_repo="own",
            gitdir=None,
            session_id="s",
            destination_class=message.DESTINATION_EXTERNAL_WORKSPACE,
            destination_owner="p4_x",
        )
        assert "p4_x" in text and "someone else's code" in text
        assert "cross-repo-memo" not in text
