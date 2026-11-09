"""Prepared offline fixtures; no CLI, socket, live DB, process or model calls."""
import asyncio,importlib.util,json,sqlite3
from pathlib import Path
from types import SimpleNamespace
import pytest

_BIN=Path(__file__).resolve().parent
if not (_BIN/'unimatrix-peer-router.py').is_file(): _BIN=_BIN.parent

spec=importlib.util.spec_from_file_location('router1360',_BIN/'unimatrix-peer-router.py')
R=importlib.util.module_from_spec(spec);spec.loader.exec_module(R)
ROOT='01a1b63b-acfb-78d1-86f2-a74ad6ca4572'
CHILD='01a1b821-54be-7281-a3c1-cfc39dd82854'
UNKNOWN='11111111-1111-4111-8111-111111111111'
# Actual local SQLite fields relayed by root; the UUID is the child's target,
# not a parent address. No test infers parent routing from this payload.
CHILD_SOURCE=json.dumps({'subagent':{'thread_spawn':{'parent_thread_id':ROOT,'depth':1,'agent_path':'/root/headless_backend','agent_nickname':'Gibbs','agent_role':None}}})
def rows(cwd):
 return {ROOT:{'id':ROOT,'cwd':str(cwd),'source':'vscode','thread_source':'user','agent_path':None},CHILD:{'id':CHILD,'cwd':str(cwd),'source':CHILD_SOURCE,'thread_source':'subagent','agent_path':'/root/headless_backend'}}

@pytest.mark.parametrize('source',['vscode','cli','"vscode"'])
def test_explicit_user_source_is_positive_not_lock_inference(source):
 assert R.codex_is_sendable_root({'source':source,'thread_source':'user','agent_path':None})

@pytest.mark.parametrize('metadata',[{}, {'source':None},{'source':'unknown'},{'source':'appServer'},{'source':'exec'}, {'source':'{broken'}, {'source':'[]'},{'source':'1'}, {'source':{'new_kind':'user'}}, {'source':'vscode','thread_source':'unknown'},{'source':'vscode','thread_source':'subagent'}, {'source':'vscode','agent_path':'/root/child'}])
def test_unknown_or_contradictory_metadata_fails_closed(metadata):
 assert R.codex_is_sendable_root(metadata) is False

@pytest.mark.parametrize('metadata',[{'source':CHILD_SOURCE,'thread_source':'subagent','agent_path':'/root/headless_backend'},{'source':json.loads(CHILD_SOURCE)},{'source':'subagent'},{'thread_source':'subagent'},{'source':'vscode','agent_path':'/root/child'}])
def test_actual_child_and_existing_supported_child_discriminators(metadata):
 assert R.codex_is_subagent(metadata) is True
 assert R.codex_is_sendable_root(metadata) is False

def test_real_sqlite_schema_reader_preserves_discriminators_read_only(tmp_path):
 db=tmp_path/'state_5.sqlite'
 with sqlite3.connect(db) as conn:
  conn.execute('CREATE TABLE threads(id TEXT,cwd TEXT,source TEXT,thread_source TEXT,agent_path TEXT)')
  for row in rows(tmp_path).values():conn.execute('INSERT INTO threads VALUES(?,?,?,?,?)',tuple(row.values()))
 before=db.read_bytes();got=R.codex_thread_metadata(tmp_path)
 assert got==rows(tmp_path) and db.read_bytes()==before
 assert R.codex_is_sendable_root(got[ROOT]) and not R.codex_is_sendable_root(got[CHILD])

def test_old_sqlite_schema_missing_source_is_not_assumed_root(tmp_path):
 with sqlite3.connect(tmp_path/'state_4.sqlite') as conn:
  conn.execute('CREATE TABLE threads(id TEXT,cwd TEXT)');conn.execute('INSERT INTO threads VALUES(?,?)',(ROOT,str(tmp_path)))
 assert R.codex_is_sendable_root(R.codex_thread_metadata(tmp_path)[ROOT]) is False

