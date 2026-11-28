"""Stop the warm supervisor, front door and server for one engine root, then verify by command line.

Order is supervisor, server, then front door: a live supervisor can respawn a server that was
just stopped, and the server and door are asked to drain in-flight work before any is signalled.
A final cmdline sweep decides success; whatever it still finds is a survivor.
"""

from __future__ import annotations

import json
import os
import re
import sys
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

# Survivor settle: the stall window restarts whenever the live count drops, so a pair that is
# visibly exiting under IO load is waited for; _SETTLE_MAX_SECS caps the marker-held window.
_SETTLE_SECS = 10.0
_SETTLE_MAX_SECS = 30.0

_ASK_READ_DEADLINE_SECS = 2.0
_OUTSTANDING_READ_DEADLINE_SECS = 0.4
_TERMINATE_GRACE_SECS = 5.0
_TERMINATE_POLL_INTERVAL_SECS = 0.1

# Added to warm.lifecycle's drain ceiling: a graceful ask is answered by a drain, not an exit.
_CONFIRM_GRACE_MARGIN_SECS = 5.0


def _confirm_grace_secs() -> float:
    from coordinator_core.warm import lifecycle

    return lifecycle._drain_ceiling_secs() + _CONFIRM_GRACE_MARGIN_SECS


def _drain_wait_secs(outcome) -> float:
    """How long to wait for a server that answered the stop ask. An acknowledged server is draining
    and waits out its own admitted ops (the longest bounded by `warm.client.MUTATION_READ_DEADLINE_SECS`
    or an `ipc._OP_TIMEOUT_OVERRIDES` row), so it is never signalled before that bound; an unanswered
    ask keeps the plain confirm grace."""
    grace = _confirm_grace_secs()
    if outcome is not True:
        return grace
    from coordinator_core import ipc
    from coordinator_core.warm import client

    longest = max([client.MUTATION_READ_DEADLINE_SECS, *ipc._OP_TIMEOUT_OVERRIDES.values()])
    return max(grace, longest + _CONFIRM_GRACE_MARGIN_SECS)


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
    # Pids the stop set out to end; None means unknown, so every survivor counts as stuck.
    initial_pids: Optional[frozenset[int]] = None
    in_flight: Optional[int] = None

    def ok(self) -> bool:
        return not self.survivors

    def stuck(self) -> tuple[PairProcess, ...]:
        """Survivors the stop failed to end: present at its start, or a terminate refused. A pid
        born after the start is a respawn by a peer's hook call while no marker was held, which
        the marked stop takes; under load that respawn lands within milliseconds of any unmarked stop."""
        if self.initial_pids is None:
            return self.survivors
        why = dict(self.failures)
        return tuple(p for p in self.survivors if p.pid in self.initial_pids or p.pid in why)

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


