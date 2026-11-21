"""Codex EM/subagent split as observed live (Codex CLI 0.167, 1b C1 run): the EM's DIY nag arrives on the
op-dispatch advisory of a coordinator-invoke response, never on the host PreToolUse hook, because the CLI does
not route `apply_patch` through that hook; a payload carrying `agent_id` gets the subagent limits.

`PAYLOADS` is the fixture list: rows shaped after the live captures (state/run-evidence/<1b run>/payloads.jsonl),
each carrying the `channel` it draws on and the verdict it must draw. `host hook` rows run through `shim.decide`, whose engine
dial is pinned down (`shim._budgeted` returns no verdict) so the real in-process guard chain answers; the
`op-dispatch advisory` row runs through `coordinator-invoke` dispatch as a Codex EM caller.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.host_hooks import shim
from coordinator_core.invoke.__main__ import _dispatch_argv
from coordinator_core.session import xs_lane_budget

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SUBAGENT_ID = "01a1ec94-2eed-7c02-8753-e7f1d8e8ed56"
_EM_THREAD = "01a1ec93-b27c-7ad2-a41b-a9f8482c599a"
_NAG = "[em-code-dispatch nudge]"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_SUBAGENT_GUARD = "block-subagent-destructive-action"
_SUBAGENT_KEYS = {"agent_id": _SUBAGENT_ID, "agent_type": "worker", "transcript_path": "rollout-subagent.jsonl"}
_OP = "baton.close_report"
_CALLER_MARKERS = (
    "COORDINATOR_AGENT_PROVIDER", "COORDINATOR_AGENT_VENDOR", "COORDINATOR_AGENT_ROLE",
    "CODEX_THREAD_ID", "ANTIGRAVITY_CONVERSATION_ID", "AGY_SESSION_ID",
)

_PATCH = "*** Begin Patch\n*** Add File: {path}\n+x = 1\n*** End Patch"

# Expected verdicts: "advisory" = the op response carries the DIY nag; "none" = the hook says nothing and does not
# deny; "allow" = no deny; "deny" = subagent deny.
PAYLOADS = [
    {"id": "em-op-dispatch-over-bound-edit", "channel": "op-dispatch advisory", "expect": "advisory",
     "env": {"COORDINATOR_AGENT_PROVIDER": "codex", "CODEX_THREAD_ID": _EM_THREAD}},
    {"id": "em-apply-patch", "channel": "host hook", "expect": "none", "payload": {
        "hook_event_name": "PreToolUse", "session_id": "codex-em-s1", "tool_name": "apply_patch",
        "tool_input": {"command": _PATCH}}},
    {"id": "em-git-clean", "channel": "host hook", "expect": "allow", "payload": {
        "hook_event_name": "PreToolUse", "session_id": "codex-em-s1", "tool_name": "exec_command",
        "tool_input": {"command": "git clean -fdx"}}},
    {"id": "subagent-git-clean", "channel": "host hook", "expect": "deny", "payload": {
        "hook_event_name": "PreToolUse", "session_id": "codex-em-s1", "tool_name": "exec_command",
        "tool_input": {"command": "git clean -fdx"}, **_SUBAGENT_KEYS}},
]
_HOOK_ROWS = [entry for entry in PAYLOADS if entry["channel"] == "host hook"]
_OP_ROWS = [entry for entry in PAYLOADS if entry["channel"] == "op-dispatch advisory"]


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


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, creationflags=_NO_WINDOW)


def _over_bound_repo(tmp_path: Path) -> Path:
    path = tmp_path / "over"
    (path / "state" / "handoffs").mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.invalid")
    _git(path, "config", "user.name", "t")
    for i in range(3):
        (path / f"f{i}.txt").write_text("base\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "base")
    for i in range(3):
        (path / f"f{i}.txt").write_text("base\n" + "x\n" * 13, encoding="utf-8")
    return path


def _invoke(monkeypatch, repo: Path, env: dict[str, str], sid: str) -> dict:
    for key in _CALLER_MARKERS:
        monkeypatch.delenv(key, raising=False)
    from coordinator_core import warp_fire

    monkeypatch.setattr(warp_fire, "_counter_dir", lambda: repo.parent / "nag-counter")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)
    xs_lane_budget.open_xs_lane(repo, sid, "x")
    for i in range(3):
        xs_lane_budget.add_path(repo, sid, f"f{i}.txt")
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    stdout, _, _ = _dispatch_argv(
        [_OP, "{}", "--repo", str(repo), "--allow-unstamped-dispatch"], str(repo), allow_warm=False
    )
    return json.loads(stdout)


@pytest.mark.parametrize("entry", _HOOK_ROWS, ids=[entry["id"] for entry in _HOOK_ROWS])
def test_codex_hook_payload_draws_the_expected_verdict(repo, entry):
    answer = shim.decide("codex", _payload(entry, repo))
    expect = entry["expect"]
    if expect == "none":
        assert not _hso(answer).get("additionalContext")
        assert _NAG not in str(answer)
        assert _hso(answer).get("permissionDecision") != "deny"
    elif expect == "allow":
        assert _hso(answer).get("permissionDecision") != "deny"
    else:
        assert _hso(answer).get("permissionDecision") == "deny"
        assert _SUBAGENT_GUARD in str(_hso(answer))


@pytest.mark.parametrize("entry", _OP_ROWS, ids=[entry["id"] for entry in _OP_ROWS])
def test_codex_em_nag_rides_the_op_dispatch_advisory(tmp_path, monkeypatch, entry):
    reply = _invoke(monkeypatch, _over_bound_repo(tmp_path), entry["env"], f"codex-live-em-{tmp_path.name}")
    assert "result" in reply
    assert "past the XS bound" in reply.get("advisory", ""), reply
    assert "coordinator-invoke engine_ask.submit" in reply["advisory"]


def test_codex_subagent_op_dispatch_draws_no_advisory(tmp_path, monkeypatch):
    env = {**_case("em-op-dispatch-over-bound-edit")["env"], "COORDINATOR_AGENT_ROLE": "worker"}
    reply = _invoke(monkeypatch, _over_bound_repo(tmp_path), env, f"codex-live-sub-{tmp_path.name}")
    assert "advisory" not in reply


def test_the_subagent_flavour_of_the_em_apply_patch_draws_no_nag(repo):
    payload = _payload(_case("em-apply-patch"), repo)
    answer = shim.decide("codex", {**payload, **_SUBAGENT_KEYS})
    assert _NAG not in str(answer)
