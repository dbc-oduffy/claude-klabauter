import hashlib,importlib.util,json,sys
from pathlib import Path
from types import SimpleNamespace
import pytest
from coordinator_core.warm import workflow_peer as peer
SID='11111111-2222-4333-8444-555555555555'
PARENT='11111111-2222-4333-8444-666666666666'
class Process:
 def __init__(self,pid,birth,parent=None,exe=''):self.pid,self.birth,self.ancestor,self.image=pid,birth,parent,exe
 def create_time(self):return self.birth
 def is_running(self):return True
 def parent(self):return self.ancestor
 def exe(self):return self.image
@pytest.fixture
def case(tmp_path,monkeypatch):
 repo=tmp_path/'repo';repo.mkdir();protected=repo/'.git/coordinator-sessions/workflow-fires/headless'/('a'*32);protected.mkdir(parents=True)
 engine=tmp_path/'engine';engine.mkdir();bin=protected/'private-bin';bin.mkdir()
 image=tmp_path/'codex.exe';image.write_bytes(b'native observed executable')
 alias=bin/'sizing-assemble.exe';alias.write_bytes(b'committed native required door')
 row={'schema_version':1,'fire_id':'a'*32,'repo_root':str(repo),'engine_root':str(engine),'engine_token':'token','source_sha':'c'*40,'script_sha256':'d'*64,'parent_session_id':PARENT,'private_bin':str(bin),'native_door_sha256':hashlib.sha256(alias.read_bytes()).hexdigest(),'owner':{'pid':10,'birth':10.0},'workers':[{'pid':20,'birth':20.0,'native_uuid':SID,'state':'ready','request_id':'r1','backend':'codex','role':'workflow-agent','exe':str(image),'exe_sha256':hashlib.sha256(image.read_bytes()).hexdigest()}],'allowed_cli':['sizing-assemble']}
 script=repo/'assigned.mjs';script.write_text('return {ok:true};')
 row['script_sha256']=hashlib.sha256(script.read_bytes()).hexdigest()
 (engine/'coordinator_core').mkdir();(engine/'coordinator_core/_engine_stamp').write_text('sha:'+row['source_sha']+'\n')
 fire_record=repo/'.git/coordinator-sessions/workflow-fires'/('a'*32+'.json')
 record={'fire_id':row['fire_id'],'state':'running','executor':'headless','backend':'codex','pid':10,'pid_birth':10.0,'caller_session_id':PARENT,'repo_root':str(repo),'script_path':str(script),'sha256':row['script_sha256'],'script_sha256':row['script_sha256'],'source_sha':row['source_sha'],'engine_root':str(engine)}
 fire_record.write_text(json.dumps(record))
 roster=protected/'worker-bindings.json';roster.write_text(json.dumps(row))
 owner=Process(10,10.0);worker=Process(20,20.0,owner,str(image));caller=Process(30,30.0,worker,str(alias));processes={10:owner,20:worker,30:caller}
 package=tmp_path/'source'/'warm';package.mkdir(parents=True);(package.parent/'ops').mkdir();(package.parent/'ops'/'warm_entrypoint_allowlist.json').write_text(json.dumps({'entrypoints':['sizing-assemble']}))
 monkeypatch.setattr(peer,'__file__',str(package/'workflow_peer.py'));monkeypatch.setattr(peer,'sys',SimpleNamespace(platform='win32'));monkeypatch.setenv(peer.PRIVATE_ROSTER_ENV,str(roster));monkeypatch.setattr(peer,'kernel_client_pid',lambda stream:30);monkeypatch.setattr(peer,'kernel_live_pid',lambda pid:True);monkeypatch.setattr(peer,'_process',lambda pid:processes[pid])
 msg={'method':'invoke.from_argv','_engine_token':'token','_caller':{'session_id':SID,'pid':'30','cwd':str(repo)},'params':{'entrypoint':'sizing-assemble','argv':[],'cwd':str(repo)}}
 return SimpleNamespace(row=row,path=roster,msg=msg,engine=engine,repo=repo,processes=processes,alias=alias,image=image,script=script,record=record,fire_record=fire_record,write=lambda:roster.write_text(json.dumps(row)))
def test_legacy(monkeypatch):
 monkeypatch.delenv(peer.PRIVATE_ROSTER_ENV,raising=False);assert peer.authorize(None,{}) is None
