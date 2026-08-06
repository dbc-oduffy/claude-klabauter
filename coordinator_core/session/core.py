"""
coordinator_core.session.core — Python engine port of the retained-in-hub
helpers from coordinator-session.sh (Port of: example-doctrine-repo e34f2484, 2026-07-22) —
the functions T0-decompose could NOT bash-extract into
``coordinator/lib/session/*.sh``:
git-root/session-dir resolution, clock helpers, PID/liveness primitives,
meta.json read/write, the confined-findings-agent SSOT, and ``cs_init``).

Every other session-registry resolver in this package transitively depends
on ``git_root()`` via cwd — see that function's docstring for the
NOT-cached-across-calls constraint this preserves from the bash original.

Recipe: scratch/subagent-sandbox/bash-to-python-engine-migration/
recipe-t4a-coordinator-session-hub.md § core.py
Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T4a-g1

Negative-spec:
    - Do NOT call ``ps -p``/``kill -0``/``psutil.pid_exists`` on a stored
      ``pid`` field for a LIVENESS decision — that field is a dead
      per-hook-subshell ``$$``, not the long-lived session process (see
      ``pid_alive()`` docstring). Only ``stable_pid_alive()`` (keyed on the
      separate ``stable_pid`` field) is a legitimate liveness signal.
    - Do NOT duplicate PID/liveness fields into any structure outside the
      session registry's ``meta.json`` — RAW-PID-LIVENESS floor
      (docs/wiki/coordinator-tripwires.md) and the single-liveness-key
      invariant (D5, pcore-03) both forbid it.
"""

from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import psutil
except ImportError:  # psutil is a declared engine dependency (pyproject.toml);
    # the guard exists ONLY so this module stays importable on a host missing
    # it. It is NOT license for silent degradation anywhere psutil is the
    # sole liveness mechanism (Windows entirely, POSIX's stable_pid_alive
    # Layer 1 since the 2026-07-27 ps-to-psutil port) — every call site that
    # is actually load-bearing there raises MissingPsutilError instead of
    # falling through. ``pid_alive`` on POSIX is the sole surviving `os.kill`
    # user; see that docstring / ``_win_create_time_epoch``.
    psutil = None  # type: ignore[assignment]


class MissingPsutilError(RuntimeError):
    """Raised when a Windows-only, psutil-dependent liveness path is reached
    with psutil absent. psutil is a required engine dependency — there is no
    correct fallback for "is this Windows PID alive?" without it, and
    returning/propagating False here is indistinguishable from a genuinely
    dead process (the exact wrongful-claim-takeover shape this class exists
    to prevent: every session would read DEAD)."""

