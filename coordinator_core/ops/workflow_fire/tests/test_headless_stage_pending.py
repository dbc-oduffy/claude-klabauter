"""A headless fire whose root returns `stage_pending` runs the host-issued staged twin."""
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import failed_fire_halt
from coordinator_core.ops.dispatch_emit.fan_launcher import MAX_STAGE_ROUNDS
from coordinator_core.ops.dispatch_emit.nested_run_contract import staged_twin_rel
from coordinator_core.ops.workflow_fire import finalizer, headless

pytestmark = pytest.mark.skipif(not shutil.which('node'), reason='node unavailable')

RUN_ID = 'warp-2026-11-28-stage-pending-aaaaaa-20261128T000000-bbbbbb'
MANIFEST_SHA = 'a' * 64
TWIN_OK = "export const meta={}; return {ok:true, twin_ran:true};"
TWIN_AGAIN = "export const meta={}; return {stage_pending:{handle:{path:'m.json',sha256:'%s',bytes:2}}};" % MANIFEST_SHA


def fire(tmp_path, monkeypatch, twin_source, *, pending_sha=MANIFEST_SHA, gate_twin_source=None):
    """Execute a record whose root gates, then returns a stage_pending naming `pending_sha`."""
    twin_rel = staged_twin_rel(RUN_ID, MANIFEST_SHA)
    twin = tmp_path / twin_rel
    twin.parent.mkdir(parents=True)
    twin.write_text(gate_twin_source or twin_source, encoding='utf-8')
    gated_sha = hashlib.sha256(twin.read_bytes()).hexdigest()
    twin.write_text(twin_source, encoding='utf-8')
    manifest_rel = f'state/warp/{RUN_ID}/manifest.json'
    stage_reply = {'twin_sha256': gated_sha, 'twin_path': twin_rel,
                   'manifest_handle': {'path': manifest_rel, 'sha256': MANIFEST_SHA, 'bytes': 2}}
    monkeypatch.setattr(headless, '_host_gate', lambda params, owner, directory, remaining: (
        {'stage_reply': stage_reply}, 'digest'))
    monkeypatch.setattr(finalizer, 'snapshot_contract', lambda repo, script: {
        'files': {twin_rel: gated_sha, manifest_rel: MANIFEST_SHA}, 'has_request': False})
    pending = {'handle': {'path': manifest_rel, 'sha256': pending_sha, 'bytes': 2}, 'twin_path': twin_rel}
    root = tmp_path / 'run.mjs'
    root.write_text("export const meta={};\nawait engineOp('gate.host_run', {});\nreturn {stage_pending: "
                    + json.dumps(pending) + "};", encoding='utf-8')
    artifacts = tmp_path / 'artifacts'
    artifacts.mkdir()
    (artifacts / 'start').touch()
    path = tmp_path / 'record.json'
    path.write_text(json.dumps({
        'fire_id': 'test', 'run_id': RUN_ID, 'script_path': str(root),
        'script_sha256': hashlib.sha256(root.read_bytes()).hexdigest(),
        'repo_root': str(tmp_path), 'artifact_dir': str(artifacts), 'backend': 'codex',
        'journal_path': str(artifacts / 'journal.jsonl'), 'timeout_s': 20}), encoding='utf-8')
    halts = []
    monkeypatch.setattr(failed_fire_halt, 'halt_failed_fire',
                        lambda root_, ids, error, commits, **named: halts.append((error, named)) or [])
    result = headless.execute(path, terminal_runner=lambda *a: None)
    journal = [json.loads(line) for line in Path(result['journal_path']).read_text().splitlines()]
    starts = [e['script_path'] for e in journal if e.get('event') == 'workflow-start']
    return result, halts, [s for s in starts if '.staged-' in s]


def test_stage_pending_with_a_bound_twin_runs_the_twin_and_continues(tmp_path, monkeypatch):
    result, halts, twin_starts = fire(tmp_path, monkeypatch, TWIN_OK)
    assert result['exit_code'] == 0, result.get('error')
    assert result['outcome'] == 'clean'
    assert result['result'] == {'ok': True, 'twin_ran': True}
    assert len(twin_starts) == 1
    assert halts == []


def test_stage_pending_naming_an_unbound_twin_still_halts_unresolved(tmp_path, monkeypatch):
    result, halts, twin_starts = fire(tmp_path, monkeypatch, TWIN_OK, pending_sha='b' * 64)
    assert result['exit_code'] == 1
    assert 'unresolved stage_pending' in result['error']
    assert twin_starts == []
    assert [named for _, named in halts] == [{'halted': failed_fire_halt.HALT_STAGE_PENDING_UNRESOLVED}]


def test_stage_pending_twin_that_differs_from_the_gate_digest_halts_unresolved(tmp_path, monkeypatch):
    result, halts, _ = fire(tmp_path, monkeypatch, TWIN_OK, gate_twin_source=TWIN_OK + ' // tampered')
    assert result['exit_code'] == 1
    assert [named for _, named in halts] == [{'halted': failed_fire_halt.HALT_STAGE_PENDING_UNRESOLVED}]


def test_a_twin_that_keeps_returning_stage_pending_stops_at_the_round_cap(tmp_path, monkeypatch):
    result, halts, twin_starts = fire(tmp_path, monkeypatch, TWIN_AGAIN)
    assert result['exit_code'] == 1
    assert len(twin_starts) == MAX_STAGE_ROUNDS
    assert [named for _, named in halts] == [{'halted': failed_fire_halt.HALT_STAGE_PENDING_UNRESOLVED}]
