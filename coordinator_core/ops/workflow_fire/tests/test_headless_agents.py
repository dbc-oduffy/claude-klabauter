"""Real scratch CLI processes exercise the headless agent wire, not a Popen mock."""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

from coordinator_core.codex_runner import review
from coordinator_core.codex_runner import workflow as agents
from coordinator_core.ops.workflow_fire import headless_agents

_REAL_ADMIT_BEFORE_SPAWN = agents.capacity.admit_before_spawn


SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}},
          "required": ["ok"], "additionalProperties": False}


@pytest.fixture(autouse=True)
def admission(monkeypatch, tmp_path):
    """Every CLI probe stays hermetic: no real box ledger or Group EM messages."""
    events = []
    (tmp_path / '.git').mkdir(exist_ok=True)
    host = SimpleNamespace(admission_file=lambda: "unused-test-ledger",
                           read_recorded_peak=lambda *_a: None)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "01a1b63b-acfb-78d1-86f2-a74ad6ca4572")
    monkeypatch.setattr(agents.capacity, "_load_box_memory_host", lambda: host)
    def admit(key, peak, **kwargs):
        events.append(("reserve", {"key": key, "peak": peak, **kwargs}))
        return SimpleNamespace(admitted=True, peak_gib=peak, reason="test admitted")
    monkeypatch.setattr(agents.capacity, "admit_before_spawn", admit)
    monkeypatch.setattr(agents.capacity, "release_spawn", lambda *a, **kw: events.append(("release", a)))
    monkeypatch.setattr(agents.admission_post, "post", lambda phase, *a, **kw:
                        events.append((phase, {"session_id": a[0], **kw})) or {"delivered": True})
    monkeypatch.setattr(agents.slot_grant, "check_slot_grant", lambda *a, **kw: (False, None))
    monkeypatch.setattr(agents.slot_wait, "request_and_wait", lambda *a, **kw: {"admitted": False, "reason": "test ungranted"})
    return events, host


def fake_cli(tmp_path, monkeypatch, *, result='{"ok":true}', exit_code=0, delay=0, events=None):
    script = tmp_path / "cli.py"
    script.write_text(
        "import sys,json,os,time\n"
        f"time.sleep({delay!r})\n"
        "args=sys.argv[1:]\n"
        "prompt=sys.stdin.read()\n"
        f"open({str(tmp_path / 'capture.json')!r},'w',encoding='utf-8').write(json.dumps({{'argv':args,'prompt':prompt,'env':dict(os.environ),'cwd':os.getcwd()}}))\n"
        f"open(args[args.index('-o')+1],'w',encoding='utf-8').write({result!r})\n"
        f"events={events!r}\n"
        "for event in (events if events is not None else [{'type':'turn.completed'}]): print(json.dumps(event))\n"
        f"sys.exit({exit_code})\n", encoding="utf-8",
    )
    monkeypatch.setattr(agents, "resolve_codex", lambda **_kw: [sys.executable, str(script)])
    monkeypatch.setattr(agents, "preflight_auth", lambda **_kw: None)
    return tmp_path / "capture.json"


