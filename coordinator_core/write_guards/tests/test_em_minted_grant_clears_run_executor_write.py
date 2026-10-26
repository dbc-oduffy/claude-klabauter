"""A pm CLAUDE.md grant minted by the EM main thread through the
claude-md-grant-cli wrapper clears the run executor's CLAUDE.md write in the
same session, and a write without a live grant for that session and repo
stays denied."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.session import claude_md_grant as cmg
from coordinator_core.session import core, liveness
from coordinator_core.win_portability import no_console_passthrough_kwargs
from coordinator_core.write_guards import block_unauthorized_claude_md_write as guard

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_WRAPPER = Path(__file__).resolve().parents[3] / "coordinator" / "bin" / "claude-md-grant-cli.py"


def _load_wrapper():
    loader = importlib.machinery.SourceFileLoader("claude_md_grant_cli_proof", str(_WRAPPER))
    spec = importlib.util.spec_from_loader("claude_md_grant_cli_proof", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


def _make_repo(path: Path) -> Path:
    path.mkdir()
    for args in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@example.com"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(args, cwd=path, **no_console_passthrough_kwargs())
    (path / "CLAUDE.md").write_text("# rules\n", encoding="utf-8")
    return path


def _payload(repo: Path, sid: str) -> dict:
    return {
        "tool_name": "Write",
        "agent_id": "run-exec",
        "session_id": sid,
        "cwd": str(repo),
        "tool_input": {
            "file_path": str(repo / "CLAUDE.md"),
            "content": "# rules\n" + "one more line, growing the file\n",
        },
    }


def _denied(result) -> bool:
    return bool(result) and result["hookSpecificOutput"].get("permissionDecision") == "deny"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.delenv(guard._OVERRIDE_ENV_VAR, raising=False)
    monkeypatch.setattr(liveness, "session_live", lambda sid, cwd=None: True)
    repo = _make_repo(tmp_path / "repo")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "S")
    monkeypatch.chdir(repo)
    return repo


def _mint(repo: Path, monkeypatch, sid: str) -> None:
    monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)
    monkeypatch.chdir(repo)
    wrapper = _load_wrapper()
    monkeypatch.setattr(wrapper, "_import_module", lambda: cmg)
    assert wrapper.main(["grant", "pm", "approved by the PM"]) == 0
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "S")


def _guard(repo: Path, sid: str):
    with core.session_identity_override(sid):
        return guard.check(_payload(repo, sid))


def test_em_minted_grant_clears_the_run_executor_write(env, monkeypatch):
    _mint(env, monkeypatch, "S")
    assert _guard(env, "S") is None


def test_no_grant_is_denied(env):
    assert _denied(_guard(env, "S"))


def test_grant_under_another_session_is_denied(env, monkeypatch):
    _mint(env, monkeypatch, "S2")
    assert _denied(_guard(env, "S"))


def test_grant_with_a_dead_session_is_denied(env, monkeypatch):
    _mint(env, monkeypatch, "S")
    monkeypatch.setattr(liveness, "session_live", lambda sid, cwd=None: False)
    assert _denied(_guard(env, "S"))


def test_grant_minted_in_another_repo_is_denied(env, tmp_path, monkeypatch):
    other = _make_repo(tmp_path / "other")
    _mint(other, monkeypatch, "S")
    grant = other / ".git" / "coordinator-sessions" / "S" / "claude-md-write-grant.json"
    assert json.loads(grant.read_text(encoding="utf-8"))["granted_by"] == "pm"
    assert _denied(_guard(env, "S"))