def find_servers_under_other_roots(engine_root: Path, *, psutil_module=None) -> list[tuple[int, str]]:
    """`(pid, root)` of warm servers running from a root other than `engine_root`.

    Lets a stop that found nothing for its own root say where the engine it was
    probably meant to stop is running (a moved or re-cloned tree). Read-only:
    one process scan, no filesystem work beyond normalising the root.
    """
    psutil = psutil_module
    if psutil is None:
        import psutil

    mine = _norm(engine_root)
    tail = _ROLE_SCRIPTS[ROLE_SERVER]
    found: list[tuple[int, str]] = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if not (proc.info.get("name") or "").lower().startswith("python"):
                continue
            argv = proc.cmdline()
            pid = proc.info["pid"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        for a in argv:
            if not a.lower().endswith(".py"):
                continue
            segs = os.path.normpath(a).replace("\\", "/").split("/")
            if tuple(segs[-3:]) == tail:
                root = os.path.normpath(a)
                for _ in tail:
                    root = os.path.dirname(root)
                if _norm(root) != mine:
                    found.append((pid, root))
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
    """Client end of the server endpoint: a named pipe on Windows, an AF_UNIX socket elsewhere,
    where open() on the socket path fails and the ask would never reach the server."""
    from coordinator_core.warm import client

    return client._open_pipe(pipe)


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


def _is_wedged_reply(line: "bytes | None") -> bool:
    if not line:
        return False
    try:
        reply = json.loads(line)
    except ValueError:
        return False
    error = reply.get("error") if isinstance(reply, dict) else None
    return isinstance(error, dict) and error.get("code") == _DISPATCH_TIMEOUT_CODE


_ASK_ACKNOWLEDGED = "acknowledged"
_ASK_WEDGED = "wedged"
_ASK_UNANSWERED = "unanswered"


def _probe_exchange(
    pipe: str, params: dict, deadline_secs: float = _ASK_READ_DEADLINE_SECS
) -> "bytes | None":
    """One `engine.stop_probe` frame over `pipe`: the raw reply line, None when unanswered."""
    request = {
        "jsonrpc": "2.0",
        "id": "warm-engine-stop",
        "method": "engine.stop_probe",
        "params": params,
        "_engine_token": _STOP_REQUEST_TOKEN,
    }
    payload = json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n"
    try:
        fh = _open_pipe(pipe)
    except OSError:
        return None
    try:
        try:
            fh.write(payload)
            fh.flush()
        except OSError:
            return None
        return _read_line_with_deadline(fh, deadline_secs)
    finally:
        try:
            fh.close()
        except OSError:
            pass


def _ask_server_outcome(pipe: str) -> str:
    """Graceful ask over the warm pipe: _ASK_ACKNOWLEDGED, _ASK_WEDGED (the -32004 envelope: the
    server proved its dispatch is stuck, so no drain is coming), or _ASK_UNANSWERED (transport
    failure or no reply within the read deadline)."""
    line = _probe_exchange(pipe, {})
    if _is_wedged_reply(line):
        return _ASK_WEDGED
    return _ASK_ACKNOWLEDGED if _is_stop_acknowledgement(line) else _ASK_UNANSWERED


def read_server_outstanding(pipe: str) -> Optional[int]:
    """The server's outstanding count (this read excluded), None when unanswered. Never raises."""
    try:
        line = _probe_exchange(pipe, {"report_outstanding": True}, _OUTSTANDING_READ_DEADLINE_SECS)
        reply = json.loads(line) if line else None
    except Exception:  # noqa: BLE001 -- a report that cannot be read is unknown, never a failed stop
        return None
    result = reply.get("result") if isinstance(reply, dict) else None
    count = result.get("outstanding") if isinstance(result, dict) else None
    return count if isinstance(count, int) and not isinstance(count, bool) else None


def _ask_server_to_stop(pipe: str) -> bool:
    """True iff the server answered within the read deadline with something other than the
    -32004 dispatch-timeout envelope."""
    return _ask_server_outcome(pipe) == _ASK_ACKNOWLEDGED


def _ask_server_with_wedge_signal(pipe: str):
    """_ASK_WEDGED, True (acknowledged) or False (unanswered); stop_warm_pair's default ask."""
    outcome = _ask_server_outcome(pipe)
    if outcome == _ASK_WEDGED:
        return _ASK_WEDGED
    return outcome == _ASK_ACKNOWLEDGED


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

    _wait_exit(proc, psutil, _TERMINATE_GRACE_SECS)
    return None if not proc.is_running() else "still running after terminate and kill"


def _wait_exit(proc, psutil, secs: float) -> None:
    """Block until `proc` exits or `secs` pass: a process-handle wait, not a poll, so a slow
    teardown under IO load costs no wakeups. A proc object without wait() is polled."""
    wait = getattr(proc, "wait", None)
    if wait is not None:
        try:
            wait(timeout=secs)
        except psutil.NoSuchProcess:
            pass
        except getattr(psutil, "TimeoutExpired", ()):
            pass
        except psutil.AccessDenied:
            pass
        return
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline and proc.is_running():
        time.sleep(_TERMINATE_POLL_INTERVAL_SECS)


def _pid_gone(pid: int, *, psutil_module=None) -> bool:
    """True iff the pid no longer names a running process; AccessDenied is not gone."""
    psutil = psutil_module
    if psutil is None:
        import psutil

    try:
        return not psutil.Process(pid).is_running()
    except psutil.NoSuchProcess:
        return True
    except psutil.AccessDenied:
        return False


def probe_pair_terminable(
    engine_root: Path, *, psutil_module=None
) -> list[tuple[int, str, str]]:
    """(pid, role, why) of each pair process of `engine_root` psutil cannot signal; signals nothing.

    Trap: reads only (Process, is_running); never terminate()/kill().
    """
    psutil = psutil_module
    if psutil is None:
        import psutil

    engine_root = Path(engine_root)
    pair = find_pair_processes(engine_root, psutil_module=psutil)
    servers = {p.pid for p in pair if p.role == ROLE_SERVER}
    pair += _pool_children(servers, psutil_module=psutil)
    blocked: list[tuple[int, str, str]] = []
    for p in pair:
        try:
            psutil.Process(p.pid).is_running()
        except psutil.AccessDenied:
            blocked.append((p.pid, p.role, "access denied"))
        except psutil.NoSuchProcess:
            continue
    return blocked


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


def _create_time(pid: int, psutil) -> Optional[float]:
    try:
        return psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
        return None


def _gone_or_reused(pid: int, born: Optional[float], psutil_module=None) -> bool:
    """Gone, or the pid now names a process born at another instant (pid reuse is no survivor)."""
    if _pid_gone(pid, psutil_module=psutil_module):
        return True
    if born is None:
        return False
    psutil = psutil_module
    if psutil is None:
        import psutil
    now = _create_time(pid, psutil)
    return now is not None and now != born


_POOL_HELPER_MARKERS = ("spawn_main", "resource_tracker")


def _is_pair_role_child(proc, parent_pid: Optional[int] = None) -> bool:
    """A multiprocessing pool worker/tracker or a pair entry script; anything else a server's op
    launched (an agy/codex peer session, an app session) is not the pair's to end.

    With `parent_pid`, a pool-helper match must be that pid's direct child: a pool of a session
    an op launched is a grandchild and stays."""
    try:
        name = (proc.info.get("name") if getattr(proc, "info", None) else None) or proc.name()
        if not name.lower().startswith("python"):
            return False
        argv = proc.cmdline()
    except Exception:  # noqa: BLE001 -- unreadable means not provably a pair process
        return False
    if any(m in a for a in argv for m in _POOL_HELPER_MARKERS):
        if parent_pid is None:
            return True
        try:
            return proc.ppid() == parent_pid
        except Exception:  # noqa: BLE001 -- unreadable parent keeps the helper match
            return True
    tails = {tuple(parts) for parts in _ROLE_SCRIPTS.values()}
    for a in argv:
        if a.lower().endswith(".py"):
            segs = tuple(os.path.normpath(a).replace("\\", "/").split("/")[-3:])
            if segs in tails:
                return True
    return False


def _child_snapshot(pid: int, *, psutil_module=None) -> list:
    """`pid`'s live descendants as psutil handles: one process-table read, no per-child cost."""
    psutil = psutil_module
    if psutil is None:
        import psutil
    try:
        return list(psutil.Process(pid).children(recursive=True))
    except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
        return []


def _live_descendants(pid: int, *, psutil_module=None, children=None) -> list[int]:
    """Live descendants of `pid` that are pair-role processes; never an op's launched session.
    `children` is a `_child_snapshot` taken earlier; the per-child argv reads are the cost.

    Trap: a server's ops launch long-lived children (agent_pipe peers, app sessions) that are
    its Windows descendants; ending every descendant on a stop or reap ends those sessions."""
    psutil = psutil_module
    if psutil is None:
        import psutil
    try:
        found = children if children is not None else psutil.Process(pid).children(recursive=True)
        return [c.pid for c in found if _is_pair_role_child(c, pid)]
    except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
        return []


class _DescendantScan:
    """Classifies the servers' snapshotted descendants (and records their birth instants) off the
    thread that asks and drains the server, so the argv reads overlap the drain. `join` blocks."""

    def __init__(self, snapshots: "dict[int, list]", births, psutil_module) -> None:
        import threading

        self.child_pids: set[int] = set()
        self.child_born: dict = {}
        self._error: Optional[BaseException] = None
        self._snapshots = snapshots
        self._births = births
        self._psutil_module = psutil_module
        self._thread = threading.Thread(target=self._run, name="pair-stop-descendants", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            for spid, children in self._snapshots.items():
                self.child_pids.update(
                    _live_descendants(spid, psutil_module=self._psutil_module, children=children)
                )
            self.child_born = {c: _create_time(c, self._births) for c in self.child_pids}
        except BaseException as exc:  # noqa: BLE001 -- re-raised on the stopping thread by `join`
            self._error = exc

    def join(self) -> None:
        self._thread.join()
        if self._error is not None:
            raise self._error


def reap_pairs_outside(
    engine_root: Path, known_pids: frozenset, *, psutil_module=None
) -> tuple[int, ...]:
    """Terminate every pair process of `engine_root` whose pid is not in `known_pids`, with its
    live descendants; return the pids confirmed gone. Never raises.

    Trap: `known_pids` must be snapshotted BEFORE the successor is spawned; it is the only
    thing telling the old pair from the orphan, since both share this root's entry scripts."""
    try:
        fresh = [
            p
            for p in find_pair_processes(engine_root, psutil_module=psutil_module)
            if p.pid not in known_pids
        ]
        pids: dict[int, None] = {p.pid: None for p in fresh}
        for p in fresh:
            for cpid in _live_descendants(p.pid, psutil_module=psutil_module):
                pids.setdefault(cpid, None)
        return tuple(
            pid
            for pid in sorted(pids, reverse=True)
            if _terminate_pid(pid, psutil_module=psutil_module)
        )
    except Exception:  # noqa: BLE001 -- reaping is best-effort and never fails the swap
        return ()


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
    ask_front_door: Optional[Callable[[Optional[dict]], bool]] = None,
    keep_pids: frozenset = frozenset(),
    server_pipe: Optional[str] = None,
    on_in_flight: Optional[Callable[[Optional[int]], None]] = None,
    read_outstanding: Optional[Callable[[str], Optional[int]]] = None,
) -> PairStopResult:
    """Stop the supervisor, then the server (asked, drained), then the front door (asked,
    drained), of `engine_root`; return who stopped and who survived. A pid is signalled only
    after its ask went unanswered or its drain grace ran out.

    Trap: a pid is signalled only when a verified record (pid plus birth instant) or the exact
    cmdline match identified it; the cmdline match is the identification the old breadcrumb-only
    rule lacked, and it is still exact-path, never "anything python-ish".

    `keep_pids` is the successor's server pid: it is never matched, asked, signalled, counted a
    survivor, or allowed to lose its breadcrumb. The successor shares this root's server.py, so
    only a pid exclusion tells the generations apart. `server_pipe` is the superseded server's
    own endpoint (the breadcrumb now names the successor), so it still gets a graceful drain ask.

    `on_in_flight`, when given, is called once with the server's outstanding count (None when
    unknown) before the supervisor or any other pid is signalled, and the count lands on the result.
    """
    from coordinator_core.warm import front_door, supervisor

    ask = ask_server if ask_server is not None else _ask_server_with_wedge_signal
    ask_door = ask_front_door if ask_front_door is not None else front_door.ask_front_door_to_stop
    engine_root = Path(engine_root)
    keep = frozenset(keep_pids)
    before = [
        p for p in find_pair_processes(engine_root, psutil_module=psutil_module) if p.pid not in keep
    ]
    seen: dict[int, PairProcess] = {p.pid: p for p in before}

    in_flight: Optional[int] = None
    if on_in_flight is not None:
        early_crumb = breadcrumb.read_breadcrumb(engine_root)
        early = _record_pid(early_crumb)
        early_pipe = server_pipe
        if early is not None and early[0] not in keep and isinstance(early_crumb, dict):
            early_pipe = early_crumb.get("pipe")
        if isinstance(early_pipe, str):
            in_flight = (read_outstanding or read_server_outstanding)(early_pipe)
        on_in_flight(in_flight)

    sup_pids: dict[int, str] = {p.pid: p.cmdline for p in before if p.role == ROLE_SUPERVISOR}
    disc = _record_pid(supervisor.read_discovery(engine_root))
    if disc is not None:
        sup_pids.setdefault(disc[0], "")
    for pid in sup_pids:
        _terminate_pid(pid, psutil_module=psutil_module)

    crumb = breadcrumb.read_breadcrumb(engine_root)
    verified = _record_pid(crumb)
    if verified is not None and verified[0] in keep:
        verified = None
    server_pids: set[int] = {p.pid for p in before if p.role == ROLE_SERVER}
    children_seen: set[int] = set()
    if verified is not None:
        server_pids.add(verified[0])
    births = psutil_module
    if births is None:
        import psutil as births
    scan = _DescendantScan(
        {spid: _child_snapshot(spid, psutil_module=psutil_module) for spid in server_pids},
        births,
        psutil_module,
    )
    if verified is not None:
        pid, epoch_str = verified
        pipe = crumb.get("pipe") if isinstance(crumb, dict) else None
        gone = False
        if isinstance(pipe, str):
            # An unanswered ask is not a refusal: the server may be mid-request. Ask twice, then
            # wait the grace while it is alive, and signal only after. No pipe means no ask was
            # made, so there is no drain to wait for.
            # A -32004 answer proves a wedged dispatch: nothing will drain, so signal at once.
            outcome = ask(pipe)
            if outcome is not _ASK_WEDGED and not outcome:
                outcome = ask(pipe)
            if outcome is not _ASK_WEDGED:
                gone = _wait_for_pid_gone(pid, epoch_str, deadline_secs=_drain_wait_secs(outcome))
        if not gone:
            _terminate_pid(pid, psutil_module=psutil_module)
    elif server_pipe is not None and server_pids:
        outcome = ask(server_pipe)
        if outcome is not _ASK_WEDGED and not outcome:
            outcome = ask(server_pipe)
        if outcome is not _ASK_WEDGED:
            grace = time.monotonic() + _drain_wait_secs(outcome)
            while time.monotonic() < grace and not all(
                _pid_gone(p, psutil_module=psutil_module) for p in server_pids
            ):
                time.sleep(_TERMINATE_POLL_INTERVAL_SECS)

    fd_pids: dict[int, str] = {p.pid: p.cmdline for p in before if p.role == ROLE_FRONT_DOOR}
    fd_record = front_door.read_discovery(engine_root)
    fd_disc = _record_pid(fd_record)
    if fd_disc is not None:
        fd_pids.setdefault(fd_disc[0], "")
    for pid in fd_pids:
        if (
            fd_disc is not None
            and pid == fd_disc[0]
            and ask_door(fd_record)
            and _wait_for_pid_gone(pid, fd_disc[1], deadline_secs=_confirm_grace_secs())
        ):
            continue
        _terminate_pid(pid, psutil_module=psutil_module)

    scan.join()
    child_pids, child_born = scan.child_pids, scan.child_born
    initial = frozenset(seen) | frozenset(sup_pids) | frozenset(server_pids) | frozenset(child_born) | frozenset(fd_pids)
    failures: dict[int, str] = {}
    order = {ROLE_SUPERVISOR: 0, ROLE_FRONT_DOOR: 1, ROLE_SERVER: 2, ROLE_POOL_CHILD: 3}
    deadline = time.monotonic() + _RESWEEP_DEADLINE_SECS
    while True:
        swept = [
            p for p in find_pair_processes(engine_root, psutil_module=psutil_module) if p.pid not in keep
        ]
        server_pids.update(p.pid for p in swept if p.role == ROLE_SERVER)
        swept += _pool_children(server_pids, psutil_module=psutil_module)
        for p in swept:
            seen.setdefault(p.pid, p)
        targets = {p.pid: p.role for p in swept}
        for cpid in child_pids:
            if not _gone_or_reused(cpid, child_born.get(cpid), psutil_module):
                targets.setdefault(cpid, ROLE_POOL_CHILD)
        children_seen.update(child_pids)
        child_pids.clear()
        targets = {
            pid: role
            for pid, role in targets.items()
            if not _pid_gone(pid, psutil_module=psutil_module)
        }
        fresh = sorted(
            (pid for pid in targets if pid not in failures),
            key=lambda pid: (order.get(targets[pid], 9), pid),
        )
        if not fresh:
            break
        for pid in fresh:
            reason = _terminate_with_reason(pid, psutil_module=psutil_module)
            if reason is not None:
                failures[pid] = reason
        if time.monotonic() >= deadline:
            break

    settle_start = time.monotonic()
    stall_until = settle_start + _SETTLE_SECS
    hard_until = settle_start + _SETTLE_MAX_SECS
    last_count: Optional[int] = None
    while True:
        found = [
            p for p in find_pair_processes(engine_root, psutil_module=psutil_module) if p.pid not in keep
        ] + _pool_children(server_pids, psutil_module=psutil_module)
        known = {p.pid for p in found}
        found += [
            PairProcess(pid=cpid, role=ROLE_POOL_CHILD, cmdline="")
            for cpid in sorted(children_seen - known)
        ]
        live = [
            p
            for p in found
            if not _gone_or_reused(p.pid, child_born.get(p.pid), psutil_module)
        ]
        now = time.monotonic()
        if not live or now >= min(stall_until, hard_until):
            break
        if last_count is None or len(live) < last_count:
            if last_count is not None:
                stall_until = now + _SETTLE_SECS
            last_count = len(live)
            print(
                f"[pair_stop] waiting on {len(live)} process(es) to exit: "
                + ", ".join(f"{p.role}={p.pid}" for p in live),
                file=sys.stderr,
            )
        time.sleep(_TERMINATE_POLL_INTERVAL_SECS)
    survivors = tuple(live)
    survivor_pids = {p.pid for p in survivors}
    stopped = tuple(p for pid, p in seen.items() if pid not in survivor_pids)

    result = PairStopResult(
        stopped=stopped,
        survivors=survivors,
        failures=tuple(sorted((pid, why) for pid, why in failures.items() if pid in survivor_pids)),
        initial_pids=initial,
        in_flight=in_flight,
    )
    if result.ok():
        if not keep:
            breadcrumb.unlink_breadcrumb(engine_root)
        supervisor.unlink_discovery(engine_root)
        front_door.unlink_discovery(engine_root)
    return result


RESTART_RESTARTED = "restarted"
RESTART_NO_ENGINE = "no-engine-running"
RESTART_NOT_ANSWERING = "successor-not-answering"
RESTART_UNSUPPORTED = "unsupported"

_RESTART_STAMP_SUFFIX_RE = re.compile(r"\+restart-[0-9a-f]+$")


@dataclass(frozen=True)
class RestartResult:
    """`outcome` is one of the `RESTART_*` constants. `RESTART_UNSUPPORTED` means the root carries
    no engine stamp, so no successor generation can exist and the caller decides on a plain stop."""

    outcome: str

    def ok(self) -> bool:
        return self.outcome in (RESTART_RESTARTED, RESTART_NO_ENGINE)


def _rotated_stamp(old_stamp: bytes) -> bytes:
    """`old_stamp` with a fresh `+restart-<nonce>` identity suffix, so its token differs and a
    successor can bind beside the old server. The `sha:` prefix is untouched: stamp readers
    cut the identity at the first `+`."""
    import uuid

    text = _RESTART_STAMP_SUFFIX_RE.sub("", old_stamp.decode("utf-8", errors="replace").strip())
    return f"{text}+restart-{uuid.uuid4().hex[:12]}\n".encode("utf-8")


def restart_warm_pair(
    engine_root: Path,
    *,
    err=None,
    start_successor: Optional[Callable[..., bool]] = None,
) -> RestartResult:
    """Make-before-break restart of `engine_root`'s warm pair: the successor boots under a rotated
    stamp token while the old pair keeps answering, and the old pair is stopped only after the
    successor answers a warm op (`percolate.round.start_successor_pair_in_hold`, the one handover).
    No hook falls to the door's cold leg during the swap.

    Nothing running: `RESTART_NO_ENGINE`, nothing spawned (the next hook starts one). A successor
    that never answers leaves the old pair running: `RESTART_NOT_ANSWERING`. A root with no stamp:
    `RESTART_UNSUPPORTED`, nothing touched.

    Trap: a same-token successor loses the election, so the stamp is rotated rather than the
    server merely respawned; a plain stop-then-start restart is the gap this exists to close."""
    from coordinator_core.warm import skew

    root = Path(engine_root)
    old_stamp = skew.read_engine_stamp_bytes(root)
    if old_stamp is None:
        return RestartResult(RESTART_UNSUPPORTED)
    if _record_pid(breadcrumb.read_breadcrumb(root)) is None and not find_pair_processes(root):
        return RestartResult(RESTART_NO_ENGINE)
    if start_successor is None:
        from coordinator_core.warm.handover import start_successor_pair_in_hold as start_successor
    answered = start_successor(
        root, err=err, old_stamp=old_stamp, new_stamp=_rotated_stamp(old_stamp)
    )
    return RestartResult(RESTART_RESTARTED if answered else RESTART_NOT_ANSWERING)
