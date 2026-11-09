from __future__ import annotations

import io
import json
import time
from types import SimpleNamespace

from coordinator_core.session import external_registry as registry, core, harness_registry
from coordinator_core.group_em import session_registry

SID = "01a1b63c-040a-76f2-a9b9-929d9a0d0098"


def test_lifecycle_verifies_host_birth_and_publishes_global_consumers(tmp_path, monkeypatch):
    directory = tmp_path / "host-registry"
    monkeypatch.setattr(registry, "settings_home", lambda: tmp_path)
    import psutil
    native = SimpleNamespace(pid=4321, name=lambda: "codex.exe", create_time=lambda: time.time() - 1)
    monkeypatch.setattr(psutil, "Process", lambda _: SimpleNamespace(parents=lambda: [native]))
    monkeypatch.setattr(core, "ensure_session", lambda *a, **kw: "session-dir")
    calls = []
    monkeypatch.setattr(core, "stable_pid_alive", lambda pid, **kw: calls.append((pid, kw)) or True)
    record = registry.register_current({"session_id": SID, "cwd": str(tmp_path)})
    assert record["pid"] == 4321
    assert registry.lookup(SID)["start_epoch"] == record["start_epoch"]
    monkeypatch.setattr(harness_registry, "registry_dir", lambda: tmp_path / "absent-claude")
    parsed = harness_registry.lookup(SID)
    assert parsed.provider == "codex" and parsed.pid == 4321
    monkeypatch.setattr(session_registry, "registry_dir", lambda: tmp_path / "absent-claude")
    assert session_registry.find_registry_row(SID).pid == 4321
    assert calls[-1][1]["stored_start_epoch"] == str(record["start_epoch"])
    registry.unregister(SID)
    assert registry.lookup(SID) is None
    assert harness_registry.lookup(SID) is None


def test_invalid_dead_and_reused_bindings_are_not_live(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "settings_home", lambda: tmp_path)
    base = registry.registry_dir()
    base.mkdir(parents=True)
    row = {"schema_version": 1, "provider": "codex", "session_id": SID, "pid": 10,
           "start_epoch": time.time(), "cwd": str(tmp_path)}
    (base / f"{SID}.json").write_text(json.dumps(row))
    monkeypatch.setattr(core, "stable_pid_alive", lambda *a, **kw: False)
    assert registry.lookup(SID) is None


def test_fractional_birth_uses_real_stable_pid_comparison(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "settings_home", lambda: tmp_path)
    base = registry.registry_dir()
    base.mkdir(parents=True)
    row = {"schema_version": 1, "provider": "codex", "session_id": SID, "pid": 10,
           "start_epoch": 1794058666.625, "cwd": str(tmp_path)}
    (base / f"{SID}.json").write_text(json.dumps(row))
    monkeypatch.setattr(core, "_win_create_time_epoch", lambda _: 1794058666)
    assert registry.lookup(SID) is not None
    monkeypatch.setattr(core, "_win_create_time_epoch", lambda _: 1794058676)
    assert registry.lookup(SID) is None
    assert registry.lookup("../escape") is None
    row["pid"] = True
    (base / f"{SID}.json").write_text(json.dumps(row))
    monkeypatch.setattr(core, "stable_pid_alive", lambda *a, **kw: True)
    assert registry.lookup(SID) is None


