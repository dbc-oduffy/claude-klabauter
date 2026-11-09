import hashlib
import json

import pytest

from coordinator_core.ops.workflow_fire import authority
from coordinator_core.ops.workflow_fire.receipt import FIRE_TOKEN_ENV, fire_token_digest


@pytest.fixture
def assignment(tmp_path, monkeypatch):
    script = tmp_path / 'run.mjs'
    script.write_text('export const meta={}; return {};')
    digest = hashlib.sha256(script.read_bytes()).hexdigest()
    registry = tmp_path / 'registry'
    registry.mkdir()
    identifier = 'a' * 32
    receipt = {'fire': {'fire_id': identifier, 'token_sha256': fire_token_digest('assigned-token')}}
    script.with_name(script.name + '.emitted.json').write_text(json.dumps(receipt))
    record = {'caller_session_id': 'parent-em', 'backend': 'codex', 'fire_id': identifier, 'sha256': digest, 'script_path': str(script),
              'repo_root': str(tmp_path), 'state': 'running'}
    path = registry / (identifier + '.json')
    path.write_text(json.dumps(record))
    monkeypatch.setattr(authority.fire, '_resolve_target_repo', lambda *a: str(tmp_path))
    monkeypatch.setattr(authority.fire, '_registry_dir', lambda *a: registry)
    return script, digest, {FIRE_TOKEN_ENV: 'assigned-token', 'COORDINATOR_PARENT_SESSION_ID': 'parent-em'}, path, record


def test_exact_assignment_is_accepted_without_creating_authority(assignment):
    script, digest, env, path, _ = assignment
    before = path.read_bytes()
    assert authority.assigned_script_delegation(str(script), digest, env)[0]
    assert path.read_bytes() == before


@pytest.mark.parametrize('change', ['digest', 'script', 'repository', 'ended', 'missing'])
def test_receipt_token_cannot_authorize_a_different_or_finished_assignment(assignment, change):
    script, digest, env, path, record = assignment
    if change == 'digest':
        record['sha256'] = '0' * 64
    elif change == 'script':
        record['script_path'] = str(script.with_name('other.mjs'))
    elif change == 'repository':
        record['repo_root'] = str(script.parent / 'other')
    elif change == 'ended':
        record['state'] = 'exited'
    if change == 'missing':
        path.unlink()
    else:
        path.write_text(json.dumps(record))
    assert not authority.assigned_script_delegation(str(script), digest, env)[0]


def test_token_and_current_bytes_are_both_required(assignment):
    script, digest, env, _, _ = assignment
    assert not authority.assigned_script_delegation(str(script), digest, {})[0]
    script.write_text('export const meta={}; return {changed:true};')
    assert not authority.assigned_script_delegation(str(script), digest, env)[0]