def test_codex_schema_prompt_label_and_environment(tmp_path, monkeypatch):
    capture = fake_cli(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "never-forward")
    monkeypatch.setenv("CODEX_API_KEY", "never-forward")
    monkeypatch.setenv("CODEX_RUNNER_ALLOW_API_KEY", "1")
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-id-never-forward")
    pids = []
    assert agents.run_agent("Do a thing: café", {"label": "size", "schema": SCHEMA, "model": "sonnet"},
        repo_root=tmp_path, artifact_dir=tmp_path / "agent", on_spawn=pids.append) == {"ok": True}
    wire = json.loads(capture.read_text())
    assert wire["prompt"].endswith("Do a thing: café")
    assert wire["argv"][0] == "exec"
    assert wire["argv"][wire["argv"].index("--sandbox") + 1] == "workspace-write"
    assert not any("default_permissions" in arg or "permissions.coordinator_workflow" in arg for arg in wire["argv"])
    assert "--output-schema" in wire["argv"]
    assert wire["argv"][wire["argv"].index("--model") + 1] == agents.model_for_tier("codex", "sonnet")
    assert wire["argv"][-1] == "-"
    assert all(key not in wire["env"] for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_RUNNER_ALLOW_API_KEY", "CODEX_THREAD_ID"))
    assert wire["env"]["COORDINATOR_PARENT_SESSION_ID"] == "01a1b63b-acfb-78d1-86f2-a74ad6ca4572"
    assert wire["env"]["COORDINATOR_WARM"] == "0"
    assert wire["env"]["COORDINATOR_ENGINE_ROOT"] == str(Path(__import__("coordinator_core").__file__).resolve().parent.parent)
    assert wire["env"]["COORDINATOR_AGENT_PROVIDER"] == "codex"
    assert wire["env"]["COORDINATOR_AGENT_VENDOR"] == "codex"
    assert wire["env"]["COORDINATOR_AGENT_ROLE"] == "workflow-agent"
    assert wire["env"]["COORDINATOR_WORKFLOW_AGENT_LABEL"] == "size"
    assert "COORDINATOR_SESSION_ID" not in wire["env"]
    assert pids and (tmp_path / "agent/stdout.jsonl").is_file()
    assert json.loads((tmp_path / "agent/process.json").read_text())["label"] == "size"


def test_invalid_worker_schema_fails(tmp_path, monkeypatch):
    fake_cli(tmp_path, monkeypatch, result='{"ok":"false"}')
    with pytest.raises(agents.AgentFailed, match="schema-valid"):
        agents.run_agent("x", {"schema": SCHEMA}, repo_root=tmp_path, artifact_dir=tmp_path / "agent")


def test_open_fire_args_preserves_dynamic_map_without_native_schema(tmp_path, monkeypatch):
    payload = {"fire_args": {"run_id": "run-1", "path": "state/run.mjs", "nested": {"flag": True}}}
    capture = fake_cli(tmp_path, monkeypatch, result=json.dumps(payload))
    schema = {"type": "object", "required": ["fire_args"], "properties": {"fire_args": {"type": "object"}}}
    original = json.loads(json.dumps(schema))
    assert agents.run_agent("emit", {"schema": schema}, repo_root=tmp_path, artifact_dir=tmp_path / "agent") == payload
    wire = json.loads(capture.read_text())
    assert "--output-schema" not in wire["argv"]
    assert "exactly one raw JSON value" in wire["prompt"]
    assert json.loads(wire["prompt"].split("field semantics; do not replace argument objects with empty objects:\n", 1)[1]) == schema
    assert schema == original
    assert json.loads((tmp_path / "agent/schema.json").read_text()) == original


@pytest.mark.parametrize("payload", [{"ok": True}, {"ok": True, "optional": None}, {"ok": True, "optional": "value"}])
def test_optional_nullable_fields_keep_original_semantics(tmp_path, monkeypatch, payload):
    capture = fake_cli(tmp_path, monkeypatch, result=json.dumps(payload))
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}, "optional": {"type": ["string", "null"]}},
              "required": ["ok"], "additionalProperties": False}
    assert agents.run_agent("x", {"schema": schema}, repo_root=tmp_path, artifact_dir=tmp_path / "agent") == payload
    assert "--output-schema" not in json.loads(capture.read_text())["argv"]


def test_closed_nullable_required_schema_stays_native(tmp_path, monkeypatch):
    capture = fake_cli(tmp_path, monkeypatch, result='{"value":null}')
    schema = {"type": "object", "properties": {"value": {"type": ["string", "null"]}},
              "required": ["value"], "additionalProperties": False}
    assert agents.run_agent("x", {"schema": schema}, repo_root=tmp_path, artifact_dir=tmp_path / "agent") == {"value": None}
    assert "--output-schema" in json.loads(capture.read_text())["argv"]


def test_open_map_fallback_still_rejects_invalid_original_schema_result(tmp_path, monkeypatch):
    fake_cli(tmp_path, monkeypatch, result='{"fire_args":"wrong"}')
    schema = {"type": "object", "required": ["fire_args"], "properties": {"fire_args": {"type": "object"}}}
    with pytest.raises(agents.AgentFailed, match="schema-valid"):
        agents.run_agent("x", {"schema": schema}, repo_root=tmp_path, artifact_dir=tmp_path / "agent")


def test_nonzero_exit_fails_even_with_valid_result(tmp_path, monkeypatch):
    fake_cli(tmp_path, monkeypatch, exit_code=7)
    with pytest.raises(agents.AgentFailed, match="exited 7"):
        agents.run_agent("x", {"schema": SCHEMA}, repo_root=tmp_path, artifact_dir=tmp_path / "agent")


def test_timeout_kills_worker(tmp_path, monkeypatch):
    fake_cli(tmp_path, monkeypatch, delay=10)
    with pytest.raises(agents.AgentFailed, match="timed out"):
        agents.run_agent("x", {}, repo_root=tmp_path, artifact_dir=tmp_path / "agent", timeout_s=.1)
    process = json.loads((tmp_path / "agent/process.json").read_text())
    assert process["pid"] > 0 and process["exit_code"] != 0
    assert process["native_thread_id"] == "unknown"