@pytest.fixture
def router(tmp_path,monkeypatch):
 metadata=rows(tmp_path);monkeypatch.setattr(R,'codex_thread_metadata',lambda home:metadata)
 monkeypatch.setattr(R,'endpoint_path',lambda seed,endpoint_dir=None:str(tmp_path/(seed+'.sock')))
 async def forbidden(*a,**kw):pytest.fail('No native adapter/fallback may run for excluded child')
 peers=[{'name':'root-face','vendor':'codex','target':ROOT},{'name':'child-face','vendor':'codex','target':CHILD,'aliases':['child-readable']},{'name':'unknown-face','vendor':'codex','target':UNKNOWN},{'name':'agy-face','vendor':'agy','target':'agy-target'}]
 obj=R.Router(peers,sessions_dir=tmp_path,state_dir=tmp_path,endpoint_dir=tmp_path,adapters={'codex':forbidden,'agy':forbidden},codex_locks_dir=tmp_path/'locks')
 return obj,metadata

def test_fixed_peer_configuration_cannot_bypass_root_eligibility(router):
 obj,metadata=router
 assert set(obj.peers)=={'root-face','agy-face'}
 assert obj.pick_peer(ROOT)=='root-face' and obj.peers['root-face']['target']==ROOT
 for alias in [CHILD,'codex-'+CHILD,'child-face','child-readable',UNKNOWN,'unknown-face']:
  with pytest.raises(ValueError,match='independently sendable root'):obj.pick_peer(alias)

@pytest.mark.parametrize('alias',[CHILD,'codex-'+CHILD,'child-face','child-readable'])
def test_fixed_child_send_refuses_without_parent_retarget_or_fallback(router,alias,monkeypatch):
 obj,_=router
 async def forbidden(*a,**kw):pytest.fail('Excluded child must not reach transport fallback')
 monkeypatch.setattr(obj,'_transport_send_to_session',forbidden)
 got=asyncio.run(obj._dispatch({'op':'send','to':alias,'text':'substantive nonce payload'}))
 assert got['ok'] is False and 'independently sendable root' in got['error']

def test_turn_rechecks_per_uuid_source_before_native_adapter(router):
 obj,metadata=router;metadata[ROOT]={'source':CHILD_SOURCE,'thread_source':'subagent'}
 assert obj.peer_readiness()['root-face'] is False
 assert 'not an independently sendable root' in asyncio.run(obj.turn('root-face','message'))

def test_lock_discovery_applies_same_positive_root_classifier(tmp_path,monkeypatch):
 locks=tmp_path/'locks';locks.mkdir();metadata=rows(tmp_path)
 for sid in [ROOT,CHILD,UNKNOWN]:(locks/(sid+'.lock')).write_text('inert lock fixture')
 monkeypatch.setattr(R,'codex_thread_metadata',lambda home:metadata);monkeypatch.setattr(R,'lock_held',lambda path:True)
 monkeypatch.setattr(R,'_contract',lambda:SimpleNamespace(read_codex_address=lambda *a:None))
 monkeypatch.setattr(R,'_roster',lambda:SimpleNamespace(settings_home=lambda:tmp_path))
 got=R.load_lock_peers(locks,'codex')
 assert len(got)==1 and got[0]['target']==ROOT
 assert all(row['target']!=CHILD for row in got)

def test_existing_non_codex_peer_with_uuid_target_is_unchanged(tmp_path,monkeypatch):
 monkeypatch.setattr(R,'endpoint_path',lambda *a:str(tmp_path/'control'))
 obj=R.Router([{'name':'agy-root','vendor':'agy','target':CHILD}],sessions_dir=tmp_path,state_dir=tmp_path,adapters={'agy':object()})
 assert obj.pick_peer(CHILD)=='agy-root'

