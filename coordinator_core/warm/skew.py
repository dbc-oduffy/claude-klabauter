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
import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from coordinator_core import engine_version, lifecycle

__all__ = [
    "ENGINE_SKEW",
    "ENGINE_STAMP_FILENAME",
    "UnstampedEngineRootError",
    "compute_client_token",
    "write_engine_stamp",
    "read_engine_stamp_sha",
    "PublishLag",
    "publish_lag",
    "publish_lag_message",
    "PUBLISH_LAG_THRESHOLD_MINUTES",
    "CURRENCY_CACHE_DIRNAME",
    "CURRENCY_CACHE_FILENAME",
    "currency_cache_path",
    "currency_cache_key",
    "source_head_sha",
    "write_currency_cache",
    "ServerVersionState",
    "build_skew_response",
    "evict_on_skew",
]

#: Repo-relative paths whose commits count as "engine-touching" for
#: `publish_lag`'s `rev-list` scoping -- mirrors the amplification gate's
#: own notion of engine surface (see
#: `coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py`) and
#: `claude-klabauter-doctor-probe.py::_run_probe_publish_provenance`'s sibling
#: computation, which this function is a placement move of, not a
#: reimplementation.
_ENGINE_TOUCHING_PATHS = ("coordinator_core/", "coordinator/")

#: DR-335's threshold: the measured median per-fix time-to-live (28 min).
#: Below this, an unpublished commit is the ordinary case and MUST stay
#: silent (docs/decisions/DR-335-publish-lag-is-surfaced-not-shortened.md
#: § Consequences) -- surfacing it here would be wallpaper, not signal.
PUBLISH_LAG_THRESHOLD_MINUTES = 30


class UnstampedEngineRootError(RuntimeError):
    """Raised by `compute_client_token` when `repo_root` carries no valid
    `coordinator_core/_engine_stamp` (`skew.ENGINE_STAMP_FILENAME`).

    Spec backlink: docs/plans/2026-08-19-an-engine-root-is-a-stamped-build.md § C4

    THE KEYSTONE. Prior to this, an unstamped tree (the live working tree)
    fell back to a `.git/HEAD`-derived token -- reachable, therefore
    SUPPORTED, as a dispatch target. That fallback is deleted outright, not
    loosened: "an engine root is a stamped build; no stamp, no engine." A
    caller reaching this on the DISPATCH axis wanted an engine and got the
    live working tree instead -- exactly the "oops, wrong var set" hole the
    PM vetoed. See § Hard constraint 1 (no new escape hatch).

    `root` and `root_exists` are carried on the instance because the two
    conditions that reach here are different operator problems with
    different remediations, and a handler that sees only the class cannot
    tell them apart: an unstamped tree that IS there is the ruling
    (`root_exists` True), while a root that is NOT there is a broken
    root-resolution channel, which no ruling covers. `warm.client` branches
    on exactly this. `root_exists` defaults to `None` -- UNKNOWN, not
    PRESENT -- so a caller constructing the error with a bare message, or a
    future call site that passes `root=` without `root_exists=`, never has
    presence fabricated on its behalf. `warm.client._live_tree_cold_message`
    only takes the "does not exist" branch on an explicit `False`; `None`
    (and `True`) fall through to the ruling-shaped message, so a caller
    constructing the error with a bare message -- a test double, an older
    call site -- still keeps that message rather than being told a path is
    absent on no evidence.
    """

    def __init__(
        self, message: str, root: Optional[Path] = None, root_exists: Optional[bool] = None
    ) -> None:
        super().__init__(message)
        self.root = Path(root) if root is not None else None
        self.root_exists = root_exists


def _no_stamp_message(root: Path) -> str:
    """Guard-messaging register (docs/wiki/guard-messaging.md § Register):
    one fact, one runnable alternative, no self-legitimacy, no apology,
    never a slash command -- this can fire on the cold path, before any
    Claude Code session exists (`COLD_PATH_MODULES`).

    TWO CONDITIONS, TWO MESSAGES. A root that does not exist is not an
    unpublished engine, and directing an operator to `scripts/setup.py`
    under a directory that is not there names a path that cannot run. That
    case is a root-resolution channel pointing somewhere this box has
    nothing -- a pointer file carried in from another machine is the live
    instance (2026-08-22 install dogfood) -- so it names the absent path and
    routes to the reader that shows which channel produced it.
    """
    root = Path(root)
    if not root.is_dir():
        return (
            "engine root does not exist: {root}\n"
            "Remediation: python3 -m coordinator_core.root_channel_reconcile"
        ).format(root=root)
    root = root.resolve()
    setup_script = root / "scripts" / "setup.py"
    return (
        "engine root has no build stamp: {root} is not a published engine.\n"
        "Remediation: python3 {setup_script}"
    ).format(root=root, setup_script=setup_script)