def test_persona_loaded_from_content_root(tmp_path, monkeypatch):
    from coordinator_core import content_root
    capture = fake_cli(tmp_path, monkeypatch)
    folder = tmp_path / "content/agents"
    folder.mkdir(parents=True)
    (folder / "executor.md").write_text("You are the executor. Write only the declared files.")
    monkeypatch.setattr(content_root, "read_content_root", lambda: str(folder.parent))
    agents.run_agent("Implement C1", {"agentType": "coordinator:executor"}, repo_root=tmp_path, artifact_dir=tmp_path / "agent")
    assert "Write only the declared files" in json.loads(capture.read_text())["prompt"]


def test_unknown_persona_fails_before_spawn(tmp_path, monkeypatch):
    with pytest.raises(agents.AgentFailed, match="unsupported agentType"):
        agents.run_agent("x", {"agentType": "../../escape"}, repo_root=tmp_path, artifact_dir=tmp_path / "agent")


def test_api_key_login_opt_in_not_admitted(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "auth.json").write_text('{"auth_mode":"apikey"}')
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("CODEX_RUNNER_ALLOW_API_KEY", "1")
    from coordinator_core.codex_runner.resolve import ApiKeyRefused
    with pytest.raises(ApiKeyRefused):
        agents.run_agent("x", {}, repo_root=tmp_path, artifact_dir=tmp_path / "agent")


def test_claude_alternate_structured_output(tmp_path, monkeypatch):
    # Exercise command construction and structured envelope with a tiny CLI process.
    script = tmp_path / "fake-claude.py"
    script.write_text("import json,sys\nsys.stdin.read()\nprint(json.dumps({'is_error':False,'structured_output':{'ok':True}}))\n")
    original = agents.subprocess.Popen
    monkeypatch.setattr(agents.shutil, "which", lambda *_a, **_k: "claude")
    def launch(argv, **kwargs):
        assert argv[:4] == ["claude", "-p", "--output-format", "json"]
        assert "--json-schema" in argv
        return original([sys.executable, str(script)], **kwargs)
    monkeypatch.setattr(agents.subprocess, "Popen", launch)
    assert agents.run_agent("x", {"schema": SCHEMA}, repo_root=tmp_path, artifact_dir=tmp_path / "agent", backend="claude") == {"ok": True}


def test_admission_reservation_spans_native_child_and_accounts_actual_pid(tmp_path, monkeypatch, admission):
    events, _ = admission
    fake_cli(tmp_path, monkeypatch)
    native_pids = []
    agents.run_agent("x", {}, repo_root=tmp_path, artifact_dir=tmp_path / "agent", on_spawn=native_pids.append,
                     parent_session_id="explicit-real-parent")
    assert [name for name, _ in events] == ["reserve", "START", "END", "release"]
    reserve = events[0][1]
    start = events[1][1]
    assert reserve["register_pid"] == os.getpid()
    assert reserve["session_id"] == start["session_id"] == "explicit-real-parent"
    assert start["pids"] == native_pids and native_pids[0] != os.getpid()
    assert start["run"] == reserve["key"]
    assert events[-1][1] == (reserve["key"], os.getpid())


def test_recorded_three_gib_peak_requests_and_rechecks_real_slot(tmp_path, monkeypatch, admission):
    events, host = admission
    fake_cli(tmp_path, monkeypatch)
    host.read_recorded_peak = lambda _path, key: 3.0 if key == "codex-workflow" else None
    grant = {"live": False}
    checks = []
    def check(sid, run, **kwargs):
        checks.append((sid, run, kwargs))
        return grant["live"], None
    def wait(sid, run, peak, **kwargs):
        events.append(("WAIT", {"session_id": sid, "run": run, "peak": peak}))
        grant["live"] = True
        return {"admitted": True, "reason": "granted"}
    monkeypatch.setattr(agents.slot_grant, "check_slot_grant", check)
    monkeypatch.setattr(agents.slot_wait, "request_and_wait", wait)
    agents.run_agent("x", {}, repo_root=tmp_path, artifact_dir=tmp_path / "agent")
    assert events[0][1]["peak"] == 3
    assert [name for name, _ in events] == ["reserve", "WAIT", "START", "END", "release"]
    assert events[1][1]["peak"] == events[2][1]["declared_peak_mb"] == 3072
    assert len(checks) == 2 and all(call[2]["peak_mb"] == 3072 for call in checks)


def test_ungranted_large_peak_never_spawns_and_releases(tmp_path, monkeypatch, admission):
    events, _ = admission
    capture = fake_cli(tmp_path, monkeypatch)
    with pytest.raises(agents.AgentFailed, match="slot admission refused"):
        agents.run_agent("x", {"declaredPeakMb": 4096}, repo_root=tmp_path, artifact_dir=tmp_path / "agent")
    assert not capture.exists()
    assert [name for name, _ in events] == ["reserve", "release"]


