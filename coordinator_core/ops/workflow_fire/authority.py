"""Validate assigned-script delegation without promoting a worker to EM.

This is cooperative workflow scoping, not an OS security boundary. The caller
role is classified separately from the actual carried invocation context.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import NamedTuple

from coordinator_core.ops.workflow_fire import fire
from coordinator_core.ops.workflow_fire.receipt import fire_token_matches


class Principal(NamedTuple):
    session_id: str
    role: str
    env: dict
    grant_verified: bool = False


def fire_record_caller_session_id(principal: Principal) -> str | None:
    """Session id the fire record names as its caller: an EM or grant-verified headless EM is its own; others inherit the parent."""
    if principal.role == 'em' or principal.grant_verified:
        return principal.session_id
    return principal.env.get('COORDINATOR_PARENT_SESSION_ID')


def caller_principal(cwd: str | None = None) -> Principal:
    from coordinator_core.session import core
    from coordinator_core.session.agent_role import role_for_caller
    from coordinator_core.session.caller_env import effective_caller_env

    env = effective_caller_env()
    sid = core.attributable_session_id(cwd)
    try:
        role = role_for_caller(env, sid)
    except (ImportError, OSError, ValueError, TypeError):
        role = 'unknown'
    return Principal(sid or '', role, env)


def require_submission_em(session_id: str, cwd: str) -> str | None:
    """Return a refusal only for a session-identity mismatch; a non-EM caller is recorded, not refused."""
    caller = caller_principal(cwd)
    if not caller.session_id or caller.session_id != session_id:
        return 'The submitted session must match the actual invoking session.'
    if caller.role != 'em':
        _record_non_em_submit(caller.session_id, cwd)
    return None


def _record_non_em_submit(session_id: str, cwd: str) -> None:
    """Advisory only: no deny_registry irreversible class covers a non-EM submit."""
    try:
        from coordinator_core.guard_advisory_counter import record_advisory_fire
        record_advisory_fire('submit-engine-ask-non-em', session_id, cwd)
    except Exception:
        pass


def require_fire_authority(script_path: str, expected: str | None, *, cwd: str | None,
                           native: bool, backend: str) -> Principal:
    """Protect native firing and explicit worker callers; retain legacy Claude admission."""
    caller = caller_principal(cwd)
    if caller.role == 'em':
        return caller
    if caller.role == 'workflow-agent':
        valid, reason = assigned_script_delegation(script_path, expected, caller.env, cwd=cwd, backend=backend)
        if valid:
            return caller
        raise ValueError('Workflow delegation refused: ' + reason)
    if _grant_verified_headless_em(caller):
        return caller._replace(grant_verified=True)
    codex = (caller.env.get('COORDINATOR_AGENT_VENDOR') == 'codex'
             or caller.env.get('COORDINATOR_AGENT_PROVIDER') == 'codex'
             or bool(caller.env.get('CODEX_THREAD_ID')))
    if native or codex or caller.role in ('worker', 'reviewer'):
        raise ValueError('Workflow fire requires a verified EM or this exact assigned-script delegation; caller role is '
                         + caller.role)
    # The existing Claude harness keeps its own admission and guard chain.
    # An unknown legacy caller is not relabelled as an EM here.
    return caller


def _explicit_worker_marker(env: dict) -> bool:
    if str(env.get('COORDINATOR_AGENT_WORKER', '')).lower() in ('1', 'true', 'yes'):
        return True
    role = env.get('COORDINATOR_AGENT_ROLE')
    return bool(role) and role != 'em'


def _grant_verified_headless_em(caller: Principal) -> bool:
    """True for a launcher-granted headless codex|agy|grok EM; an explicit worker marker always refuses."""
    if not caller.session_id or _explicit_worker_marker(caller.env):
        return False
    from coordinator_core.ops.workflow_fire.headless_em_grant import headless_em_grant_matches
    return headless_em_grant_matches(caller.env, caller.session_id)


def assigned_script_delegation(script_path: str, expected: str | None,
                               caller_env: dict, *, cwd: str | None = None,
                               backend: str | None = None) -> tuple[bool, str]:
    """Require the same script, digest, receipt token and existing fire record.

    Ordinary operation parameters cannot supply a replacement receipt or fire
    record. No token, role or registry row is created by this check.
    """
    if not isinstance(expected, str) or re.fullmatch(r'[0-9a-f]{64}', expected) is None:
        return False, 'delegated execution requires the assigned script digest'
    try:
        script = Path(script_path).resolve(strict=True)
        repo = fire._resolve_target_repo(script, cwd)
        fire._assert_script_under_repo_root(script, repo)
        if hashlib.sha256(script.read_bytes()).hexdigest() != expected:
            return False, 'assigned script bytes changed'
        receipt = json.loads(script.with_name(script.name + '.emitted.json').read_text(encoding='utf-8'))
        if not isinstance(receipt, dict) or not fire_token_matches(receipt, caller_env):
            return False, 'assigned script receipt token does not match'
        fire_id = receipt.get('fire', {}).get('fire_id')
        if not isinstance(fire_id, str) or re.fullmatch(r'[0-9a-f]{32}', fire_id) is None:
            return False, 'assigned script receipt has no canonical fire handle'
        record = fire._read_record(fire._record_path(fire._registry_dir(repo), fire_id))
        if not isinstance(record, dict):
            return False, 'assigned fire record is missing'
        if record.get('fire_id') != fire_id or record.get('sha256') != expected:
            return False, 'assigned fire record does not bind this digest'
        recorded_script = record.get('script_path')
        if not isinstance(recorded_script, str) or Path(recorded_script).resolve() != script:
            return False, 'assigned fire record names a different script'
        recorded_repo = record.get('repo_root')
        if not isinstance(recorded_repo, str) or Path(recorded_repo).resolve() != Path(repo).resolve():
            return False, 'assigned fire record names a different repository'
        if record.get('state') not in ('pending', 'running'):
            return False, 'assigned fire is no longer active'
        owner = record.get('caller_session_id')
        if not isinstance(owner, str) or not owner or caller_env.get('COORDINATOR_PARENT_SESSION_ID') != owner:
            return False, 'assigned fire does not bind this worker parent'
        if backend is not None and record.get('backend') != backend:
            return False, 'assigned fire uses a different backend'
        return True, 'assigned script delegation verified'
    except (OSError, ValueError, TypeError, AttributeError, fire.RepoRootUnresolvableError,
            fire.ScriptOutsideRepoRootError):
        return False, 'assigned script delegation evidence is unreadable or invalid'
