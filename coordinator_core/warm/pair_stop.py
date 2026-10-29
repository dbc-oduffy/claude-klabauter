"""Stop the warm supervisor, front door and server for one engine root, then verify by command line.

Order is supervisor, front door, then server: a live supervisor can respawn a server that was
just stopped. A final cmdline sweep decides success; whatever it still finds is a survivor.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from coordinator_core.session.core import stable_pid_alive
from coordinator_core.warm import breadcrumb

ROLE_SUPERVISOR = "supervisor"
ROLE_FRONT_DOOR = "front_door"
ROLE_SERVER = "server"
ROLE_POOL_CHILD = "pool_child"

_ROLE_SCRIPTS = {
    ROLE_SUPERVISOR: ("coordinator_core", "warm", "supervisor.py"),
    ROLE_FRONT_DOOR: ("coordinator_core", "warm", "front_door.py"),
    ROLE_SERVER: ("coordinator_core", "warm", "server.py"),
}

# Bound on the re-sweep loop: a server that appears mid-stop (a duplicate, a late respawn) is taken
# in a later pass, not missed by a single one.
_RESWEEP_DEADLINE_SECS = 15.0

_ASK_READ_DEADLINE_SECS = 2.0
_TERMINATE_GRACE_SECS = 5.0
_TERMINATE_POLL_INTERVAL_SECS = 0.1

# Sized above warm.lifecycle's drain ceiling: a graceful ask is answered by a drain, not an exit.
_ASK_CONFIRM_GRACE_SECS = 40.0

# A deliberately wrong token: warm.server._serve_line evicts (drain and exit) on any mismatch.
_STOP_REQUEST_TOKEN = "warm-engine-stop-requested-0000"

# warm.server's dispatch-indeterminate code: the ask went through dispatch and timed out, so the
# server is wedged and an answer carrying it is no acknowledgement of a stop.
_DISPATCH_TIMEOUT_CODE = -32004

_PARENT_PID_RE = re.compile(r"parent_pid=(\d+)(?!\d)")


@dataclass(frozen=True)
class PairProcess:
    pid: int
    role: str
    cmdline: str


@dataclass(frozen=True)
class PairStopResult:
    stopped: tuple[PairProcess, ...]
    survivors: tuple[PairProcess, ...]
    failures: tuple[tuple[int, str], ...] = ()

    def ok(self) -> bool:
        return not self.survivors

    def render_survivors(self) -> str:
        why = dict(self.failures)
        return "\n".join(
            f"pid={p.pid} role={p.role} why={why.get(p.pid, 'respawned or still present after sweep')}"
            f" cmd={p.cmdline}"
            for p in self.survivors
        )


def _norm(path: object) -> str:
    """Comparison key: an absolute path is resolved (junction, symlink, 8.3 name, subst drive);
    a relative one keeps the normpath form so it never matches by accident of cwd."""
    from coordinator_core.write_guards._case_fold_path import strip_extended_length_prefix

    normed = os.path.normpath(str(path))
    if os.path.isabs(normed):
        try:
            normed = os.path.realpath(normed)
        except (OSError, ValueError):
            pass
    return os.path.normcase(strip_extended_length_prefix(normed))


def _role_targets(engine_root: Path) -> dict[str, str]:
    return {
        role: _norm(Path(engine_root).joinpath(*parts))
        for role, parts in _ROLE_SCRIPTS.items()
    }


def find_pair_processes(engine_root: Path, *, psutil_module=None) -> list[PairProcess]:
    """Python processes whose argv holds this root's supervisor.py, front_door.py or server.py.

    Trap: an exact argv-element compare, never a substring and never "anything python-ish";
    a sibling root such as `<root>-old` and a `-m` launch do not match. The root counts only
    inside the role's .py element: a root carried as a non-.py element (`--root <root>`, `-m`)
    is not a match key.
    """
    psutil = psutil_module
    if psutil is None:
        import psutil

    targets = _role_targets(engine_root)
    found: list[PairProcess] = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if not name.startswith("python"):
                continue
            argv = proc.cmdline()
            pid = proc.info["pid"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        # Only a script element can name a role target; realpath on every argv element
        # of every python process is wasted filesystem work on the hot path.
        normed = {_norm(a) for a in argv if a.lower().endswith(".py")}
        for role, target in targets.items():
            if target in normed:
                found.append(PairProcess(pid=pid, role=role, cmdline=" ".join(argv)))
                break
    return found


def _read_line_with_deadline(fh, deadline_secs: float) -> "bytes | None":
    import threading

    result: dict = {}

    def _read() -> None:
        try:
            result["line"] = fh.readline()
        except OSError as exc:
            result["exc"] = exc

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    reader.join(deadline_secs)
    if reader.is_alive() or "exc" in result:
        return None
    return result.get("line")


def _open_pipe(pipe: str):
    return open(pipe, "r+b")


def _is_stop_acknowledgement(line: "bytes | None") -> bool:
    """A reply counts only when it is not the wedged-dispatch timeout envelope."""
    if not line:
        return False
    try:
        reply = json.loads(line)
    except ValueError:
        return True
    error = reply.get("error") if isinstance(reply, dict) else None
    if isinstance(error, dict) and error.get("code") == _DISPATCH_TIMEOUT_CODE:
        return False
    return True


def _ask_server_to_stop(pipe: str) -> bool:
    """Graceful ask over the warm pipe. True iff the server answered within the read deadline
    with something other than the -32004 dispatch-timeout envelope; False on a transport
    failure, an unanswered ask, or a wedged-dispatch answer, the caller's cue to
    signal directly instead of waiting out the confirm grace."""
    request = {
        "jsonrpc": "2.0",
        "id": "warm-engine-stop",
        "method": "engine.stop_probe",
        "params": {},
        "_engine_token": _STOP_REQUEST_TOKEN,
    }
    payload = json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n"
    try:
        fh = _open_pipe(pipe)
    except OSError:
        return False
    try:
        try:
            fh.write(payload)
            fh.flush()
        except OSError:
            return False
        return _is_stop_acknowledgement(_read_line_with_deadline(fh, _ASK_READ_DEADLINE_SECS))
    finally:
        try:
            fh.close()
        except OSError:
            pass


def _pid_confirmed_gone(pid: int, stored_epoch_str: str) -> bool:
    return not stable_pid_alive(pid, stored_start_epoch=stored_epoch_str)


def _wait_for_pid_gone(
    pid: int,
    stored_epoch_str: str,
    *,
    deadline_secs: float,
    poll_interval: float = _TERMINATE_POLL_INTERVAL_SECS,
) -> bool:
    deadline = time.monotonic() + deadline_secs
    while True:
        if _pid_confirmed_gone(pid, stored_epoch_str):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval)


def _terminate_with_reason(pid: int, *, psutil_module=None) -> Optional[str]:
    """terminate(), then kill() after the grace window. None iff confirmed gone, else why not.
    Signals only a pid the caller already identified."""
    psutil = psutil_module
    if psutil is None:
        import psutil

    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return None

    try:
        proc.terminate()
    except psutil.NoSuchProcess:
        return None
    except psutil.AccessDenied:
        return "terminate denied (access denied)"

    deadline = time.monotonic() + _TERMINATE_GRACE_SECS
    while time.monotonic() < deadline:
        if not proc.is_running():
            return None
        time.sleep(_TERMINATE_POLL_INTERVAL_SECS)

    try:
        proc.kill()
    except psutil.NoSuchProcess:
        return None
    except psutil.AccessDenied:
        return "kill denied (access denied)"

    deadline = time.monotonic() + _TERMINATE_GRACE_SECS
    while time.monotonic() < deadline:
        if not proc.is_running():
            return None
        time.sleep(_TERMINATE_POLL_INTERVAL_SECS)
    return None if not proc.is_running() else "still running after terminate and kill"


def _terminate_pid(pid: int, *, psutil_module=None) -> bool:
    return _terminate_with_reason(pid, psutil_module=psutil_module) is None


def _pool_children(
    server_pids: set[int], *, psutil_module=None
) -> list[PairProcess]:
    """Python `spawn_main` processes whose `parent_pid=<n>` names one of `server_pids`.

    Trap: a pool child outlives its server's exit and is reparented, so a live-children walk
    misses it; the spawn argv still names the dead parent. Exact `parent_pid=<n>` element
    compare, only for a server pid this stop identified.
    """
    psutil = psutil_module
    if psutil is None:
        import psutil

    wanted = {f"parent_pid={pid}" for pid in server_pids}
    found: list[PairProcess] = []
    if not wanted:
        return found
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if not name.startswith("python"):
                continue
            argv = proc.cmdline()
            pid = proc.info["pid"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if any("spawn_main" in a for a in argv) and any(
            f"parent_pid={m}" in wanted
            for a in argv
            for m in _PARENT_PID_RE.findall(a)
        ):
            found.append(PairProcess(pid=pid, role=ROLE_POOL_CHILD, cmdline=" ".join(argv)))
    return found


def _live_descendants(pid: int, *, psutil_module=None) -> list[int]:
    psutil = psutil_module
    if psutil is None:
        import psutil
    try:
        return [c.pid for c in psutil.Process(pid).children(recursive=True)]
    except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
        return []


def _record_pid(record: Optional[dict]) -> Optional[tuple[int, str]]:
    """(pid, stored_epoch_str) of a record whose pid is still the same process, else None."""
    if not isinstance(record, dict):
        return None
    pid = record.get("pid")
    if not isinstance(pid, int):
        return None
    epoch = record.get("stable_pid_start_epoch")
    epoch_str = str(epoch) if epoch is not None else ""
    if not stable_pid_alive(pid, stored_start_epoch=epoch_str):
        return None
    return pid, epoch_str


def stop_warm_pair(
    engine_root: Path,
    *,
    psutil_module=None,
    ask_server: Optional[Callable[[str], bool]] = None,
) -> PairStopResult:
    """Stop the supervisor, then the front door, then the server, of `engine_root`; return who
    stopped and who survived.

    Trap: a pid is signalled only when a verified record (pid plus birth instant) or the exact
    cmdline match identified it; the cmdline match is the identification the old breadcrumb-only
    rule lacked, and it is still exact-path, never "anything python-ish".
    """
    from coordinator_core.warm import front_door, supervisor

    ask = ask_server if ask_server is not None else _ask_server_to_stop
    engine_root = Path(engine_root)
    before = find_pair_processes(engine_root, psutil_module=psutil_module)
    seen: dict[int, PairProcess] = {p.pid: p for p in before}

    sup_pids: dict[int, str] = {p.pid: p.cmdline for p in before if p.role == ROLE_SUPERVISOR}
    disc = _record_pid(supervisor.read_discovery(engine_root))
    if disc is not None:
        sup_pids.setdefault(disc[0], "")
    for pid in sup_pids:
        _terminate_pid(pid, psutil_module=psutil_module)

    fd_pids: dict[int, str] = {p.pid: p.cmdline for p in before if p.role == ROLE_FRONT_DOOR}
    fd_disc = _record_pid(front_door.read_discovery(engine_root))
    if fd_disc is not None:
        fd_pids.setdefault(fd_disc[0], "")
    for pid in fd_pids:
        _terminate_pid(pid, psutil_module=psutil_module)

    crumb = breadcrumb.read_breadcrumb(engine_root)
    verified = _record_pid(crumb)
    server_pids: set[int] = {p.pid for p in before if p.role == ROLE_SERVER}
    child_pids: set[int] = set()
    if verified is not None:
        server_pids.add(verified[0])
    for spid in server_pids:
        child_pids.update(_live_descendants(spid, psutil_module=psutil_module))
    if verified is not None:
        pid, epoch_str = verified
        pipe = crumb.get("pipe") if isinstance(crumb, dict) else None
        asked = isinstance(pipe, str) and ask(pipe)
        gone = asked and _wait_for_pid_gone(pid, epoch_str, deadline_secs=_ASK_CONFIRM_GRACE_SECS)
        if not gone:
            _terminate_pid(pid, psutil_module=psutil_module)

    failures: dict[int, str] = {}
    order = {ROLE_SUPERVISOR: 0, ROLE_FRONT_DOOR: 1, ROLE_SERVER: 2, ROLE_POOL_CHILD: 3}
    deadline = time.monotonic() + _RESWEEP_DEADLINE_SECS
    while True:
        swept = find_pair_processes(engine_root, psutil_module=psutil_module)
        server_pids.update(p.pid for p in swept if p.role == ROLE_SERVER)
        swept += _pool_children(server_pids, psutil_module=psutil_module)
        for p in swept:
            seen.setdefault(p.pid, p)
        targets = {p.pid: p.role for p in swept}
        for cpid in child_pids:
            targets.setdefault(cpid, ROLE_POOL_CHILD)
        child_pids.clear()
        fresh = sorted(
            (pid for pid in targets if pid not in failures),
            key=lambda pid: (order.get(targets[pid], 9), pid),
        )
        if not fresh:
            break
        for pid in fresh:
            reason = _terminate_with_reason(pid, psutil_module=psutil_module)
            if reason is None:
                # Confirmed gone; a pid still enumerated afterwards is a stale entry, not a new server.
                failures.setdefault(pid, "respawned or still present after sweep")
            else:
                failures[pid] = reason
        if time.monotonic() >= deadline:
            break

    survivors = tuple(
        find_pair_processes(engine_root, psutil_module=psutil_module)
        + _pool_children(server_pids, psutil_module=psutil_module)
    )
    survivor_pids = {p.pid for p in survivors}
    stopped = tuple(p for pid, p in seen.items() if pid not in survivor_pids)

    result = PairStopResult(
        stopped=stopped,
        survivors=survivors,
        failures=tuple(sorted((pid, why) for pid, why in failures.items() if pid in survivor_pids)),
    )
    if result.ok():
        breadcrumb.unlink_breadcrumb(engine_root)
        supervisor.unlink_discovery(engine_root)
        front_door.unlink_discovery(engine_root)
    return result
