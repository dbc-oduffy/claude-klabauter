"""coordinator_core.warm.skew — traffic-driven version-skew eviction.

Spec backlink: docs/plans/2026-08-16-one-engine-for-the-whole-box.md § C16

THE INVERSION: the retired resident (pre-teardown) had the SERVER decide
staleness on connect and evict through a drain gate armed only when an idle
reaper judged the server quiet -- so continuous hook traffic kept resetting
the reaper, the drain never completed, and traffic PROTECTED a stale
server. This module inverts that: on a detected mismatch the listener is
closed BEFORE any drain step runs, with no reference to idleness, in-flight
count, or a drain flag anywhere in the eviction path (`evict_on_skew`'s
signature carries none of those) -- so the highest-frequency caller on this
box (heartbeat traffic) is what evicts a stale server, and more traffic
strictly SHORTENS a stale server's remaining life.

Two independent mismatch axes feed eviction, both compared live, never
behind a drain-armed idle gate:

  1. Primary (commit-level), `compute_client_token()`: (st_mtime_ns,
     st_size) of `.git/HEAD` and of the ref it names. Two stats, no
     subprocess. Called by BOTH the client (per dispatch -- see
     `warm.client.engine_token`, C15's seam this module fills) and the
     server (per request, same function, same cost) so both sides always
     compare the CURRENT git state, not a cached one. `.git/index` is
     deliberately excluded (staff-eng finding 3): ordinary `git add` /
     status-refresh rewrites it from any of the 50-70 sessions sharing
     this checkout, not only an engine-source commit, which would make the
     token COARSER than engine-source change -- the opposite of what a
     skew signal needs.

  2. Secondary (source-level, server-side only, `ServerVersionState`):
     catches a bare editor save or other uncommitted edit that never moves
     `.git/HEAD` or its ref, so axis 1 alone would miss it. Throttled to
     `_REFRESH_INTERVAL_SECS` -- a full source hash is not cheap enough to
     pay per request -- using `max(st_mtime)` over non-test
     `coordinator_core/**.py` as a prefilter and falling through to
     `coordinator_core.lifecycle._compute_core_version()` (reused as-is;
     it survived the daemon teardown) only on a prefilter miss, plus
     `coordinator_core.engine_version.resolve_engine_dirty()` -- the
     purpose-built DR-313 check -- rather than rolling a bespoke
     mtime-based dirty approximation, per the plan body's explicit
     negative spec.

Deliberately NOT `resolve_engine_sha()` per request: that is a
`git rev-parse` at ~5-15ms, and paying a subprocess per request to avoid a
subprocess per request is self-defeating. `ServerVersionState` resolves it
ONCE at construction (server boot) for the human-readable sha carried in
`build_skew_response`'s error payload.

NEGATIVE SPEC -- does an `engine.target` flip (C3's box-wide fact in the
machine-local registry) evict a running server? A flip that changes WHICH
CLONE is resolved changes the pipe name itself (keyed on
`sha1(realpath(engine_clone))` -- `election.pipe_name`), so a client
computing the new token simply finds a different pipe and the OLD server
is never reached: self-routing, no eviction needed. A flip BETWEEN REFS
inside one clone (`main` <-> `candidate` in the same working tree) is
covered ONLY if the checkout mechanics move `.git/HEAD` in that clone.
Verified against this box's actual channel-switch tool,
`coordinator/bin/klabauter-channel.py --set`: its `_set` path runs
`git -C <tree> checkout -B <target> --track origin/<target>`, and
`git checkout -B` REWRITES `.git/HEAD` to `ref: refs/heads/<target>` --
this module's axis-1 stat pair observes exactly that write (pinned by
`test_skew_eviction.py::test_client_token_changes_across_real_git_checkout`,
which runs a real `git checkout -B` in a temp repo). So: the in-clone ref
flip is the case this box actually exercises, and it IS already covered by
the primary signal -- no separate `engine.target` handling belongs here.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Callable, Optional

from coordinator_core import engine_version, lifecycle

__all__ = [
    "ENGINE_SKEW",
    "compute_client_token",
    "ServerVersionState",
    "build_skew_response",
    "evict_on_skew",
]

# Mirrors `coordinator_core.warm.client.ENGINE_SKEW`. Duplicated rather than
# imported: `client.engine_token` (C15's placeholder seam) is meant to call
# into THIS module, so importing `client` here would invert that edge into a
# cycle. Pinned equal to `client.ENGINE_SKEW` by test.
ENGINE_SKEW = -32001

_REFRESH_INTERVAL_SECS = 2.0


def _default_engine_clone() -> Path:
    # Same computation as `election._default_engine_clone` and
    # `client._engine_clone_root`, kept as a local copy per this codebase's
    # convention of not reaching into a peer module's private name.
    return Path(__file__).resolve().parents[2]


def _stat_pair(path: Path) -> tuple:
    """Return (st_mtime_ns, st_size) for `path`, or (0, 0) if it cannot be
    stat'd. (0, 0) is a safe "absent/unreadable" sentinel for this token's
    equality comparisons -- two absent paths compare equal, and no real
    file's mtime_ns lands on the epoch, so it never collides with a
    present file's stat pair."""
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return (0, 0)


