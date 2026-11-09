"""Live native finalizer through the existing private CLI; never worker Git access."""
from __future__ import annotations

from contextvars import ContextVar
from collections.abc import Mapping
from functools import wraps
import hashlib
import inspect
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
import threading

_ACTIVE = ContextVar('private_terminal_action', default=None)
_LOCK = threading.RLock()
OPS = frozenset({'dispatch.terminal_commit', 'dispatch.land_run'})


class FinalizerRefused(RuntimeError):
    pass


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def _write(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
    with temporary.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(_json(value) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _git(repo, args):
    from coordinator_core.git.run import run_git
    reply = run_git(args, cwd=str(repo))
    if not reply.ok:
        raise FinalizerRefused('host Git observation failed: ' + ' '.join(args[:2]))
    return reply.stdout.strip()


def _position(repo):
    return {'branch': _git(repo, ['symbolic-ref', '--quiet', 'HEAD']),
            'head': _git(repo, ['rev-parse', 'HEAD'])}


def snapshot_contract(repo, script):
    """Pin the existing emitter's marker graph before any workflow worker runs.

    This is not a new source of scope: paths come from CommitRequest/StageManifest.
    The protected fire record authenticates these bytes, not their workspace names.
    """
    from coordinator_core.ops.dispatch_emit.commit_request import parse_marker, parse_manifest_marker
    from coordinator_core.ops.dispatch_emit.ask_contract import StageManifest
    root = Path(repo).resolve(strict=True)
    files = {}

    def read(path):
        path = Path(path)
        if not path.is_absolute():
            path = root / path
        relative = path.relative_to(root)
        current = root
        for part in relative.parts:
            if part in ('', '.', '..'):
                raise FinalizerRefused('terminal marker path is not canonical')
            current = current / part
            if current.is_symlink() or getattr(current.lstat(), 'st_file_attributes', 0) & 0x400:
                raise FinalizerRefused('terminal marker path is a reparse point')
        before = path.lstat()
        limit = 2 * 1024 * 1024
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise FinalizerRefused('terminal marker is not a bounded regular file')
        with path.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            if (not stat.S_ISREG(opened.st_mode) or opened.st_size > limit
                    or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)):
                raise FinalizerRefused('terminal marker changed while opening')
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise FinalizerRefused('terminal marker exceeds its byte bound')
        files[relative.as_posix()] = hashlib.sha256(raw).hexdigest()
        return raw.decode('utf-8')

    text = read(script)
    manifest_path = parse_manifest_marker(text)
    manifest = None
    if manifest_path is not None:
        manifest = StageManifest.from_json(json.loads(read(manifest_path)))
        request = parse_marker(read(manifest.marker_path))
    else:
        request = parse_marker(text)
    return {'script_path': Path(script).relative_to(root).as_posix(),
            'files': files, 'declared_paths': sorted({p.replace('\\', '/')
            for chunk in (request.chunks if request else ()) for p in chunk.paths}),
            'declared_prefixes': sorted({p.replace('\\', '/')
            for chunk in (request.chunks if request else ()) for p in chunk.prefixes}),
            'manifest_declared_paths': sorted(manifest.review_declared_paths) if manifest else [],
            'has_request': request is not None}


def verify_contract(repo, contract):
    root = Path(repo).resolve(strict=True)
    script = contract.get('script_path')
    if (not isinstance(script, str) or not script or PureWindowsPath(script).drive
            or ':' in script or PurePosixPath(script).is_absolute()
            or any(part in ('', '.', '..') for part in script.replace('\\', '/').split('/'))):
        raise FinalizerRefused('terminal action lacks the pre-worker emitter contract')
    # Revalidate ancestry and bounds BEFORE reading any mutable workspace bytes.
    if snapshot_contract(root, root / script) != contract:
        raise FinalizerRefused('engine-authored terminal script/manifest changed after fire')


def prepare(private, result, script, request_id):
    """Owner writes the exact engine-authored action before spawning its finalizer."""
    action = result.get('next_action') if isinstance(result, dict) else None
    if not isinstance(action, dict) or action.get('op') not in OPS:
        return None
    if not isinstance(action.get('params'), dict):
        raise FinalizerRefused('terminal action params must be an object')
    original = json.loads(_json(action))
    params = {**action['params'], 'script_path': str(script)}
    authored_sid = params.pop('session_id', None)
    if authored_sid is not None and authored_sid != private.parent_session_id:
        raise FinalizerRefused('authored terminal session is not the protected parent')
    contract = private.roster.get('terminal_contract')
    if not isinstance(contract, dict) or not contract.get('has_request'):
        raise FinalizerRefused('terminal action lacks an engine-emitted commit request')
    verify_contract(private.repo_root, contract)
    assignment = {'schema_version': 1, 'state': 'prepared', 'request_id': request_id,
                  'operation': action['op'], 'params': params,
                  'position': _position(private.repo_root), 'commits': [],
                  'original_engine_action': original, 'authored_parent_session_id': authored_sid,
                  'plan_owner_session_id': private.parent_session_id,
                  'terminal_contract': contract}
    with private._lock:
        if private.roster.get('terminal_finalizer') is not None:
            raise FinalizerRefused('terminal finalizer is single use')
        private.roster['terminal_finalizer'] = assignment
        private._write_roster()
    path = private.artifact_dir / 'terminal-action.json'
    _write(path, params)
    return path


def _actor(repo):
    from coordinator_core.session.native_actor import current_verified_native_actor, validate_actor
    actor = current_verified_native_actor()
    if actor is None:
        raise FinalizerRefused('live finalizer requires the server-verified native worker')
    return validate_actor(actor, actor.session_id, str(repo))


def _assignment(repo):
    from coordinator_core.warm import workflow_peer
    actor = _actor(repo)
    path = os.environ.get(workflow_peer.PRIVATE_ROSTER_ENV)
    if not path:
        raise FinalizerRefused('private finalizer has no protected roster')
    row = workflow_peer.load_roster(path)
    workflow_peer._validate_assignment(row)
    assigned = row.get('terminal_finalizer')
    workers = [w for w in row['workers'] if w.get('request_id') == actor.request_id
               and w.get('native_uuid') == actor.session_id and w.get('pid') == actor.pid
               and w.get('state') == 'ready']
    if (not isinstance(assigned, dict) or assigned.get('state') != 'prepared'
            or assigned.get('request_id') != actor.request_id or len(workers) != 1
            or row['fire_id'] != actor.fire_id or not os.path.samefile(row['repo_root'], repo)):
        raise FinalizerRefused('wrong stage or non-ready finalizer binding')
    return actor, Path(path), row, assigned


def terminal_action(operation):
    """Wrap existing terminal handlers only when a private native worker invokes them."""
    def decorate(function):
        @wraps(function)
        def call(params, repo_root=None, *args, **kwargs):
            from coordinator_core.warm.workflow_peer import PRIVATE_ROSTER_ENV
            if not os.environ.get(PRIVATE_ROSTER_ENV):
                return function(params, repo_root, *args, **kwargs)
            with _LOCK:
                actor, path, row, assigned = _assignment(repo_root)
                supplied = dict(params)
                sid = supplied.pop('session_id', actor.session_id)
                if sid != actor.session_id or operation != assigned['operation'] or supplied != assigned['params']:
                    raise FinalizerRefused('terminal operation/params/session differ from protected assignment')
                if _position(repo_root) != assigned['position']:
                    raise FinalizerRefused('branch or HEAD moved before finalizer')
                verify_contract(repo_root, assigned['terminal_contract'])
                assigned.update(state='running', native_uuid=actor.session_id, vendor='codex')
                state_path = path.parent / 'terminal-finalizer-state.json'
                # Exclusive cross-process consumption; never race owner roster updates.
                with state_path.open('x', encoding='utf-8') as stream:
                    stream.write(_json(assigned) + '\n')
                context = {'actor': actor, 'repo': Path(repo_root), 'position': assigned['position'],
                           'commits': [], 'assignment': assigned}
                token = _ACTIVE.set(context)
                try:
                    reply = function({**supplied, 'session_id': actor.session_id}, repo_root, *args, **kwargs)
                    if not isinstance(reply, dict) or not reply.get('committed') or not context['commits']:
                        raise FinalizerRefused('terminal handler did not produce a verified host commit')
                    if reply.get('sha') != context['commits'][0]['sha']:
                        raise FinalizerRefused('terminal reply differs from actual host product commit')
                    assigned.update(state='completed', commits=context['commits'], result=reply)
                    _write(state_path, assigned)
                    _write(path.parent / 'terminal-finalizer-receipt.json', {
                        'fire_id': actor.fire_id, 'request_id': actor.request_id,
                        'native_uuid': actor.session_id, 'vendor': 'codex', 'result': reply,
                        'commits': context['commits'], 'state': 'completed',
                        'original_engine_action': assigned['original_engine_action'],
                        'authored_parent_session_id': assigned['authored_parent_session_id']})
                    return reply
                except BaseException:
                    assigned['state'] = 'failed'
                    _write(state_path, assigned)
                    raise
                finally:
                    _ACTIVE.reset(token)
        return call
    return decorate


def require_session(params):
    """A private worker cannot override commit attribution, even through nested dispatch."""
    from coordinator_core.warm.workflow_peer import PRIVATE_ROSTER_ENV
    if not os.environ.get(PRIVATE_ROSTER_ENV):
        return None
    actor = _actor(_ACTIVE.get()['repo'] if _ACTIVE.get() else '')
    if _ACTIVE.get() is None or params.get('session_id', actor.session_id) != actor.session_id:
        raise FinalizerRefused('commit requires the assigned live worker session')
    return actor.session_id


def review_owner(session_id):
    """The emitting session owns review evidence; it never supplies commit authorship."""
    context = _ACTIVE.get()
    if context is None:
        return session_id
    return context['assignment']['plan_owner_session_id']


def _explicit_path(raw):
    p = str(raw).replace('\\', '/')
    if (not p or ':' in p or PureWindowsPath(p).drive or p.startswith('/')
            or any(x in ('', '.', '..', '.git') for x in p.split('/'))
            or PurePosixPath(p).is_absolute()):
        raise FinalizerRefused('host commit pathspec is not normalized and explicit')
    return p


def authorize_product(repo, paths, deleted, reviewed, host_metadata=None, *, supplied_blobs=None, merging=False):
    """Existing terminal guard hands its reviewed product and named own writes to Git guard.

    Only the producer calls this after its normal plan/review gates; model parameters cannot
    supply a scope or exempt a file. Partial-hunk and merge landings are deliberately unsupported.
    """
    context = _ACTIVE.get()
    if context is None:
        return
    if supplied_blobs or merging:
        raise FinalizerRefused('private terminal does not support merge/settled/partial blobs')
    contract = context['assignment']['terminal_contract']
    verify_contract(repo, contract)
    declared = set(contract['declared_paths']) | set(contract['manifest_declared_paths'])
    prefixes = [p.rstrip('/') + '/' for p in contract['declared_prefixes']]
    deleted = {_explicit_path(p) for p in deleted}
    if host_metadata is not None and not isinstance(host_metadata, dict):
        raise FinalizerRefused('host metadata needs explicit producer categories')
    sources = {kind: sorted(set(items) - {None, ''}) for kind, items in (host_metadata or {}).items()}
    metadata = {p for items in sources.values() for p in items}
    actual = {_explicit_path(p) for p in paths} | deleted
    # A product named by the emitter cannot be relabelled as generated bookkeeping.
    metadata -= {p for p in actual if p in declared or any(p.startswith(prefix) for prefix in prefixes)}
    product = actual - metadata
    if not product or not reviewed:
        raise FinalizerRefused('private terminal requires nonempty reviewed product bytes')
    expectations = {}
    for p in product:
        if p not in declared and not any(p.startswith(prefix) for prefix in prefixes):
            raise FinalizerRefused('product path outside protected emitter scope: ' + p)
        if p in deleted:
            # A review's deletion certificate must explicitly record absence.
            if p not in reviewed or reviewed[p] is not None or (Path(repo) / p).exists():
                raise FinalizerRefused('missing reviewed deletion evidence: ' + p)
            expectations[p] = None
        else:
            digest = reviewed.get(p)
            if not isinstance(digest, str) or len(digest) != 64:
                raise FinalizerRefused('missing reviewed product fingerprint: ' + p)
            expectations[p] = digest
    for p in actual & metadata:
        if p in deleted:
            raise FinalizerRefused('host metadata deletion is not an authorized generated write')
        file = Path(repo) / p
        if not file.is_file():
            raise FinalizerRefused('host generated metadata is not a regular file')
        expectations[p] = hashlib.sha256(file.read_bytes()).hexdigest()
    context['approved_commit'] = {'paths': sorted(actual), 'hashes': expectations,
                                'host_metadata': {kind: sorted(set(items) & actual & metadata)
                                                  for kind, items in sources.items()}}


def guarded_commit(function):
    signature = inspect.signature(function)
    @wraps(function)
    def call(*args, **kwargs):
        from coordinator_core.warm.workflow_peer import PRIVATE_ROSTER_ENV
        if not os.environ.get(PRIVATE_ROSTER_ENV):
            return function(*args, **kwargs)
        context = _ACTIVE.get()
        if context is None:
            raise FinalizerRefused('worker Git commit outside the terminal finalizer stage')
        actor = _actor(context['repo'])
        if actor.session_id != context['actor'].session_id:
            raise FinalizerRefused('host commit actor changed')
        bound = signature.bind(*args, **kwargs);bound.apply_defaults();values = bound.arguments
        repo = values.get('repo_root') or values.get('repo')
        if not os.path.samefile(repo, context['repo']) or _position(repo) != context['position']:
            raise FinalizerRefused('host commit repository/branch/HEAD changed')
        declared = []
        for key in ('paths', 'deleted_paths', 'untracked_paths'):
            for raw in values.get(key, ()):
                declared.append(_explicit_path(raw))
        if not declared or len(set(declared)) != len(declared):
            raise FinalizerRefused('host commit pathspec is empty or ambiguous')
        approved = context.pop('approved_commit', None)
        if not isinstance(approved, dict) or set(declared) != set(approved['paths']):
            raise FinalizerRefused('host commit lacks exact producer-authorized reviewed scope')
        verify_contract(repo, context['assignment']['terminal_contract'])
        # Preserve the engine's reviewed expectations; commit_paths enforces them before any write.
        reviewed = dict(approved['hashes'])
        for p, digest in reviewed.items():
            file = Path(repo) / p
            if (digest is None and file.exists()) or (digest is not None and (
                    not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest() != digest)):
                raise FinalizerRefused('reviewed host commit bytes drifted')
        supplied = values.get('expected_content_sha256')
        if supplied is not None and not isinstance(supplied, Mapping):
            raise FinalizerRefused('host commit expectations must be a mapping')
        from coordinator_core.git.commit import _index_key
        merged = {}
        for raw, digest in (supplied or {}).items():
            key = _index_key(Path(repo), raw)
            if key in merged and merged[key] != digest:
                raise FinalizerRefused('caller expectations conflict for one path')
            merged[key] = digest
        for p, digest in reviewed.items():
            if digest is None:  # Existing Git deletion guards own the absence check.
                continue
            if p in merged and merged[p] != digest:
                raise FinalizerRefused('caller expectations conflict with protected reviewed bytes')
            merged[p] = digest
        bound.arguments['expected_content_sha256'] = merged or None
        outcome = function(*bound.args, **bound.kwargs)
        sha = outcome.sha
        actual = set(_git(repo, ['diff-tree', '--root', '--no-commit-id', '--name-only', '-r', sha]).splitlines())
        if actual != set(declared):
            raise FinalizerRefused('actual host commit paths differ from declared pathspec')
        message = _git(repo, ['show', '-s', '--format=%B', sha])
        if [line for line in message.splitlines() if line.startswith('Session-Id:')] != ['Session-Id: ' + actor.session_id]:
            raise FinalizerRefused('host commit did not retain the native worker Session-Id')
        context['commits'].append({'sha': sha, 'paths': sorted(actual), 'session_id': actor.session_id,
                                   'vendor': 'codex', 'reviewed_content_sha256': reviewed,
                                   'host_generated_metadata': approved['host_metadata']})
        context['position'] = _position(repo)
        return outcome
    return call