def test_reservation_write_failure_is_fail_closed(tmp_path, monkeypatch, admission):
    from dataclasses import dataclass
    # Call the genuine checked reservation helper with a filesystem-failing host.
    @dataclass
    class Memory:
        avail_phys: int = 32 * 1024 ** 3
        commit_used: int = 0
    def write_failed(*_args):
        raise OSError("ledger write denied")
    host = SimpleNamespace(
        GIB=1024**3, admission_file=lambda: "unused-test-ledger",
        read_recorded_peak=lambda *_a: None, read_memory_status=Memory,
        reserved_bytes=lambda *_a: 0,
        admit=lambda *_a: SimpleNamespace(admit=True), register_command=write_failed,
    )
    monkeypatch.setattr(agents.capacity, "_load_box_memory_host", lambda: host)
    monkeypatch.setattr(agents.capacity, "admit_before_spawn", _REAL_ADMIT_BEFORE_SPAWN)
    capture = fake_cli(tmp_path, monkeypatch)
    with pytest.raises(agents.AgentFailed, match="reservation write failed"):
        agents.run_agent("x", {}, repo_root=tmp_path, artifact_dir=tmp_path / "agent")
    assert not capture.exists()


def test_start_delivery_failure_reaps_child_and_releases(tmp_path, monkeypatch, admission):
    events, _ = admission
    fake_cli(tmp_path, monkeypatch, delay=10)
    def post(phase, sid, **kwargs):
        events.append((phase, kwargs))
        return {"delivered": phase != "START", "reason": "short_write"}
    monkeypatch.setattr(agents.admission_post, "post", post)
    with pytest.raises(agents.AgentFailed, match="START admission delivery failed"):
        agents.run_agent("x", {}, repo_root=tmp_path, artifact_dir=tmp_path / "agent")
    assert [name for name, _ in events] == ["reserve", "START", "END", "release"]


def test_parallel_agents_use_distinct_reservation_keys(tmp_path, monkeypatch, admission):
    events, _ = admission
    fake_cli(tmp_path, monkeypatch)
    for index in range(2):
        agents.run_agent("x", {}, repo_root=tmp_path, artifact_dir=tmp_path / f"agent-{index}")
    reserved = [data["key"] for name, data in events if name == "reserve"]
    assert len(set(reserved)) == 2


def test_missing_parent_identity_fails_before_spawn(tmp_path, monkeypatch):
    capture = fake_cli(tmp_path, monkeypatch)
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    with pytest.raises(agents.AgentFailed, match="real parent session ID"):
        agents.run_agent("x", {}, repo_root=tmp_path, artifact_dir=tmp_path / "agent")
    assert not capture.exists()


