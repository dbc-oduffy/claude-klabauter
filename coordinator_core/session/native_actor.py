"""Server-owned Codex exec facts. Internal typing is not an authorization boundary."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import math
import os
import uuid

BIRTH_TOLERANCE = 0.000001  # Existing workflow_peer observer contract.

class NativeActorMismatch(RuntimeError):
    """Verified actor cannot safely initialize this session."""

@dataclass(frozen=True)
class VerifiedCodexExecActor:
    session_id: str
    repo_root: str
    pid: int
    start_epoch: float
    exe: str
    exe_sha256: str
    parent_session_id: str
    fire_id: str
    request_id: str | int

_ACTOR = ContextVar("verified_codex_exec_actor", default=None)

def current_verified_native_actor():
    return _ACTOR.get()

@contextmanager
def bind_verified_native_actor(actor):
    # Caller is the trusted server/observer AFTER protected validation. Never wire input.
    if actor is not None and type(actor) is not VerifiedCodexExecActor:
        raise NativeActorMismatch("typed server-owned actor required")
    token = _ACTOR.set(actor)
    try:
        yield actor
    finally:
        _ACTOR.reset(token)

def validate_actor(actor, sid, cwd):
    """Recheck target/process freshness, not the owner's protected proof protocol."""
    try:
        if type(actor) is not VerifiedCodexExecActor or str(uuid.UUID(sid)) != sid or actor.session_id != sid:
            raise ValueError("session mismatch")
        if not cwd or not os.path.samefile(actor.repo_root, cwd):
            raise ValueError("repository mismatch")
        if type(actor.pid) is not int or actor.pid <= 0 or isinstance(actor.start_epoch, bool) or not math.isfinite(actor.start_epoch) or actor.start_epoch <= 0:
            raise ValueError("process generation mismatch")
        import psutil
        from coordinator_core.warm.workflow_peer import kernel_live_pid
        if not kernel_live_pid(actor.pid):
            raise ValueError("kernel process is not live")
        process = psutil.Process(actor.pid)
        if process.status() in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
            raise ValueError("process is terminal")
        if abs(process.create_time() - actor.start_epoch) > BIRTH_TOLERANCE:
            raise ValueError("process birth mismatch")
        if not os.path.samefile(process.exe(), actor.exe):
            raise ValueError("executable mismatch")
        # A second read closes generation changes while inspecting executable metadata.
        fresh = psutil.Process(actor.pid)
        if (not kernel_live_pid(actor.pid) or not fresh.is_running()
                or abs(fresh.create_time() - actor.start_epoch) > BIRTH_TOLERANCE):
            raise ValueError("process birth changed")
        return actor
    except Exception as exc:
        raise NativeActorMismatch("native actor freshness/target verification failed") from exc

def precise_pid_alive(pid, birth):
    try:
        import psutil
        value = float(birth)
        if not math.isfinite(value) or value <= 0:
            return False
        from coordinator_core.warm.workflow_peer import kernel_live_pid
        if not kernel_live_pid(pid):
            return False
        process = psutil.Process(pid)
        fresh = psutil.Process(pid)
        return (kernel_live_pid(pid) and fresh.is_running()
                and abs(fresh.create_time() - value) <= BIRTH_TOLERANCE
                and process.status() not in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD)
                and abs(process.create_time() - value) <= BIRTH_TOLERANCE)
    except Exception:
        return False  # Native-only unknown is never verified live; legacy policy unchanged.
