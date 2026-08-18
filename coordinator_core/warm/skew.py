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
    "ENGINE_STAMP_FILENAME",
    "compute_client_token",
    "write_engine_stamp",
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


#: Filename of the engine build stamp, written into a PUBLISHED engine tree
#: by the publish round (see `coordinator/bin/publish.py`). Its presence is
#: what distinguishes "this is a published/installed engine, whose code only
#: changes at publish time" from "this is a live working tree".
#:
#: NOT dot-prefixed, and that is load-bearing: the publish sync skips dotfiles
#: at a synced directory's top level by design
#: (`publish_sync._sync_mirror_top_level_files`), so a `.engine-stamp` was
#: built into the restricted tree and then silently never shipped -- the round
#: reported 9/9 success with no stamp in the mirror (observed 2026-08-18).
ENGINE_STAMP_FILENAME = "_engine_stamp"


def _engine_stamp_path(repo_root: Path) -> Path:
    return Path(repo_root) / "coordinator_core" / ENGINE_STAMP_FILENAME


def compute_client_token(repo_root: Optional[Path] = None) -> str:
    """Primary version-skew token: a stat-only fingerprint of this engine's
    identity, no subprocess.

    TWO SOURCES, and which one applies is a property of the tree, not a
    setting:

      1. A PUBLISHED engine tree carries a build stamp
         (`coordinator_core/_engine_stamp`). The token fingerprints the
         stamp's bytes, so it changes exactly when a publish round ships new
         engine code and at no other time.
      2. A LIVE WORKING TREE has no stamp. The token falls back to the
         original git-ref fingerprint -- `(st_mtime_ns, st_size)` of
         `.git/HEAD` and the ref it points at (or `(0, 0)` on a detached
         HEAD).

    WHY THE STAMP EXISTS -- measured, 2026-08-18. The token is embedded in
    the pipe name (`election.pipe_name`), so rotating it strands the running
    server and makes the next client spawn a successor. Keyed on the git
    ref, the token rotated **every ~32 seconds** on this box's shared branch,
    because ANY of the 50-70 concurrent sessions committing ANYTHING -- a
    doc, a `state/` artifact, a peer's unrelated code -- moves the ref. A
    server takes ~1s to boot and was stale before a client could reach it:
    warm served 0/6 with the feature fully enabled and correctly wired.
    Engine code had not changed once in that window.

    That is the defect this fixes: the token was COARSER than engine-source
    change in the one direction that matters, firing on commits that touch
    no engine code. The fleet runs the PUBLISHED mirror, where the stamp
    makes the generation stable between publishes -- which is precisely
    when the engine's code actually differs.

    NEGATIVE SPEC -- a stale or absent stamp cannot serve stale code, and
    this is why keying on it is safe rather than merely cheap. Staleness
    detection does not rest on this token at all: axis 2
    (`ServerVersionState`, server-side, source-level) hashes the engine
    package itself and catches any real code change including an
    uncommitted editor save, on a tree with a stamp or without one. Axis 1's
    job is GENERATION BINDING -- letting a successor bind a fresh pipe while
    a predecessor drains -- not staleness. Making axis 1 track engine
    identity therefore removes false rotations without removing a safety
    check; the check lives elsewhere and still runs.

    Deliberately NOT a per-call source hash or `git rev-parse`: the client
    pays this on every dispatch, and paying a subprocess (or a ~200-file
    walk) per call to avoid a process spawn per call is self-defeating --
    the same reasoning the module docstring already applies to
    `resolve_engine_sha()`. One stat is the budget.
    """
    root = Path(repo_root) if repo_root is not None else _default_engine_clone()

    stamp = _engine_stamp_path(root)
    try:
        stamp_bytes = stamp.read_bytes()
    except OSError:
        stamp_bytes = None
    if stamp_bytes is not None:
        return hashlib.sha1(b"engine-stamp:" + stamp_bytes).hexdigest()[:16]

    git_dir = root / ".git"
    ref_path = _resolve_ref_path(git_dir)
    signal = (
        _stat_pair(git_dir / "HEAD"),
        _stat_pair(ref_path) if ref_path is not None else (0, 0),
    )
    return hashlib.sha1(repr(signal).encode("utf-8")).hexdigest()[:16]


def write_engine_stamp(repo_root: Path, identity: str) -> Path:
    """Write the engine build stamp into a PUBLISHED/INSTALLED engine tree,
    making `compute_client_token` stable between publishes for that tree.

    `identity` is whatever uniquely names this shipped engine build -- the
    publish round's pinned source sha is the intended value. It is recorded
    verbatim so the stamp is human-readable when debugging which build a
    resident server is serving; only its BYTES matter to the token.

    Callers: the install/publish seam. A live working tree must NOT have one
    (see `compute_client_token`'s two sources) -- writing a stamp into a tree
    whose engine code then changes underneath it would pin the generation
    while the code moved. That is not a staleness hole (axis 2 still hashes
    the package and evicts on any real change) but it is a pointless
    generation pin, so the seam is publish/install-time only, never a
    development convenience.
    """
    stamp = _engine_stamp_path(repo_root)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(identity.strip() + "\n", encoding="utf-8")
    return stamp


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