def test_late_slot_grant_cannot_spawn_after_agent_deadline(tmp_path, monkeypatch, admission):
    events, _ = admission
    clock = [100.0]
    monkeypatch.setattr(agents, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    capture = fake_cli(tmp_path, monkeypatch)
    granted = [False]
    monkeypatch.setattr(agents.slot_grant, "check_slot_grant", lambda *a, **kw: (granted[0], None))
    def wait(*args, **kwargs):
        assert kwargs["timeout_s"] == 5
        clock[0] += 6
        granted[0] = True
        return {"admitted": True, "reason": "granted"}
    monkeypatch.setattr(agents.slot_wait, "request_and_wait", wait)
    def forbidden_spawn(*a, **kw):
        pytest.fail("a late admission grant must not spawn a native worker")
    monkeypatch.setattr(agents.subprocess, "Popen", forbidden_spawn)
    with pytest.raises(agents.AgentFailed, match="deadline exhausted"):
        agents.run_agent("x", {"declaredPeakMb": 3072}, repo_root=tmp_path,
                         artifact_dir=tmp_path / "agent", timeout_s=5)
    assert not capture.exists()
    assert [name for name, _ in events] == ["reserve", "release"]


def test_native_communicate_receives_only_budget_left_after_admission(tmp_path, monkeypatch, admission):
    events, _ = admission
    clock = [100.0]
    monkeypatch.setattr(agents, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    fake_cli(tmp_path, monkeypatch)
    original_admit = agents.capacity.admit_before_spawn
    def admit(*args, **kwargs):
        result = original_admit(*args, **kwargs)
        clock[0] += 4
        return result
    monkeypatch.setattr(agents.capacity, "admit_before_spawn", admit)
    original_popen = agents.subprocess.Popen
    timeouts = []
    def popen(*args, **kwargs):
        proc = original_popen(*args, **kwargs)
        original_communicate = proc.communicate
        def communicate(*a, **kw):
            timeouts.append(kw.get("timeout"))
            return original_communicate(*a, **kw)
        proc.communicate = communicate
        return proc
    monkeypatch.setattr(agents.subprocess, "Popen", popen)
    agents.run_agent("x", {}, repo_root=tmp_path, artifact_dir=tmp_path / "agent", timeout_s=10)
    assert timeouts == [6]
    assert [name for name, _ in events] == ["reserve", "START", "END", "release"]


def test_mac_provider_helper_reinsertion_cannot_impersonate_parent(tmp_path, monkeypatch):
    capture = fake_cli(tmp_path, monkeypatch)
    original = agents._unattended_env
    def reinsert(*a, **kw):
        return {**original(*a, **kw), **{key: "parent-identity" for key in agents._OWN_IDENTITY_ENV}}
    monkeypatch.setattr(agents, "_unattended_env", reinsert)
    monkeypatch.setenv(agents.FIRE_TOKEN_ENV, "existing-fire-token")
    agents.run_agent("x", {"label": "execute"}, repo_root=tmp_path, artifact_dir=tmp_path / "agent", parent_session_id="real-parent")
    env = json.loads(capture.read_text())["env"]
    for key in agents._OWN_IDENTITY_ENV:
        if key in ("COORDINATOR_AGENT_PROVIDER", "COORDINATOR_AGENT_VENDOR"):
            assert env[key] == "codex"
        else:
            assert key not in env
    assert env["COORDINATOR_PARENT_SESSION_ID"] == "real-parent"
    assert env[agents.FIRE_TOKEN_ENV] == "existing-fire-token"
    assert env["COORDINATOR_AGENT_ROLE"] == "workflow-agent"


def test_fire_token_is_never_minted_when_absent(tmp_path, monkeypatch):
    capture = fake_cli(tmp_path, monkeypatch)
    monkeypatch.delenv(agents.FIRE_TOKEN_ENV, raising=False)
    agents.run_agent("x", {}, repo_root=tmp_path, artifact_dir=tmp_path / "agent")
    assert agents.FIRE_TOKEN_ENV not in json.loads(capture.read_text())["env"]


@pytest.mark.parametrize("tier", ["haiku", "sonnet", "opus"])
def test_tier_model_reuses_shared_catalogue(tmp_path, monkeypatch, tier):
    capture = fake_cli(tmp_path, monkeypatch)
    monkeypatch.setattr(agents, "model_for_tier", lambda provider, requested: f"catalogue-{provider}-{requested}")
    agents.run_agent("x", {"model": tier}, repo_root=tmp_path, artifact_dir=tmp_path / "agent")
    argv = json.loads(capture.read_text())["argv"]
    assert argv[argv.index("--model") + 1] == f"catalogue-codex-{tier}"


@pytest.mark.parametrize("tier", ["fable", "mythos", "unknown", "", 8])
def test_explicit_unmapped_tier_fails_before_spawn(tmp_path, monkeypatch, tier):
    capture = fake_cli(tmp_path, monkeypatch)
    with pytest.raises(agents.AgentFailed, match="mapping|tier"):
        agents.run_agent("x", {"model": tier}, repo_root=tmp_path, artifact_dir=tmp_path / "agent")
    assert not capture.exists()


def test_explicit_native_model_wins_and_absent_model_keeps_cli_default(tmp_path, monkeypatch):
    capture = fake_cli(tmp_path, monkeypatch)
    agents.run_agent("x", {"model": "unknown", "codexModel": "native-explicit"}, repo_root=tmp_path, artifact_dir=tmp_path / "explicit")
    argv = json.loads(capture.read_text())["argv"]
    assert argv[argv.index("--model") + 1] == "native-explicit"
    agents.run_agent("x", {}, repo_root=tmp_path, artifact_dir=tmp_path / "default")
    assert "--model" not in json.loads(capture.read_text())["argv"]


def test_explicit_effort_catalogue_validation_and_native_flag(tmp_path, monkeypatch):
    capture = fake_cli(tmp_path, monkeypatch)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "models_cache.json").write_text(json.dumps({"models": [{"slug": "test-model", "supported_reasoning_levels": [{"effort": "low"}]}]}))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    agents.run_agent("x", {"codexModel": "test-model", "effort": "low"}, repo_root=tmp_path, artifact_dir=tmp_path / "agent")
    argv = json.loads(capture.read_text())["argv"]
    assert 'model_reasoning_effort="low"' in [argv[i + 1] for i, value in enumerate(argv[:-1]) if value == '-c']
    with pytest.raises(agents.AgentFailed, match="unsupported"):
        agents.run_agent("x", {"codexModel": "test-model", "effort": "ultra"}, repo_root=tmp_path, artifact_dir=tmp_path / "bad")


def test_process_provenance_uses_native_thread_and_observed_usage(tmp_path, monkeypatch):
    native = "01a1b82a-7064-7342-9eda-a5d50fcbf3eb"
    events = [
        {"type": "thread.started", "thread_id": native, "parent_thread_id": "not-the-native-thread"},
        {"type": "turn.started", "model": "observed-model", "reasoning_effort": "high"},
        {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 4, "cache_write_input_tokens": 2, "output_tokens": 3}},
        {"type": "turn.completed", "usage": {"input_tokens": 7, "cached_input_tokens": 1, "cache_write_input_tokens": 0, "output_tokens": 2}},
    ]
    fake_cli(tmp_path, monkeypatch, events=events)
    agents.run_agent("x", {"codexModel": "requested-model", "effort": "low"}, repo_root=tmp_path, artifact_dir=tmp_path / "agent")
    process = json.loads((tmp_path / "agent/process.json").read_text())
    assert process["native_thread_id"] == native
    assert process["model"] == "observed-model" and process["effort"] == "high"
    assert process["requested_model"] == "requested-model" and process["requested_effort"] == "low"
    assert process["usage"] == {"input_tokens": 17, "cached_input_tokens": 5, "cache_write_input_tokens": 2, "output_tokens": 5}
    assert len(process["usage_events"]) == 2
    assert "parent_thread_id" not in process


