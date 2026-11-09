"""Host terminal authority tests; all Git/process/registry observations are fakes."""
import hashlib
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from coordinator_core.ops.workflow_fire import finalizer as F

SID = '11111111-2222-4333-8444-555555555555'
PARENT = '22222222-2222-4333-8444-555555555555'


@pytest.fixture
def host(tmp_path, monkeypatch):
    from coordinator_core.warm.workflow_peer import PRIVATE_ROSTER_ENV
    (tmp_path / '.git').mkdir()
    (tmp_path / 'a.py').write_text('reviewed\n')
    monkeypatch.setenv(PRIVATE_ROSTER_ENV, 'protected-fixture')
    actor = NS(session_id=SID, fire_id='a'*32, request_id='commit-1', pid=22)
    monkeypatch.setattr(F, '_actor', lambda repo: actor)
    position = {'branch': 'refs/heads/work', 'head': 'a'*40}
    monkeypatch.setattr(F, '_position', lambda repo: dict(position))
    contract = {'declared_paths': ['a.py'], 'declared_prefixes': [],
                'manifest_declared_paths': [], 'has_request': True, 'files': {'work.js': 'c'*64}}
    monkeypatch.setattr(F, 'verify_contract', lambda repo, value: None)
    state = {'actor': actor, 'repo': tmp_path, 'position': dict(position), 'commits': [],
             'assignment': {'terminal_contract': contract, 'plan_owner_session_id': PARENT}}
    token = F._ACTIVE.set(state)
    yield NS(repo=tmp_path, actor=actor, state=state, position=position, contract=contract)
    F._ACTIVE.reset(token)


def digest(host, path='a.py'):
    return hashlib.sha256((host.repo/path).read_bytes()).hexdigest()


def operation(monkeypatch, host, actual='a.py', trailer=SID):
    calls = []
    monkeypatch.setattr(F, '_git', lambda repo, args: actual if args[0] == 'diff-tree'
                        else 'Scoped product\n\nSession-Id: '+trailer)

    @F.guarded_commit
    def commit(repo, paths, message, *, deleted_paths=(), untracked_paths=(), expected_content_sha256=None):
        calls.append((repo, paths, message))
        return NS(sha='b'*40)
    return commit, calls


def approve(host, **kwargs):
    F.authorize_product(host.repo, ['a.py'], [], {'a.py': digest(host)}, **kwargs)


def test_host_commit_keeps_live_codex_author_not_host_ancestry(host, monkeypatch):
    monkeypatch.setenv('CLAUDE_SESSION_ID', 'ambient-host-not-author')
    commit, calls = operation(monkeypatch, host)
    approve(host)
    commit(host.repo, ['a.py'], 'Scoped product')
    assert len(calls) == 1
    assert host.state['commits'][0]['session_id'] == SID
    assert host.state['commits'][0]['vendor'] == 'codex'
    assert F.review_owner(SID) == PARENT


def test_dead_worker_never_reaches_writer(host, monkeypatch):
    commit, calls = operation(monkeypatch, host)
    approve(host)
    def dead(repo):
        raise F.FinalizerRefused('worker is dead')
    monkeypatch.setattr(F, '_actor', dead)
    with pytest.raises(F.FinalizerRefused, match='dead'):
        commit(host.repo, ['a.py'], 'x')
    assert not calls


@pytest.mark.parametrize('field', ['branch', 'head'])
def test_branch_head_drift_before_write(host, monkeypatch, field):
    commit, calls = operation(monkeypatch, host)
    approve(host)
    host.position[field] = 'moved'
    with pytest.raises(F.FinalizerRefused, match='branch/HEAD'):
        commit(host.repo, ['a.py'], 'x')
    assert not calls


def test_reviewed_diff_drift_before_write(host, monkeypatch):
    commit, calls = operation(monkeypatch, host)
    approve(host)
    (host.repo/'a.py').write_text('unreviewed')
    with pytest.raises(F.FinalizerRefused, match='bytes drifted'):
        commit(host.repo, ['a.py'], 'x')
    assert not calls


def test_exact_trusted_scope_not_commit_argument_authority(host, monkeypatch):
    commit, calls = operation(monkeypatch, host)
    approve(host)
    with pytest.raises(F.FinalizerRefused, match='exact producer-authorized'):
        commit(host.repo, ['foreign.py'], 'x')
    assert not calls


