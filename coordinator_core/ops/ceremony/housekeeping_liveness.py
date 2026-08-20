"""
coordinator_core.ops.ceremony.housekeeping_liveness — per-class housekeeping liveness stamps.

Purpose: a small JSON file under the repo's ``state/`` tree recording, per shed housekeeping
class (archive sweeps, completion scaffold, tracker regen, roadmap callout, coverage.gate),
the timestamp of its last run — nothing more. Written by `stamp_liveness`, read by
`check_stale`, and surfaced (alongside the housekeeping-failures log, `detached_spawn.py`)
through the orientation cache's ``## Housekeeping`` section
(`coordinator_core.orientation.regenerate_cache`).

Deliberately NOT a registry (plan `docs/plans/2026-07-23-wsc-tail-slim-down.md` C17, finding
10): claude-klabauter does not own doctrine (project CLAUDE.md § Project Overview), and a registry
asserting "the tracker refreshes at /handoff" would drift silently the moment a DoE edit
quietly removed that call — looking authoritative while lying, which is worse than today's
honest silence. A timestamp makes NO claim about WHERE or WHY a class fires, only THAT it
last fired, computed entirely from claude-klabauter's own substrate with zero dependency on DoE's
prose being accurate. Do not "improve" this into a call-site registry.

This module lands the stamp-writing helper + the staleness check only (C17b's remit). The
shed housekeeping CLIs (C2/C4/C5/C6/C14's call sites) each call `stamp_liveness` with their
own class key as those chunks land — that wiring is explicitly OUT of this chunk's scope.

Three-state contract (added once C2/C4/C5/C6/C14's op call sites landed without ever wiring
a `stamp_liveness` call — see `liveness_status` below): a class is one of `STATUS_FRESH`,
`STATUS_STALE`, or `STATUS_NEVER_STAMPED`. The first two require at least one recorded
stamp; the third means no stamp has EVER been recorded for that class. These are distinct
states, not two names for "healthy" — `STATUS_NEVER_STAMPED` makes no claim that the class
is fine, only that this store has never observed it running. `liveness_status` is the
accessor that reports all three; `check_stale`/`check_stale_detailed` below remain
message-only-for-staleness accessors (unchanged, see their own docstrings) precisely so
existing consumers keep their current no-false-alarm behavior for a class that has simply
never been wired.

Negative-spec:
  - `check_stale` / `check_stale_detailed` do NOT report `STATUS_NEVER_STAMPED` classes —
    that would collapse "verified fresh" and "never observed" into the same "nothing to see
    here" bucket from the opposite direction: screaming about a class that was never wired
    to stamp in the first place, before its call site has landed, is false-alarm noise (see
    below), not a real signal. This is NOT because never-stamped is safe to treat as
    healthy — see `liveness_status` for the accessor that reports it as its own distinct
    state. A class key with NO stamp on record at all is treated by `check_stale` /
    `check_stale_detailed` as "not yet observed, don't flag as stale" — those two functions
    only flag a key that has been stamped at least once and has since gone quiet. This is
    deliberate: at the moment this module first shipped, none of the C2/C4/C5/C6/C14 call
    sites had landed yet, and reporting every known class as perpetually "stale" before
    those chunks land would be false-alarm noise on every session boundary, not a real
    signal. Once a class first stamps, staleness becomes a real, checkable claim via
    `check_stale`/`check_stale_detailed`; `liveness_status` reports the never-stamped state
    at every point in between, for a consumer that wants to distinguish it from fresh.
  - `REMEDY_COMMANDS` (C21 leg 2) is a list of on-demand CLI commands per class, NOT a
    call-site registry — it makes no claim about where or when a class fires automatically
    (that claim is exactly the one the negative-spec above forbids). It says only: "if this
    class has gone quiet, here is the manual command an EM can run today." A class with no
    on-demand CLI maps to an empty tuple and renders nothing — do not invent commands to
    fill a gap.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Generator-provenance declaration: stamp_liveness() writes only
# state/housekeeping-liveness.json, which is gitignored (.gitignore:70) --
# never a tracked repo artifact.
GENERATES = []

_LOG = logging.getLogger(__name__)

_LIVENESS_RELPATH = ("state", "housekeeping-liveness.json")


class InvalidLivenessRoot(ValueError):
    """Raised by `liveness_path` when `repo_root` fails validation.

    Not absolute, does not exist, or does not resolve to a git repo (a `.git` directory OR
    `.git` file root — worktrees/submodules use the file form). `stamp_liveness` catches
    precisely this type (never a bare `ValueError`) and returns without writing anything;
    the read paths (`liveness_status`, `check_stale`, `check_stale_detailed`) catch it too
    and report their existing `never_stamped` shape. See module docstring's THE DESIGN CALL
    note: the predicate raises, callers swallow it -- do not invert that split.
    """

# One key per shed housekeeping class named in the plan's § Substrate table.
ARCHIVE_SWEEPS = "archive_sweeps"
COMPLETION_SCAFFOLD = "completion_scaffold"
ROADMAP_CALLOUT = "roadmap_callout"
EOL_SWEEP = "eol_sweep"

KNOWN_CLASSES: tuple = (
    ARCHIVE_SWEEPS,
    COMPLETION_SCAFFOLD,
    ROADMAP_CALLOUT,
    EOL_SWEEP,
)

# Per-class on-demand remedy commands (C21 leg 2) -- see module negative-spec above: this
# is a "here's the manual escape hatch" list, not a claim about automatic call sites. Only
# ARCHIVE_SWEEPS has on-demand CLIs on disk today; every other known class maps to an empty
# tuple and MUST render nothing (do not invent commands for classes with no CLI yet).
REMEDY_COMMANDS: Dict[str, Tuple[str, ...]] = {
    ARCHIVE_SWEEPS: (
        "python3 coordinator/bin/sweep-consumed-handoffs.py",
        "python3 coordinator/bin/sweep-shipped-handoffs.py",
        "python3 coordinator/bin/sweep-terminal-plans.py",
        "python3 coordinator/bin/sweep-actioned-memos.py",
    ),
    COMPLETION_SCAFFOLD: (),
    ROADMAP_CALLOUT: (),
    EOL_SWEEP: (),
}

# Conservative uniform default — each shed class's actual expected cadence differs (daily
# boot_sweep vs. on-demand roadmap callout), but no chunk has landed a call site yet to
# calibrate against; a per-class threshold is a reasonable follow-up once real cadence data
# exists, not a blocker for landing the mechanism itself.
_DEFAULT_STALE_THRESHOLD_S: float = 7 * 24 * 3600.0

# Three-state liveness contract (see module docstring's "Three-state contract" section).
# `STATUS_NEVER_STAMPED` is NOT a synonym for `STATUS_FRESH` -- it means "this store has
# never observed a stamp for this class", which is a distinct, reportable unknown, not a
# clean bill of health. Do not collapse it into FRESH or STALE in a new caller.
STATUS_FRESH = "fresh"
STATUS_STALE = "stale"
STATUS_NEVER_STAMPED = "never_stamped"


def liveness_path(repo_root: str) -> Path:
    """Return the shared housekeeping-liveness JSON path for `repo_root`.

    Raises `InvalidLivenessRoot` if `repo_root` is not absolute, does not exist, or does
    not resolve to a git repo (a `.git` directory OR `.git` file root). Deliberately does
    NOT walk up to find a `.git` ancestor -- see module's THE DESIGN CALL note; that
    normalize-at-the-seam alternative was considered and rejected as strictly worse than
    the bug it replaces.
    """
    root = Path(repo_root)
    if not root.is_absolute():
        raise InvalidLivenessRoot(f"repo_root is not absolute: {repo_root!r}")
    if not root.exists():
        raise InvalidLivenessRoot(f"repo_root does not exist: {repo_root!r}")
    if not (root / ".git").exists():
        raise InvalidLivenessRoot(f"repo_root is not a git repo: {repo_root!r}")
    return Path(repo_root, *_LIVENESS_RELPATH)


def stamp_liveness(repo_root: str, housekeeping_class: str) -> None:
    """Best-effort record that `housekeeping_class` last ran now (UTC).

    Read-modify-write of the small shared JSON file — last-writer-wins under rare
    concurrent stamps of DIFFERENT classes is an acceptable race for a liveness signal
    (not a correctness-critical store); a lost stamp merely means that class's next
    staleness check has slightly stale information, self-healing on its own next run.
    Never raises -- including when `repo_root` fails `liveness_path`'s validation, in
    which case exactly one WARNING is logged and no filesystem entry of any kind is
    created (not even the parent directory).
    """
    try:
        path = liveness_path(repo_root)
    except InvalidLivenessRoot as exc:
        _LOG.warning(
            "stamp_liveness: rejected repo_root %r for class %r (%s)",
            repo_root,
            housekeeping_class,
            type(exc).__name__,
        )
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data: Dict[str, str] = {}
    try:
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
        data[housekeeping_class] = now
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    except OSError:
        pass


def check_stale_detailed(
    repo_root: str,
    classes: Optional[List[str]] = None,
    stale_threshold_s: float = _DEFAULT_STALE_THRESHOLD_S,
) -> List[Tuple[str, str]]:
    """Return one ``(class_key, human-readable message)`` pair per class that HAS a recorded
    stamp but is older than `stale_threshold_s`. A class with no stamp at all is silently
    skipped (see module negative-spec) — never raises; an unreadable/missing liveness file
    yields an empty list.

    Structured accessor (C21 leg 2) so callers that need the class key alongside the message
    (e.g. the orientation-cache renderer, to look up `REMEDY_COMMANDS`) do not have to
    string-parse `check_stale`'s human-readable output. `check_stale` (below) delegates here
    and discards the class key for its existing message-only contract.
    """
    classes = list(classes) if classes is not None else list(KNOWN_CLASSES)
    try:
        path = liveness_path(repo_root)
    except InvalidLivenessRoot:
        return []
    data: Dict[str, str] = {}
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}

    now = datetime.now(timezone.utc)
    results: List[Tuple[str, str]] = []
    for cls in classes:
        raw = data.get(cls)
        if not raw:
            continue  # never stamped -- not yet wired, not a staleness signal (see negative-spec)
        raw_str = str(raw).strip()
        try:
            if raw_str.endswith("Z"):
                raw_str = raw_str[:-1] + "+00:00"
            ts = datetime.fromisoformat(raw_str)
        except (ValueError, AttributeError):
            results.append((cls, f"{cls}: unparseable liveness timestamp {raw!r}"))
            continue
        age_s = (now - ts).total_seconds()
        if age_s > stale_threshold_s:
            days = age_s / 86400.0
            results.append((cls, f"{cls}: stale ({days:.1f}d since last run, last={raw})"))
    return results


def liveness_status(
    repo_root: str,
    classes: Optional[List[str]] = None,
    stale_threshold_s: float = _DEFAULT_STALE_THRESHOLD_S,
) -> Dict[str, str]:
    """Return every class in `classes` (default `KNOWN_CLASSES`) mapped to one of
    `STATUS_FRESH`, `STATUS_STALE`, or `STATUS_NEVER_STAMPED` -- the three-state contract
    described in the module docstring. Unlike `check_stale`/`check_stale_detailed` (which
    silently omit a never-stamped class so as not to false-alarm on a class whose call site
    simply hasn't landed yet), this accessor reports EVERY known class's true state,
    including `STATUS_NEVER_STAMPED`, so a consumer that wants to distinguish "verified
    fresh" from "never observed" can. An unparseable recorded timestamp is treated as
    `STATUS_STALE` (a corrupt/garbage stamp is not evidence of freshness). Never raises; an
    unreadable/missing liveness file yields `STATUS_NEVER_STAMPED` for every requested class.
    """
    classes = list(classes) if classes is not None else list(KNOWN_CLASSES)
    try:
        path = liveness_path(repo_root)
    except InvalidLivenessRoot:
        return {cls: STATUS_NEVER_STAMPED for cls in classes}
    data: Dict[str, str] = {}
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}

    now = datetime.now(timezone.utc)
    statuses: Dict[str, str] = {}
    for cls in classes:
        raw = data.get(cls)
        if not raw:
            statuses[cls] = STATUS_NEVER_STAMPED
            continue
        raw_str = str(raw).strip()
        try:
            if raw_str.endswith("Z"):
                raw_str = raw_str[:-1] + "+00:00"
            ts = datetime.fromisoformat(raw_str)
        except (ValueError, AttributeError):
            statuses[cls] = STATUS_STALE
            continue
        age_s = (now - ts).total_seconds()
        statuses[cls] = STATUS_STALE if age_s > stale_threshold_s else STATUS_FRESH
    return statuses


def check_stale(
    repo_root: str,
    classes: Optional[List[str]] = None,
    stale_threshold_s: float = _DEFAULT_STALE_THRESHOLD_S,
) -> List[str]:
    """Return one human-readable message per class that HAS a recorded stamp but is older
    than `stale_threshold_s`. A class with no stamp at all is silently skipped (see module
    negative-spec) — never raises; an unreadable/missing liveness file yields an empty list.

    Signature and return type are UNCHANGED (live callers/tests depend on message-only List[str]
    output) — this now delegates to `check_stale_detailed` and drops the class key.
    """
    return [msg for _cls, msg in check_stale_detailed(repo_root, classes, stale_threshold_s)]