@pytest.fixture
def fallback_roster(router,tmp_path,monkeypatch):
 obj,metadata=router
 # Canonical roster functions are mocked only at the process/field boundary;
 # source eligibility still uses the actual classifier and observed metadata.
 monkeypatch.setattr(R,'_roster',lambda:SimpleNamespace(
  session_identity=lambda e:{'vendor':e.get('vendor','unknown')},
  pid_alive=lambda pid:pid in (700,701),proc_start=lambda pid:'birth-'+str(pid),pid_domain=lambda:'test-domain'))
 monkeypatch.setattr(R,'_contract',lambda:SimpleNamespace(
  projected_addresses=lambda e:e.get('aliases',[]),
  build_frame=lambda *a:{'msg_id':'fixture-msg'},stamp=lambda f,*a:f))
 def put(name,**extra):
  row={'name':name,'vendor':'codex','pid':700,'procStart':'birth-700','pidDomain':'test-domain','messagingSocketPath':'fixture-local-endpoint',**extra}
  (tmp_path/(str(len(list(tmp_path.glob('*.json'))))+'.json')).write_text(json.dumps(row))
  return row
 return obj,metadata,put

def test_remote_legacy_codex_uuid_uses_verified_bridge_not_local_metadata(fallback_roster,monkeypatch):
 obj,metadata,put=fallback_roster;remote_sid='22222222-2222-4222-8222-222222222222';address='codex-'+remote_sid
 put('readable-remote-face',unimatrixFarName=address,unimatrixRemote='mac-mini-lan',macSocketPath='uds:/tmp/remote-face.sock',farPid=812,box='mac-mini',messagingSocketPath='fixture-bridge-endpoint')
 assert remote_sid not in metadata and obj.pick_peer(address) is None
 assert obj.resolve_sender(address)=='fixture-bridge-endpoint'
 observed=[]
 async def face(*a):return 'transport-fixture'
 async def deliver(peer,ep,frame):observed.append(ep)
 monkeypatch.setattr(obj,'_transport_face',face);monkeypatch.setattr(obj,'_deliver_as_peer',deliver)
 reply=asyncio.run(obj._dispatch({'op':'send','to':address,'text':'real payload nonce'}))
 assert reply['ok'] and observed==['fixture-bridge-endpoint']

@pytest.mark.parametrize('address',['child-custom-owned',CHILD,'codex-'+CHILD])
def test_discovered_child_custom_owned_alias_fails_at_resolved_identity_boundary(fallback_roster,address):
 obj,metadata,put=fallback_roster;obj.refused_codex_addresses.clear()
 put('stale-child-face',target=CHILD,session_id=CHILD,aliases=['child-custom-owned',CHILD,'codex-'+CHILD])
 assert obj.pick_peer(address) is None
 assert obj.resolve_sender(address,'unsafe-fallback')==''

def test_local_codex_missing_metadata_fails_closed_without_name_or_pid_sid_inference(fallback_roster):
 obj,metadata,put=fallback_roster
 put('unknown-local',target=UNKNOWN)
 assert obj.resolve_sender('unknown-local','unsafe-fallback')==''

def test_local_codex_face_with_only_uuid_name_never_infers_native_target(fallback_roster):
 obj,metadata,put=fallback_roster
 put('codex-'+ROOT,sessionId='peer-codex-face-700')
 assert obj.resolve_sender('codex-'+ROOT,'unsafe-fallback')==''

@pytest.mark.parametrize('change',['missing_far_socket','missing_box','same_box','dead_generation','wrong_domain'])
def test_unverified_remote_fields_cannot_bypass_local_source_gate(fallback_roster,change):
 obj,metadata,put=fallback_roster
 fields={'unimatrixRemote':'mac-mini-lan','macSocketPath':'uds:/tmp/far.sock','farPid':812,'box':'mac-mini'}
 if change=='missing_far_socket':del fields['macSocketPath']
 elif change=='missing_box':del fields['box']
 elif change=='same_box':fields['box']=R.socket.gethostname()
 elif change=='dead_generation':fields['procStart']='old-birth'
 else:fields['pidDomain']='foreign-domain'
 put('unverified-remote',**fields)
 assert obj.resolve_sender('unverified-remote','unsafe-fallback')==''

