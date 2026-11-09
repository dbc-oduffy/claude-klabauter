"""Acceptance CLI wiring only: Git and workflow dispatch are always fake."""
import json
import os
from pathlib import Path
import subprocess

import pytest

from coordinator_core.git import run as shared_git
from coordinator_core.ipc import dispatch_message
from coordinator_core.ops.workflow_fire import acceptance


@pytest.fixture
def fake_acceptance(tmp_path, monkeypatch):
    calls=[];requests=[]
    def git(words,**kwargs):
        calls.append((words,kwargs))
        if words==['init','-q']:(Path(kwargs['cwd'])/'.git').mkdir()
        return shared_git.GitResult(0,'','',False)
    async def dispatch(message):
        requests.append(message)
        return {'result':{'submitted':True,'run_id':'fake-run'}}
    monkeypatch.setattr(shared_git,'run_git',git)
    monkeypatch.setattr('coordinator_core.ipc.dispatch_message',dispatch)
    def forbidden(*args,**kwargs):raise AssertionError('private/native subprocess.run is forbidden')
    monkeypatch.setattr(subprocess,'run',forbidden)
    args=['--workspace',str(tmp_path/'workspace'),'--session-id','real-caller-test','--caller','codex-em']
    return args,calls,requests


def test_acceptance_defaults_route_git_through_ssot_and_preserve_request(fake_acceptance,capsys):
    args,calls,requests=fake_acceptance
    acceptance.main(args)
    result=json.loads(capsys.readouterr().out)
    assert [call[0] for call in calls]==[['init','-q'],['add','README.md'],['commit','-q','-m','seed isolated native Workflow acceptance']]
    assert len({call[1]['cwd'] for call in calls})==1
    message=requests[0];request=message['params']
    assert message['method']=='engine_ask.submit'
    assert request['session_id']=='real-caller-test' and request['caller']=='codex-em'
    assert request['fire'] is True and request['executor']=='headless' and request['backend']=='codex'
    assert 'private_engine' not in request
    root=Path(result['repo_root'])
    assert (root/'README.md').read_text()=='# Native Workflow acceptance\n'
    assert json.loads((root/'.git/acceptance-request.json').read_text())==request
    assert json.loads((root/'.git/acceptance-submitted.json').read_text())['submitted'] is True


@pytest.mark.skipif(os.name!='nt',reason='private native backend is Windows-only')
def test_acceptance_explicit_private_paths_reach_submit_unchanged(fake_acceptance,tmp_path):
    args,_,requests=fake_acceptance
    paths={name:str(tmp_path/name) for name in ('door_image','door_provenance','shared_engine_root')}
    for name,value in paths.items():args += ['--'+name.replace('_','-'),value]
    acceptance.main(args)
    assert requests[0]['params']['private_engine']==paths


@pytest.mark.parametrize('flags',[['--door-image','C:/fixture/door.exe'],
    ['--door-image','relative.exe','--door-provenance','C:/fixture/proof.json','--shared-engine-root','C:/fixture/shared']])
def test_invalid_private_config_refuses_before_git_or_repository_creation(fake_acceptance,tmp_path,flags):
    args,calls,requests=fake_acceptance
    with pytest.raises(SystemExit) as exc:acceptance.main(args+flags)
    assert exc.value.code==2 and calls==[] and requests==[]
    assert not (tmp_path/'workspace').exists()


@pytest.mark.parametrize('returncode,timed_out',[(128,False),(-1,True)])
def test_git_failure_and_timeout_abort_before_submission(fake_acceptance,monkeypatch,returncode,timed_out):
    args,calls,requests=fake_acceptance
    failed=[]
    def git(words,**kwargs):
        failed.append(words)
        return shared_git.GitResult(returncode,'','fake diagnostic',timed_out)
    monkeypatch.setattr(shared_git,'run_git',git)
    with pytest.raises(subprocess.CalledProcessError) as exc:acceptance.main(args)
    assert exc.value.returncode==returncode
    assert exc.value.cmd==['git','init','-q'] and exc.value.stderr=='fake diagnostic'
    assert failed==[['init','-q']] and requests==[]
