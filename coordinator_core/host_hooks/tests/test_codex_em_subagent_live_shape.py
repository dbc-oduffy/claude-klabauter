"""Codex EM/subagent split through `shim.decide`: an EM payload draws the DIY nag and keeps `git clean`; a payload
carrying `agent_id` gets the subagent limits.

`PAYLOADS` is the fixture list: each entry is a Codex PreToolUse payload dict plus the verdict it must draw.
Swap the synthetic entries for scrubbed live captures without touching the tests. The engine dial is pinned
down (`shim._budgeted` returns no verdict), so the real in-process guard chain answers and no warm engine is reached.
"""
from __future__ import annotations

import subprocess

import pytest

from coordinator_core.host_hooks import shim

_SUBAGENT_ID = "019a0000-0000-7000-8000-000000000001"
_NAG = "[em-code-dispatch nudge]"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_SUBAGENT_GUARD = "block-subagent-destructive-action"
_SUBAGENT_KEYS = {"agent_id": _SUBAGENT_ID, "agent_type": "worker", "transcript_path": "rollout-subagent.jsonl"}

_PATCH = "*** Begin Patch\n*** Add File: {path}\n+x = 1\n*** End Patch"

# Expected verdicts: "nag" = additionalContext carries the DIY nag, no deny; "allow" = no deny; "deny" = subagent deny.
PAYLOADS = [
    {"id": "em-apply-patch", "expect": "nag", "payload": {
        "hook_event_name": "PreToolUse", "session_id": "codex-em-s1", "tool_name": "apply_patch",
        "tool_input": {"command": _PATCH}}},
    {"id": "em-git-clean", "expect": "allow", "payload": {
        "hook_event_name": "PreToolUse", "session_id": "codex-em-s1", "tool_name": "exec_command",
        "tool_input": {"command": "git clean -fdx"}}},
    {"id": "subagent-git-clean", "expect": "deny", "payload": {
        "hook_event_name": "PreToolUse", "session_id": "codex-em-s1", "tool_name": "exec_command",
        "tool_input": {"command": "git clean -fdx"}, **_SUBAGENT_KEYS}},
]


@pytest.fixture(autouse=True)
def cold_engine(monkeypatch, tmp_path):
    monkeypatch.setattr(shim, "_budgeted", lambda request, budget: ({}, False))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "registry"))
    monkeypatch.setenv("COORDINATOR_WARM_RUNTIME_BASE", str(tmp_path / "warm"))
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "owned"
    path.mkdir()
    for args in (("init", "-q", "-b", "main"),
                 ("remote", "add", "origin", "https://github.com/me/thing.git")):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, creationflags=_NO_WINDOW)
    (path / "untracked.txt").write_text("u", encoding="utf-8")
    return path


def _case(case_id: str) -> dict:
    return next(entry for entry in PAYLOADS if entry["id"] == case_id)


def _payload(entry: dict, repo) -> dict:
    payload = {**entry["payload"], "cwd": str(repo)}
    args = dict(payload["tool_input"])
    if payload["tool_name"] == "apply_patch":
        args["command"] = args["command"].format(path=str(repo / "a.py"))
    return {**payload, "tool_input": args}


def _hso(answer: dict) -> dict:
    return answer.get("hookSpecificOutput") or {}


@pytest.mark.parametrize("entry", PAYLOADS, ids=[entry["id"] for entry in PAYLOADS])
def test_codex_payload_draws_the_expected_verdict(repo, entry):
    answer = shim.decide("codex", _payload(entry, repo))
    expect = entry["expect"]
    if expect == "nag":
        assert _NAG in _hso(answer)["additionalContext"]
        assert _hso(answer).get("permissionDecision") != "deny"
    elif expect == "allow":
        assert _hso(answer).get("permissionDecision") != "deny"
    else:
        assert _hso(answer).get("permissionDecision") == "deny"
        assert _SUBAGENT_GUARD in str(_hso(answer))


def test_the_subagent_flavour_of_the_em_apply_patch_draws_no_nag(repo):
    payload = _payload(_case("em-apply-patch"), repo)
    answer = shim.decide("codex", {**payload, **_SUBAGENT_KEYS})
    assert _NAG not in str(answer)