def test_missing_native_provenance_stays_unknown(tmp_path, monkeypatch):
    fake_cli(tmp_path, monkeypatch, events=[{"type": "turn.started", "parent_thread_id": "01a1b82a-7064-7342-9eda-a5d50fcbf3eb"}])
    agents.run_agent("x", {"codexModel": "requested-model", "effort": "low"}, repo_root=tmp_path, artifact_dir=tmp_path / "agent")
    process = json.loads((tmp_path / "agent/process.json").read_text())
    assert all(process[key] == "unknown" for key in ("native_thread_id", "model", "effort", "usage"))


def test_native_observer_callback_wiring_uses_protected_artifacts(tmp_path, monkeypatch):
    # This is synthetic CLI wiring, not actual-native provenance proof.
    sid='01a1b826-0012-72a0-a4d0-eae0ae828f4c'
    fake_cli(tmp_path, monkeypatch, events=[{'type':'thread.started','thread_id':sid}])
    monkeypatch.setattr(agents,'_native_child_facts',lambda proc,repo,parent,fire,request,argv:
        {'pid':proc.pid,'start_epoch':1.0,'parent_session_id':parent,'fire_id':fire,'request_id':request})
    seen=[]
    assert agents.run_agent('x',{'schema':SCHEMA},repo_root=tmp_path,
        artifact_dir=tmp_path/'.git/private-fire/agent',on_native_thread=seen.append,
        fire_id='fire-1',request_id=4)=={'ok':True}
    assert len(seen)==1 and seen[0]['session_id']==sid
    assert seen[0]['parent_session_id']!=sid
    assert seen[0]['fire_id']=='fire-1' and seen[0]['request_id']==4


def test_native_observer_binding_starts_after_start_delivery_and_pending_record(tmp_path,monkeypatch,admission):
    sid='01a1b826-0012-72a0-a4d0-eae0ae828f4c'
    fake_cli(tmp_path,monkeypatch,events=[{'type':'thread.started','thread_id':sid}])
    monkeypatch.setattr(agents,'_native_child_facts',lambda *_: {})
    order=[]
    def pending(pid):
        assert any(event[0]=='START' for event in admission[0])
        order.append('pending')
    def ready(facts):
        assert order==['pending']
        order.append('ready')
    agents.run_agent('x',{'schema':SCHEMA},repo_root=tmp_path,artifact_dir=tmp_path/'.git/private-fire/agent',
        on_spawn=pending,on_native_thread=ready)
    assert order==['pending','ready']


def test_native_observer_callback_error_cannot_return_success(tmp_path, monkeypatch):
    fake_cli(tmp_path,monkeypatch,events=[{'type':'thread.started','thread_id':'01a1b826-0012-72a0-a4d0-eae0ae828f4c'}])
    monkeypatch.setattr(agents,'_native_child_facts',lambda *_: {})
    def failed(_):raise RuntimeError('binding callback failed')
    with pytest.raises(agents.AgentFailed,match='binding callback failed'):
        agents.run_agent('x',{'schema':SCHEMA},repo_root=tmp_path,
            artifact_dir=tmp_path/'.git/private-fire/agent',on_native_thread=failed)
    assert not (tmp_path/'.git/private-fire/agent/result.json').exists()


def test_native_observer_rejects_workspace_artifact_before_spawn(tmp_path,monkeypatch):
    def unexpected(**_):pytest.fail('auth/spawn reached for unsafe artifacts')
    monkeypatch.setattr(agents,'preflight_auth',unexpected)
    with pytest.raises(agents.AgentFailed,match='protected repo .git'):
        agents.run_agent('x',{},repo_root=tmp_path,artifact_dir=tmp_path/'workspace',on_native_thread=lambda _:None)


