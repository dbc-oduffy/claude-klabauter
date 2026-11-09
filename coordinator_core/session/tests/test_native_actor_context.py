"""Pure fixtures only: these tests do not prove protected native execution."""
from dataclasses import replace
import json
import os
import pickle
import sys
from types import SimpleNamespace, ModuleType
import pytest
from coordinator_core.session import core, native_actor as native
from coordinator_core.warm.caller_context import resolve_caller_context, with_verified_native_actor
from coordinator_core.warm.entry_seam import per_request_state

SID = "aaaaaaaa-0000-4000-8000-000000000001"
PARENT = "bbbbbbbb-0000-4000-8000-000000000002"

@pytest.fixture
def actor(tmp_path, monkeypatch):
    import psutil
    exe = tmp_path / 'codex.exe'
    exe.touch()
    value = native.VerifiedCodexExecActor(SID, str(tmp_path), 123, 456.125,
        str(exe), 'a'*64, PARENT, 'b'*32, 1)
    process = SimpleNamespace(status=lambda: 'running', create_time=lambda:456.125,
        exe=lambda:str(exe), is_running=lambda:True)
    monkeypatch.setattr(psutil, 'Process', lambda pid:process)
    module=ModuleType('coordinator_core.warm.workflow_peer')
    module.kernel_live_pid=lambda pid:True
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(core, 'git_root', lambda cwd:str(tmp_path))
    return value, process, module

def test_wire_actor_ignored_and_typed_attachment_pickle_safe(actor):
    value, _, _ = actor
    caller=resolve_caller_context({'session_id':SID, 'verified_native_actor':value})
    assert caller.verified_native_actor is None
    caller=with_verified_native_actor(caller,value)
    assert pickle.loads(pickle.dumps(caller)).verified_native_actor==value
    with pytest.raises(TypeError):
        with_verified_native_actor(caller,vars(value))

def test_nested_scope_restores_and_new_request_clears_without_env_changes(actor):
    value, _, _ = actor
    before=dict(os.environ)
    with native.bind_verified_native_actor(value):
        with per_request_state(isolated=True):
            assert native.current_verified_native_actor()==value
        with per_request_state(isolated=True,warm_served=True):
            assert native.current_verified_native_actor() is None
        with pytest.raises(RuntimeError):
            with native.bind_verified_native_actor(None):
                raise RuntimeError('fixture')
        assert native.current_verified_native_actor()==value
    assert native.current_verified_native_actor() is None
    assert dict(os.environ)==before

@pytest.mark.parametrize('change',[{'session_id':PARENT},{'start_epoch':456.126},{'pid':True}])
def test_mismatch_refuses_before_any_session_creation(actor,tmp_path,change):
    value, _, _=actor
    base=tmp_path/'uncreated'
    with native.bind_verified_native_actor(replace(value,**change)):
        with pytest.raises(native.NativeActorMismatch):
            core.ensure_session(SID,cwd=str(tmp_path),sessions_base=str(base))
        with pytest.raises(native.NativeActorMismatch):
            core.init(SID,cwd=str(tmp_path),sessions_base=str(base))
    assert not base.exists()

def test_wrong_repo_dead_and_ambiguous_refuse(actor,tmp_path):
    value, process, kernel=actor
    other=tmp_path/'other';other.mkdir()
    with pytest.raises(native.NativeActorMismatch):
        native.validate_actor(value,SID,str(other))
    process.status=lambda:'zombie'
    with pytest.raises(native.NativeActorMismatch):
        native.validate_actor(value,SID,str(tmp_path))
    process.status=lambda: (_ for _ in ()).throw(PermissionError())
    with pytest.raises(native.NativeActorMismatch):
        native.validate_actor(value,SID,str(tmp_path))
    kernel.kernel_live_pid=lambda pid:False
    assert not native.precise_pid_alive(123,'456.125')

def test_existing_meta_fast_path_still_refreshes_via_sole_init(actor,tmp_path,monkeypatch):
    value, _, _=actor
    base=tmp_path/'sessions';sdir=base/SID;sdir.mkdir(parents=True)
    (sdir/'meta.json').write_text(json.dumps({'stable_pid':'999'}))
    calls=[]
    monkeypatch.setattr(core,'init',lambda *a,**kw:calls.append((a,kw)) or True)
    with native.bind_verified_native_actor(value):
        assert core.ensure_session(SID,cwd=str(tmp_path),sessions_base=str(base))==str(sdir)
    assert len(calls)==1 and calls[0][0][0]==SID

def test_native_fractional_birth_rejects_same_second_reuse_legacy_unchanged(actor,monkeypatch):
    _, process, _=actor
    assert core.stable_pid_alive('123','','456.125')
    process.create_time=lambda:456.126
    assert not core.stable_pid_alive('123','','456.125')
    monkeypatch.setattr(core,'_win_create_time_epoch',lambda pid:456)
    assert core.stable_pid_alive('123','','456')

def test_init_persists_precise_native_witness_and_preserves_goal(actor,tmp_path,monkeypatch):
    value, _, _=actor
    from coordinator_core.git import git_state
    from coordinator_core.session import harness_registry
    monkeypatch.setattr(git_state,'head_sha',lambda root:None)
    monkeypatch.setattr(git_state,'head_branch',lambda root:None)
    monkeypatch.setattr(harness_registry,'self_record',lambda:None)
    base=tmp_path/'sessions'
    with native.bind_verified_native_actor(value):
        core.init(SID,'original',cwd=str(tmp_path),sessions_base=str(base),root=str(tmp_path))
        core.ensure_session(SID,goal='replacement',cwd=str(tmp_path),sessions_base=str(base),root=str(tmp_path))
    row=json.loads((base/SID/'meta.json').read_text())
    assert row['stable_pid']=='123'
    assert row['stable_pid_start_epoch']=='456.125'
    assert row['stable_pid_capture']=='verified-codex-exec-actor'
    assert row['goal']=='original'


def test_actor_contexts_are_independent_and_explicit_entry_binds(actor):
    from contextvars import Context
    value, _, _ = actor
    def read_bound():
        with native.bind_verified_native_actor(value):
            assert Context().run(native.current_verified_native_actor) is None
            with per_request_state(isolated=True, verified_native_actor=value):
                assert native.current_verified_native_actor() == value
    Context().run(read_bound)
    assert native.current_verified_native_actor() is None


@pytest.mark.parametrize('initialized,has_meta', [(False, False), (False, True), (True, False)])
def test_actor_ensure_rejects_failed_init_or_missing_metadata(actor,tmp_path,monkeypatch,initialized,has_meta):
    value, _, _=actor
    base=tmp_path/'sessions'
    if has_meta:
        sdir=base/SID;sdir.mkdir(parents=True)
        (sdir/'meta.json').write_text('{}')
    monkeypatch.setattr(core,'init',lambda *a,**kw:initialized)
    with native.bind_verified_native_actor(value):
        with pytest.raises(native.NativeActorMismatch,match='did not produce metadata'):
            core.ensure_session(SID,cwd=str(tmp_path),sessions_base=str(base))
