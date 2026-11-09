"""Real C-temporary rollout bytes, mocked CLI only; no native queue/model/server."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import pytest

_BIN=Path(__file__).resolve().parent
if not (_BIN/'unimatrix-peer-codex.py').is_file(): _BIN=_BIN.parent

spec=importlib.util.spec_from_file_location('tail_candidate',_BIN/'unimatrix-peer-codex.py')
M=importlib.util.module_from_spec(spec);spec.loader.exec_module(M)

def event(role,text,phase=None):
    p={'type':'message','role':role,'content':[{'type':'input_text' if role=='user' else 'output_text','text':text}]}
    if phase:p['phase']=phase
    return (json.dumps({'type':'response_item','payload':p},ensure_ascii=False)+'\n').encode()

@pytest.fixture
def setup(tmp_path,monkeypatch):
    path=tmp_path/'rollout.jsonl';path.write_bytes(event('assistant','OLD','final_answer'))
    monkeypatch.setattr(M,'rollout_path',lambda tid:path if path.exists() else None)
    monkeypatch.setattr(M,'codex_binary',lambda:'mock-codex')
    monkeypatch.setattr(M,'rollout_rows',lambda *a:pytest.fail('full history must never be parsed'))
    return path,monkeypatch

def cli(monkeypatch,action,code=0):
    calls=[]
    def run(argv,**kwargs):
        calls.append((argv,kwargs));action()
        return subprocess.CompletedProcess(argv,code,'queued','refused' if code else '')
    monkeypatch.setattr(M.subprocess,'run',run)
    return calls

def test_large_existing_prefix_never_read_or_parsed(setup):
    path,mp=setup;path.write_bytes(b'x'*(4*1024*1024)+b'\n')
    seen=[];original_open=Path.open
    class Reader:
        def __init__(self,s):self.s=s
        def __enter__(self):return self
        def __exit__(self,*a):return self.s.__exit__(*a)
        def __getattr__(self,n):return getattr(self.s,n)
        def read(self,n=-1):seen.append((self.s.tell(),n));return self.s.read(n)
    def opened(p,*a,**k):
        s=original_open(p,*a,**k)
        return Reader(s) if p==path and a and a[0]=='rb' else s
    mp.setattr(Path,'open',opened)
    def append():
        with original_open(path,'ab') as s:s.write(event('user','hello')+event('assistant','NEW','final_answer'))
    calls=cli(mp,append)
    assert asyncio.run(M.queue_turn('t','hello',timeout=.02,interval=.001))=='NEW'
    assert seen and all(offset>=4*1024*1024+1 and 0<n<=M.RolloutTail.CHUNK_BYTES for offset,n in seen)
    assert calls[0][0][1:4]==['--remote','unix://','queue']

def test_fragmented_utf8_and_json_line_survives_polls(setup):
    path,mp=setup;tail=M.RolloutTail('t');line=event('user','héllo');split=line.index('é'.encode())+1
    with path.open('ab') as s:s.write(line[:split])
    assert list(tail.rows())==[]
    with path.open('ab') as s:s.write(line[split:])
    assert M.user_text(list(tail.rows())[0])=='héllo'
    assert list(tail.rows())==[]

def test_stream_state_ignores_inflight_and_remembers_echo(setup):
    path,mp=setup
    def append():
        with path.open('ab') as s:s.write(event('assistant','INFLIGHT','final_answer')+event('user','hello'))
    calls=cli(mp,append);slept=[]
    async def sleep(_):
        slept.append(True)
        with path.open('ab') as s:s.write(event('assistant','AFTER','final_answer'))
    mp.setattr(M.asyncio,'sleep',sleep)
    assert asyncio.run(M.queue_turn('t','hello',timeout=.1,interval=.001))=='AFTER'
    assert len(calls)==1 and slept

@pytest.mark.parametrize('change',['truncate','replace','disappear'])
def test_changed_generation_fails_closed_without_resend(setup,change):
    path,mp=setup
    def action():
        if change=='truncate':path.write_bytes(b'')
        elif change=='replace':
            other=path.with_suffix('.new');other.write_bytes(event('user','hello')+event('assistant','BAD','final_answer'));os.replace(other,path)
        else:path.unlink()
    calls=cli(mp,action)
    assert asyncio.run(M.queue_turn('t','hello',timeout=.01,interval=.001))==M.NO_REPLY
    assert len(calls)==1

def test_missing_initial_rollout_still_enqueues_but_never_accepts_old_history(setup):
    path,mp=setup;path.unlink()
    calls=cli(mp,lambda:path.write_bytes(event('user','hello')+event('assistant','NEW','final_answer')))
    assert asyncio.run(M.queue_turn('t','hello',timeout=.01,interval=.001))==M.NO_REPLY
    assert len(calls)==1

def test_unreadable_observer_still_enqueues_once(setup):
    path,mp=setup
    def unavailable(*args):raise PermissionError('observer denied')
    mp.setattr(M,'rollout_path',unavailable)
    calls=cli(mp,lambda:None)
    assert asyncio.run(M.queue_turn('t','hello',timeout=.01,interval=.001))==M.NO_REPLY
    assert len(calls)==1

def test_rollout_discovery_occurs_once_across_polls(setup):
    path,mp=setup;discover=[]
    def known(tid):discover.append(tid);return path
    mp.setattr(M,'rollout_path',known)
    tail=M.RolloutTail('t')
    assert list(tail.rows())==[] and list(tail.rows())==[]
    with path.open('ab') as s:s.write(event('user','hello'))
    assert len(list(tail.rows()))==1
    assert discover==['t']

def test_queue_error_has_no_retry_or_history_poll(setup):
    path,mp=setup;calls=cli(mp,lambda:None,code=2)
    mp.setattr(M.RolloutTail,'rows',lambda *a:pytest.fail('must not poll after queue error'))
    with pytest.raises(RuntimeError,match='codex queue failed'):
        asyncio.run(M.queue_turn('t','hello',timeout=.01,interval=.001))
    assert len(calls)==1

def test_poll_byte_and_partial_line_limits(setup):
    path,mp=setup;tail=M.RolloutTail('t')
    mp.setattr(tail,'MAX_POLL_BYTES',16);mp.setattr(tail,'MAX_LINE_BYTES',20)
    with path.open('ab') as s:s.write(b'x'*40)
    assert list(tail.rows())==[] and len(tail.partial)==16
    with pytest.raises(ValueError,match='line exceeds'):list(tail.rows())