def test_ineligible_local_identity_never_retargets_to_same_named_remote(fallback_roster):
 obj,metadata,put=fallback_roster
 put('shared-address',target=CHILD)
 put('shared-address',unimatrixRemote='mac-mini-lan',macSocketPath='uds:/tmp/far.sock',farPid=812,box='mac-mini')
 assert obj.resolve_sender('shared-address')==''

def test_local_root_and_non_codex_resolved_sessions_are_preserved(fallback_roster):
 obj,metadata,put=fallback_roster
 put('external-root',target=ROOT);put('external-agy',vendor='agy',target=CHILD)
 assert obj.resolve_sender('external-root')=='fixture-local-endpoint'
 assert obj.resolve_sender('external-agy')=='fixture-local-endpoint'

@pytest.fixture
def registration_api(tmp_path,monkeypatch):
 monkeypatch.setattr(R,'_roster',lambda:SimpleNamespace(key_name=lambda pid,ep:str(pid)+'.key',proc_start=lambda pid:'birth-'+str(pid),pid_domain=lambda:'test-domain'))
 return tmp_path

def test_legacy_root_face_publishes_explicit_accepted_native_target(registration_api):
 name='codex-claude-klabauter-d3cfb00b01-on-machine-a'
 peer={'name':name,'vendor':'codex','target':ROOT}
 face=R.Registration(name,'fixture-pipe',str(registration_api),vendor='codex',peer=peer)
 assert face.data['target']==ROOT
 assert face.data['sessionId'].startswith('peer-') and face.data['sessionId']!=ROOT
 assert face.data['name']==name and face.data['nameSource']=='derived'
 assert 'address_version' not in face.data and 'role' not in face.data and 'session_id' not in face.data

@pytest.mark.parametrize('peer',[None,{}, {'name':'face','vendor':'codex'},{'name':'face','vendor':'codex','target':'not-a-uuid'},{'name':'face','vendor':'codex','target':ROOT,'session_id':CHILD},{'name':'other-name','vendor':'codex','target':ROOT},{'name':'face','vendor':'agy','target':ROOT}])
def test_codex_face_without_accepted_native_target_refuses_before_roster_write(registration_api,peer):
 with pytest.raises(ValueError,match='accepted peer native target'):R.Registration('face','fixture-pipe',str(registration_api),vendor='codex',peer=peer)
 assert list(registration_api.iterdir())==[]

def test_old_legitimate_local_root_face_without_native_binding_is_not_guessed(fallback_roster):
 obj,metadata,put=fallback_roster
 name='codex-claude-klabauter-d3cfb00b01-on-machine-a'
 put(name,sessionId='peer-'+name+'-3940')
 assert obj.resolve_sender(name,'unsafe-fallback')==''

def test_new_legacy_face_explicit_target_restores_same_name_root_route(fallback_roster):
 obj,metadata,put=fallback_roster
 name='codex-claude-klabauter-d3cfb00b01-on-machine-a'
 put(name,sessionId='peer-'+name+'-3940',target=ROOT)
 assert obj.resolve_sender(name)=='fixture-local-endpoint'

def test_known_child_inbound_refuses_once_before_background_task_or_adapter(router,monkeypatch):
 obj,metadata=router;metadata[ROOT]={'source':CHILD_SOURCE,'thread_source':'subagent'}
 def forbidden(*a,**kw):pytest.fail('Known child must refuse before scheduling any background task')
 monkeypatch.setattr(R.asyncio,'create_task',forbidden)
 before=set(obj.tasks)
 result=asyncio.run(obj._dispatch({'op':'inbound','peer':'root-face','frame':{'content':'substantive nonce'}}))
 assert result=={'ok':False,'error':'Codex target is not an independently sendable root'}
 assert obj.tasks==before