def test_ready(case):
 result=peer.authorize(None,case.msg,engine_root=case.engine)
 assert result['session_id']==SID;assert case.msg['_env']['COORDINATOR_AGENT_ROLE']=='workflow-agent';assert case.msg['_env']['COORDINATOR_PARENT_SESSION_ID']==PARENT
@pytest.mark.parametrize('kind',['sid','pid','role','envsid','repo','cli','direct','token','repoarg','payloadpeer','recycle','pending','ended','exe','alias','aliashash','nativehash','parent','vendor'])
def test_refused(case,kind):
 m=case.msg
 if kind=='sid':m['_caller']['session_id']=PARENT
 if kind=='pid':m['_caller']['pid']='20'
 if kind=='role':m['_env']={'COORDINATOR_AGENT_ROLE':'em'}
 if kind=='envsid':m['_env']={'CODEX_THREAD_ID':PARENT}
 if kind=='repo':m['params']['cwd']=str(case.repo.parent)
 if kind=='cli':m['params']['entrypoint']='uncommitted'
 if kind=='direct':m['method']='dispatch.terminal_commit'
 if kind=='token':m['_engine_token']='other'
 if kind=='repoarg':m['params']['argv']=['--repo',str(case.repo.parent)]
 if kind=='payloadpeer':case.processes[30].ancestor=None;m['_transport_peer']={'pid':20,'birth':20.0}
 if kind=='recycle':case.processes[20].birth+=1
 if kind in ('pending','ended'):case.row['workers'][0]['state']=kind;case.row['workers'][0].pop('native_uuid');case.write()
 if kind=='exe':case.processes[20].image=str(case.alias)
 if kind=='alias':case.processes[30].image=str(case.image)
 if kind=='aliashash':case.alias.write_bytes(b'replaced alias')
 if kind=='nativehash':case.image.write_bytes(b'replaced native executable')
 if kind=='parent':m['_env']={'COORDINATOR_PARENT_SESSION_ID':SID}
 if kind=='vendor':m['_env']={'COORDINATOR_AGENT_VENDOR':'claude'}
 with pytest.raises(peer.PrivatePeerRefused):peer.authorize(None,m,engine_root=case.engine)
def test_owner_only_ping(case,monkeypatch):
 monkeypatch.setattr(peer,'kernel_client_pid',lambda stream:10);case.msg['method']='ping';assert peer.authorize(None,case.msg,engine_root=case.engine)['kind']=='owner';case.msg['method']='engine.stop_probe'
 with pytest.raises(peer.PrivatePeerRefused):peer.authorize(None,case.msg,engine_root=case.engine)
def test_nonwindows(case,monkeypatch):
 monkeypatch.setattr(peer,'sys',SimpleNamespace(platform='linux'))
 with pytest.raises(peer.PrivatePeerRefused):peer.authorize(None,case.msg,engine_root=case.engine)
def test_outside_metadata(case,monkeypatch):
 p=case.repo/'writable.json';p.write_text(json.dumps(case.row));monkeypatch.setenv(peer.PRIVATE_ROSTER_ENV,str(p))
 with pytest.raises(peer.PrivatePeerRefused):peer.authorize(None,case.msg,engine_root=case.engine)
def test_bound(case):
 case.path.write_text(' '*(peer.MAX_ROSTER_BYTES+1))
 with pytest.raises(peer.PrivatePeerRefused):peer.authorize(None,case.msg,engine_root=case.engine)

def test_recycled_owner_refused(case):
 case.processes[10].birth+=1
 with pytest.raises(peer.PrivatePeerRefused):peer.authorize(None,case.msg,engine_root=case.engine)