def __getattr__(name: str):
    """PEP 562 lazy re-export of `client.ENGINE_SKEW`, the sole definition
    (`coordinator_core.warm.client.ENGINE_SKEW`) -- `skew` no longer keeps a
    duplicate literal. A module-scope `import` here would close
    `engine_root -> skew -> client -> election -> engine_root` into a real
    cycle (both `client` and `election` import `engine_root` at their own
    module scope), so the re-export is deferred to first attribute access
    instead, well past both modules' load time."""
    if name == "ENGINE_SKEW":
        from coordinator_core.warm.client import ENGINE_SKEW

        return ENGINE_SKEW
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


_REFRESH_INTERVAL_SECS = 2.0

#: Which axis found a request skewed, recorded on the server's exit row so
#: the two can be told apart after the fact. `SKEW_AXIS_SOURCE` is axis 2 --
#: this server's own engine source changed since boot, including an
#: uncommitted edit in the serving clone. `SKEW_AXIS_TOKEN` is axis 1 -- the
#: client's build-stamp token disagrees with this server's, which happens
#: when a publish ships engine-touching code. `ServerVersionState.is_skewed`
#: may report both.
SKEW_AXIS_SOURCE = "source"
SKEW_AXIS_TOKEN = "token"


def _default_engine_clone() -> Path:
    # Collapsed onto the single shared definition (plan
    # 2026-08-19-an-engine-root-is-a-stamped-build § C3): every one of the
    # seven former local `Path(__file__).resolve().parents[N]` copies now
    # calls `engine_root.current_engine_clone()` instead. Import kept
    # local to the function body: `engine_root` imports THIS module for
    # its stamp-path helpers, so a module-level import here would cycle.
    from coordinator_core.warm.engine_root import current_engine_clone

    return current_engine_clone()


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

    ONE SOURCE, unconditionally (docs/plans/2026-08-19-an-engine-root-is-a-
    stamped-build.md § C4): a PUBLISHED engine tree carries a build stamp
    (`coordinator_core/_engine_stamp`). The token fingerprints the stamp's
    bytes, so it changes exactly when a publish round ships new engine code
    and at no other time. `repo_root` carrying no stamp is no longer a
    second, ref-based source -- it is `UnstampedEngineRootError`. "An
    engine root is a stamped build. No stamp, no engine."

    WHY THE STAMP EXISTS -- measured, 2026-08-18. The token is embedded in
    the pipe name (`election.pipe_name`), so rotating it strands the running
    server and makes the next client spawn a successor. Keyed on the (now
    deleted) git-ref fallback, the token rotated **every ~32 seconds** on
    this box's shared branch, because ANY of the 50-70 concurrent sessions
    committing ANYTHING -- a doc, a `state/` artifact, a peer's unrelated
    code -- moves the ref. A server takes ~1s to boot and was stale before a
    client could reach it: warm served 0/6 with the feature fully enabled
    and correctly wired. Engine code had not changed once in that window.

    That is the defect this fixes: the token was COARSER than engine-source
    change in the one direction that matters, firing on commits that touch
    no engine code. The fleet runs the PUBLISHED mirror, where the stamp
    makes the generation stable between publishes -- which is precisely
    when the engine's code actually differs.

    THIS READER'S HALF OF THE CONTRACT WAS ALWAYS HONEST; THE WRITER'S
    WAS NOT, until 2026-08-21. This function has always changed exactly
    when the STAMP BYTES change -- that part was never in question. What
    was aspirational was the sentence above it: `publish.py`'s stamp
    writer pinned the round's raw toplevel HEAD (`_round_pin_source_sha`),
    which moves on every commit to the shared branch, not only an
    engine-touching one -- so in practice the stamp, and therefore this
    token, rotated on every publish round regardless of content, the
    exact coarseness this docstring claims was fixed. Measured
    2026-08-21: skew/superseded server exits ran 55%/33% of all warm
    generations, at medians of ~7min/~2.5min against a 15min idle
    deadline, tracking the ~9min publish cadence 1:1. `publish.py` now
    scopes the stamp's sha to the last commit touching
    `coordinator_core/`/`coordinator/` at or before the round's pin
    (`_scoped_engine_stamp_sha`, mirroring this module's own
    `_ENGINE_TOUCHING_PATHS`/`publish_lag` scoping) before writing it, so
    the sentence above is now enforced at the write site, not merely
    asserted at this read site.

    NEGATIVE SPEC -- a stale or absent stamp cannot serve stale code, and
    this is why keying on it is safe rather than merely cheap. Staleness
    detection does not rest on this token at all: axis 2
    (`ServerVersionState`, server-side, source-level) hashes the engine
    package itself and catches any real code change including an
    uncommitted editor save. Axis 1's job is GENERATION BINDING -- letting a
    successor bind a fresh pipe while a predecessor drains -- not
    staleness. Making axis 1 track engine identity therefore removes false
    rotations without removing a safety check; the check lives elsewhere
    and still runs.

    Deliberately NOT a per-call source hash or `git rev-parse`: the client
    pays this on every dispatch, and paying a subprocess (or a ~200-file
    walk) per call to avoid a process spawn per call is self-defeating --
    the same reasoning the module docstring already applies to
    `resolve_engine_sha()`. One stat is the budget.

    Raises:
        UnstampedEngineRootError: `root` carries no valid engine stamp.
    """
    root = Path(repo_root) if repo_root is not None else _default_engine_clone()

    stamp = _engine_stamp_path(root)
    try:
        stamp_bytes = stamp.read_bytes()
    except OSError:
        stamp_bytes = None
    if not stamp_bytes:
        raise UnstampedEngineRootError(
            _no_stamp_message(root), root=root, root_exists=root.is_dir()
        )
    return hashlib.sha1(b"engine-stamp:" + stamp_bytes).hexdigest()[:16]


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
    # Atomic, and load-bearing rather than tidy. `Path.write_text` opens
    # mode "w", which TRUNCATES TO ZERO before writing, so every publish
    # opened a window in which a concurrent reader observes a zero-length
    # stamp. Every consumer folds that into "not an engine root at all":
    # `engine_root.is_engine_root` and the C door's own
    # `is_valid_engine_root_w` (`warm/door/door.c`) both collapse missing
    # and zero-length into ONE failing verdict, and `compute_client_token`
    # derives a DIFFERENT token from empty bytes -- which renames the warm
    # pipe, since the token is embedded in it. A reader landing mid-write
    # therefore did not merely see a stale engine; it saw no engine, or
    # dialled a pipe nobody serves. The mkstemp + os.replace shape
    # (`session/fleet_delegation.py`, `session/grant.py`) closes the window
    # outright -- os.replace is atomic on Windows and POSIX alike, so no
    # reader can observe a partial or empty stamp at any instant.
    fd, tmp_name = tempfile.mkstemp(
        prefix=ENGINE_STAMP_FILENAME + ".", dir=str(stamp.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(identity.strip() + "\n")
        os.replace(tmp_name, stamp)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return stamp


def read_engine_stamp_sha(engine_root: Path) -> Optional[str]:
    """Bare source sha off a published engine's `_engine_stamp`, or `None`.

    The stamp's on-disk shape is `sha:<source-commit>` (`write_engine_stamp`'s
    `identity` convention, publish.py's writer). Returns `None` -- never
    raises -- for a missing file, an unreadable file, or a stamp that does
    not carry the `sha:` prefix; this is the read leg `publish_lag` composes
    with a `source_root` history check, and per DR-335 the caller must be
    able to tell "cannot establish" from "current" without a try/except of
    its own.
    """
    stamp = _engine_stamp_path(engine_root)
    try:
        raw = stamp.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        # ValueError covers UnicodeDecodeError: a stamp carrying invalid
        # UTF-8 is unreadable in exactly the sense this function's "never
        # raises" contract promises to absorb. Catching only OSError left
        # that one byte-level corruption escaping past a docstring that
        # said otherwise.
        return None
    if not raw.startswith("sha:"):
        return None
    sha = raw[len("sha:"):].strip()
    # The identity may carry a `+dirty-<hex>` suffix when the round that
    # wrote it shipped a dirty engine scope (`round.py :: stamp_engine_row`).
    # That suffix exists so the stamp's BYTES -- and therefore
    # `compute_client_token` -- distinguish two rounds at the same HEAD whose
    # shipped content differs. It is deliberately NOT part of this function's
    # answer: callers here want the source COMMIT, and `publish_lag` feeds it
    # straight to a git history check that cannot resolve a decorated ref.
    sha = sha.split("+", 1)[0].strip()
    return sha or None


@dataclass(frozen=True)
class PublishLag:
    """DR-335's publish-lag verdict -- computed once per call site, never
    per item. See `publish_lag`'s docstring for the two-git-call bound and
    the full `None` contract.
    """

    stamp_sha: str
    engine_commits_behind: int
    oldest_unpublished_iso: Optional[str]
    age_minutes: Optional[float]


def publish_lag(engine_root: Path, source_root: Path) -> Optional[PublishLag]:
    """DR-335's publish-lag computation -- reused from, not a reimplementation
    of, `bin/claude-klabauter-doctor-probe.py::_run_probe_publish_provenance`'s verdict
    shape, moved onto the consuming path per the decision's § Consequences.

    At most TWO bounded git subprocess calls, both scoped to
    `_ENGINE_TOUCHING_PATHS`, never per-commit -- the amplification gate
    (`coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py`)
    governs:
      1. `git rev-list --count <sha>..HEAD -- coordinator_core/ coordinator/`
         -- the commit count.
      2. Only if that count is > 0: `git log -1 --format=%aI <sha>..HEAD --
         coordinator_core/ coordinator/ | tail -1`-equivalent (`--reverse`
         plus a single-line take) for the OLDEST unpublished commit's
         author-date, to compute `age_minutes`.

    Returns `None` whenever freshness cannot be established -- no stamp, a
    stamp sha unresolvable in `source_root`'s history, git unavailable, or
    any unexpected exception. Never raises into the caller and never
    asserts freshness it cannot prove (DR-335's own negative spec: a stale
    mirror between rounds is expected behaviour, not a defect to detect
    wrong).
    """
    try:
        sha = read_engine_stamp_sha(engine_root)
        if not sha:
            return None

        count_proc = subprocess.run(
            [
                "git", "-C", str(source_root), "rev-list", "--count",
                f"{sha}..HEAD", "--", *_ENGINE_TOUCHING_PATHS,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if count_proc.returncode != 0:
            return None
        count_str = count_proc.stdout.strip()
        if not count_str.isdigit():
            return None
        commits_behind = int(count_str)

        if commits_behind == 0:
            return PublishLag(
                stamp_sha=sha,
                engine_commits_behind=0,
                oldest_unpublished_iso=None,
                age_minutes=None,
            )

        oldest_proc = subprocess.run(
            [
                "git", "-C", str(source_root), "log",
                "--format=%aI", f"{sha}..HEAD", "--", *_ENGINE_TOUCHING_PATHS,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if oldest_proc.returncode != 0:
            return None
        # NEGATIVE SPEC -- do NOT "optimise" this into `--reverse ... -1`.
        # git applies the commit limit BEFORE reversing, so `--reverse -1`
        # returns the NEWEST unpublished commit. That silently pins
        # `age_minutes` near zero on an active branch, holds the advisory
        # below its threshold forever, and disables the signal entirely
        # while every field still looks populated. Take the last line of
        # the full range instead: same single subprocess, no limit flag.
        oldest_lines = [ln for ln in oldest_proc.stdout.splitlines() if ln.strip()]
        if not oldest_lines:
            return None
        oldest_iso = oldest_lines[-1].strip()

        try:
            from datetime import datetime

            oldest_dt = datetime.fromisoformat(oldest_iso)
            now_dt = datetime.now(oldest_dt.tzinfo)
            age_minutes = (now_dt - oldest_dt).total_seconds() / 60.0
        except (ValueError, TypeError):
            return None

        return PublishLag(
            stamp_sha=sha,
            engine_commits_behind=commits_behind,
            oldest_unpublished_iso=oldest_iso,
            age_minutes=age_minutes,
        )
    except Exception:
        # DELIBERATELY BROAD, and narrower was the bug. The docstring's
        # contract is "never raises into the caller", and a named-type list
        # cannot honour that: `UnicodeDecodeError` from a corrupt stamp is a
        # `ValueError`, not an `OSError`, and slipped straight through the
        # previous `(OSError, subprocess.SubprocessError)` pair. This is an
        # advisory whose entire purpose is to be ignorable -- a lag helper
        # that can take down a fire or a close-out is strictly worse than one
        # that goes quiet, so absorbing the unknown case is the correct trade
        # here and nowhere else.
        return None


def publish_lag_message(lag: PublishLag, *, site: str = "fire") -> Optional[str]:
    """DR-335's advisory text, authored once for both call sites
    (`ops/workflow_fire/fire.py`, `workstream_complete/directives_commit_tail.py`)
    so the register (docs/wiki/guard-messaging.md § Register -- one fact,
    one runnable alternative, no self-legitimacy, no repetition, no
    reassurance, no apology) is authored once, not twice.

    `site` selects the ONE sentence whose truth condition differs between
    them, and exists because sharing that sentence verbatim was wrong:
    "this run executes the published mirror" is true where a fire is about
    to execute the mirror, and false at close-out, where nothing is
    executing and the fact is that THIS session's commits are not yet live
    for anyone. Centralising prose across two audiences is the
    amplification gotcha the same doc names (§ Gotchas); the fix is one
    parameter, not two copies.

    Returns `None` below `PUBLISH_LAG_THRESHOLD_MINUTES` or when
    `engine_commits_behind` is 0 -- callers gate on this return, not on a
    separately-recomputed threshold check.

    INCIDENTAL on the remedy's repo name (probe row 21, same shape as
    `cc_invoke._announce_engine_cli_split`): this text surfaces broadly
    (engine floor, cross-repo) regardless of the reader's own repo, but
    `claude-klabauter` is the publish DESTINATION, not the reader's
    problem to name unless the reader owns this engine's own checkout.
    `_reader_owns_engine_repo` suppresses only that repo name for a
    third-repo reader; the lag fact and the rest of the sentence are
    SUBJECT and always render.
    """
    if lag.engine_commits_behind <= 0:
        return None
    if lag.age_minutes is None or lag.age_minutes <= PUBLISH_LAG_THRESHOLD_MINUTES:
        return None
    age_hours = lag.age_minutes / 60.0
    scope = (
        "This run executes the published mirror, not your tree."
        if site == "fire"
        else "These are not live for any session until a round lands them."
    )
    remedy = (
        "Publish: python coordinator/bin/percolate-round.py claude-klabauter"
        if _reader_owns_engine_repo()
        else "Publish: run a percolate round to publish this engine."
    )
    return (
        f"Engine lag: {lag.engine_commits_behind} commit(s) touching engine "
        f"code are unpublished (oldest {age_hours:.1f}h). {scope} "
        f"{remedy}"
    )


def _reader_owns_engine_repo() -> bool:
    """Whether the calling session's own repo (its cwd's git root) is this
    running engine's own checkout -- the same reader-identity question
    `cc_invoke._announce_engine_cli_split` asks, answered here without a
    second resolver: `current_engine_clone()` is already the locator-axis
    root for this process (see that function's own docstring), so the only
    new lookup is the reader's own git root.

    Fails OPEN to True (name shown) whenever the reader's own root cannot be
    resolved or compared -- an unresolvable reader is not evidence of a
    third-repo reader, and this is advisory text, never a gate.
    """
    try:
        from coordinator_core.git.repo_root import show_toplevel
        from coordinator_core.warm.engine_root import current_engine_clone

        reader_root = show_toplevel()
        if reader_root is None:
            return True
        return Path(reader_root).resolve() == current_engine_clone().resolve()
    except Exception:
        return True


# --- the currency cache: computed where git is already paid for, read where -
#     it is not ---------------------------------------------------------------
#
# The forwarder door (`coordinator/lib/resolve-claude-klabauter/_resolve_claude_klabauter.py`)
# diverts a session to the published mirror and, until now, said nothing about
# that mirror's vintage. It cannot call `publish_lag` itself: it is on the
# interpreter floor of every coordinator invocation on a box carrying 50-70
# concurrent sessions, it is forbidden to import `coordinator_core` at all, and
# `publish_lag` costs 15.6ms of process time / 99.3ms wall (measured here,
# k=5, 2026-08-28). So the verdict is computed by a writer that ALREADY spawns
# git, and the door only reads it.
#
# THE WRITER RUNS ON THE INVALIDATING EVENT. A commit is what changes the
# answer, so the cache is refreshed by the post-commit path
# (`hooks/auto_push.run_push_with_retry`, in its already-detached child) rather
# than by a timeout. That is what answers C5's standing objection to a stored
# ref: this one is never "a ref nothing compares against", because the thing
# that would falsify it is the thing that rewrites it. Publish rounds are the
# other invalidating event and should write here too when one is wired.
#
# NEGATIVE SPEC -- the key is the whole safety property. A verdict whose key
# does not match what the reader observes is treated as ABSENT, never as a
# lower-confidence answer: reporting a lag computed under a source HEAD that
# has since moved is worse than silence, and silence is what this degrades to
# everywhere else already.
CURRENCY_CACHE_DIRNAME = "coordinator"
CURRENCY_CACHE_FILENAME = "engine-currency.json"


def currency_cache_path() -> Optional[Path]:
    """Where the currency verdict lives, or `None` where `LOCALAPPDATA` is
    unset (non-Windows / a stripped environment).

    `%LOCALAPPDATA%/coordinator/` is the convention
    `install/substrate.py::_dispatch_root_cache_path` already established for
    exactly this shape -- a machine-local accelerator a later reader consults,
    never authoritative state. Kept byte-compatible with the door's own
    standalone twin (`_resolve_claude_klabauter.py::_currency_cache_path`), which cannot
    import this module; the two are synchronised by hand and by
    `test_resolve_claude_klabauter_currency_signal.py`, which asserts they agree.
    """
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if not local:
        return None
    return Path(local) / CURRENCY_CACHE_DIRNAME / CURRENCY_CACHE_FILENAME


def source_head_sha(source_root: Path) -> Optional[str]:
    """The source tree's HEAD commit sha, read straight off `.git` with no
    subprocess -- the half of the cache key that moves on every commit.

    Git-free because both ends need it and only one of them may spawn: the
    writer could afford `git rev-parse`, but the door pays 0.078ms for this
    whole key read (measured, k=200) and would pay ~25ms for one `git`
    process. Resolves the symref by hand, then the loose ref, then
    `packed-refs`; a detached HEAD is the raw sha and returns as-is.

    `None` on anything unexpected -- an absent `.git`, a worktree's `gitdir:`
    indirection, an unreadable ref. The caller treats that as "no key", which
    means "no verdict", never an error.
    """
    try:
        git_dir = Path(source_root) / ".git"
        raw = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if not raw.startswith("ref: "):
            return raw or None
        ref = raw[5:].strip()
        loose = git_dir / ref
        try:
            return loose.read_text(encoding="utf-8").strip() or None
        except OSError:
            pass
        for line in (git_dir / "packed-refs").read_text(encoding="utf-8").splitlines():
            if line.endswith(" " + ref):
                return line.split()[0]
        return None
    except Exception:
        return None


def currency_cache_key(engine_root: Path, source_root: Path) -> Optional[dict]:
    """The `(source HEAD, engine stamp bytes)` pair a verdict is only valid
    under. `None` when either half is unavailable, which is the checkout-free
    box's ordinary state -- see this section's header comment."""
    head = source_head_sha(source_root)
    if not head:
        return None
    try:
        stamp = _engine_stamp_path(engine_root).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not stamp:
        return None
    return {"source_head": head, "engine_stamp": stamp}


def write_currency_cache(engine_root: Path, source_root: Path) -> Optional[Path]:
    """Compute the publish lag once and persist it with the key it holds
    under. Returns the path written, or `None` when nothing was written.

    BEST-EFFORT IN BOTH DIRECTIONS, and the caller must treat it that way:
    this never raises, and no caller may fail its own operation over a cache
    write. A `publish_lag` of `None` (no stamp, stamp unresolvable in this
    history, git unavailable) writes NOTHING and leaves any prior verdict in
    place -- overwriting a good verdict with an empty one would convert a
    transient git failure into a permanently silent door.

    Written atomically via `os.replace`, matching
    `substrate._write_native_forwarder_manifest`: a dozen sessions commit to
    this branch concurrently and a torn read must be impossible rather than
    merely unlikely.
    """
    path = currency_cache_path()
    if path is None:
        return None
    key = currency_cache_key(engine_root, source_root)
    if key is None:
        return None
    lag = publish_lag(Path(engine_root), Path(source_root))
    if lag is None:
        return None
    tmp_path: Optional[Path] = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "engine_commits_behind": lag.engine_commits_behind,
            "oldest_unpublished_iso": lag.oldest_unpublished_iso,
            "key": key,
        }
        tmp_path = path.parent / f".{path.name}.{os.getpid()}.tmp"
        tmp_path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
        os.replace(tmp_path, path)
        return path
    except Exception:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass
        return None


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
        self._last_skew_axes: tuple = ()

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

        BOTH AXES ARE EVALUATED, NOT SHORT-CIRCUITED, and the deciding set
        is left on `last_skew_axes` for the exit record. An earlier version
        tested `_source_stale` first and returned early, which is correct
        for the boolean and wrong for attribution: when both axes hold,
        only axis 2 was ever reachable, so any count built on it
        under-reports axis 1 BY CONSTRUCTION (claude-klabauter-22,
        2026-08-26). The two have opposite remediations -- axis 1 is the
        publish cadence stranding servers, axis 2 is something editing
        engine source in the clone that serves the fleet -- so a telemetry
        row that cannot tell them apart sends the next reader at the wrong
        one. The extra cost is one stat (`compute_client_token`'s stamp
        read) on a request that is already about to evict this server, not
        on the served path.
        """
        self.refresh()
        axes = []
        if self._source_stale:
            axes.append(SKEW_AXIS_SOURCE)
        if compute_client_token(self._root) != client_token:
            axes.append(SKEW_AXIS_TOKEN)
        self._last_skew_axes = tuple(axes)
        return bool(axes)

    @property
    def last_skew_axes(self) -> tuple:
        """The axes that decided the most recent `is_skewed` call -- a
        subset of (`SKEW_AXIS_SOURCE`, `SKEW_AXIS_TOKEN`), empty when that
        call returned False or none has run yet. Read by `warm.server`'s
        eviction path to record WHICH axis evicted; carries no meaning
        outside the call that set it."""
        return self._last_skew_axes


def build_skew_response(request_id, server_sha: Optional[str], client_token: str) -> dict:
    """JSON-RPC 2.0 error envelope for a detected skew -- `ENGINE_SKEW`
    (-32002), naming both `server_sha` (the human-readable sha resolved
    once at boot; `None` if `resolve_engine_sha` could not resolve it --
    see that function's own `None` contract) and the `client_token` that
    triggered the mismatch, for operator diagnosis."""
    from coordinator_core.warm.client import ENGINE_SKEW

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
    docstring). `close_listener` running before `drain` stops this server
    accepting NEW work while the drain finishes the work it already has.

    WHAT THIS ORDERING DOES NOT DO, corrected 2026-08-26. This docstring
    previously asserted that `close_listener` "makes every OTHER session on
    the box see `FileNotFoundError` on its next connect attempt and start a
    current server." That is false as implemented, and the anti-storm
    reasoning built on it is false with it. The `close_listener` the server
    passes here (`warm/server.py :: _ServerContext.close_listener`) flips
    an in-process boolean; the OS-level close and unlink happen in
    `_ServerContext._ctx_shutdown`, AFTER the drain, up to a 35s ceiling.
    Until then the endpoint stays bound: a caller arriving in that window is
    accepted and dropped with zero bytes (a NON-spawning outcome per
    `warm/client.py`'s table), and a successor computing the SAME token
    cannot bind at all -- `ERROR_ACCESS_DENIED` on Windows, `EADDRINUSE` on
    POSIX with the staleness probe reading the still-bound socket as live.
    A successor with a DIFFERENT token is unaffected; it binds a different
    endpoint immediately, which is the case this path was designed around.

    Releasing the endpoint at this step is a candidate fix, not a pending
    one: it converts the drain window from spawn-suppressing to
    spawn-triggering for every caller at once, and interacts with
    `breadcrumb.should_spawn`'s debounce. See
    `docs/research/2026-08-26-repo-warm-succession-advisory.md` item 3 for
    the hazards and `docs/research/2026-08-26-repo-warm-succession.md` § 2
    for the verification.
    """
    respond(build_skew_response(request_id, server_sha, client_token))
    close_listener()
    drain()