@pytest.mark.parametrize('reviewed', [{}, {'foreign.py': 'a'*64}])
def test_empty_or_incomplete_product_hashes_refuse(host, reviewed):
    with pytest.raises(F.FinalizerRefused):
        F.authorize_product(host.repo, ['a.py'], [], reviewed)


def test_product_cannot_be_relabelled_host_metadata(host):
    with pytest.raises(F.FinalizerRefused):
        F.authorize_product(host.repo, ['a.py'], [], {}, host_metadata={'receipt': ['a.py']})


def test_actual_changed_paths_checked_against_authorized_set(host, monkeypatch):
    commit, calls = operation(monkeypatch, host, actual='a.py\nforeign.py')
    approve(host)
    with pytest.raises(F.FinalizerRefused, match='actual host commit paths'):
        commit(host.repo, ['a.py'], 'x')
    assert len(calls) == 1 and not host.state['commits']


def test_wrong_stage_and_wrong_session_override(host, monkeypatch):
    commit, calls = operation(monkeypatch, host)
    with pytest.raises(F.FinalizerRefused):
        F.require_session({'session_id': PARENT})
    token = F._ACTIVE.set(None)
    try:
        with pytest.raises(F.FinalizerRefused, match='outside'):
            commit(host.repo, ['a.py'], 'x')
    finally:
        F._ACTIVE.reset(token)
    assert not calls


def test_postcommit_wrong_session_trailer_cannot_qualify(host, monkeypatch):
    commit, calls = operation(monkeypatch, host, trailer=PARENT)
    approve(host)
    with pytest.raises(F.FinalizerRefused, match='Session-Id'):
        commit(host.repo, ['a.py'], 'x')
    assert len(calls) == 1 and not host.state['commits']


@pytest.mark.parametrize('kwargs', [{'merging': True}, {'supplied_blobs': {'a.py': 'blob'}}])
def test_partial_blob_and_merge_refused_before_git(host, kwargs):
    with pytest.raises(F.FinalizerRefused, match='merge/settled/partial'):
        approve(host, **kwargs)


def test_generated_metadata_has_separate_authorization_and_actual_hash(host, monkeypatch):
    (host.repo/'receipt.json').write_text('{}')
    commit, calls = operation(monkeypatch, host, actual='a.py\nreceipt.json')
    F.authorize_product(host.repo, ['a.py', 'receipt.json'], [], {'a.py': digest(host)},
                        host_metadata={'terminal-receipts': ['receipt.json']})
    commit(host.repo, ['a.py', 'receipt.json'], 'x')
    assert len(calls) == 1
    assert host.state['commits'][0]['reviewed_content_sha256']['receipt.json'] == digest(host, 'receipt.json')


def test_prepare_normalizes_only_exact_protected_parent(host, monkeypatch):
    import threading
    private = NS(repo_root=host.repo, parent_session_id=PARENT, artifact_dir=host.repo/'.git',
                 roster={'terminal_contract': host.contract}, _lock=threading.RLock(), _write_roster=lambda: None)
    action = {'next_action': {'op': 'dispatch.land_run', 'params': {'session_id': PARENT}}}
    path = F.prepare(private, action, host.repo/'work.js', 'commit-1')
    assert 'session_id' not in __import__('json').loads(path.read_text())
    assert private.roster['terminal_finalizer']['original_engine_action']['params']['session_id'] == PARENT
    private.roster.pop('terminal_finalizer')
    action['next_action']['params']['session_id'] = SID
    with pytest.raises(F.FinalizerRefused, match='protected parent'):
        F.prepare(private, action, host.repo/'work.js', 'commit-1')


def test_script_and_pointer_graph_is_bound_before_worker(tmp_path):
    from coordinator_core.ops.dispatch_emit.commit_request import CommitRequest, ChunkCommit, render_marker
    script = tmp_path/'work.js'
    script.write_text(render_marker(CommitRequest(chunks=(ChunkCommit('one', 'one', paths=('a.py',)),))))
    contract = F.snapshot_contract(tmp_path, script)
    F.verify_contract(tmp_path, contract)
    script.write_text(script.read_text()+'\n// worker change')
    with pytest.raises(F.FinalizerRefused, match='changed after fire'):
        F.verify_contract(tmp_path, contract)