def test_server_gate_refuses_before_caller_or_skew():
 import ast
 source=Path(peer.__file__).with_name('server.py').read_text()
 tree=ast.parse(source);function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_serve_line')
 start=next(i for i,n in enumerate(function.body) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='request_id' for t in n.targets))
 end=next(i for i,n in enumerate(function.body[start:],start) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='client_token' for t in n.targets))
 block=[n for n in function.body[start:end] if not isinstance(n,ast.ImportFrom)]
 wrapper=ast.parse('def gate(msg,transport_stream,engine_root,workflow_peer,_write_and_release,_untrusted_caller_response):\n pass').body[0]
 wrapper.body=block+[ast.Return(ast.Constant('would_dispatch'))]
 compiled=compile(ast.fix_missing_locations(ast.Module(body=[wrapper],type_ignores=[])),'actual_server_auth_block','exec');ns={};exec(compiled,ns)
 denied=[]
 def refuse(*args,**kwargs):raise peer.PrivatePeerRefused('fake kernel refusal')
 assert ns['gate']({'id':7},None,None,SimpleNamespace(authorize=refuse,PrivatePeerRefused=peer.PrivatePeerRefused),denied.append,lambda request_id:request_id) is None
 assert denied==[7]
 caller=next(i for i,n in enumerate(function.body) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='caller_payload' for t in n.targets))
 assert end<caller
 handle=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_handle_connection')
 assert any(isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='_serve_line' and any(k.arg=='transport_stream' and isinstance(k.value,ast.Name) and k.value.id=='io' for k in n.keywords) for n in ast.walk(handle))

def test_omitted_default_alias_preserves_wire(case):
 case.row['allowed_cli'].append('coordinator-invoke');case.write();alias=case.alias.with_name('coordinator-invoke.exe');alias.write_bytes(case.alias.read_bytes());case.processes[30].image=str(alias)
 allow=Path(peer.__file__).resolve().parents[1]/'ops/warm_entrypoint_allowlist.json';allow.write_text(json.dumps({'entrypoints':['sizing-assemble','coordinator-invoke']}))
 case.msg['params'].pop('entrypoint')
 assert peer.authorize(None,case.msg,engine_root=case.engine)['session_id']==SID
 assert 'entrypoint' not in case.msg['params']
@pytest.mark.parametrize('kind',['other_roster','other_bin','duplicate_uuid','parent_uuid'])
def test_exact_fire_binding_and_uuid_uniqueness(case,monkeypatch,kind):
 if kind=='other_roster':
  other=case.path.with_name('other.json');other.write_text(case.path.read_text());monkeypatch.setenv(peer.PRIVATE_ROSTER_ENV,str(other))
 if kind=='other_bin':case.row['private_bin']=str(case.alias.parent.parent/'other-private-bin');case.write()
 if kind=='duplicate_uuid':
  import copy
  other=copy.deepcopy(case.row['workers'][0]);other['pid']=21;other['birth']=21.0;case.row['workers'].append(other);case.write()
 if kind=='parent_uuid':case.row['workers'][0]['native_uuid']=PARENT;case.write()
 with pytest.raises(peer.PrivatePeerRefused):peer.authorize(None,case.msg,engine_root=case.engine)
def test_reviewed_full_cli_catalog_bound(case):
 case.row['allowed_cli']=['cli-'+str(i) for i in range(391)];case.write()
 assert len(peer.load_roster(case.path)['allowed_cli'])==391
 case.row['allowed_cli']=['cli-'+str(i) for i in range(513)];case.write()
 with pytest.raises(peer.PrivatePeerRefused):peer.load_roster(case.path)

@pytest.mark.parametrize('kind',['inactive','executor','backend','owner','ownerbirth','caller','repo','scriptdigest','sourcedigest','stamp','scriptdrift','scriptoutside','missingrecord','provider'])
def test_existing_assignment_proof_required(case,kind):
 if kind=='inactive':case.record['state']='exited'
 if kind=='executor':case.record['executor']='claude'
 if kind=='backend':case.record['backend']='claude'
 if kind=='owner':case.record['pid']=99
 if kind=='ownerbirth':case.record['pid_birth']=99.0
 if kind=='caller':case.record['caller_session_id']=SID
 if kind=='repo':case.record['repo_root']=str(case.repo.parent)
 if kind=='scriptdigest':case.record['sha256']='f'*64
 if kind=='sourcedigest':case.record['source_sha']='f'*40
 if kind=='stamp':(case.engine/'coordinator_core/_engine_stamp').write_text('sha:'+'f'*40)
 if kind=='scriptdrift':case.script.write_text('return {changed:true};')
 if kind=='scriptoutside':case.record['script_path']=str(case.image)
 if kind=='provider':case.msg['_env']={'COORDINATOR_AGENT_PROVIDER':'claude'}
 case.fire_record.write_text(json.dumps(case.record))
 if kind=='missingrecord':case.fire_record.unlink()
 with pytest.raises(peer.PrivatePeerRefused):peer.authorize(None,case.msg,engine_root=case.engine)
