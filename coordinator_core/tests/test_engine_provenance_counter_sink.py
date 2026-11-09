"""Declared provenance sink override: real temp writes, no Git or source mutation."""
import json
from pathlib import Path

import pytest

from coordinator_core import engine_provenance_counter as epc
from coordinator_core.session.declared_writes import collecting


@pytest.fixture
def origin(tmp_path,monkeypatch):
    monkeypatch.delenv(epc.ENGINE_PROVENANCE_SINK_ENV,raising=False)
    repo=tmp_path/'source';repo.mkdir()
    monkeypatch.setattr(epc,'resolve_git_root_cheap',lambda cwd=None:str(repo))
    monkeypatch.setattr(epc,'_may_write',lambda root:True)
    return repo


def call(verdict='match'):
    epc.record_engine_provenance('fixture-wrapper','dispatch',verdict,'fixture.py','fixture-engine')


def test_unset_override_preserves_default_path_and_record(origin):
    expected=origin/'state'/epc._COUNTS_FILENAME
    assert epc.resolve_engine_provenance_sink(str(origin))==expected
    with collecting() as declared:call()
    record=json.loads(expected.read_text())
    assert set(record)=={'caller','axis','verdict','imported_file','engine_root','at'}
    assert record['caller']=='fixture-wrapper' and record['verdict']=='match'
    assert declared==[str(expected)]


def test_actual_temp_sink_outside_source_is_used_and_declared(origin,tmp_path,monkeypatch):
    sink=tmp_path/'outside-source/counts.jsonl';seen=[]
    monkeypatch.setenv(epc.ENGINE_PROVENANCE_SINK_ENV,str(sink))
    monkeypatch.setattr(epc,'_may_write',lambda root:seen.append(root) or True)
    with collecting() as declared:call()
    assert json.loads(sink.read_text())['imported_file']=='fixture.py'
    assert epc.resolve_engine_provenance_sink(str(origin))==sink.resolve()
    assert declared==[str(sink.resolve())] and seen==[str(origin)]
    assert list(origin.iterdir())==[]


def test_override_does_not_bypass_origin_standing(origin,tmp_path,monkeypatch):
    sink=tmp_path/'outside/counts.jsonl';seen=[]
    monkeypatch.setenv(epc.ENGINE_PROVENANCE_SINK_ENV,str(sink))
    monkeypatch.setattr(epc,'_may_write',lambda root:seen.append(root) or False)
    call()
    assert seen==[str(origin)] and not sink.exists() and list(origin.iterdir())==[]


@pytest.mark.parametrize('kind',['empty','relative','whitespace','parent-traversal','directory'])
def test_invalid_explicit_sink_raises_without_source_fallback(origin,tmp_path,monkeypatch,kind):
    invalid={'empty':'','relative':'counts.jsonl','whitespace':' '+str(tmp_path/'counts.jsonl'),
             'parent-traversal':str(tmp_path/'sub/../counts.jsonl'),'directory':str(tmp_path)}[kind]
    monkeypatch.setenv(epc.ENGINE_PROVENANCE_SINK_ENV,invalid)
    with collecting() as declared:
        with pytest.raises(ValueError,match=epc.ENGINE_PROVENANCE_SINK_ENV):call()
    assert declared==[] and list(origin.iterdir())==[]


def test_pytest_protects_resolved_own_counter_even_for_other_origin(origin,tmp_path,monkeypatch):
    own=tmp_path/'own-source';own.mkdir()
    sink=own/'state'/epc._COUNTS_FILENAME
    monkeypatch.setattr(epc,'_OWN_REPO_ROOT',own)
    monkeypatch.setenv('PYTEST_CURRENT_TEST','fixture')
    monkeypatch.setenv(epc.ENGINE_PROVENANCE_SINK_ENV,str(sink))
    call()
    assert not sink.exists() and list(origin.iterdir())==[]


def test_pytest_allows_explicit_temp_override_from_own_source(origin,tmp_path,monkeypatch):
    sink=tmp_path/'outside-source/counts.jsonl'
    monkeypatch.setattr(epc,'_OWN_REPO_ROOT',origin)
    monkeypatch.setenv('PYTEST_CURRENT_TEST','fixture')
    monkeypatch.setenv(epc.ENGINE_PROVENANCE_SINK_ENV,str(sink))
    call()
    assert sink.is_file() and list(origin.iterdir())==[]


def test_pytest_default_own_counter_remains_protected(origin,monkeypatch):
    monkeypatch.setattr(epc,'_OWN_REPO_ROOT',origin)
    monkeypatch.setenv('PYTEST_CURRENT_TEST','fixture')
    call()
    assert list(origin.iterdir())==[]


def test_override_obeys_existing_soft_and_hard_caps(origin,tmp_path,monkeypatch):
    sink=tmp_path/'bounded.jsonl'
    monkeypatch.setenv(epc.ENGINE_PROVENANCE_SINK_ENV,str(sink))
    sink.write_bytes(b'x'*epc._MATCH_CAP_BYTES)
    with collecting() as declared:call('match')
    assert sink.stat().st_size==epc._MATCH_CAP_BYTES and declared==[]
    with collecting() as declared:call('divergent')
    assert sink.stat().st_size>epc._MATCH_CAP_BYTES and declared==[str(sink)]
    sink.write_bytes(b'x'*epc._HARD_CAP_BYTES)
    with collecting() as declared:call('unresolved')
    assert sink.stat().st_size==epc._HARD_CAP_BYTES and declared==[]
    assert list(origin.iterdir())==[]


def test_unresolved_origin_still_writes_nothing(origin,tmp_path,monkeypatch):
    sink=tmp_path/'outside.jsonl'
    monkeypatch.setenv(epc.ENGINE_PROVENANCE_SINK_ENV,str(sink))
    monkeypatch.setattr(epc,'resolve_git_root_cheap',lambda cwd=None:None)
    call()
    assert not sink.exists()