def private_cli_fixture(tmp_path):
    """Synthetic protected owner metadata; never starts an engine or native door."""
    import hashlib,psutil
    fire='a'*32;request=7;parent='01a1b63b-acfb-78d1-86f2-a74ad6ca4572'
    base=tmp_path/'.git/fire-private';private_bin=base/'private-bin';private_bin.mkdir(parents=True)
    engine=tmp_path/'private-engine';engine.mkdir();settings=tmp_path/'private-settings';settings.mkdir()
    aliases=['coordinator-invoke','dispatch-emit'];image=b'fake-reviewed-native-image'
    for name in aliases:(private_bin/(name+'.exe')).write_bytes(image)
    bindings=base/'worker-bindings.json'
    common={'schema_version':1,'fire_id':fire,'engine_root':str(engine),'repo_root':str(tmp_path),
        'engine_token':'fake-generation','source_sha':'b'*40,'script_sha256':'c'*64}
    sidecar={**common,'settings_home':str(settings),'bindings_path':str(bindings),
        'server_pid':123,'server_creation_time_ft':'123456789'}
    roster={**common,'owner':{'pid':os.getpid(),'birth':psutil.Process().create_time()},
        'parent_session_id':parent,'private_bin':str(private_bin),'allowed_cli':aliases,
        'native_door_sha256':hashlib.sha256(image).hexdigest(),'workers':[]}
    sidecar_path=private_bin/'door.private.json';sidecar_path.write_text(json.dumps(sidecar));bindings.write_text(json.dumps(roster))
    contract={'env':{'COORDINATOR_ENGINE_ROOT':str(engine),'COORDINATOR_WARM':'0',
        'COORDINATOR_WARM_LISTENER_AUTOSTART':'0','PATH':str(private_bin)+os.pathsep+os.environ.get('PATH','')},
        'private_bin':str(private_bin),'allowed_cli':aliases,'fire_id':fire,'request_id':request,'bindings_path':str(bindings)}
    args={'repo_root':tmp_path,'artifact_dir':base/'steps/1','parent_session_id':parent,'fire_id':fire,'request_id':request}
    return contract,args,sidecar,roster


def test_private_cli_fake_wire_resolves_native_before_private_path(tmp_path,monkeypatch):
    contract,kwargs,_,_=private_cli_fixture(tmp_path)
    capture=fake_cli(tmp_path,monkeypatch,events=[{'type':'thread.started','thread_id':'01a1b826-0012-72a0-a4d0-eae0ae828f4c'}])
    original=agents.resolve_codex;resolve_env=[]
    def resolve(**kw):resolve_env.append(dict(kw['env']));return original(**kw)
    monkeypatch.setattr(agents,'resolve_codex',resolve)
    monkeypatch.setattr(agents,'_native_child_facts',lambda *_:{})
    assert agents.run_agent('Preserve --params-file abc.json',{'schema':SCHEMA},**kwargs,
        on_spawn=lambda _:None,on_native_thread=lambda _:None,private_cli=contract)=={'ok':True}
    wire=json.loads(capture.read_text());audit=json.loads((kwargs['artifact_dir']/'private-cli.json').read_text())
    assert resolve_env[0]['PATH']!=contract['env']['PATH']
    for key,value in contract['env'].items():
        if key=='PATH':
            # The final-env shaper may insert PowerShell 7 ahead of a WindowsApps pwsh; private-bin stays first.
            assert wire['env'][key].split(os.pathsep)[0]==contract['private_bin']
            assert set(value.split(os.pathsep))<=set(wire['env'][key].split(os.pathsep))
        else:assert wire['env'][key]==value
    assert 'COORDINATOR_SESSION_ID' not in wire['env'] and 'CODEX_THREAD_ID' not in wire['env']
    assert wire['env']['COORDINATOR_PARENT_SESSION_ID']==kwargs['parent_session_id']
    assert wire['argv'][wire['argv'].index('--sandbox')+1]=='workspace-write'
    assert not any('default_permissions' in arg for arg in wire['argv'])
    assert 'Preserve every argument exactly' in wire['prompt']
    assert '--params-file abc.json' in wire['prompt']
    assert contract['private_bin'] in wire['prompt']
    assert audit['runtime_verified'] is False and len(audit['images'])==2
    assert json.loads((kwargs['artifact_dir']/'process.json').read_text())['private_cli']==audit