def test_ready_binds_actual_protected_receipt(case):
 assert peer._validate_assignment(case.row)==case.record
 assert peer.authorize(None,case.msg,engine_root=case.engine)['session_id']==SID
 assert case.msg['_env']['COORDINATOR_AGENT_PROVIDER']=='codex'

@pytest.mark.parametrize('dead_pid',[10,20])
def test_psutil_visible_kernel_dead_refused(case,monkeypatch,dead_pid):
 monkeypatch.setattr(peer,'kernel_live_pid',lambda pid:pid!=dead_pid)
 assert case.processes[dead_pid].is_running() is True
 with pytest.raises(peer.PrivatePeerRefused):peer.authorize(None,case.msg,engine_root=case.engine)
@pytest.mark.parametrize('handle,wait,expected',[(0,0x102,False),(123,0,False),(123,0xffffffff,False),(123,0x102,True)])
def test_kernel_query_only_and_always_close(monkeypatch,handle,wait,expected):
 calls=[]
 class API:
  def OpenProcess(self,rights,inherit,pid):calls.append(('open',rights,inherit,pid));return handle
  def WaitForSingleObject(self,h,millis):calls.append(('wait',h,millis));return wait
  def CloseHandle(self,h):calls.append(('close',h));return True
 monkeypatch.setattr(peer,'sys',SimpleNamespace(platform='win32'));monkeypatch.setattr(peer,'_kernel_api',lambda:API())
 assert peer.kernel_live_pid(999)==expected
 assert calls[0]==('open',0x101000,False,999)
 assert calls[1:]==([('wait',123,0),('close',123)] if handle else [])
def test_kernel_query_failure_closes_handle(monkeypatch):
 closed=[]
 class API:
  def OpenProcess(self,*args):return 123
  def WaitForSingleObject(self,*args):raise OSError('denied')
  def CloseHandle(self,h):closed.append(h);return True
 monkeypatch.setattr(peer,'sys',SimpleNamespace(platform='win32'));monkeypatch.setattr(peer,'_kernel_api',lambda:API())
 assert peer.kernel_live_pid(999) is False;assert closed==[123]
def test_readonly_native_own_process_kernel_query():
 import os
 if sys.platform!='win32':pytest.skip('Windows kernel proof only')
 assert peer.kernel_live_pid(os.getpid()) is True

def test_verified_actor_uses_native_worker_not_transport_alias(case):
 import dataclasses,pickle
 result=peer.authorize(None,case.msg,engine_root=case.engine)
 actor=result['verified_native_actor']
 assert actor.session_id==SID and actor.pid==20 and actor.start_epoch==20.0
 assert actor.repo_root==str(case.repo.resolve()) and actor.exe==str(case.image.resolve())
 assert actor.exe_sha256==case.row['workers'][0]['exe_sha256']
 assert actor.parent_session_id==PARENT and actor.fire_id==case.row['fire_id'] and actor.request_id=='r1'
 assert result['pid']==30 and case.msg['_caller']['pid']=='30'
 assert pickle.loads(pickle.dumps(actor))==actor
 with pytest.raises(dataclasses.FrozenInstanceError):actor.pid=30

def test_wire_actor_cannot_replace_authenticated_actor(case):
 case.msg['_verified_native_actor']={'session_id':PARENT,'pid':30}
 case.msg['_caller']['verified_native_actor']={'session_id':PARENT,'pid':30}
 result=peer.authorize(None,case.msg,engine_root=case.engine)
 assert result['verified_native_actor'].session_id==SID and result['verified_native_actor'].pid==20

def test_server_actor_attachment_and_both_dispatch_bindings():
 import ast
 tree=ast.parse(Path(peer.__file__).with_name('server.py').read_text())
 serve=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_serve_line')
 merged=next(n.lineno for n in ast.walk(serve) if isinstance(n,ast.Call) and ast.unparse(n.func)=='caller_context.merge_env_axis')
 attach=next(n.lineno for n in ast.walk(serve) if isinstance(n,ast.Call) and ast.unparse(n.func)=='caller_context.with_verified_native_actor')
 assert merged<attach
 for name in ('_run_dispatch','_pool_dispatch_worker_body'):
  fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name)
  calls=[n for n in ast.walk(fn) if isinstance(n,ast.Call) and ast.unparse(n.func)=='per_request_state']
  assert len(calls)==1
  assert any(k.arg=='verified_native_actor' and 'caller.verified_native_actor' in ast.unparse(k.value) for k in calls[0].keywords)
