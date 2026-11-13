"""Trusted owner wiring fixtures; no real service or native registration proof."""
import asyncio,dataclasses,json,os,pickle
from pathlib import Path
import pytest
from coordinator_core.session.native_actor import VerifiedCodexExecActor,current_verified_native_actor
from coordinator_core.warm import server,workflow_peer
from coordinator_core.warm.caller_context import resolve_caller_context,with_verified_native_actor

SID='11111111-2222-4333-8444-555555555555'
PARENT='11111111-2222-4333-8444-666666666666'
@pytest.fixture
def actor(tmp_path,monkeypatch):
 monkeypatch.delenv(workflow_peer.PRIVATE_ROSTER_ENV,raising=False)
 return VerifiedCodexExecActor(session_id=SID,repo_root=str(tmp_path),pid=20,start_epoch=20.0,exe=str(tmp_path/'codex.exe'),exe_sha256='a'*64,parent_session_id=PARENT,fire_id='b'*32,request_id=1)

def _caller(actor):
 return with_verified_native_actor(resolve_caller_context({'session_id':SID,'pid':'30','cwd':actor.repo_root}),actor)

@pytest.mark.parametrize('route',['sync','pool_pickle'])
def test_actual_dispatch_binds_native_actor_and_keeps_transport_pid(actor,monkeypatch,route):
 from coordinator_core import ipc
 async def leaf(msg,**kwargs):
  return {'jsonrpc':'2.0','id':msg['id'],'result':{'actor':dataclasses.asdict(current_verified_native_actor()),'transport_pid':os.environ.get('CLAUDE_PID')}}
 monkeypatch.setattr(ipc,'dispatch_message',leaf)
 msg={'jsonrpc':'2.0','id':1,'method':'noop','params':{}}
 caller=_caller(actor)
 if route=='pool_pickle':
  msg,caller=pickle.loads(pickle.dumps((msg,caller)))
  response=server._pool_dispatch_worker_body(msg,caller)
 else:response=server._run_dispatch(msg,caller=caller,isolated=True)
 assert response['result']['actor']==dataclasses.asdict(actor)
 assert response['result']['transport_pid']=='30'
 assert current_verified_native_actor() is None

def test_next_actorless_dispatch_clears_and_previous_actor_resets(actor,monkeypatch):
 from coordinator_core import ipc
 observed=[]
 async def leaf(msg,**kwargs):
  observed.append(current_verified_native_actor());return {'jsonrpc':'2.0','id':msg['id'],'result':'ok'}
 monkeypatch.setattr(ipc,'dispatch_message',leaf)
 msg={'jsonrpc':'2.0','id':1,'method':'noop','params':{}}
 server._run_dispatch(msg,caller=_caller(actor),isolated=True)
 server._run_dispatch(msg,caller=resolve_caller_context({'session_id':SID,'pid':'30'}),isolated=True)
 assert observed==[actor,None] and current_verified_native_actor() is None

def test_authenticated_result_attaches_after_env_merge_and_ignores_wire_actor(actor,monkeypatch):
 observed=[];written=[]
 monkeypatch.setattr(workflow_peer,'authorize',lambda *args,**kwargs:{'kind':'worker','verified_native_actor':actor})
 class Version:
  def is_skewed(self,token):return False
  def is_token_skewed(self,token):return False
 def dispatch(msg,*,caller=None):
  observed.append(caller);return {'jsonrpc':'2.0','id':msg['id'],'result':'ok'}
 frame={'jsonrpc':'2.0','id':1,'method':'invoke.from_argv','params':{'argv':['ping'],'cwd':actor.repo_root},'_engine_token':'token','_caller':{'session_id':SID,'pid':'30','cwd':actor.repo_root,'verified_native_actor':{'pid':999}},'_env':{'CODEX_THREAD_ID':SID},'_verified_native_actor':{'pid':999}}
 server._serve_line(json.dumps(frame).encode(),write=written.append,version_state=Version(),server_sha='pin',close_listener=lambda:None,drain=lambda:None,release_in_flight=lambda:None,dispatch=dispatch,transport_stream=object())
 assert len(observed)==1
 assert observed[0].verified_native_actor==actor and observed[0].pid=='30'
 assert json.loads(written[0])['result']=='ok'