def _resolve_ref_path(git_dir: Path) -> Optional[Path]:
    """Resolve the ref file `.git/HEAD` currently points at, or None on a
    detached HEAD (a raw sha, no `ref:` prefix) or an unreadable HEAD."""
    head = git_dir / "HEAD"
    try:
        content = head.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not content.startswith("ref:"):
        return None
    ref_rel = content[len("ref:"):].strip()
    return git_dir / ref_rel


def compute_client_token(repo_root: Optional[Path] = None) -> str:
    """Primary version-skew token: a stat-only fingerprint of the engine
    clone's current git ref, no subprocess.

    Fingerprints `(st_mtime_ns, st_size)` of `.git/HEAD` and of the ref
    file it points at (or `(0, 0)` for the ref half on a detached HEAD).
    Called by the client on every dispatch (`warm.client.engine_token`)
    and by the server on every request (`ServerVersionState.is_skewed`) --
    both sides read the same files at effectively the same moment, so this
    needs no caching or timer on either side; see module docstring axis 1.
    """
    root = Path(repo_root) if repo_root is not None else _default_engine_clone()
    git_dir = root / ".git"
    ref_path = _resolve_ref_path(git_dir)
    signal = (
        _stat_pair(git_dir / "HEAD"),
        _stat_pair(ref_path) if ref_path is not None else (0, 0),
    )
    return hashlib.sha1(repr(signal).encode("utf-8")).hexdigest()[:16]


def _source_pkg_dir() -> Path:
    """The exact directory `lifecycle._compute_core_version()` hashes --
    that function takes no root argument and is hardcoded to its own
    `Path(__file__).parent` (i.e. this running process's `coordinator_core`
    package dir), so the axis-2 prefilter below walks the same directory
    rather than an arbitrary `repo_root`, to stay a true prefilter for it."""
    return Path(lifecycle.__file__).resolve().parent


def _max_source_mtime(pkg_dir: Path) -> float:
    """Cheap prefilter: the latest mtime among non-test `.py` files under
    `pkg_dir`, mirroring `lifecycle._compute_core_version`'s own walk
    (same `tests/` pruning) without hashing any file content."""
    latest = 0.0
    for dirpath, dirnames, filenames in os.walk(pkg_dir):
        dirnames[:] = [d for d in dirnames if d != "tests"]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            try:
                latest = max(latest, (Path(dirpath) / fn).stat().st_mtime)
            except OSError:
                continue
    return latest


