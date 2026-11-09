"""Mocked CLI only: no executable, daemon, service or model is launched."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import pytest

_BIN=Path(__file__).resolve().parent
if not (_BIN/'unimatrix-peer-codex.py').is_file(): _BIN=_BIN.parent

spec=importlib.util.spec_from_file_location('candidate_peer_codex',_BIN/'unimatrix-peer-codex.py')
pcx=importlib.util.module_from_spec(spec);spec.loader.exec_module(pcx)

def row(role,text,phase=None):
    payload={'type':'message','role':role,'content':[{'type':'input_text' if role=='user' else 'output_text','text':text}]}
    if phase:payload['phase']=phase
    return {'type':'response_item','payload':payload}

def setup_cli(monkeypatch,*,echo=True,reply='answer',prior=False,code=0,phase='final_answer'):
    rows=[row('assistant','OLD','final_answer')];calls=[]
    monkeypatch.setattr(pcx,'codex_binary',lambda:'C:/native codex/codex.exe')
    monkeypatch.setattr(pcx,'rollout_path',lambda tid:Path('mock-rollout'))
    monkeypatch.setattr(pcx,'rollout_rows',lambda path:list(rows))
    class Tail:
        def __init__(self,thread_id):self.offset=len(rows)
        def rows(self):
            new=rows[self.offset:];self.offset=len(rows);return iter(new)
    monkeypatch.setattr(pcx,'RolloutTail',Tail)
    def run(argv,**kwargs):
        calls.append((argv,kwargs,os.environ.copy()))
        if prior:rows.append(row('assistant','EARLIER','final_answer'))
        if echo:rows.append(row('user','wrapped hello'))
        rows.append(row('assistant',reply,phase))
        return subprocess.CompletedProcess(argv,code,'queued','failure' if code else '')
    monkeypatch.setattr(pcx.subprocess,'run',run)
    return calls

def test_global_remote_argv_and_inherited_context(monkeypatch):
    monkeypatch.setenv('CODEX_HOME','C:/kept/home');monkeypatch.setenv('CODEX_THREAD_ID','parent-kept')
    monkeypatch.setenv('CODEX_API_KEY','test-only-not-a-secret')
    calls=setup_cli(monkeypatch)
    assert asyncio.run(pcx.queue_turn('native-uuid','hello',timeout=.02,interval=.001))=='answer'
    argv,kwargs,env=calls[0]
    assert argv==['C:/native codex/codex.exe','--remote','unix://','queue','--thread','native-uuid','--message','hello']
    assert 'env' not in kwargs
    assert env['CODEX_HOME']=='C:/kept/home' and env['CODEX_THREAD_ID']=='parent-kept'
    assert env['CODEX_API_KEY']=='test-only-not-a-secret'
    assert len(calls)==1

def test_old_inflight_reply_before_user_echo_is_ignored(monkeypatch):
    setup_cli(monkeypatch,prior=True)
    assert asyncio.run(pcx.queue_turn('native-uuid','hello',timeout=.02,interval=.001))=='answer'

@pytest.mark.parametrize('echo,phase',[(False,'final_answer'),(True,'commentary')])
def test_enqueue_is_not_reply_or_consumption(monkeypatch,echo,phase):
    calls=setup_cli(monkeypatch,echo=echo,phase=phase)
    assert asyncio.run(pcx.queue_turn('native-uuid','hello',timeout=.005,interval=.001))==pcx.NO_REPLY
    assert len(calls)==1

def test_managed_remote_failure_has_no_embedded_retry(monkeypatch):
    calls=setup_cli(monkeypatch,code=2)
    with pytest.raises(RuntimeError,match='codex queue failed'):
        asyncio.run(pcx.queue_turn('native-uuid','hello',timeout=.02,interval=.001))
    assert len(calls)==1

def test_tui_route_uses_remote_queue_without_socket_probe(monkeypatch):
    calls=setup_cli(monkeypatch)
    async def forbidden(*args,**kwargs):raise AssertionError('TUI route must not open socket')
    monkeypatch.setattr(pcx.WsClient,'connect',forbidden)
    assert asyncio.run(pcx.codex_turn('unused.sock','native-uuid','hello',tui_held=True))=='answer'
    assert calls[0][0][1:4]==['--remote','unix://','queue']