@pytest.mark.parametrize('case',['identity-env','warm','listener','path','request','owner','sidecar-extra','image','catalog','source','other-fire-bindings'])
def test_private_cli_contract_rejects_authority_or_path_changes(tmp_path,case):
    contract,kwargs,sidecar,roster=private_cli_fixture(tmp_path)
    if case=='identity-env':contract['env']['COORDINATOR_SESSION_ID']='parent-spoof'
    elif case=='warm':contract['env']['COORDINATOR_WARM']='1'
    elif case=='listener':contract['env']['COORDINATOR_WARM_LISTENER_AUTOSTART']='1'
    elif case=='path':contract['env']['PATH']='C:/installed/shared'
    elif case=='request':contract['request_id']=8
    elif case=='owner':roster['owner']['pid']+=1
    elif case=='sidecar-extra':sidecar['request_id']=kwargs['request_id']
    elif case=='image':(Path(contract['private_bin'])/'coordinator-invoke.exe').write_bytes(b'changed')
    elif case=='catalog':contract['allowed_cli']=['../escape']
    elif case=='source':roster['source_sha']='d'*40
    elif case=='other-fire-bindings':
        p=tmp_path/'.git/other-fire';p.mkdir();other=p/'worker-bindings.json';other.write_text(json.dumps(roster));contract['bindings_path']=str(other)
    (Path(contract['private_bin'])/'door.private.json').write_text(json.dumps(sidecar))
    Path(contract['bindings_path']).write_text(json.dumps(roster))
    with pytest.raises(agents.AgentFailed,match='private CLI'):
        agents._private_cli_settings(contract,**kwargs)


@pytest.mark.parametrize("environment", [agents._environment, headless_agents._environment])
def test_windows_parent_env_reaches_headless_spawn_env(monkeypatch, environment):
    keys = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "COMSPEC")
    for k in keys:
        monkeypatch.setenv(k, f"v-{k}")
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    monkeypatch.setattr(review, "sys", SimpleNamespace(platform="win32"))
    env = environment()
    assert all(env[k] == f"v-{k}" for k in keys)
    assert "OPENAI_API_KEY" not in env


def test_model_options_cannot_select_private_cli(tmp_path,monkeypatch):
    capture=fake_cli(tmp_path,monkeypatch)
    agents.run_agent('x',{'schema':SCHEMA,'private_cli':{'env':{'COORDINATOR_WARM':'1'}}},repo_root=tmp_path,artifact_dir=tmp_path/'agent')
    assert not (tmp_path/'agent/private-cli.json').exists()
    assert json.loads(capture.read_text())['env']['COORDINATOR_WARM']=='0'


class _Spawned(Exception):
    pass


def _headless_shape_rig(tmp_path, monkeypatch, *, ps7):
    import functools
    from coordinator_core.codex_runner import codex_spawn_env, sandbox_preflight
    pf = tmp_path / "ProgramFiles"
    apps = tmp_path / "WindowsApps"
    apps.mkdir()
    (apps / "pwsh.exe").write_text("")
    if ps7:
        (pf / "PowerShell" / "7").mkdir(parents=True)
        (pf / "PowerShell" / "7" / "pwsh.exe").write_text("")
    repo = tmp_path / "repo"
    repo.mkdir()
    seen = {}
    def spawn(argv, prompt, *, env, **_kw):
        seen["env"] = dict(env)
        raise _Spawned
    monkeypatch.setattr(sandbox_preflight, "_is_windows", lambda: True)
    monkeypatch.setattr(headless_agents, "shape_codex_child", functools.partial(
        codex_spawn_env.shape_codex_child, environ={"ProgramFiles": str(pf)}, windows=True))
    monkeypatch.setattr(headless_agents, "preflight_auth", lambda **_kw: None)
    monkeypatch.setattr(headless_agents, "resolve_codex", lambda **_kw: ["codex"])
    monkeypatch.setattr(headless_agents, "_environment",
                        lambda: {"PATH": str(apps), "TEMP": "denied-temp", "TMP": "denied-temp"})
    monkeypatch.setattr(headless_agents, "_run_admitted", spawn)
    return repo, seen, str(pf / "PowerShell" / "7"), str(apps)


def test_headless_codex_spawn_env_has_ps7_ahead_of_windowsapps_and_writable_temp(tmp_path, monkeypatch):
    repo, seen, ps7, apps = _headless_shape_rig(tmp_path, monkeypatch, ps7=True)
    with pytest.raises(_Spawned):
        headless_agents.run_agent("x", {}, repo_root=repo, artifact_dir=tmp_path / "agent")
    env = seen["env"]
    assert env["PATH"].split(os.pathsep)[:2] == [ps7, apps]
    expected = repo.resolve() / "scratch" / "codex-tmp"
    assert expected.is_dir() and env["TEMP"] == env["TMP"] == str(expected)


def test_headless_codex_windowsapps_only_path_refuses_before_admission(tmp_path, monkeypatch):
    repo, seen, _, _ = _headless_shape_rig(tmp_path, monkeypatch, ps7=False)
    with pytest.raises(headless_agents.AgentFailed, match="WindowsApps|pwsh"):
        headless_agents.run_agent("x", {}, repo_root=repo, artifact_dir=tmp_path / "agent")
    assert "env" not in seen