class ServerVersionState:
    """Server-side generation state: boot-time sha plus the throttled
    secondary staleness check (module docstring axis 2).

    Constructed once at server boot. `is_skewed(client_token)` is the sole
    per-request entry point.
    """

    def __init__(
        self,
        repo_root: Optional[Path] = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._root = Path(repo_root) if repo_root is not None else _default_engine_clone()
        self._clock = clock
        self.server_sha = engine_version.resolve_engine_sha()
        self._boot_hash = lifecycle._compute_core_version()
        self._last_mtime_prefilter = _max_source_mtime(_source_pkg_dir())
        self._last_refresh = self._clock()
        self._source_stale = False

    def refresh(self, *, force: bool = False) -> None:
        """Run the throttled axis-2 check if `_REFRESH_INTERVAL_SECS` has
        elapsed since the last run (or unconditionally when `force`).

        Order per check: the mtime prefilter first. On NO change from the
        last run, falls through to `engine_version.resolve_engine_dirty()`
        -- the purpose-built DR-313 check -- and only re-hashes when dirty
        is truthy or unresolvable (None); dirty=False with an unchanged
        prefilter means nothing moved, so re-hashing would just re-pay the
        walk-and-hash cost this axis is throttled to avoid. On a prefilter
        miss (mtime changed), always re-hashes via
        `lifecycle._compute_core_version()` regardless of dirty, since a
        changed mtime with a clean git status is exactly the "edit that
        got committed between checks" case this axis exists to catch.
        Once flagged stale, `_source_stale` is sticky for this server's
        lifetime -- a fresh hash matching `_boot_hash` again would mean the
        edit was reverted, not that the server that already served stale
        responses became retroactively current.
        """
        now = self._clock()
        if not force and (now - self._last_refresh) < _REFRESH_INTERVAL_SECS:
            return
        self._last_refresh = now

        pkg_dir = _source_pkg_dir()
        prefilter = _max_source_mtime(pkg_dir)
        prefilter_changed = prefilter != self._last_mtime_prefilter
        self._last_mtime_prefilter = prefilter

        if not prefilter_changed:
            dirty = engine_version.resolve_engine_dirty()
            if dirty is False:
                return

        current_hash = lifecycle._compute_core_version()
        if current_hash != self._boot_hash:
            self._source_stale = True

    def is_skewed(self, client_token: str) -> bool:
        """True iff this request should be treated as version-skewed --
        either axis 2 (the throttled secondary check has flagged this
        server's own source stale since boot) or axis 1 (the client's live
        primary token disagrees with the server's live primary token).

        Runs `refresh()` first (a no-op clock read on every call that
        isn't yet due, per the throttle), so callers need no separate
        timer -- calling `is_skewed` once per request is the entire
        server-side integration contract for axis 2.
        """
        self.refresh()
        if self._source_stale:
            return True
        return compute_client_token(self._root) != client_token


def build_skew_response(request_id, server_sha: Optional[str], client_token: str) -> dict:
    """JSON-RPC 2.0 error envelope for a detected skew -- `ENGINE_SKEW`
    (-32001), naming both `server_sha` (the human-readable sha resolved
    once at boot; `None` if `resolve_engine_sha` could not resolve it --
    see that function's own `None` contract) and the `client_token` that
    triggered the mismatch, for operator diagnosis."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": ENGINE_SKEW,
            "message": "engine skew: this warm server is running stale source",
            "data": {"server_sha": server_sha, "client_token": client_token},
        },
    }


def evict_on_skew(
    *,
    respond: Callable[[dict], None],
    close_listener: Callable[[], None],
    drain: Callable[[], None],
    request_id,
    server_sha: Optional[str],
    client_token: str,
) -> None:
    """THE INVERSION, executed. Runs `respond` -> `close_listener` ->
    `drain`, in that fixed order, with no idleness/in-flight/drain-flag
    input anywhere in this signature -- the whole point of C16 (module
    docstring). `close_listener` running before `drain` is the
    non-negotiable half: it is what makes every OTHER session on the box
    see `FileNotFoundError` on its next connect attempt and start a
    current server, rather than queueing behind a drain that continuous
    traffic would otherwise keep rearming.
    """
    respond(build_skew_response(request_id, server_sha, client_token))
    close_listener()
    drain()
