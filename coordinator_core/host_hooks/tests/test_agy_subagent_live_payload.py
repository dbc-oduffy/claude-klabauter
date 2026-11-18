"""The live agy subagent payload (child conversation UUID, `run_command`) through `shim.decide`, cold engine.

Post-fix contract of the 2076b459c8 live probe: a dry run is an explicit allow (never `guard did not run`), the
destructive form is still denied, and the dashed conversation UUID resolves as a subagent id. The first two
`designed_red` tests turn green with the shim fallback fix, the third with the id-shape fix; each fix removes
its own marker.
"""
from __future__ import annotations

import subprocess

import pytest

from coordinator_core.host_hooks import agy_adapter, shim

_CONVERSATION = "d8f5a804-4fe2-40a9-a267-0c7e7c9929f3"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_GUARD_DID_NOT_RUN = "guard did not run"
_AGENT_ID_LEG = "agent_id-canonicalization"


@pytest.fixture(autouse=True)
def cold_subagent_engine(monkeypatch, tmp_path):
    monkeypatch.setattr(shim, "_budgeted", lambda request, budget: (None, False))
    monkeypatch.setattr(agy_adapter, "is_subagent_conversation", lambda conversation: True)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "registry"))
    monkeypatch.setenv("COORDINATOR_WARM_RUNTIME_BASE", str(tmp_path / "warm"))
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "probe"
    path.mkdir()
    for args in (("init", "-q", "-b", "main"), ("remote", "add", "origin", "https://github.com/me/thing.git")):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, creationflags=_NO_WINDOW)
    (path / "untracked-b.txt").write_text("u", encoding="utf-8")
    return path


def _live_payload(repo, command: str) -> dict:
    return {
        "conversationId": _CONVERSATION,
        "workspacePaths": [str(repo)],
        "toolCall": {"name": "run_command", "args": {"CommandLine": command}},
        "stepIdx": 3,
    }


@pytest.mark.designed_red
def test_subagent_git_clean_dry_run_is_an_explicit_allow(repo):
    answer = shim.decide("agy", _live_payload(repo, "git clean -n"))
    assert answer["decision"] == "allow"
    assert _GUARD_DID_NOT_RUN not in str(answer)


def test_subagent_git_clean_force_is_still_denied(repo):
    answer = shim.decide("agy", _live_payload(repo, "git clean -f untracked-b.txt"))
    assert answer["decision"] == "deny"
    assert "block-subagent-destructive-action" in answer["reason"]


@pytest.mark.designed_red
@pytest.mark.parametrize("command", ["git clean -n", "git clean -f untracked-b.txt"])
def test_subagent_conversation_uuid_resolves_so_no_canonicalization_leg_is_logged(repo, capsys, command):
    shim.decide("agy", _live_payload(repo, command))
    assert _AGENT_ID_LEG not in capsys.readouterr().err