def test_hook_boundary_stays_nonblocking_and_reads_payload(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(registry.sys, "stdin", io.StringIO(json.dumps({"session_id": SID, "cwd": "fixture"})))
    monkeypatch.setattr(registry, "register_current", lambda payload: seen.append(payload))
    assert registry.main(["--event", "SessionStart"]) == 0
    assert seen[0]["session_id"] == SID
    assert capsys.readouterr().out == "{}\n"
    monkeypatch.setattr(registry.sys, "stdin", io.StringIO("malformed"))
    assert registry.main(["--event", "SessionEnd"]) == 0
    out = capsys.readouterr()
    assert out.out == "{}\n" and "external-session" in out.err


def test_session_end_does_not_revive_from_shared_pid_local_metadata_and_resume_reopens(tmp_path, monkeypatch):
    from coordinator_core.session import liveness
    import psutil
    sid2 = "01a1b63c-040a-76f2-a9b9-929d9a0d0099"
    monkeypatch.setattr(registry, "settings_home", lambda: tmp_path)
    native = SimpleNamespace(pid=4321, name=lambda: "codex.exe", create_time=lambda: time.time() - 1.25)
    monkeypatch.setattr(psutil, "Process", lambda _: SimpleNamespace(parents=lambda: [native]))
    monkeypatch.setattr(core, "ensure_session", lambda *a, **kw: "session-dir")
    monkeypatch.setattr(core, "stable_pid_alive", lambda *a, **kw: True)
    monkeypatch.setattr(harness_registry, "registry_dir", lambda: tmp_path / "absent-claude")
    hub = tmp_path / "session-hub"
    for sid in (SID, sid2):
        (hub / sid).mkdir(parents=True)
        (hub / sid / "meta.json").write_text(json.dumps({"stable_pid": "4321", "stable_pid_start_epoch": "1794058666"}))
    monkeypatch.setattr(core, "sessions_dir", lambda cwd=None: str(hub))
    own = str(tmp_path / "own")
    elsewhere = str(tmp_path / "elsewhere")
    payload = {"session_id": SID, "cwd": own}
    registry.register_current(payload)
    registry.register_current({"session_id": sid2, "cwd": elsewhere})
    assert liveness.session_live(SID, own) and liveness.session_live(SID, elsewhere)
    registry.unregister(SID)
    assert registry.is_ended(SID)
    for cwd in (own, elsewhere):
        assert not liveness.session_live(SID, cwd)
        assert liveness.session_verdict(SID, cwd)[:2] == (False, "harness-registry-superseded")
        verdicts = liveness.live_session_verdicts(cwd)
        assert verdicts[SID][:2] == (False, "harness-registry-superseded")
        assert liveness.session_live(sid2, cwd)
    registry.register_current(payload)
    assert not registry.is_ended(SID)
    assert liveness.session_live(SID, own) and liveness.session_live(SID, elsewhere)


def test_registered_thread_sid_routes_without_mutating_explicit_bindings_and_end_withdraws(tmp_path, monkeypatch):
    from coordinator_core.agent_pipe import roster, bridge, codex_app_server
    from coordinator_core.agent_pipe.codex import CodexAdapter
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    monkeypatch.setattr(registry, "settings_home", lambda: tmp_path)
    base = registry.registry_dir()
    base.mkdir(parents=True)
    row = {"schema_version": 1, "provider": "codex", "session_id": SID, "pid": 4321,
           "start_epoch": 1794058666, "cwd": str(tmp_path)}
    (base / f"{SID}.json").write_text(json.dumps(row))
    monkeypatch.setattr(core, "stable_pid_alive", lambda *a, **kw: True)
    monkeypatch.setattr(CodexAdapter, "proxy_argv", lambda self: ["native-codex", "app-server", "proxy"])
    roster.bind("other-owner", "claude", "their-session")
    assert roster.resolve(SID) == {"kind": "codex", "id": SID, "token": None,
                                   "argv": ["native-codex", "app-server", "proxy"]}
    assert SID in roster.list()
    queued = []
    monkeypatch.setattr(codex_app_server, "enqueue", lambda *args: queued.append(args))
    message = bridge.make_message("workflow.fire", SID, "default-SID terminal notice")
    assert bridge.route(message).status == "queued"
    assert queued[0][0] == ["native-codex", "app-server", "proxy"]
    assert queued[0][1] == SID
    assert "default-SID terminal notice" in queued[0][2]
    registry.unregister(SID)
    assert roster.resolve(SID) is None and SID not in roster.list()
    assert bridge.route(message).status == "unroutable"
    assert roster.resolve("other-owner")["id"] == "their-session"



def test_context_metadata_preserved_across_end_and_resume(tmp_path, monkeypatch):
    import psutil
    monkeypatch.setattr(registry,'settings_home',lambda:tmp_path)
    native=SimpleNamespace(pid=4321,name=lambda:'codex.exe',create_time=lambda:time.time()-1,
                           cmdline=lambda:['codex','app-server'])
    monkeypatch.setattr(psutil,'Process',lambda _:SimpleNamespace(parents=lambda:[native]))
    monkeypatch.setattr(core,'ensure_session',lambda *a,**kw:'session-dir')
    monkeypatch.delenv('COORDINATOR_AGENT_ROLE',raising=False)
    monkeypatch.delenv('COORDINATOR_AGENT_WORKER',raising=False)
    monkeypatch.delenv('COORDINATOR_JOB_MODE',raising=False)
    parent='00000000-0000-4000-8000-000000000001'
    payload={'session_id':SID,'cwd':str(tmp_path),'hook_event_name':'SessionStart','source':'startup',
             'transcript_path':str(tmp_path/'rollout.jsonl'),'parent_session_id':parent,'title':'Reported task'}
    first=registry.register_current(payload)
    assert first['role']=='em'
    registry.unregister(SID)
    prior=json.loads((registry.registry_dir()/f'{SID}.json').read_text())
    assert prior['parent_session_id']==parent and prior['reported_task_title']=='Reported task'
    second=registry.register_current({'session_id':SID,'cwd':str(tmp_path),'hook_event_name':'SessionStart','source':'resume'})
    assert 'ended_at' not in second
    for key in ('transcript_path','parent_session_id','reported_task_title'):
        assert second[key]==first[key]
