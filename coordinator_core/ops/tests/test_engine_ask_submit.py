"""engine_ask.submit: the explicit-argument door onto the shared engine-ask gates."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from coordinator_core.hooks import _engine_ask_gates as gates
from coordinator_core.hooks import submit_engine_ask as hook
from coordinator_core.hooks._envelope import deny, rewrite_input
from coordinator_core.ipc import dispatch_message
from coordinator_core.ops import engine_ask_submit as op

OP = "engine_ask.submit"


def _allow(script, context: str = "Submitted to the engine as run run-abc123. It runs."):
    envelope = rewrite_input("PreToolUse", {"scriptPath": str(script)}, context)
    envelope["hookSpecificOutput"]["permissionDecision"] = "allow"
    return envelope


@pytest.fixture(autouse=True)
def verified_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.ops.workflow_fire import authority
    monkeypatch.setattr(authority, 'caller_principal', lambda *a: authority.Principal('sess-1', 'em', {}))


@pytest.fixture
def script(tmp_path: Path) -> Path:
    path = tmp_path / "run.mjs"
    path.write_bytes(b"export const meta = {}\n")
    return path


@pytest.fixture
def params(tmp_path: Path) -> dict[str, str]:
    return {
        "ask": "build the thing",
        "ask_source": "pm",
        "session_id": "sess-1",
        "caller": "codex-em",
        "cwd": str(tmp_path),
    }


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch, script):
    """Payloads the gates received; the answer is an allow naming `script`."""
    payloads: list[dict] = []

    def evaluate(payload):
        payloads.append(payload)
        return _allow(script)

    monkeypatch.setattr(hook, "evaluate", evaluate)
    return payloads


def _dispatch(params):
    msg = {
        "jsonrpc": "2.0", "id": 1, "method": OP, "params": params,
        "_origin_worktree": str(Path(__file__).resolve().parents[3]),
    }
    reply = asyncio.run(dispatch_message(msg))
    assert "result" in reply, reply
    return reply["result"]


def test_submit_returns_run_handle_through_dispatch(params, script, seen) -> None:
    result = _dispatch({**params, "fire": False})
    assert result == {
        "run_id": "run-abc123",
        "script_path": str(script),
        "sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
    }
    sent = seen[0]
    assert sent["tool_name"] == "Workflow" and sent["session_id"] == "sess-1" and sent["cwd"] == params["cwd"]
    assert sent["tool_input"]["args"] == {"ask": "build the thing", "source": "pm"}


def test_sizing_submission_carries_no_source_and_the_hook_args(params, seen) -> None:
    del params["ask"], params["ask_source"]
    params.update(sizing="state/sizings/x.yaml", stop_after="plan", em_context="ctx", fire=False)
    _dispatch(params)
    assert seen[0]["tool_input"]["args"] == {"sizing": "state/sizings/x.yaml", "stop_after": "plan", "em_context": "ctx"}


def test_gate_deny_is_a_named_refusal_with_the_gate_text(params, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hook, "evaluate", lambda payload: deny("PreToolUse", "Run r1 is already working it."))
    result = _dispatch(params)
    assert result["refused"] == "gate-refused" and "Run r1 is already working it." in result["reason"]


@pytest.mark.parametrize(
    "mutate",
    [
        {"ask_source": None},
        {"ask_source": "operator"},
        {"session_id": ""},
        {"caller": None},
        {"cwd": "relative/dir"},
        {"sizing": "state/sizings/x.yaml"},
    ],
)
def test_bad_request_is_refused_before_any_gate_runs(params, seen, mutate) -> None:
    result = _dispatch({**params, **mutate})
    assert result["refused"] == "bad-request" and result["reason"]
    assert seen == []


def test_unknown_param_is_refused_by_name(params, seen) -> None:
    result = _dispatch({**params, "bogus": 1})
    assert result["refused"] == "bad-request" and "bogus" in result["reason"]


def test_subagent_caller_reaches_the_hook_refusal(params) -> None:
    result = _dispatch({**params, "agent_id": "sub-1"})
    assert result["refused"] == "gate-refused"
    assert "subagent" in result["reason"].lower() and "cannot submit" in result["reason"].lower()


@pytest.mark.parametrize(
    "answer",
    [{}, {"hookSpecificOutput": {"permissionDecision": "allow"}}, {"hookSpecificOutput": {"updatedInput": {}}}],
)
def test_an_answer_without_a_run_is_never_an_empty_reply(params, monkeypatch: pytest.MonkeyPatch, answer) -> None:
    monkeypatch.setattr(hook, "evaluate", lambda payload: answer)
    result = _dispatch(params)
    assert result and result["refused"] == "no-run"


def test_gate_exception_is_a_named_refusal(params, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(payload):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(hook, "evaluate", boom)
    result = _dispatch(params)
    assert result["refused"] == "gate-refused" and "disk gone" in result["reason"]


def test_fire_hands_the_sha256_to_workflow_fire(params, script, seen, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(
        "coordinator_core.ops.workflow_fire.op._workflow_fire",
        lambda p, repo_root=None: calls.append(p) or {"fire_id": "f1"},
    )
    result = _dispatch({**params, "fire": True})
    assert calls == [{"script_path": str(script), "expected": result["sha256"], "notify_to": "sess-1", "executor": "headless", "backend": "codex", "caller_session_id": "sess-1", "run_id": "run-abc123"}]
    assert result["fire"] == {"fire_id": "f1"}


def test_codex_em_fire_runs_through_the_real_fire_authority(params, seen, monkeypatch: pytest.MonkeyPatch) -> None:
    import psutil

    from coordinator_core.ops.workflow_fire import authority, headless
    from coordinator_core.session import external_registry

    sid = "01a1d4b3-17b4-7621-84cd-aa8e05a1cecc"
    monkeypatch.setattr(authority, "caller_principal", lambda *a: authority.Principal(sid, "unknown", {}))
    monkeypatch.setattr(external_registry, "lookup", lambda s: {"session_id": s, "pid": 7})
    monkeypatch.setattr(psutil, "Process", lambda pid: type("P", (), {"cmdline": lambda self: ["node", "codex.js"]})())
    started = []
    monkeypatch.setattr(headless, "start", lambda *a, **k: started.append(k) or {"fire_id": "f1", "state": "running", "notify_to": sid})
    result = _dispatch({**params, "session_id": sid, "fire": True})
    assert "fire_error" not in result and result["fire"]["fire_id"] == "f1"
    assert started[0]["caller_session_id"] == sid


@pytest.mark.parametrize(
    "family,argv",
    [("codex", ["node", "codex.js"]), ("agy", ["agy"]), ("grok", ["grok"])],
)
def test_default_submit_fires_for_each_non_claude_family(params, seen, monkeypatch: pytest.MonkeyPatch, family, argv) -> None:
    import psutil

    from coordinator_core.ops.workflow_fire import authority, headless
    from coordinator_core.session import external_registry

    sid = "01a1d4b3-17b4-7621-84cd-aa8e05a1cecc"
    monkeypatch.setattr(authority, "caller_principal", lambda *a: authority.Principal(sid, "unknown", {}))
    monkeypatch.setattr(external_registry, "lookup", lambda s: {"session_id": s, "pid": 7})
    monkeypatch.setattr(psutil, "Process", lambda pid: type("P", (), {"cmdline": lambda self: argv})())
    started = []
    monkeypatch.setattr(headless, "start", lambda *a, **k: started.append(k) or {"fire_id": "f1", "state": "running", "notify_to": sid})
    assert "fire" not in params
    result = _dispatch({**params, "session_id": sid, "caller": f"{family}-em", "backend": family})
    assert "fire_error" not in result and result["fire"]["fire_id"] == "f1"
    assert started[0]["caller_session_id"] == sid


@pytest.mark.parametrize("family,argv", [("agy", ["agy"]), ("grok", ["grok"])])
def test_unverified_family_em_default_submit_names_the_refusal(params, seen, monkeypatch: pytest.MonkeyPatch, family, argv) -> None:
    from coordinator_core.ops.workflow_fire import authority, headless
    from coordinator_core.session import external_registry

    monkeypatch.setattr(authority, "caller_principal", lambda *a: authority.Principal("sess-1", "unknown", {}))
    monkeypatch.setattr(external_registry, "lookup", lambda s: None)
    monkeypatch.setattr(headless, "start", lambda *a, **k: pytest.fail("fired an unverified caller"))
    result = _dispatch({**params, "backend": family})
    assert result["run_id"] == "run-abc123" and "verified EM" in result["fire_error"]


def test_fire_false_mints_without_firing(params, seen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "coordinator_core.ops.workflow_fire.op._workflow_fire",
        lambda p, repo_root=None: pytest.fail("fired a fire:false submission"),
    )
    result = _dispatch({**params, "fire": False})
    assert result["run_id"] == "run-abc123" and "fire" not in result and "fire_error" not in result


@pytest.mark.parametrize("fire", ["false", "", 0, 1, None])
def test_non_bool_fire_is_refused_before_any_gate_runs(params, seen, fire) -> None:
    result = _dispatch({**params, "fire": fire})
    assert result["refused"] == "bad-request" and "fire" in result["reason"]
    assert seen == []


def test_returned_fire_refusal_is_copied_to_top_level_fire_error(params, seen, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "coordinator_core.ops.workflow_fire.op._workflow_fire",
        lambda p, repo_root=None: {"refused": "lease-held", "reason": "another fire holds the lease"},
    )
    result = _dispatch(params)
    assert result["run_id"] == "run-abc123"
    assert result["fire_error"] == "lease-held: another fire holds the lease"


def test_family_em_caller_without_a_verified_session_is_refused_at_fire(params, seen, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.ops.workflow_fire import authority, headless
    from coordinator_core.session import external_registry

    monkeypatch.setattr(authority, "caller_principal", lambda *a: authority.Principal("sess-1", "unknown", {}))
    monkeypatch.setattr(external_registry, "lookup", lambda s: None)
    monkeypatch.setattr(headless, "start", lambda *a, **k: pytest.fail("fired an unverified caller"))
    result = _dispatch({**params, "fire": True})
    assert result["run_id"] == "run-abc123" and "verified EM" in result["fire_error"]


@pytest.mark.parametrize("backend", ["claude", "grok", "agy"])
def test_fire_carries_an_opt_in_backend_family(params, seen, monkeypatch: pytest.MonkeyPatch, backend) -> None:
    calls = []
    monkeypatch.setattr(
        "coordinator_core.ops.workflow_fire.op._workflow_fire",
        lambda p, repo_root=None: calls.append(p) or {"fire_id": "f1"},
    )
    _dispatch({**params, "fire": True, "backend": backend})
    assert [c["backend"] for c in calls] == [backend]


@pytest.mark.parametrize("backend", ["gemini", "", None])
def test_unknown_backend_is_refused_naming_the_set(params, seen, backend) -> None:
    result = _dispatch({**params, "backend": backend})
    assert result["refused"] == "bad-request" and "codex|claude|grok|agy" in result["reason"]
    assert seen == []


def test_fire_uses_explicit_notification_peer(params, seen, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(
        "coordinator_core.ops.workflow_fire.op._workflow_fire",
        lambda p, repo_root=None: calls.append(p) or {"fire_id": "f1"},
    )
    _dispatch({**params, "fire": True, "notify_to": "other-peer"})
    assert calls[0]["notify_to"] == "other-peer"


@pytest.mark.parametrize("target", ["", "  ", 17, []])
def test_bad_notification_target_never_submits(params, seen, target) -> None:
    result = _dispatch({**params, "fire": True, "notify_to": target})
    assert result["refused"] == "bad-request"
    assert seen == []


def test_failed_fire_keeps_the_run_handle(params, seen, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(p, repo_root=None):
        raise ValueError("drift")

    monkeypatch.setattr("coordinator_core.ops.workflow_fire.op._workflow_fire", refuse)
    result = _dispatch({**params, "fire": True})
    assert result["run_id"] == "run-abc123" and result["fire_error"] == "ValueError: drift"


def test_refusal_never_fires(params, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hook, "evaluate", lambda payload: deny("PreToolUse", "no"))
    monkeypatch.setattr(
        "coordinator_core.ops.workflow_fire.op._workflow_fire",
        lambda p, repo_root=None: pytest.fail("fired a refused submission"),
    )
    assert _dispatch({**params, "fire": True})["refused"] == "gate-refused"


def test_op_runs_the_gates_through_the_shared_function(params, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []
    monkeypatch.setattr(gates, "submit_ask", lambda **kw: seen.append(kw) or {"refused": "no-run", "reason": "x"})
    assert op._engine_ask_submit(params)["refused"] == "no-run"
    assert seen[0]["caller"] == "codex-em" and seen[0]["ask_source"] == "pm"


@pytest.fixture
def launcher_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """A granted headless EM: returns the env a launcher-spawned codex EM carries."""
    from coordinator_core.ops.workflow_fire import headless_em_grant as grant

    monkeypatch.setenv(grant.SETTINGS_HOME_KEY, str(tmp_path / "settings"))
    token = grant.write_headless_em_grant("sess-1", "codex")
    env = {"COORDINATOR_SESSION_ID": "sess-1", grant.HEADLESS_EM_TOKEN_ENV: token}
    monkeypatch.setattr("coordinator_core.session.caller_env.effective_caller_env", lambda: env)
    return env


def _no_args(tmp_path) -> dict[str, str]:
    return {"ask": "build the thing", "ask_source": "pm"}


def test_empty_params_load_the_canonical_file_under_the_repo(params, tmp_path: Path, seen) -> None:
    canonical = tmp_path / ".coordinator-local" / "em-submit.json"
    canonical.parent.mkdir()
    canonical.write_text(json.dumps({**params, "fire": False}), encoding="utf-8")
    result = op._engine_ask_submit({}, repo_root=tmp_path)
    assert result["run_id"] == "run-abc123"
    assert seen[0]["tool_input"]["args"] == {"ask": "build the thing", "source": "pm"}


@pytest.mark.parametrize("body", [None, "[1]", "not json"])
def test_empty_params_with_a_missing_or_non_object_file_name_the_path(tmp_path: Path, seen, body) -> None:
    if body is not None:
        (tmp_path / ".coordinator-local").mkdir()
        (tmp_path / ".coordinator-local" / "em-submit.json").write_text(body, encoding="utf-8")
    result = op._engine_ask_submit({}, repo_root=tmp_path)
    assert result["refused"] == "bad-request" and ".coordinator-local/em-submit.json" in result["reason"]
    assert seen == []


def test_missing_canonical_file_is_scaffolded_and_the_next_call_reads_it(params, tmp_path: Path, seen) -> None:
    canonical = tmp_path / ".coordinator-local" / "em-submit.json"
    first = op._engine_ask_submit({}, repo_root=tmp_path)
    assert first["refused"] == "bad-request" and "No ask staged" in first["reason"] and "unreadable" not in first["reason"]
    assert json.loads(canonical.read_text(encoding="utf-8")) == {"ask": "", "ask_source": "pm"}
    assert seen == []
    canonical.write_text(json.dumps({**params, "fire": False}), encoding="utf-8")
    assert op._engine_ask_submit({}, repo_root=tmp_path)["run_id"] == "run-abc123"


def test_existing_canonical_file_is_never_overwritten_by_the_scaffold(tmp_path: Path, seen) -> None:
    canonical = tmp_path / ".coordinator-local" / "em-submit.json"
    canonical.parent.mkdir()
    canonical.write_text("[1]", encoding="utf-8")
    result = op._engine_ask_submit({}, repo_root=tmp_path)
    assert "must hold a JSON object" in result["reason"] and canonical.read_text(encoding="utf-8") == "[1]"


def test_explicit_params_ignore_the_canonical_file(params, tmp_path: Path, seen) -> None:
    (tmp_path / ".coordinator-local").mkdir()
    (tmp_path / ".coordinator-local" / "em-submit.json").write_text('{"ask": "other"}', encoding="utf-8")
    op._engine_ask_submit({**params, "fire": False}, repo_root=tmp_path)
    assert seen[0]["tool_input"]["args"]["ask"] == "build the thing"


def test_grant_bound_env_supplies_identity_when_params_omit_it(tmp_path: Path, launcher_env, seen) -> None:
    result = op._engine_ask_submit(_no_args(tmp_path), repo_root=tmp_path)
    assert "refused" not in result
    assert seen[0]["session_id"] == "sess-1" and seen[0]["cwd"] == str(tmp_path)


def test_grant_bound_env_names_the_caller_by_granted_family(tmp_path: Path, launcher_env, monkeypatch: pytest.MonkeyPatch) -> None:
    got = []
    monkeypatch.setattr(gates, "submit_ask", lambda **kw: got.append(kw) or {"refused": "no-run", "reason": "x"})
    op._engine_ask_submit(_no_args(tmp_path), repo_root=tmp_path)
    assert got[0]["caller"] == "codex-em" and got[0]["session_id"] == "sess-1"


@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_env_session_without_a_binding_token_is_refused(tmp_path: Path, launcher_env, seen, token) -> None:
    from coordinator_core.ops.workflow_fire.headless_em_grant import HEADLESS_EM_TOKEN_ENV

    if token is None:
        del launcher_env[HEADLESS_EM_TOKEN_ENV]
    else:
        launcher_env[HEADLESS_EM_TOKEN_ENV] = token
    result = op._engine_ask_submit(_no_args(tmp_path), repo_root=tmp_path)
    assert result["refused"] == "bad-request" and "session_id is required" in result["reason"]
    assert seen == []


def test_env_session_the_grant_binds_to_another_session_is_refused(tmp_path: Path, launcher_env, seen) -> None:
    launcher_env["COORDINATOR_SESSION_ID"] = "sess-other"
    result = op._engine_ask_submit(_no_args(tmp_path), repo_root=tmp_path)
    assert result["refused"] == "bad-request" and seen == []


def test_explicit_session_id_is_never_replaced_by_the_env(tmp_path: Path, launcher_env, monkeypatch: pytest.MonkeyPatch) -> None:
    got = []
    monkeypatch.setattr(gates, "submit_ask", lambda **kw: got.append(kw) or {"refused": "no-run", "reason": "x"})
    op._engine_ask_submit({**_no_args(tmp_path), "session_id": "sess-explicit", "caller": "me", "cwd": str(tmp_path)})
    assert (got[0]["session_id"], got[0]["caller"]) == ("sess-explicit", "me")


@pytest.mark.parametrize("answer", [None, {}, {"run_id": "r1"}, {"script_path": "x"}])
def test_incomplete_submit_result_is_not_success(params, monkeypatch: pytest.MonkeyPatch, answer) -> None:
    monkeypatch.setattr(gates, "submit_ask", lambda **kw: answer)
    monkeypatch.setattr(
        "coordinator_core.ops.workflow_fire.op._workflow_fire",
        lambda *a, **k: pytest.fail("fired an incomplete submission"),
    )
    assert op._engine_ask_submit({**params, "fire": True})["refused"] == "no-run"


@pytest.mark.parametrize(("actual_sid", "role", "supplied"), [
    ("thread", "em", True), ("different", "em", False),
    ("thread", "unknown", False), ("thread", "worker", False),
])
def test_native_codex_submit_identity(tmp_path, monkeypatch, actual_sid, role, supplied):
    from coordinator_core.ops.workflow_fire import authority
    from coordinator_core.session.caller_env import bind_caller_env

    env = {"CODEX_THREAD_ID": "thread"}
    monkeypatch.setattr(authority, "caller_principal",
                        lambda cwd: authority.Principal(actual_sid, role, env))
    with bind_caller_env(env):
        params = op._with_bound_identity({}, tmp_path)
    assert params == ({"session_id": "thread", "caller": "codex-em", "cwd": str(tmp_path)} if supplied else {})
