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
    digest = hashlib.sha256(script.read_bytes()).hexdigest()
    assert result == {
        "fire_params": {"script_path": str(script), "expected": digest, "notify_to": "sess-1",
                        "executor": "headless", "backend": "codex", "caller_session_id": "sess-1",
                        "run_id": "run-abc123"},
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


def test_fresh_reaches_the_hook_so_a_cancelled_predecessor_can_be_refired(params, seen) -> None:
    del params["ask"], params["ask_source"]
    params.update(sizing="state/sizings/x.yaml", fresh=True, fire=False)
    _dispatch(params)
    assert seen[0]["tool_input"]["args"] == {"sizing": "state/sizings/x.yaml", "fresh": True}


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


def test_default_fire_preserves_native_run_binding(params, script, monkeypatch):
    args = {"repoRoot": params["cwd"], "runToken": "a" * 32}
    envelope = _allow(script)
    envelope["hookSpecificOutput"]["updatedInput"]["args"] = args
    monkeypatch.setattr(hook, "evaluate", lambda payload: envelope)
    calls = []
    monkeypatch.setattr("coordinator_core.ops.workflow_fire.op._workflow_fire",
                        lambda p, repo_root=None: calls.append((p, repo_root)) or {"fire_id": "native"})
    result = _dispatch(params)
    fire, root = calls[0]
    assert "fire" not in params and "executor" not in params and "backend" not in params
    assert fire["args"] == args and fire["run_id"] == result["run_id"]
    assert fire["expected"] == result["sha256"]
    assert fire["executor"] == "headless" and fire["backend"] == "codex"
    assert root == Path(params["cwd"])


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
    continuation = result["fire_params"]
    assert continuation["executor"] == "headless"
    assert continuation["backend"] == "codex"
    assert continuation["caller_session_id"] == params["session_id"]
    assert continuation["expected"] == result["sha256"]


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


@pytest.mark.parametrize("caller,expected", [
    ("grok", "grok"), ("grok-em", "grok"), ("agy", "agy"), ("agy-em", "agy"),
    ("claude", "claude"), ("claude-em", "claude"), ("codex-em", "codex"),
])
def test_omitted_backend_follows_the_callers_family(params, seen, monkeypatch: pytest.MonkeyPatch, caller, expected) -> None:
    calls = []
    monkeypatch.setattr(
        "coordinator_core.ops.workflow_fire.op._workflow_fire",
        lambda p, repo_root=None: calls.append(p) or {"fire_id": "f1"},
    )
    assert "backend" not in params
    _dispatch({**params, "caller": caller})
    assert [c["backend"] for c in calls] == [expected]


def test_explicit_backend_overrides_the_callers_family(params, seen, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(
        "coordinator_core.ops.workflow_fire.op._workflow_fire",
        lambda p, repo_root=None: calls.append(p) or {"fire_id": "f1"},
    )
    _dispatch({**params, "caller": "grok-em", "backend": "claude"})
    assert [c["backend"] for c in calls] == ["claude"]


@pytest.mark.parametrize("caller", ["gemini-em", "operator", "", "codex-emx"])
def test_unknown_caller_without_backend_is_refused_never_codex(params, seen, monkeypatch: pytest.MonkeyPatch, caller) -> None:
    monkeypatch.setattr(
        "coordinator_core.ops.workflow_fire.op._workflow_fire",
        lambda p, repo_root=None: pytest.fail("fired without a resolvable backend"),
    )
    result = _dispatch({**params, "caller": caller})
    assert result["refused"] == "bad-request" and "backend" in result["reason"]
    assert seen == []


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


@pytest.mark.parametrize("fire_reply", [None, {}, {"fire_id": "  "}])
def test_requested_fire_requires_a_waitable_handle(params, seen, monkeypatch, fire_reply):
    monkeypatch.setattr("coordinator_core.ops.workflow_fire.op._workflow_fire",
                        lambda *a, **kw: fire_reply)
    result = _dispatch(params)
    assert result["run_id"] == "run-abc123"
    assert "fire_id" in result["fire_error"]


def test_failed_fire_keeps_the_run_handle(params, seen, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(p, repo_root=None):
        raise ValueError("drift")

    monkeypatch.setattr("coordinator_core.ops.workflow_fire.op._workflow_fire", refuse)
    result = _dispatch({**params, "fire": True})
    assert result["run_id"] == "run-abc123" and result["fire_error"] == "ValueError: drift"


def test_indeterminate_fire_preserves_the_warm_reconciliation_handle(params, seen, monkeypatch) -> None:
    from coordinator_core.warm.sandboxed_spawn import SpawnIndeterminate

    error = {
        "code": -32004,
        "message": "warm workflow.fire timed out",
        "data": {"dispatch_key": "dispatch-1", "reconcile": "dispatch.request_status"},
    }

    def indeterminate(p, repo_root=None):
        raise SpawnIndeterminate(p["run_id"], error)

    monkeypatch.setattr("coordinator_core.ops.workflow_fire.op._workflow_fire", indeterminate)
    result = _dispatch({**params, "fire": True})
    assert result["run_id"] == "run-abc123"
    assert result["fire"] == {"state": "indeterminate", "run_id": "run-abc123", "error": error}
    assert "reconcile before retrying" in result["fire_error"]


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
    assert "same command again" in first["reason"]
    assert json.loads(canonical.read_text(encoding="utf-8")) == {
        "ask": "", "ask_source": "pm", "session_id": "sess-1", "cwd": str(tmp_path)}
    assert seen == []
    canonical.write_text(json.dumps({**params, "fire": False}), encoding="utf-8")
    assert op._engine_ask_submit({}, repo_root=tmp_path)["run_id"] == "run-abc123"


def test_scaffold_stages_the_callers_session_and_a_staged_ask_submits_cold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seen) -> None:
    from coordinator_core.ops.workflow_fire import authority

    monkeypatch.setattr(authority, "caller_principal", lambda *a: authority.Principal("thread-9", "unknown", {"CODEX_THREAD_ID": "thread-9"}))
    canonical = tmp_path / ".coordinator-local" / "em-submit.json"
    op._engine_ask_submit({}, repo_root=tmp_path)
    staged = json.loads(canonical.read_text(encoding="utf-8"))
    assert staged["session_id"] == "thread-9" and staged["caller"] == "codex-em"
    canonical.write_text(json.dumps({**staged, "ask": "build the thing", "fire": False}), encoding="utf-8")
    got = []
    monkeypatch.setattr(gates, "submit_ask", lambda **kw: got.append(kw) or {"refused": "no-run", "reason": "x"})
    op._engine_ask_submit({}, repo_root=tmp_path)
    assert (got[0]["session_id"], got[0]["caller"], got[0]["cwd"]) == ("thread-9", "codex-em", str(tmp_path))


def test_scaffold_stages_no_session_when_the_caller_resolves_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.ops.workflow_fire import authority

    monkeypatch.setattr(authority, "caller_principal", lambda *a: authority.Principal("", "unknown", {}))
    op._engine_ask_submit({}, repo_root=tmp_path)
    staged = json.loads((tmp_path / ".coordinator-local" / "em-submit.json").read_text(encoding="utf-8"))
    assert staged == {"ask": "", "ask_source": "pm"}


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
    op._engine_ask_submit({**_no_args(tmp_path), "session_id": "sess-explicit", "caller": "me", "backend": "codex", "cwd": str(tmp_path)})
    assert (got[0]["session_id"], got[0]["caller"]) == ("sess-explicit", "me")


def test_documented_payload_without_cwd_defaults_to_the_repo_root(tmp_path: Path, seen) -> None:
    documented = {"ask": "build the thing", "ask_source": "pm", "session_id": "sess-1", "caller": "codex-em", "fire": False}
    for root in (tmp_path, tmp_path / ".git"):
        seen.clear()
        result = op._engine_ask_submit(dict(documented), repo_root=root)
        assert "refused" not in result, result
        assert seen[0]["cwd"] == str(tmp_path)


def test_a_missing_cwd_is_still_refused_when_the_op_has_no_repo_root(seen) -> None:
    result = op._engine_ask_submit(
        {"ask": "x", "ask_source": "pm", "session_id": "sess-1", "caller": "codex-em", "fire": False})
    assert result["refused"] == "bad-request" and "cwd" in result["reason"] and seen == []


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


def test_submit_correlates_launcher_and_live_native_em(tmp_path, launcher_env, seen, monkeypatch):
    import os
    import uuid

    import psutil
    from coordinator_core.ops.workflow_fire import headless_em_grant as grant
    from coordinator_core.session import external_registry

    native = str(uuid.uuid4())
    launcher_env["CODEX_THREAD_ID"] = native
    process = psutil.Process(os.getpid())
    observed = dict(native_session_id=native, launcher_session_id="sess-1", pid=process.pid,
                    start_epoch=int(process.create_time()), cwd=str(tmp_path.resolve()))
    row = dict(session_id=native, pid=process.pid, start_epoch=observed["start_epoch"],
               cwd=str(tmp_path), role="em", provider="codex")
    monkeypatch.setattr(external_registry, "lookup", lambda thread: row if thread == native else None)
    assert grant.observe_codex_registration(launcher_env, native, process.pid, str(tmp_path))

    params = {**_no_args(tmp_path), "fire": False}
    assert op._engine_ask_submit(params, tmp_path)["launch_observation"] == observed
    row["start_epoch"] -= 1
    assert "launch_observation" not in op._engine_ask_submit(params, tmp_path)


@pytest.mark.parametrize("changed", [None, "run_id", "script_sha256"])
def test_default_fire_preserves_handle_and_reports_conflicting_identity(params, seen, monkeypatch, changed):
    from coordinator_core.invoke.__main__ import _dispatch_argv

    from coordinator_core.testing.tmp_git_repo import init_tmp_git_repo

    init_tmp_git_repo(Path(params["cwd"]))
    calls = []

    def fire(request, repo_root):
        receipt = {"fire_id": "b" * 32, "run_id": request["run_id"], "script_sha256": request["expected"]}
        if changed:
            receipt[changed] = "other"
        calls.append(receipt)
        return receipt

    monkeypatch.setattr("coordinator_core.ops.workflow_fire.op._workflow_fire", fire)
    stdout, _, code = _dispatch_argv([OP, json.dumps(params)], params["cwd"], allow_warm=False)
    assert "result" in json.loads(stdout), stdout
    result = json.loads(stdout)["result"]
    assert calls == [result["fire"]]
    assert result["run_id"] == "run-abc123"
    if changed:
        assert changed in result["fire_error"]
    else:
        assert "fire_error" not in result
    assert code == int(bool(changed))