_NO_CONSOLE = (
    {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
)

#: Platform seam for the PID-liveness branches (POSIX ``ps`` vs Windows
#: ``psutil.create_time()``). A dedicated constant — not an inline
#: ``os.name == "nt"`` — so a test can exercise the Windows create_time path on
#: a POSIX host (psutil is cross-platform) WITHOUT globally forcing
#: ``os.name = "nt"`` (which would flip ``pathlib`` to the un-instantiable
#: ``WindowsPath`` on POSIX). Read at call time, so a test may monkeypatch it.
_IS_WINDOWS = os.name == "nt"

#: Port of ``_cs_is_confined_findings_agent``'s CONFINED SET — a single
#: member as of the 2026-07-01 findings-agents-self-persist change. Review
#: personas were removed from this set on 2026-07-01 (see bash source
#: comment for rationale — they are trusted full-tool Opus agents, not
#: confined). Adding a member here is the single edit point.
_CONFINED_FINDINGS_AGENTS = frozenset({"coordinator:code-reviewer"})

#: Port of ``_cs_lstart_to_epoch``'s python-fallback strptime format —
#: matches ``ps -o lstart=`` output exactly (e.g. "Tue Jul 14 15:26:28 2026").
_LSTART_FORMAT = "%a %b %d %H:%M:%S %Y"

#: Tolerance (seconds) for comparing a psutil-derived ``create_time()``
#: epoch against a STORED ``stable_pid_start_epoch``. Every ``meta.json``
#: on disk as of the 2026-07-27 ps-to-psutil port was written with an epoch
#: derived from ``lstart_to_epoch(ps -o lstart=)`` — a different derivation
#: path than ``int(psutil.Process(pid).create_time())`` for the SAME
#: process, and the two can legitimately differ by ~1s (independent
#: truncation/rounding; ``/proc`` btime rounding on Linux). Under exact
#: equality that skew reads every currently-live persisted session as DEAD —
#: precisely the wrongful-takeover failure this function exists to prevent.
#: A recycled PID whose birth instant falls within this window of the
#: original process's is not a realistic collision, so the marginal
#: PID-reuse detection loss is negligible against the certainty of breaking
#: every persisted record. See ``docs/plans/2026-06-27-liveness-first-claim-staleness.md``.
_STABLE_PID_EPOCH_TOLERANCE_SECS = 2

#: Collapse runs of whitespace — mirrors the bash helper's ``tr -s ' '``
#: BSD-double-space normalization (single-digit %e day pad on macOS/BSD ps).
_WHITESPACE_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# git-root / session-dir resolution
# ---------------------------------------------------------------------------


def git_root(cwd: Optional[str] = None) -> str:
    """Port of ``_cs_git_root``: the git root for the given/current cwd, or
    ``""`` on failure (not a git repo, git missing, etc.).

    Deliberately NOT cached across calls — cwd can legitimately change
    mid-process for a long-lived import (e.g. a reaper's documented
    cwd-relative resolution discipline). Every caller that needs a
    fixed root must pass ``cwd`` explicitly rather than rely on process-wide
    caching.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            cwd=cwd,
            # House value (dispatch_checks._run_git) -- 2026-08-05 hot-path
            # hardening pass. Fail-open, joining the pre-existing OSError
            # leg: a hung git is indistinguishable from "not a git repo"
            # for every caller of this function.
            timeout=2.0,
            **_NO_CONSOLE,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _sessions_dir_resolve(cwd: Optional[str]) -> str:
    """Uncached ``git rev-parse --path-format=absolute --git-common-dir``
    spawn — the sole place this repo shells out for the session hub path.
    Shared by ``sessions_dir()``'s ``cwd is None`` branch and by
    ``_sessions_dir_cached()``; see ``sessions_dir`` docstring for the
    caching policy layered on top of this.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            cwd=cwd,
            # House value -- 2026-08-05 hot-path hardening pass. Fail-open,
            # joining the pre-existing OSError leg (see git_root() above).
            timeout=2.0,
            **_NO_CONSOLE,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    common = result.stdout.strip()
    if not common:
        return ""
    return str(Path(common) / "coordinator-sessions")


class _SessionsDirResolutionFailed(Exception):
    """Internal-only signal raised by ``_sessions_dir_cached`` on a failed
    resolution, so ``functools.lru_cache`` does NOT memoize it —
    ``lru_cache`` never caches a call that raises. See ``sessions_dir``
    docstring, point (3): a failure must not poison the cache entry for the
    rest of the process the way a successful resolution legitimately can.
    """


@functools.lru_cache(maxsize=32)
def _sessions_dir_cached(cwd: str) -> str:
    result = _sessions_dir_resolve(cwd)
    if not result:
        raise _SessionsDirResolutionFailed(cwd)
    return result


def reset_sessions_dir_cache() -> None:
    """Test/diagnostic escape hatch — clears the process-local
    ``sessions_dir()`` cache. Call this in test teardown/setup for any test
    that creates a git repo under a path that could collide with an entry
    left behind by an earlier test in the same process (e.g. reusing a
    fixed ``tmp_path``-adjacent path across parametrized runs); ``tmp_path``
    itself is already unique per test so ordinary tests do not need this."""
    _sessions_dir_cached.cache_clear()


def sessions_dir(cwd: Optional[str] = None) -> str:
    """Port of ``_cs_sessions_dir``: ``<git-common-dir>/coordinator-sessions``,
    or ``""`` if not in a git repo.

    Resolves the session hub via ``git rev-parse --git-common-dir`` rather
    than joining a literal ``".git"`` onto ``git_root()``'s toplevel. In a
    linked git worktree, ``<worktree>/.git`` is a gitdir-pointer FILE, not a
    directory — joining onto it and calling ``mkdir`` raises
    ``NotADirectoryError``. ``--git-common-dir`` instead resolves to the MAIN
    worktree's real ``.git`` directory from any worktree of the repo. In a
    normal (non-worktree) repo this is byte-identical to the prior
    ``<root>/.git`` behavior — no existing session hub relocates.

    Deliberately uses ``--git-common-dir``, NOT ``--git-dir``: ``--git-dir``
    inside a worktree returns the worktree-PRIVATE
    ``<main>/.git/worktrees/<name>`` directory, which would give each
    worktree of one repo its own private claim namespace under
    ``memo-claims/``, ``handoff-claims/``, ``plan-claims/`` — silently
    permitting two sessions in different worktrees of the SAME repo to claim
    the same memo/handoff/plan concurrently, exactly the collision the claim
    locks exist to prevent. Claims must contend across every worktree of one
    repo, so the hub is the shared common dir, never the worktree-local one.

    Uses ``--path-format=absolute`` so git emits an absolute path directly —
    avoids the trap where ``Path(x).resolve()`` on a relative
    ``--git-common-dir`` result (the common case, a bare ``".git"``) would
    resolve against the CURRENT PROCESS cwd instead of the subprocess's
    ``cwd``, silently producing a wrong path.

    Caching policy (added to eliminate repeat byte-identical spawns —
    ``session_dir()``/``session_live()`` call this multiple times per
    op-invocation with the same explicit ``cwd``): cached, process-local,
    keyed on ``cwd`` ONLY when ``cwd is not None``. A caller passing an
    explicit ``cwd`` has asked for a FIXED root and can be served from
    cache; ``cwd=None`` means "resolve against whatever the process cwd is
    right now" — the same case ``git_root()``'s docstring documents as able
    to legitimately change mid-process — so that branch is never cached and
    always re-spawns, mirroring ``git_root()``'s own uncached contract.

    A FAILED resolution (not a git repo, git missing, transient spawn
    error) is deliberately NOT cached even for an explicit ``cwd`` — only a
    successful resolution is memoized. A transient failure (e.g. a git lock
    contention, or a repo mid-initialization) caching as authoritative would
    poison every subsequent call for that ``cwd`` for the rest of the
    process; the cost of re-spawning on the (rare, off-hot-path) failure
    case is far cheaper than that failure mode. See ``reset_sessions_dir_cache()``
    for the cache-clear escape hatch, and ``lifecycle.git_common_dir`` for
    the sibling ``--git-common-dir`` resolver this module deliberately does
    NOT route through (different failure contract — raises ``RuntimeError``
    rather than returning ``""`` — and different worktree semantics
    downstream; two resolvers stay, this one alone gains a cache).
    """
    if cwd is None:
        return _sessions_dir_resolve(cwd)
    try:
        return _sessions_dir_cached(cwd)
    except _SessionsDirResolutionFailed:
        return ""


def session_dir(sid: str, cwd: Optional[str] = None) -> str:
    """Port of ``_cs_session_dir <session_id>``."""
    if not sid:
        raise ValueError("session_id required")
    base = sessions_dir(cwd)
    if not base:
        return ""
    return str(Path(base) / sid)


# ---------------------------------------------------------------------------
# Clock helpers
# ---------------------------------------------------------------------------


def now_iso() -> str:
    """Port of ``_cs_now_iso``: ISO-8601 timestamp, second resolution, UTC."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_epoch() -> int:
    """Port of ``_cs_now_epoch``: seconds since epoch."""
    return int(datetime.now(timezone.utc).timestamp())


# ---------------------------------------------------------------------------
# PID / liveness primitives
# ---------------------------------------------------------------------------


def pid_alive(pid) -> bool:
    """Port of ``_cs_pid_alive <pid>``.

    NOT a session-liveness signal in-harness — every Bash/hook tool call
    has a fresh, short-lived ``$$``. Retained only for the legacy pid-only
    claim-dir fallback and diagnostics; never gate session liveness on
    this. Distinct from ``stable_pid_alive`` — see that function's
    docstring.

    Parity note: the bash original is ``kill -0 "$pid" 2>/dev/null``,
    which reports DEAD (nonzero exit) on EPERM (process exists but is
    owned by another uid) as well as ESRCH — ``kill -0`` treats
    "can't signal it" the same as "can't find it". ``psutil.pid_exists``
    has no such notion (it reports True for any visible-but-unowned PID),
    so on POSIX we shell out to the same ``os.kill(pid, 0)`` primitive
    bash uses to preserve that EPERM-is-dead parity exactly.
    Review: code-reviewer P2 — psutil.pid_exists() and the prior
    PermissionError->True branch both diverged from bash's kill-0-as-dead
    EPERM handling.

    Raises :class:`MissingPsutilError` on Windows when psutil is absent —
    there is no POSIX-equivalent fallback there, and returning False would
    silently report every PID dead (see that class's docstring).
    """
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False  # non-numeric/absent pid -> not alive
    if pid_int <= 0:
        return False
    if _IS_WINDOWS:
        # No kill(pid, 0) equivalent on native Windows; psutil is
        # authoritative there (bash's kill -0 doesn't run natively either).
        if psutil is None:
            raise MissingPsutilError(
                "psutil is a required dependency and is not installed in this "
                "interpreter, but Windows PID-liveness (pid_alive) has no "
                "fallback without it — reporting every PID dead here would "
                "risk wrongful claim takeover. Install it "
                "(`pip install psutil` / `pip install -e .` from the "
                "coordinator_core project root) or re-run claude-klabauter's setup "
                "(scripts/setup.py) to provision the engine venv."
            )
        return psutil.pid_exists(pid_int)
    try:
        os.kill(pid_int, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False  # bash `kill -0` also reports dead (nonzero) on EPERM
    except OSError:
        return False  # any other kill(2) failure -> treat as dead (same parity)
    return True


class _WinLivenessAmbiguous(Exception):
    """Review: code-reviewer P2 — ``psutil.Process.create_time()`` raised
    something other than ``NoSuchProcess`` (``AccessDenied``, ``ZombieProcess``,
    etc.): the QUERY failed, not necessarily the process. Signalled distinctly
    from "definitely dead" so ``stable_pid_alive`` can fail toward ALIVE
    rather than collapsing an ambiguous read into DEAD — the exact
    wrongful-takeover shape (2026-06-23) this leg exists to close."""


def _win_create_time_epoch(pid_int: int) -> Optional[int]:
    """``stable_pid_alive``'s Layer-1 birth-instant on BOTH platforms:
    ``psutil.Process(pid).create_time()`` as integer epoch seconds, or
    ``None`` when the process is gone / ``psutil`` is absent. Name retained
    from its Windows-only origin (test call sites pin it directly) — it is
    no longer Windows-exclusive.

    Originally added for Windows (no ``ps`` binary there), then converged
    onto by POSIX too in the 2026-07-27 ps-to-psutil port (example-doctrine-repo
    cross-repo memo ``2026-07-27-example-doctrine-repo-em-ac6-ps-to-psutil-yes-oracle-
    retired.md`` confirmed the bash ``_cs_stable_pid_alive`` parity oracle
    this POSIX arm preserved was itself retired 2026-07-22 — there is no
    remaining counterparty to diff against). ``create_time()`` is an
    absolute Unix epoch, directly comparable to the ``stable_pid_start_epoch``
    captured (ALSO via ``create_time()`` on the writing side) at ``init()``
    time — the SAME comparison now used on both platforms rather than two
    independent implementations (see ``stable_pid_alive``). ``create_time()``
    is a fixed per-process value, so ``int()`` truncation is deterministic
    across capture and check for a given derivation path (see
    ``_STABLE_PID_EPOCH_TOLERANCE_SECS`` for why the CAPTURE path can still
    disagree by ~1s against an OLDER ps-derived stored value).

    Returns ``None`` on ``NoSuchProcess`` (the ESRCH-equivalent -> genuinely
    dead). Raises :class:`MissingPsutilError` when ``psutil`` is absent —
    this is the sole liveness mechanism for ``stable_pid_alive``'s Layer 1
    on both platforms now, so collapsing "psutil absent" into ``None``/dead
    here would be the exact every-session-reads-DEAD wrongful-takeover shape
    this module's negative-spec exists to close; NOT the same case as
    ``NoSuchProcess``, which is a real dead-process signal. Raises
    ``_WinLivenessAmbiguous`` on any OTHER ``psutil`` error (``AccessDenied``
    / ``ZombieProcess`` / etc.): unlike POSIX's OLD ``ps -p`` mechanism
    (unprivileged even for other users' processes), ``psutil.Process
    .create_time()`` CAN raise ``AccessDenied`` for a perfectly live process
    (documented Windows behavior; possible in restrictive POSIX sandboxes
    too), so collapsing that into "dead" (the EPERM-is-dead parity
    ``pid_alive`` keeps on POSIX, for a field that is explicitly NOT a
    liveness signal — see ``pid_alive`` docstring) would be a genuinely new
    false-DEAD path. The caller fails toward ALIVE on this signal instead.
    NEVER ``ps -p``; NEVER gate on the dead per-hook ``pid`` field — the
    caller passes the separate long-lived ``stable_pid`` (RAW-PID-LIVENESS
    floor).
    """
    if psutil is None:
        raise MissingPsutilError(
            "psutil is a required dependency and is not installed in this "
            "interpreter, but session-liveness (stable_pid_alive) has no "
            "fallback without it — collapsing this into 'dead' would "
            "risk wrongful claim takeover across every session on this "
            "host. Install it (`pip install psutil` / `pip install -e .` "
            "from the coordinator_core project root) or re-run claude-klabauter's "
            "setup (scripts/setup.py) to provision the engine venv."
        )
    try:
        return int(psutil.Process(pid_int).create_time())
    except psutil.NoSuchProcess:
        return None
    except Exception as exc:
        raise _WinLivenessAmbiguous(str(exc)) from exc


def stable_pid_alive(pid, stored_lstart: str = "", stored_start_epoch: str = "") -> bool:
    """Port of ``_cs_stable_pid_alive <pid> <stored_lstart> <stored_start_epoch>``.

    Returns True (alive, same process) iff:
      1. The process still exists (``psutil`` resolves a ``create_time()``),
         AND
      2. Its birth instant matches the stored value WITHIN
         ``_STABLE_PID_EPOCH_TOLERANCE_SECS`` (same process, not a recycled
         PID that reused the number after the original died).

    Layered fallback:
      (1) If ``stored_start_epoch`` is present and nonzero: compare the
          current process's ``create_time()`` epoch against
          ``stored_start_epoch`` within tolerance. Match = alive; mismatch =
          recycled PID = dead.
      (2) On no such process (``psutil.NoSuchProcess``) -> dead (False).
      (3) If no ``stored_start_epoch`` (pre-upgrade meta with only
          ``stored_lstart``): derive an epoch from ``stored_lstart`` and
          reuse the SAME tolerant compare — see the PLATFORM SPLIT note
          below for how that derivation differs by platform. Self-heals on
          the session's next ``init()`` write of ``stable_pid_start_epoch``.

    This helper is called ONLY from liveness's session-live check. It is
    NOT ``pid_alive`` on the ``pid`` (``$$``) field — that path is
    prohibited from session liveness (see ``pid_alive`` docstring). This
    helper keys on the separate ``stable_pid`` field (the long-lived
    claude session process) captured at ``init()`` time.

    PLATFORM SPLIT (psutil-everywhere; POSIX converged onto the Windows
    ``create_time()`` path 2026-07-27 — the example-doctrine-repo bash ``_cs_stable_pid_alive``
    parity oracle this preserved was itself retired 2026-07-22, and there is
    no live counterparty left to diff against). The birth-instant fetch AND
    the tolerant epoch compare are ONE implementation shared by both
    platforms (``_win_create_time_epoch`` + the inline tolerance check
    below) — the only per-platform difference left is the LEGACY
    (no-stored-epoch) fallback's INPUT shape, because the two platforms
    wrote genuinely different token formats into ``stable_pid_lstart``
    before this port:
      - POSIX legacy tokens are ``ps -o lstart=`` date strings (e.g. "Tue
        Jul 14 15:26:28 2026") — parsed via ``lstart_to_epoch`` (pure
        Python, no subprocess) into an epoch, then tolerance-compared like
        every other case. POSIX's ``init()`` stopped WRITING this field
        2026-07-27 (writes ``stable_pid_start_epoch`` only); the read arm
        stays to resolve records already on disk. DEPRECATED read-only
        back-compat, not a live write path — do not resurrect writing it.
      - Windows legacy tokens are the ``create_time()`` epoch itself
        rendered as a string (Windows never had a ps-format lstart), so
        that arm is a direct string compare — no re-derivation needed, and
        Windows's ``init()`` is unaffected by this port (it never called
        ``ps`` and still writes both fields).
    The never-gate-on-``pid`` negative-spec is identical on both platforms.

    Spec: docs/plans/2026-06-27-liveness-first-claim-staleness.md
    § _cs_stable_pid_alive
    """
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False  # non-numeric/absent pid -> not alive
    if pid_int <= 0:
        return False

    try:
        now_epoch_val = _win_create_time_epoch(pid_int)
    except _WinLivenessAmbiguous:
        # Review: code-reviewer P2 — an ambiguous psutil query failure
        # (AccessDenied, ZombieProcess, ...) is NOT the same as
        # NoSuchProcess: the process may well be alive, only the query
        # failed. Fail toward ALIVE, never collapse into DEAD (the
        # 2026-06-23 wrongful-takeover shape).
        return True
    if now_epoch_val is None:
        return False  # process gone (NoSuchProcess) -> dead; psutil-absent
        # raises MissingPsutilError from _win_create_time_epoch above and
        # is never reached here (see that function's docstring).

    if stored_start_epoch and stored_start_epoch != "0":
        try:
            stored_epoch_int = int(stored_start_epoch)
        except (TypeError, ValueError):
            # Corrupt STORED epoch must not fall through to a string/legacy
            # compare that could misread a recycled process as alive —
            # fail closed.
            return False
        return abs(now_epoch_val - stored_epoch_int) <= _STABLE_PID_EPOCH_TOLERANCE_SECS

    # Legacy fallback (pre-upgrade meta with no stored epoch).
    if not stored_lstart:
        return False
    if _IS_WINDOWS:
        # Windows never had a ps-date lstart string; init() stored the
        # create_time() int itself in this field, so an identity compare is
        # a direct string match against the freshly-fetched epoch.
        return str(now_epoch_val) == stored_lstart
    # POSIX legacy: stored_lstart is a `ps -o lstart=` date string from a
    # pre-2026-07-27 meta.json. Parse it (pure-Python, no subprocess) into
    # an epoch and reuse the SAME tolerant compare as the primary path —
    # this is the one epoch-comparison implementation, never a second one,
    # and it never spawns `ps` even for legacy records.
    legacy_epoch = lstart_to_epoch(stored_lstart)
    if not legacy_epoch:
        return False  # unparseable legacy token -> fail closed, not alive
    return abs(now_epoch_val - legacy_epoch) <= _STABLE_PID_EPOCH_TOLERANCE_SECS


# ---------------------------------------------------------------------------
# Timestamp conversion helpers
# ---------------------------------------------------------------------------


def mtime_epoch(path) -> int:
    """Port of ``_cs_mtime_epoch <file>``: mtime as epoch seconds, or 0 if
    the path is not a regular file (missing, a directory, a socket, etc.) —
    matches the bash original's ``[[ -f "$f" ]] || { echo 0; return; }``
    gate. Review: code-reviewer nit — a bare ``os.stat`` succeeds on
    directories too, silently returning a directory's mtime where bash
    returns 0.
    """
    p = Path(path)
    if not p.is_file():
        return 0
    try:
        return int(p.stat().st_mtime)
    except OSError:
        return 0  # TOCTOU: path vanished between is_file() and stat()


def iso_to_epoch(iso: str) -> int:
    """Port of ``_cs_iso_to_epoch <iso>``: parse ``YYYY-MM-DDTHH:MM:SSZ``
    (our sole output format) to epoch seconds. UTC-anchored — the ``Z``
    suffix is stripped and the resulting naive datetime is treated as UTC,
    matching the bash original's ``-u`` / ``tzinfo=utc`` behavior on every
    branch. Returns 0 on empty input or parse failure.
    """
    if not iso:
        return 0
    text = iso[:-1] if iso.endswith("Z") else iso
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return 0
    dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def lstart_to_epoch(lstart: str) -> int:
    """Port of ``_cs_lstart_to_epoch <lstart>``: parse a ``ps -o lstart=``
    local-time string (e.g. "Sat Jun 27 22:41:38 2026") to epoch seconds,
    interpreted in LOCAL TZ (no UTC coercion — opposite of ``iso_to_epoch``,
    since ``ps`` lstart carries no TZ marker and is rendered in the
    machine's local zone).

    BSD single-digit-day space-pad ("Sat Jun  7 ...", double space) is
    collapsed via whitespace normalization before parsing, matching the
    bash helper's ``tr -s ' '`` step.

    Returns 0 on empty input or parse failure — a real process birth at the
    Unix epoch (1970) is impossible, so 0 unambiguously signals "failed
    parse", matching the bash contract.

    Accepted residual (verbatim from bash source comment): a process born
    in the zone's fall-back DST repeated hour renders an ambiguous
    local-time string; disambiguation may be +/-3600s — negligible,
    strictly better than the status-quo false-dead on any TZ shift.
    """
    if not lstart:
        return 0
    normalized = _WHITESPACE_RE.sub(" ", lstart).strip()
    try:
        dt = datetime.strptime(normalized, _LSTART_FORMAT)
    except ValueError:
        return 0
    return int(dt.timestamp())  # naive datetime -> interpreted as LOCAL TZ


# ---------------------------------------------------------------------------
# meta.json read/write
# ---------------------------------------------------------------------------


def read_meta_field(sdir: str, field: str) -> str:
    """Port of ``_cs_read_meta_field <session_dir> <field>``: the string
    value of ``field`` in ``<session_dir>/meta.json``, or ``""`` if the
    file/field is missing/null.

    Non-string scalar values ARE stringified — this mirrors the bash
    original's ``jq -r ".${field} // empty"`` (raw output stringifies any
    non-null scalar, not just strings; ``// empty`` maps null/absent to
    ``""``). Booleans need an explicit branch: ``jq -r`` prints lowercase
    ``true``/``false``, whereas Python's ``str(True)`` is ``"True"`` —
    without this branch the two diverge on any boolean meta field.
    Review: code-reviewer P2 — docstring previously claimed non-string
    values return "", contradicting the (jq-parity-correct) implementation;
    fixed the one real divergence (boolean casing) and corrected the doc.
    """
    meta_path = Path(sdir) / "meta.json"
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    value = data.get(field, "") if isinstance(data, dict) else ""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def update_meta_field(sdir: str, field: str, value) -> bool:
    """Port of ``_cs_update_meta_field <session_dir> <field> <value>``:
    updates (or adds) one field in ``<session_dir>/meta.json``, coercing
    ``value`` to its string form (matching the bash original's ``jq --arg``
    always-string write). Atomic rewrite via tempfile + ``os.replace`` in
    the same directory as the target, mirroring the bash ``mktemp`` +
    ``mv`` pattern (concurrent-reader safety — never a truncated
    mid-write read).

    Returns False (no-op) if ``meta.json`` does not exist yet — matches
    the bash original, which requires ``cs_init`` to have created the file
    first.

    Raises ``ValueError`` if ``value`` stringifies to the empty string —
    the bash original declares ``value="${3:?}"``, which hard-errors on
    an unset OR empty-string third arg; this is a required-non-empty
    contract, not an optional one. Review: code-reviewer P2 — the port
    previously accepted "" silently.
    """
    if str(value) == "":
        raise ValueError("value required (non-empty)")
    meta_path = Path(sdir) / "meta.json"
    if not meta_path.is_file():
        return False
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False  # unreadable or non-JSON meta.json -> no-op update
    if not isinstance(data, dict):
        return False
    data[field] = str(value)
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix="meta.json.", dir=str(meta_path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
                fh.write("\n")
            os.replace(tmp_name, meta_path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                # Best-effort tmp-file cleanup on the error path; the original
                # exception is re-raised below regardless.
                pass
            raise
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Confined-findings-agent SSOT
# ---------------------------------------------------------------------------


def is_confined_findings_agent(effective_type: str) -> bool:
    """Port of ``_cs_is_confined_findings_agent <subagent_type>``.

    Returns True iff ``effective_type`` is a member of the confined
    findings-agent SET (currently ``{"coordinator:code-reviewer"}``);
    False otherwise (including for ``""``/``None``). SSOT for the
    guard-before-grant set consumed by the reviewer-bash-outside-allowlist
    guard's dual-resolver OR (primary ``agent_type`` leg, secondary
    back-pointer-resolved ``subagent_type`` leg) — callers OR the two
    booleans; this predicate itself is single-argument.
    """
    return (effective_type or "") in _CONFINED_FINDINGS_AGENTS


# ---------------------------------------------------------------------------
# Session-id resolution (4-tier chain, Tier-4 concurrency-ambiguity guard)
# ---------------------------------------------------------------------------

#: Canonical env-var precedence for tiers 1-3 of ``resolve_session_id`` —
#: the SOLE source of truth for this chain. Previously duplicated across
#: three independent sites (this function's inline calls,
#: ``handoff_correct_body._SESSION_ENV_PRECEDENCE``, and
#: ``block_consumed_handoff_edit.check``'s inlined ``is_holder`` lookup); a
#: chain-review found a break-class defect (slice D, F1) caused precisely by
#: two of those copies disagreeing — the guard read only
#: ``COORDINATOR_SESSION_ID`` while the op it routes to walked the full
#: chain, so a real session with only ``CLAUDE_CODE_SESSION_ID`` set was
#: told "Not your claim." A guard and the op it routes to MUST agree on this
#: chain; centralize it here rather than re-inlining a fourth copy.
SESSION_ENV_PRECEDENCE = (
    "COORDINATOR_SESSION_ID",
    "CLAUDE_SESSION_ID",
    "CLAUDE_CODE_SESSION_ID",
)


def resolve_session_id(cwd: Optional[str] = None) -> str:
    """Port of ``_cs_resolve_session_id`` / public alias ``cs_resolve_session_id``.

    Resolve THIS session's id via the canonical 4-tier chain:
      1. ``COORDINATOR_SESSION_ID``  (explicit test override)
      2. ``CLAUDE_SESSION_ID``       (explicit override slot)
      3. ``CLAUDE_CODE_SESSION_ID``  (platform-injected, Claude Code >= ~2.1.150)
      4. ``.git/coordinator-sessions/.current-session-id`` sentinel (old
         Claude Code), anchored to the running (cwd) session — never a
         baton repo.

    Tier-4 ambiguity semantics (C2 — the sentinel is a last-writer-wins
    file; under concurrent sessions it names whichever session most
    recently initialized, not necessarily the caller's). Tier-4 returns
    empty (the canonical "unresolvable" signal) under concurrency
    ambiguity:
      - >= 2 live sessions (always ambiguous)
      - exactly 1 live but sentinel NOT in the live set (stale/phantom writer)
      - 0 live but the sentinel names an existing session dir (dead/racing writer)
    It returns the sentinel only when unambiguous:
      - 0 live AND no session dirs exist (true legacy — tier-4's designed case)
      - 1 live AND sentinel IS the live session (solo session, no ambiguity)

    Tiers 1-3 (env vars) are byte-for-byte unchanged; the ambiguity guard
    applies only to the tier-4 sentinel path. Always returns successfully
    (empty string signals "unresolvable", never an exception) — callers
    gate on empty.
    """
    sid = ""
    for var in SESSION_ENV_PRECEDENCE:
        sid = os.environ.get(var, "")
        if sid:
            break
    if sid:
        return sid

    root = git_root(cwd)
    if not root:
        return ""

    sentinel_file = Path(root) / ".git" / "coordinator-sessions" / ".current-session-id"
    try:
        # Review: code-reviewer nit — bash's `$(cat "$sentinel_file")` command
        # substitution strips only trailing newline(s), never other whitespace
        # or leading content; `.strip()` over-strips (leading/embedded spaces)
        # relative to that. rstrip("\n") is the byte-parity-correct read.
        sid = sentinel_file.read_text(encoding="utf-8").rstrip("\n")
    except OSError:
        sid = ""

    if not sid:
        return ""

    # Tier-4 ambiguity guard. Live-set source: the in-package native
    # liveness module's live_session_ids(cwd) — repointed off the old
    # top-level bash-bridged coordinator_core.liveness now that
    # coordinator_core.session.liveness has landed (RAW-PID-LIVENESS +
    # the two-layer verdict preserved there; see liveness.py docstring).
    # Review: code-reviewer (Finding 1) — must thread this function's own
    # `cwd` param through to the live-set lookup; the zero-arg
    # resolve_live_session_ids() alias always enumerates the process cwd's
    # registry, silently querying the wrong repo when cwd != os.getcwd().
    from coordinator_core.session import liveness as _liveness

    live = _liveness.live_session_ids(cwd)
    live_count = len(live)
    sentinel_dir = Path(root) / ".git" / "coordinator-sessions" / sid

    if live_count >= 2:
        return ""
    if live_count == 1:
        return sid if sid in live else ""
    # 0 live: trust the sentinel only when no session dir exists.
    if sentinel_dir.is_dir():
        return ""
    return sid


# ---------------------------------------------------------------------------
# cs_init
# ---------------------------------------------------------------------------


def init(session_id: str, goal: str = "", cwd: Optional[str] = None) -> bool:
    """Port of ``cs_init <session_id> [goal]``.

    Create the session directory and write initial files. Idempotent: if
    the session dir already exists, refreshes ``meta.json`` activity
    fields only (does not overwrite an existing ``goal``).

    Fires on EVERY session start (session-init.sh hook) — HOT. Per the
    recipe's GIVES-PAUSE item 5: ``session-init.sh``'s own comments
    describe a native/legacy dual-path for the REAP calls but NOT for
    ``cs_init`` itself (confirmed on HEAD: the hook still calls the plain
    bash ``cs_init "$SESSION_ID"``) — this function is therefore a NEW
    native op, not a repoint of an already-existing dual-path.

    Returns True on success, False on failure (not in a git repo, etc.).
    """
    if not session_id:
        raise ValueError("session_id required")

    base = sessions_dir(cwd)
    if not base:
        return False
    sdir = Path(base) / session_id
    sdir.mkdir(parents=True, exist_ok=True)

    started_at = sdir / "started_at"
    if not started_at.is_file():
        started_at.write_text(now_iso(), encoding="utf-8")

    head_at_start = sdir / "head_at_start"
    if not head_at_start.is_file():
        try:
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                cwd=cwd,
                # House value -- 2026-08-05 hot-path hardening pass.
                # Fail-open, joining the pre-existing OSError leg: a
                # timed-out probe degrades to the same "unknown" this
                # bookkeeping field already tolerates on any other
                # resolution failure.
                timeout=2.0,
                **_NO_CONSOLE,
            )
            head_sha = head.stdout.strip() if head.returncode == 0 else "unknown"
        except (OSError, subprocess.TimeoutExpired):
            head_sha = "unknown"
        head_at_start.write_text(head_sha or "unknown", encoding="utf-8")

    touched = sdir / "touched.txt"
    if not touched.is_file():
        touched.touch()

    try:
        branch_proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            cwd=cwd,
            # House value -- 2026-08-05 hot-path hardening pass. Fail-open,
            # joining the pre-existing OSError leg (see head_sha above).
            timeout=2.0,
            **_NO_CONSOLE,
        )
        branch = branch_proc.stdout.strip() if branch_proc.returncode == 0 else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        branch = "unknown"
    branch = branch or "unknown"

    now = now_iso()
    pid = os.getpid()

    # Guard 1 — comm-verify the parent process before storing as stable_pid.
    # Requires an EXACT match on "claude" (Linux truncates comm to 15
    # chars; "claude" is 6 chars, safe) — a prefix match (e.g.
    # "claude-helper") would store the wrong stable_pid and defeat the
    # skip-safe purpose. Non-match (shell, CI runner, future binary)
    # leaves stable_pid empty -> recency-only fallback, zero regression.
    stable_pid = ""
    stable_pid_lstart = ""
    stable_pid_start_epoch = ""
    ppid = os.getppid()

    if _IS_WINDOWS:
        # Windows Guard-1 (psutil-everywhere, 2026-07-19 PM W2 decision): comm-
        # verify the parent via psutil — native Windows has no `ps` binary. The
        # process name carries a `.exe` suffix ("claude.exe"); strip it and
        # require an EXACT "claude" match (a prefix like "claude-helper" would
        # store the wrong stable_pid and defeat the skip-safe purpose, mirroring
        # the POSIX comm-verify). create_time() is captured as BOTH the epoch
        # identity (stable_pid_start_epoch) and the birth-instant token
        # (stable_pid_lstart) — there is no ps lstart string on Windows, and a
        # non-empty stable_pid_lstart is what gates session_live's Layer 1.
        # Non-match / psutil absent leaves stable_pid empty -> recency-only
        # Layer-2 fallback, zero regression. UNTESTED on this POSIX host — the
        # Windows-box dogfood is the deferred Gate (d) of the W2 leg.
        #
        # Deliberate exception to fail-loud-on-missing-psutil: unlike
        # pid_alive / stable_pid_alive, an absent psutil HERE degrades to the
        # SAME recency-only Layer-2 path that a non-"claude" parent (shell,
        # CI runner) already takes today — it never causes a live process to
        # read DEAD, so there is no wrongful-takeover shape to close. Raising
        # here would turn a harmless precision loss into a hard init()
        # failure for no correctness gain.
        if psutil is not None:
            try:
                parent = psutil.Process(ppid)
                ppid_name = parent.name() or ""
                ppid_ct = parent.create_time()
            except Exception:
                ppid_name, ppid_ct = "", None
            ppid_comm = (
                ppid_name[:-4] if ppid_name.lower().endswith(".exe") else ppid_name
            )
            if ppid_comm == "claude" and ppid_ct is not None:
                stable_pid = str(ppid)
                epoch_i = int(ppid_ct)
                # Review: code-reviewer nit — mirror the POSIX branch's `!= 0`
                # guard (`0` is lstart_to_epoch's "parse failed" sentinel; a
                # real process birth at the Unix epoch is impossible).
                stable_pid_start_epoch = str(epoch_i) if epoch_i != 0 else ""
                stable_pid_lstart = str(epoch_i)
    else:
        # POSIX Guard-1 (ps-to-psutil port, 2026-07-27): comm-verify the
        # parent via psutil instead of spawning `ps -p <ppid> -o
        # comm=,lstart=`. This removes ``lstart_to_epoch`` from the WRITE
        # path entirely — the persisted epoch is now derived the same way
        # ``stable_pid_alive``'s primary compare derives it
        # (``psutil.Process(ppid).create_time()``), so a stored record and
        # its freshest liveness check are derivation-consistent end to end.
        # ``lstart_to_epoch`` remains load-bearing only on the legacy READ
        # path (``stable_pid_alive``'s pre-2026-07-27-meta fallback above).
        #
        # ``psutil.Process.name()`` is the POSIX equivalent of the retired
        # ``ps -o comm=`` column — both report the (potentially
        # 15-char-truncated-on-Linux) executable/comm name, and "claude" (6
        # chars) is short enough that the historical truncation risk this
        # exact-match guard was written to dodge never applies. No `.exe`
        # stripping is needed here (POSIX has no such suffix) — that step
        # stays specific to the Windows branch above.
        try:
            parent = psutil.Process(ppid)
            ppid_comm = parent.name() or ""
            ppid_ct = parent.create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.Error):
            ppid_comm, ppid_ct = "", None

        if ppid_comm == "claude" and ppid_ct is not None:
            stable_pid = str(ppid)
            epoch_i = int(ppid_ct)
            # `0` is lstart_to_epoch's historical "parse failed" sentinel;
            # a real process birth at the Unix epoch is impossible, so the
            # same `!= 0` guard is kept for the psutil-derived epoch too.
            stable_pid_start_epoch = str(epoch_i) if epoch_i != 0 else ""

    meta_path = sdir / "meta.json"
    if meta_path.is_file():
        # Refresh path: pid/last_activity/branch/stable_pid* only. `goal` is
        # deliberately NOT written back here — bash `cs_init` writes goal
        # only on first create (see its "write goal only on first create"
        # comment); a re-init with a new non-empty goal does NOT overwrite
        # the stored goal. Do not "fix" this to write effective_goal.
        update_meta_field(str(sdir), "pid", pid)
        update_meta_field(str(sdir), "last_activity", now)
        update_meta_field(str(sdir), "branch", branch)
        if stable_pid:
            update_meta_field(str(sdir), "stable_pid", stable_pid)
            # POSIX stopped WRITING stable_pid_lstart 2026-07-27 (ps-to-
            # psutil port, stable_pid_alive docstring PLATFORM SPLIT note):
            # stable_pid_start_epoch supersedes it, and the read-only legacy
            # fallback exists solely for records already on disk. Windows
            # still writes it — that field IS its create_time() token, not a
            # derived-and-superseded duplicate.
            if _IS_WINDOWS:
                update_meta_field(str(sdir), "stable_pid_lstart", stable_pid_lstart)
        if stable_pid_start_epoch:
            update_meta_field(str(sdir), "stable_pid_start_epoch", stable_pid_start_epoch)
    else:
        data = {
            "session_id": session_id,
            "branch": branch,
            "pid": str(pid),
            "last_activity": now,
            "goal": goal or "",
        }
        if stable_pid:
            data["stable_pid"] = stable_pid
            if _IS_WINDOWS:
                data["stable_pid_lstart"] = stable_pid_lstart
            if stable_pid_start_epoch:
                data["stable_pid_start_epoch"] = stable_pid_start_epoch
        meta_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    return True
