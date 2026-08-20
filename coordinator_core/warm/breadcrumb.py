"""coordinator_core.warm.breadcrumb — the spawn-debounce breadcrumb.

Spec backlink: docs/plans/2026-08-16-one-engine-for-the-whole-box.md § C18

WHAT THE BREADCRUMB IS -- a SPAWN DEBOUNCE, never a wait gate (corrected
after staff-eng review finding 4; an earlier plan draft read "waits",
which contradicts C15's no-wait rule -- see `warm.client`'s module
docstring, "NO CLIENT EVER WAITS FOR A SERVER TO BOOT"). `<svc dir>/
warm.json` records the most recent spawn this engine clone has seen, so a
client arriving in the window between one client's spawn trigger and that
spawned server's own election finishing can look at this file, see a
recent, still-alive spawn, and skip spawning a second one -- it goes cold
this call instead, exactly as it would on any other cold-path outcome.
It is a HINT: correctness never depends on it, and total breadcrumb
failure (missing, corrupt, or simply never written) costs at most N
short-lived redundant processes across the box, never N servers -- the
kernel-enforced named-pipe election (`warm.election`) is the actual
correctness mechanism; this module only bounds how many client processes
pay a spawn attempt during one cold-start burst.

THE DEBOUNCE CHECK, `should_spawn()`: a breadcrumb younger than
`SPAWN_DEBOUNCE_SECS` (2.0 -- the retired `client.READ_DEADLINE_SECS`-
adjacent `START_DEADLINE` constant's OLD value, reused here for a
DIFFERENT job now that no client ever waits on it) whose `pid` passes
`session.core.stable_pid_alive` (pid PLUS the stored psutil birth
instant, so a recycled pid reads dead rather than falsely alive) means a
spawn is already in flight or has just completed -- `should_spawn()`
returns False and the caller goes cold without spawning. Any other
combination (no breadcrumb, unreadable/corrupt breadcrumb, breadcrumb
older than the debounce window, or a pid that no longer passes
`stable_pid_alive`) returns True: nothing currently vouches for an
in-flight spawn, so the caller's own spawn trigger should proceed.

WIRING NOTE (scope boundary, not a gap in this module): this row's
`writes:` is exactly `{breadcrumb.py, warm-engine-stop.py, this module's
own test file}`. Binding `should_spawn()` into `warm.client._spawn_once`
and binding `write_breadcrumb()` into `warm.server.main`'s boot sequence
are both edits to files OUTSIDE that list (`warm/client.py`, `warm/
server.py`) and are therefore NOT made by this chunk -- this module
supplies the debounce's storage and decision primitives; wiring either
call site is a follow-up chunk's job, exactly as `warm.server`'s own
docstring already flags ("breadcrumb -> C18, not yet landed ...
`_ctx_shutdown` below is a documented no-op pending that chunk").

SVC DIR RESOLUTION -- a user-local, per-clone runtime directory OUTSIDE
the engine clone, keyed by a hash of the resolved clone path. NOT
`<engine_root>/state/warm/`, which is what this resolved until
2026-08-19; `svc_dir` below carries the PM ruling that moved it and the
rationale the original resolution was reasoning from. `engine_root` defaults to THIS
module's own resolved clone root, `engine_root.current_engine_clone()`
(the single shared definition every former local copy now calls -- plan
2026-08-19-an-engine-root-is-a-stamped-build § C3) -- so on the klabauter box, running
this module's functions from the klabauter clone resolves the
KLABAUTER clone's own runtime directory, never one keyed to whatever
repo happens to be the caller's cwd. `warm-engine-stop.py` relies on exactly this: it is a
runnable script shipped inside each clone, and resolves the breadcrumb
of the clone IT WAS INVOKED FROM.

Negative-spec:
  - Does NOT write the breadcrumb from a server boot -- no live call site
    exists yet (see WIRING NOTE).
  - Does NOT gate `warm.client`'s spawn trigger -- same reason.
  - Does NOT treat a missing/corrupt breadcrumb as an error -- every read
    path degrades to "no information," never raises, per the module's own
    "it is a HINT" contract.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Optional

from coordinator_core import locked_write
from coordinator_core.session.core import stable_pid_alive
from coordinator_core.warm.engine_root import current_engine_clone

__all__ = [
    "SPAWN_DEBOUNCE_SECS",
    "RUNTIME_BASE_ENV",
    "BREADCRUMB_FILENAME",
    "svc_dir",
    "breadcrumb_path",
    "write_breadcrumb",
    "read_breadcrumb",
    "unlink_breadcrumb",
    "should_spawn",
]

# The old `client.START_DEADLINE` value, reused for a different job now
# that no client ever waits on it -- see module docstring.
SPAWN_DEBOUNCE_SECS = 2.0

BREADCRUMB_FILENAME = "warm.json"


def _default_engine_clone() -> Path:
    """This module's own resolved clone root -- collapsed onto the single
    shared definition, `engine_root.current_engine_clone()` (plan
    2026-08-19-an-engine-root-is-a-stamped-build § C3)."""
    return current_engine_clone()


def svc_dir(engine_root: Optional[Path] = None) -> Path:
    """The breadcrumb's containing directory: a per-user, per-clone runtime
    directory OUTSIDE the engine clone.

    NOT `<engine_root>/state/warm/`, which is what this resolved until
    2026-08-19. PM ruling that day: **`state/` is for active repos, and must
    not exist in a publish mirror.** The engine runs out of the klabauter
    publish clone, so writing runtime state under its `state/` put a
    directory there that a publish repo is not supposed to have at all.

    It also broke publishing to that clone. The publish round's end-of-run
    unscanned-published check walks the FILESYSTEM, not git, so a gitignored
    `state/warm/warm.json` is invisible to `git status` and fatal to the
    round: all nine rows synced and the round still refused
    (`state/warm/warm.json is published but was never visited by any row's
    content-transform sweep`). Stopping the server cleared it, which made
    "stop the engine before publishing to it" an unwritten operator
    precondition discovered by tripping over it.

    The original rationale was sound for the repo it reasoned about --
    `state/` IS this repo's documented substrate for per-machine, gitignored
    single-file artifacts (`state/doctor-last-run.json`,
    `state/housekeeping-liveness.json`) and for concern-scoped
    subdirectories (`state/subagent-share/`, `state/peer-notices/`). What it
    missed is that the warm engine is the one component that runs from a
    PUBLISH clone rather than an active one, where none of those precedents
    apply.

    PER-CLONE KEYING IS PRESERVED, which is the property the old resolution
    actually bought. The directory is keyed by a hash of the resolved clone
    path, in the same derivation `election.pipe_name` already uses to keep
    two clones' pipes distinct -- so the klabauter clone and a live working
    tree still get separate breadcrumbs and separate telemetry, and
    `warm-engine-stop.py` (which passes its own containing clone's root)
    still resolves exactly the directory its clone's server writes.

    Per-USER isolation comes from the base directory being user-local
    (`%LOCALAPPDATA%`), matching `pipe_name`'s own SID scoping. The base is
    deliberately a LOCAL app-data path, never a synced or settings-home one:
    a resident server's breadcrumb describes a pid on THIS machine and must
    never follow the user to another (see the cross-machine-sync hazard the
    coordinator settings root exists to avoid).

    `engine_root` defaults to this module's own resolved clone root, exactly
    as before.
    """
    root = Path(engine_root) if engine_root is not None else _default_engine_clone()
    clone_hash = hashlib.sha1(str(root.resolve()).encode("utf-8")).hexdigest()[:16]
    return _runtime_base() / "coordinator" / "warm" / clone_hash


RUNTIME_BASE_ENV = "COORDINATOR_WARM_RUNTIME_BASE"


def _runtime_base() -> Path:
    r"""User-local, non-synced base for warm runtime state.

    `%LOCALAPPDATA%` on Windows (the warm engine is Windows-only --
    `server.main` refuses to start elsewhere), falling back to
    `~/.cache` so this module stays importable and testable on any
    platform rather than raising at import or resolution time.

    `RUNTIME_BASE_ENV` overrides both, and exists for ONE reason: test
    isolation. Passing a `tmp_path` as `engine_root` varies only the
    clone-hash component -- the base stays the operator's real one, so
    every warm test run minted a fresh REAL directory under
    `%LOCALAPPDATA%\coordinator\warm\` that nothing ever removed
    (measured 2026-08-20: 1027 clone-key directories, 244 of them
    holding obviously synthetic fixture content). The fix is a seam
    here rather than a sweeper: a sweeper deletes what the next suite
    run immediately re-creates.

    Read at CALL time, never cached, so a fixture's `monkeypatch.setenv`
    is honoured by code that imported this module earlier. Unset (the
    operator's real case) leaves the resolution exactly as it was.
    Setting it in a real shell would move a live server's breadcrumb and
    telemetry with it, and a warm-served handler reads the SERVER's
    environment rather than the caller's -- it is a test seam, not an
    operator knob.
    """
    override = os.environ.get(RUNTIME_BASE_ENV, "").strip()
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local)
    return Path.home() / ".cache"


def breadcrumb_path(engine_root: Optional[Path] = None) -> Path:
    """`<svc dir>/warm.json` for `engine_root` (see `svc_dir`)."""
    return svc_dir(engine_root) / BREADCRUMB_FILENAME


def write_breadcrumb(
    *,
    pipe: str,
    pid: int,
    stable_pid_start_epoch: int,
    engine_sha: Optional[str],
    started_at: Optional[str] = None,
    engine_root: Optional[Path] = None,
) -> None:
    """Write the breadcrumb under `locked_write.held_lock`, replacing any
    prior content -- this is a snapshot of the current boot, not an
    append log; the only reader that matters (`should_spawn`, and the
    stop script) wants the LATEST spawn, never a history.

    `started_at` defaults to the current UTC wall time in the same
    `%Y-%m-%dT%H:%M:%SZ` ISO-8601 shape `detached_spawn`'s own failure
    log uses, for consistency across this package's on-disk timestamps.
    Creates `svc_dir()` if it does not yet exist. Never raises past a
    lock timeout (`locked_write.LockTimeout`) or an `OSError` writing the
    file itself -- both propagate, since a caller that asked this module
    to RECORD a spawn needs to know if that recording failed, unlike the
    read side (module docstring's "it is a HINT" applies to CONSUMERS of
    the breadcrumb, not to a writer's own request to persist one).
    """
    if started_at is None:
        started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    record = {
        "pipe": pipe,
        "pid": pid,
        "stable_pid_start_epoch": stable_pid_start_epoch,
        "engine_sha": engine_sha,
        "started_at": started_at,
    }
    path = breadcrumb_path(engine_root)
    path.parent.mkdir(parents=True, exist_ok=True)

    with locked_write.held_lock(path, holder_label="warm.breadcrumb"):
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8", newline="\n")


def read_breadcrumb(engine_root: Optional[Path] = None) -> Optional[dict]:
    """Read and parse the breadcrumb, or return None if it is absent,
    unreadable, or not a well-formed JSON object -- never raises. See
    module docstring's "it is a HINT": every consumer of this function
    must treat None as "no information," not as an error condition."""
    path = breadcrumb_path(engine_root)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        record = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict):
        return None
    return record


def unlink_breadcrumb(
    engine_root: Optional[Path] = None,
    *,
    owner_pid: Optional[int] = None,
) -> None:
    """Best-effort remove the breadcrumb file. Never raises -- mirrors
    every other best-effort cleanup in this package (`detached_spawn`'s
    log writers, `warm.lifecycle`'s ctx-shutdown contract this function
    is meant to eventually back, per that module's own "unlinking the
    breadcrumb" step-3 language -- not yet wired, see module docstring's
    WIRING NOTE).

    `owner_pid` makes the unlink OWNERSHIP-CHECKED: the file is removed
    only if the breadcrumb on disk still names that pid. There is exactly
    ONE breadcrumb per clone and every winning server overwrites it at
    boot, so a departing server is NOT necessarily the server the current
    breadcrumb describes -- a superseded generation exiting would
    otherwise delete its LIVE SUCCESSOR's breadcrumb:

        A boots (pid 1111)          -> breadcrumb names 1111
        publish; B boots (pid 2222) -> breadcrumb names 2222, A's clobbered
        A exits, unconditional unlink -> breadcrumb GONE while B still serves

    The consequences are not cosmetic: `should_spawn` reads a missing
    breadcrumb as "no spawn in flight" and returns True, so the debounce
    this file exists to provide silently stops debouncing, and
    `coordinator/bin/warm-engine-stop.py` -- which identifies its target
    solely from this file -- reports "nothing to stop" for a server that
    is in fact still running.

    This is why the check is keyed on the RECORDED pid rather than on the
    caller merely believing it owns the file. `warm.skew`'s token
    rotation makes generational overlap ordinary rather than exceptional,
    and `warm.idle`'s superseded-generation arm retires a predecessor
    within one watchdog poll of its successor binding -- i.e. exactly
    when the successor's breadcrumb is freshest.

    Omitted (the default), the unlink is unconditional: correct for a
    caller that has ALREADY established ownership by other means, which
    is `warm-engine-stop.py`'s case -- it stops the pid this same file
    named and then clears it.
    """
    path = breadcrumb_path(engine_root)
    if owner_pid is not None:
        current = read_breadcrumb(engine_root)
        # An absent/corrupt breadcrumb (None) leaves nothing to remove; a
        # breadcrumb naming a DIFFERENT pid belongs to another generation
        # and is not this caller's to delete. Both are no-ops rather than
        # a forced unlink -- an ownership check that falls back to
        # deleting on doubt is not a check.
        if current is None or current.get("pid") != owner_pid:
            return
    try:
        path.unlink()
    except OSError:
        pass


def should_spawn(
    engine_root: Optional[Path] = None,
    *,
    now: Optional[float] = None,
) -> bool:
    """The debounce decision: True iff the caller's own spawn trigger
    should proceed (no live breadcrumb currently vouches for an
    in-flight spawn); False iff a young, alive breadcrumb means a spawn
    already happened and this caller should go cold without spawning a
    second one. See module docstring's "THE DEBOUNCE CHECK".

    `now` is an injectable clock for tests (`time.time()`-shaped: unix
    epoch seconds), matching `started_at`'s wall-clock provenance --
    unlike `warm.lifecycle`'s `time.monotonic()` clocks, this comparison
    is inherently cross-process (one process's `started_at`, another
    process's `now`), so a monotonic clock (meaningless across processes)
    would be the wrong choice here.
    """
    record = read_breadcrumb(engine_root)
    if record is None:
        return True

    started_at = record.get("started_at")
    if not isinstance(started_at, str):
        return True
    try:
        started_epoch = calendar.timegm(time.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return True

    current = time.time() if now is None else now
    age = current - started_epoch
    if age < 0 or age >= SPAWN_DEBOUNCE_SECS:
        return True

    pid = record.get("pid")
    if not isinstance(pid, int):
        return True
    stored_epoch = record.get("stable_pid_start_epoch")
    stored_epoch_str = str(stored_epoch) if stored_epoch is not None else ""

    try:
        alive = stable_pid_alive(pid, stored_start_epoch=stored_epoch_str)
    except Exception:
        # `stable_pid_alive` can raise `MissingPsutilError` -- a HINT
        # consumer must not let that propagate; treat as "cannot vouch
        # for this breadcrumb," which means spawn.
        return True

    return not alive