def test_actual_manifest_pointer_pins_request_and_manifest_bytes(tmp_path):
    import json
    from coordinator_core.ops.dispatch_emit.commit_request import CommitRequest, ChunkCommit, render_marker
    from coordinator_core.ops.dispatch_emit.ask_contract import StageManifest, ASK_MANIFEST_MARKER
    folder = tmp_path/'state'/'warp'/'one'
    folder.mkdir(parents=True)
    marker = folder/'request.txt'
    marker.write_text(render_marker(CommitRequest(chunks=(ChunkCommit('one', 'one', paths=('a.py',)),))))
    manifest = StageManifest(run_dir='state/warp/one', rows=(), review_declared_paths=('a.py',),
                             marker_path='state/warp/one/request.txt')
    path = folder/'a.json'
    path.write_text(json.dumps(manifest.to_json()))
    script = tmp_path/'z.workflow.mjs'
    script.write_text(ASK_MANIFEST_MARKER+'state/warp/one/a.json')
    contract = F.snapshot_contract(tmp_path, script)
    # Roster serialization sorts keys: the script must not be selected by object order.
    contract = json.loads(json.dumps(contract, sort_keys=True))
    assert next(iter(contract['files'])) != 'z.workflow.mjs'
    F.verify_contract(tmp_path, contract)
    assert set(contract['files']) == {'z.workflow.mjs', 'state/warp/one/a.json', 'state/warp/one/request.txt'}
    assert contract['manifest_declared_paths'] == ['a.py']
    marker.write_text(marker.read_text().replace('a.py', 'foreign.py'))
    with pytest.raises(F.FinalizerRefused):
        F.verify_contract(tmp_path, contract)


