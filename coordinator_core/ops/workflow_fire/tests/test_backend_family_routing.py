"""End-to-end stage routing per backend: engine_ask.submit -> fire params -> fired script's agent stages.

The spawn is stubbed at codex_runner.workflow._run_admitted, the seam the production run_agent calls with
the built argv, so the routing through record['backend'] is exercised for real.
"""
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from coordinator_core.codex_runner import workflow as runner
from coordinator_core.ops.dispatch_emit import stage_family
from coordinator_core.ops.workflow_fire import backends, headless
from coordinator_core.ops import engine_ask_submit

STAGES = ('sizing', 'plan', 'execute', 'review', 'landing')
EXE = {'codex': 'fake-codex', 'grok': 'fake-grok', 'agy': 'fake-agy'}
SCRIPT = ("export const meta={};\n"
          + "".join(f"await phase('{s}'); await agent('do {s}',{{label:'{s}'}});\n" for s in STAGES)
          + "return {ok:true};")
SUBMIT = {'ask': 'x', 'ask_source': 'relayed', 'session_id': 'sess', 'caller': 'c', 'fire': True}


def test_backends_set_equals_stage_family():
    assert set(backends.BACKENDS) == set(stage_family.FAMILIES)


def _submit(tmp_path, monkeypatch, **extra):
    fired = []
    import coordinator_core.hooks._engine_ask_gates as gates
    import coordinator_core.ops.workflow_fire.op as op
    monkeypatch.setattr(gates, 'submit_ask', lambda **k: {'script_path': 'run.mjs', 'sha256': 'abc', 'run_id': 'r1'})
    monkeypatch.setattr(op, '_workflow_fire', lambda params, cwd: fired.append(params) or {'fired': True})
    reply = engine_ask_submit._engine_ask_submit({**SUBMIT, 'cwd': str(tmp_path), **extra})
    return reply, fired


def _fire_record(tmp_path, backend):
    source = tmp_path / 'run.mjs'
    source.write_text(SCRIPT, encoding='utf-8')
    artifacts = tmp_path / 'artifacts'
    artifacts.mkdir()
    (artifacts / 'start').touch()
    path = tmp_path / 'record.json'
    path.write_text(json.dumps({
        'fire_id': 'test', 'script_path': str(source), 'script_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'repo_root': str(tmp_path), 'artifact_dir': str(artifacts), 'backend': backend,
        'journal_path': str(artifacts / 'journal.jsonl'), 'timeout_s': 30}), encoding='utf-8')
    return path


def _stub_spawn(monkeypatch, spawned):
    for name, attr in (('grok', 'resolve_grok'), ('agy', 'resolve_agy'), ('codex', 'resolve_codex')):
        monkeypatch.setattr(runner, attr, lambda *a, _n=name, **k: [EXE[_n]])
    monkeypatch.setattr(runner, 'preflight_auth', lambda **k: None)

    def fake(argv, prompt, *, backend, artifact_dir, stdout, **kw):
        spawned.append((backend, argv[0]))
        if backend == 'grok':
            stdout.write(json.dumps({'text': 'done'}).encode())
        elif backend == 'agy':
            stdout.write(json.dumps({'event': 'result', 'result': {'status': 'success', 'response': 'done'}}).encode())
        else:
            (artifact_dir / 'last.txt').write_text('done', encoding='utf-8')
        return SimpleNamespace(pid=1, returncode=0)
    monkeypatch.setattr(runner, '_run_admitted', fake)


@pytest.mark.skipif(not shutil.which('node'), reason='node unavailable')
@pytest.mark.parametrize('backend', ['grok', 'agy', None])
def test_every_stage_routes_to_the_named_family_only(tmp_path, monkeypatch, backend):
    extra = {} if backend is None else {'backend': backend}
    reply, fired = _submit(tmp_path, monkeypatch, **extra)
    assert 'refused' not in reply and len(fired) == 1
    expected = backend or 'codex'
    assert fired[0]['backend'] == expected
    spawned = []
    _stub_spawn(monkeypatch, spawned)
    result = headless.execute(_fire_record(tmp_path, fired[0]['backend']), terminal_runner=lambda *a: {})
    assert result['exit_code'] == 0, result.get('error')
    assert spawned == [(expected, EXE[expected])] * len(STAGES)


def test_unknown_backend_refused_and_never_fires(tmp_path, monkeypatch):
    reply, fired = _submit(tmp_path, monkeypatch, backend='gemini')
    assert reply['refused'] == 'bad-request' and 'grok' in reply['reason'] and 'agy' in reply['reason']
    assert fired == []
