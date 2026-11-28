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
import shlex
import threading

_ACTIVE = ContextVar('private_terminal_action', default=None)
_LOCK = threading.RLock()
OPS = frozenset({'dispatch.terminal_commit', 'dispatch.land_run'})


class FinalizerRefused(RuntimeError):
    pass


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def _write(path, value) -> None:
    path = Path(path)
    temporary = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
    with temporary.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(_json(value) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _git(repo, args) -> str:
    from coordinator_core.git.run import run_git
    reply = run_git(args, cwd=str(repo))
    if not reply.ok:
        raise FinalizerRefused('host Git observation failed: ' + ' '.join(args[:2]))
    return reply.stdout.strip()


def _position(repo) -> dict[str, str]:
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

    def read(path) -> str:
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
    issued_sizing = None
    if manifest and request:
        from coordinator_core.ops.warp_run_deliverable import record_sizing_rel
        sizing = record_sizing_rel(root, Path(manifest_path).parent.name)
        owners = [chunk for chunk in request.chunks if sizing in chunk.paths]
        if (sizing.startswith('state/sizings/') and sizing.endswith('.yaml') and len(owners) == 1
                and owners[0].id == 'sizing' and list(owners[0].paths) == [sizing]
                and sizing not in manifest.review_declared_paths):
            # This is the original run input, bound at staging before any product worker.
            read(sizing)
            issued_sizing = sizing
    return {'script_path': Path(script).relative_to(root).as_posix(),
            'files': files, 'issued_sizing': issued_sizing, 'declared_paths': sorted({p.replace('\\', '/')
            for chunk in (request.chunks if request else ()) for p in chunk.paths}),
            'declared_prefixes': sorted({p.replace('\\', '/')
            for chunk in (request.chunks if request else ()) for p in chunk.prefixes}),
            'manifest_declared_paths': sorted(manifest.review_declared_paths) if manifest else [],
            'has_request': request is not None}


def verify_contract(repo, contract) -> None:
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


def durable_landing_contract(repo, staged, root):
    """Token redemption retires the executable twin. Keep checking its original scope at the Git write."""
    verify_contract(repo, staged)
    original = snapshot_contract(repo, Path(root['script_path']))
    root_files = dict(original['files'])
    staged_files = dict(staged['files'])
    if (root_files.pop(original['script_path']) != root['script_sha256']
            or staged_files.pop(staged['script_path'], None) is None or staged_files != root_files
            or any(original[key] != staged[key] for key in
                   ('has_request', 'issued_sizing', 'declared_paths', 'declared_prefixes', 'manifest_declared_paths'))):
        raise FinalizerRefused('durable root and issued staged workflow differ in landing scope')
    return original


class ObservedLanding:
    """Single host-issued product request. Native command events acknowledge only its exact command."""

    def __init__(self, event, record, contract, reply_stream, *, sizing_actor=None):
        from coordinator_core.ops.dispatch_emit.land_run_contract import mode_of
        from coordinator_core.ops.dispatch_emit.terminal_commit_params_channel import prompt_params
        from coordinator_core.ops.dispatch_emit import run_token

        self.repo = Path(record['repo_root']).resolve(strict=True)
        self.request_id = str(event['id'])
        self.fire_id = record['fire_id']
        self.parent = record['caller_session_id']
        params = prompt_params(event['prompt'])
        root = record.get('emitted_root') or record
        options = event['options']
        if (options.get('phase'), options.get('label'), options.get('agentType')) != (
                'commit', 'commit', 'coordinator:git-commit-agent'):
            raise FinalizerRefused('host landing requires the product commit worker')
        if (not params or mode_of(params) != 'product' or not isinstance(params.get('inline_review'), dict)
                or params.get('session_id') != self.parent or params['run_id'] != root['run_id']
                or (self.repo / params['script_path']).resolve(strict=True) != Path(root['script_path'])
                or not run_token.admits(self.repo, params['run_id'], params['run_token'], root['script_path'])):
            raise FinalizerRefused('commit assignment differs from issued root, caller or token')
        self.contract = durable_landing_contract(self.repo, contract, root)
        self.sizing_session = None
        if sizing_actor is not None:
            run_id, scout = sizing_actor
            if (run_id != params['run_id'] or scout.fire_id != self.fire_id
                    or scout.parent_session_id != self.parent or not os.path.samefile(scout.repo_root, self.repo)):
                raise FinalizerRefused('observed sizing session differs from landing assignment')
            self.sizing_session = scout.session_id
        self.params = params
        self.position = _position(self.repo)
        self.reply_stream = reply_stream
        self.command = shlex.join(['python3', str(Path(__file__).with_name('landing_wait.py')), str(reply_stream.name)])
        self.consumed = False
        self.result = None

    def acknowledge(self, actor, command):
        from coordinator_core.session.native_actor import bind_verified_native_actor
        from coordinator_core.ops.dispatch_emit.land_run import _handler
        if not isinstance(command, str):
            return
        try:
            shell = shlex.split(command)
        except (ValueError, TypeError):
            return
        if (len(shell) != 3 or Path(shell[0]).name not in {'bash', 'sh', 'zsh'}
                or shell[1] not in {'-c', '-lc'} or shell[2] != self.command):
            return
        if (actor.request_id != self.request_id or actor.fire_id != self.fire_id
                or actor.parent_session_id != self.parent or actor.session_id == self.parent):
            raise FinalizerRefused('worker does not own this host landing assignment')
        if self.consumed:
            raise FinalizerRefused('host landing command already consumed')
        with bind_verified_native_actor(actor):
            _actor(self.repo)
            if _position(self.repo) != self.position:
                raise FinalizerRefused('branch or HEAD moved before host landing')
            verify_contract(self.repo, self.contract)
            self.consumed = True
            context = {'actor': actor, 'repo': self.repo, 'position': self.position, 'commits': [],
                       'assignment': {'terminal_contract': self.contract, 'plan_owner_session_id': self.parent},
                       'observed_landing': True, 'run_id': self.params['run_id'], 'sizing_session': self.sizing_session}
            token = _ACTIVE.set(context)
            try:
                result = _handler({**self.params, 'session_id': actor.session_id}, self.repo)
                if result.get('committed') and (not context['commits']
                        or result.get('sha') != context['commits'][0]['sha']):
                    raise FinalizerRefused('host landing reply differs from observed product commit')
                self.result = result
            finally:
                _ACTIVE.reset(token)
        self.reply_stream.write(_json(self.result).encode())
        self.reply_stream.flush()


def require_session(params):
    """A private worker cannot override commit attribution, even through nested dispatch."""
    from coordinator_core.warm.workflow_peer import PRIVATE_ROSTER_ENV
    if not os.environ.get(PRIVATE_ROSTER_ENV) and not (_ACTIVE.get() or {}).get('observed_landing'):
        return None
    actor = _actor(_ACTIVE.get()['repo'] if _ACTIVE.get() else '')
    if _ACTIVE.get() is None or params.get('session_id', actor.session_id) != actor.session_id:
        raise FinalizerRefused('commit requires the assigned live worker session')
    return actor.session_id


def observed_sizing_session(repo, run_id):
    """Only the same run's host-observed sizing worker can discharge its own path claim."""
    context = _ACTIVE.get()
    if (context and context.get('observed_landing') and context['run_id'] == run_id
            and os.path.samefile(repo, context['repo'])):
        return context['sizing_session']
    return None


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


def authorize_product(repo, paths, deleted, reviewed, host_metadata=None, *, supplied_blobs=None, merging: bool = False) -> None:
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
    issued_sizing = contract.get('issued_sizing')
    if (context.get('observed_landing') and issued_sizing in actual and issued_sizing not in deleted
            and issued_sizing not in contract['manifest_declared_paths'] and issued_sizing in contract['files']):
        metadata.add(issued_sizing)
        sources['gate-issued-sizing'] = [issued_sizing]
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
        if p == issued_sizing and expectations[p] != contract['files'][p]:
            raise FinalizerRefused('issued sizing bytes changed before landing')
    context['approved_commit'] = {'paths': sorted(actual), 'hashes': expectations,
                                'host_metadata': {kind: sorted(set(items) & actual & metadata)
                                                  for kind, items in sources.items()}}


def guarded_commit(function):
    signature = inspect.signature(function)
    @wraps(function)
    def call(*args, **kwargs):
        from coordinator_core.warm.workflow_peer import PRIVATE_ROSTER_ENV
        if not os.environ.get(PRIVATE_ROSTER_ENV) and not (_ACTIVE.get() or {}).get('observed_landing'):
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
        from coordinator_core.git.commit_trailers import can_format_trailers_in_process, format_trailers_in_process
        message = bound.arguments['message']
        expected_trailer = 'Session-Id: ' + actor.session_id
        present = [line for line in message.splitlines() if line.lower().startswith('session-id:')]
        if present and present != [expected_trailer]:
            raise FinalizerRefused('host commit conflicts with the native worker Session-Id')
        if not present:
            raw = message.encode('utf-8', errors='surrogateescape')
            if not can_format_trailers_in_process(raw):
                raise FinalizerRefused('host commit cannot safely format the native worker Session-Id')
            bound.arguments['message'] = format_trailers_in_process(raw, [expected_trailer]).decode(
                'utf-8', errors='surrogateescape')
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