def test_oversized_script_drift_refused_before_read(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit.commit_request import CommitRequest, ChunkCommit, render_marker
    script = tmp_path/'work.js'
    script.write_text(render_marker(CommitRequest(chunks=(ChunkCommit('one', 'one', paths=('a.py',)),))))
    contract = F.snapshot_contract(tmp_path, script)
    script.write_bytes(b'x' * (2 * 1024 * 1024 + 1))
    original_open = Path.open
    def forbidden_open(path, *args, **kwargs):
        if path == script:
            raise AssertionError('oversized source must not be opened')
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', forbidden_open)
    with pytest.raises(F.FinalizerRefused, match='bounded regular'):
        F.verify_contract(tmp_path, contract)


def test_reparse_script_drift_refused_before_read(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit.commit_request import CommitRequest, ChunkCommit, render_marker
    script = tmp_path/'work.js'
    script.write_text(render_marker(CommitRequest(chunks=(ChunkCommit('one', 'one', paths=('a.py',)),))))
    contract = F.snapshot_contract(tmp_path, script)
    original = Path.is_symlink
    monkeypatch.setattr(Path, 'is_symlink', lambda path: True if path == script else original(path))
    original_open = Path.open
    def forbidden_open(path, *args, **kwargs):
        if path == script:
            raise AssertionError('reparse source must not be opened')
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', forbidden_open)
    with pytest.raises(F.FinalizerRefused, match='reparse'):
        F.verify_contract(tmp_path, contract)


@pytest.mark.parametrize('path', ['C:/outside.py', 'C:relative.py', ':(glob)outside.py'])
def test_windows_drive_and_gitmagic_paths_refused_before_hash_or_writer(host, monkeypatch, path):
    commit, calls = operation(monkeypatch, host)
    original_read = Path.read_bytes
    def forbidden_read(file, *args, **kwargs):
        if str(file).replace('\\', '/') in (path, str(host.repo / path).replace('\\', '/')):
            raise AssertionError('invalid path must not be read')
        return original_read(file, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_bytes', forbidden_read)
    with pytest.raises(F.FinalizerRefused, match='normalized'):
        F.authorize_product(host.repo, [path], [], {path: 'a'*64})
    with pytest.raises(F.FinalizerRefused, match='normalized'):
        commit(host.repo, [path], 'x')
    assert not calls


def test_existing_commit_stage_uses_protected_host_params_not_workspace_file(host):
    import json
    import threading
    from coordinator_core.ops.workflow_fire import headless
    from coordinator_core.ops.dispatch_emit.terminal_commit_params_channel import render_params_block
    private = NS(repo_root=host.repo, parent_session_id=PARENT, artifact_dir=host.repo/'.git',
                 roster={'terminal_contract': host.contract}, _lock=threading.RLock(),
                 _write_roster=lambda: None, allowed_cli=['coordinator-invoke'],
                 private_bin=host.repo/'.git'/'private-bin')
    event = {'id': 'commit-1', 'options': {'phase': 'commit', 'label': 'commit',
             'agentType': 'coordinator:git-commit-agent'},
             'prompt': render_params_block({'run_id': 'one', 'run_token': 'token',
                       'script_path': str(host.repo/'work.js'), 'session_id': PARENT})}
    prompt = headless._prepare_private_commit(private, event, host.repo/'work.js', {'repo_root': str(host.repo)})
    protected = host.repo/'.git'/'terminal-action.json'
    assert str(protected) in prompt and 'dispatch.land_run' in prompt
    assert 'session_id' not in json.loads(protected.read_text())
    assert private.roster['terminal_finalizer']['request_id'] == 'commit-1'
    # A second invocation does not silently create a second terminal assignment.
    with pytest.raises(F.FinalizerRefused, match='single use'):
        headless._prepare_private_commit(private, event, host.repo/'work.js', {'repo_root': str(host.repo)})


def test_host_receipt_must_bind_actual_finalizer_not_model_reply(host):
    import json
    from coordinator_core.ops.workflow_fire import headless
    private = NS(artifact_dir=host.repo/'.git', _worker=lambda request: {'native_uuid': SID})
    path = private.artifact_dir/'terminal-finalizer-receipt.json'
    receipt = {'state': 'completed', 'fire_id': host.actor.fire_id, 'request_id': 'commit-1',
               'native_uuid': PARENT, 'vendor': 'codex', 'commits': [{'sha': 'a'*40}],
               'result': {'committed': True, 'sha': 'a'*40}}
    path.write_text(json.dumps(receipt))
    record = {'fire_id': host.actor.fire_id}
    with pytest.raises(F.FinalizerRefused, match='mismatched'):
        headless._private_commit_receipt(private, 'commit-1', record)
    receipt['native_uuid'] = SID
    path.write_text(json.dumps(receipt))
    assert headless._private_commit_receipt(private, 'commit-1', record) == receipt['result']
    assert record['terminal_finalizer']['native_uuid'] == SID


def test_existing_host_entrypoint_route_performs_constructor_writes_in_host(tmp_path, monkeypatch):
    """The real shared entrypoint loader owns cwd/state; the child need not write .git.

    This constructor fixture tests the route, not sizing's domain algorithm. Authenticated
    native binding is covered separately in warm/test_workflow_peer, not fabricated here.
    """
    import os
    import json
    from coordinator_core.ops import invoke_from_argv as invoke
    root = tmp_path/'repo'
    root.mkdir()
    script = tmp_path/'sizing-assemble.py'
    script.write_text(
        'import os, json\nfrom pathlib import Path\n'
        'def main(argv):\n'
        '    for leaf in ("coordinator-locks", "coordinator-sessions"):\n'
        '        p=Path.cwd()/".git"/leaf\n'
        '        p.mkdir(parents=True,exist_ok=True)\n'
        '        (p/"constructor-evidence").write_text(str(os.getpid()))\n'
        '    print(json.dumps({"pid":os.getpid(),"argv":argv}))\n'
        '    return 0\n'
        'if __name__ == "__main__":\n'
        '    import sys\n    sys.exit(main(sys.argv[1:]))\n')
    monkeypatch.setattr(invoke, '_resolve_entrypoint_script', lambda name: script)
    reply = invoke._run_entrypoint('sizing-assemble', ['--fixture'], str(root))
    assert reply['exit_code'] == 0, reply
    assert json.loads(reply['stdout']) == {'pid': os.getpid(), 'argv': ['--fixture']}
    for leaf in ('coordinator-locks', 'coordinator-sessions'):
        assert (root/'.git'/leaf/'constructor-evidence').read_text() == str(os.getpid())


def test_atomic_writer_receives_all_protected_hashes_including_host_metadata(host, monkeypatch):
    metadata = host.repo / 'receipt.json'
    metadata.write_text('host generated receipt\n')
    expected = {'a.py': digest(host), 'receipt.json': digest(host, 'receipt.json')}
    F.authorize_product(host.repo, list(expected), [], {'a.py': expected['a.py']},
                        host_metadata={'terminal-receipts': ['receipt.json']})
    seen = []

    @F.guarded_commit
    def writer(repo, paths, message, *, deleted_paths=(), untracked_paths=(), expected_content_sha256=None):
        seen.append(dict(expected_content_sha256))
        assert expected_content_sha256 == expected
        (repo / 'a.py').write_text('changed after wrapper precheck\n')
        if hashlib.sha256((repo / 'a.py').read_bytes()).hexdigest() != expected_content_sha256['a.py']:
            raise F.FinalizerRefused('atomic writer refused drift')
        pytest.fail('drift must not qualify as a host commit')

    with pytest.raises(F.FinalizerRefused, match='atomic writer refused drift'):
        writer(host.repo, list(expected), 'Scoped product')
    assert seen == [expected] and host.state['commits'] == []


def test_matching_caller_expectations_are_preserved_and_completed(host, monkeypatch):
    from types import MappingProxyType
    metadata = host.repo / 'receipt.json'
    metadata.write_text('host generated receipt\n')
    expected = {'a.py': digest(host), 'receipt.json': digest(host, 'receipt.json')}
    F.authorize_product(host.repo, list(expected), [], {'a.py': expected['a.py']},
                        host_metadata={'terminal-receipts': ['receipt.json']})
    monkeypatch.setattr(F, '_git', lambda repo, args: 'a.py\nreceipt.json' if args[0] == 'diff-tree'
                        else 'Scoped product\nSession-Id: ' + SID)
    seen = []

    @F.guarded_commit
    def writer(repo, paths, message, *, deleted_paths=(), untracked_paths=(), expected_content_sha256=None):
        seen.append(expected_content_sha256)
        return NS(sha='b'*40)

    writer(host.repo, list(expected), 'Scoped product',
           expected_content_sha256=MappingProxyType({'a.py': expected['a.py']}))
    assert seen == [expected]


@pytest.mark.parametrize('absolute', [False, True])
def test_conflicting_caller_hash_refuses_before_underlying_writer(host, monkeypatch, absolute):
    commit, calls = operation(monkeypatch, host)
    approve(host)
    key = str(host.repo / 'a.py') if absolute else 'a.py'
    with pytest.raises(F.FinalizerRefused, match='expectations conflict'):
        commit(host.repo, ['a.py'], 'Scoped product', expected_content_sha256={key: '0'*64})
    assert calls == []


def test_legacy_writer_without_private_roster_keeps_original_expectations(host, monkeypatch):
    from coordinator_core.warm.workflow_peer import PRIVATE_ROSTER_ENV
    monkeypatch.delenv(PRIVATE_ROSTER_ENV)
    original = {'a.py': '0'*64}
    seen = []

    @F.guarded_commit
    def writer(repo, paths, message, *, expected_content_sha256=None):
        seen.append(expected_content_sha256)
        return 'legacy result'

    assert writer(host.repo, ['a.py'], 'Legacy', expected_content_sha256=original) == 'legacy result'
    assert seen[0] is original


def test_approved_deletions_remain_absence_guards_not_content_hash_entries(host, monkeypatch):
    (host.repo / 'a.py').unlink()
    metadata = host.repo / 'receipt.json'
    metadata.write_text('host receipt\n')
    F.authorize_product(host.repo, ['receipt.json'], ['a.py'], {'a.py': None},
                        host_metadata={'terminal-receipts': ['receipt.json']})
    monkeypatch.setattr(F, '_git', lambda repo, args: 'a.py\nreceipt.json' if args[0] == 'diff-tree'
                        else 'Scoped product\nSession-Id: ' + SID)
    seen = []

    @F.guarded_commit
    def writer(repo, paths, message, *, deleted_paths=(), untracked_paths=(), expected_content_sha256=None):
        seen.append((deleted_paths, expected_content_sha256))
        return NS(sha='b'*40)

    writer(host.repo, ['receipt.json'], 'Scoped product', deleted_paths=['a.py'])
    assert seen == [(['a.py'], {'receipt.json': digest(host, 'receipt.json')})]
