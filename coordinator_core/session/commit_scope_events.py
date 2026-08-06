"""
coordinator_core.session.commit_scope_events — forward instrumentation for
commit-scope sweeps: one append-only JSONL record per commit ATTEMPT seen by
Check 5 of ``bash_guards.dispatch_checks.check_validate_commit``.

Purpose: make a real SWEEP rate and a real FALSE-POSITIVE rate computable.
``scope-warnings.log`` records that a foreign path was staged, which is not
the same fact as a sweep: a scoped ``git commit -- <paths>`` warns about a
peer's untouched staged file and then commits none of it. Pairing the staged
set with ``pathspec_scoped``/``sweep_all`` in ONE record is what lets a later
reader separate a genuine sweep candidate (foreign paths staged AND the
commit was unscoped) from a benign warning (foreign paths staged BUT the
commit named a pathspec). The read-only aggregator over these records is
``coordinator/bin/commit-scope-report.py``.

Spec backlink: state/sizings/2026-08-04-forward-instrumentation-for-commit-scope.yaml

Record shape (one JSON object per line, schema ``commit-scope-event/v1``):

    session_id       — the observing session
    timestamp        — ISO-8601 UTC, ``...+00:00``
    staged           — full staged path list at decision time
    staged_truncated — True iff ``staged`` was clipped at MAX_STAGED_PATHS
    warned_paths     — the subset Check 5 actually warned/denied on
    attribution      — {path: {owner, liveness, claim_source}} projected from
                       ``ScopeResult.attribution`` for staged paths only
    pathspec_scoped  — from ``_bt_commit_has_explicit_pathspec``
    sweep_all        — from ``_bt_commit_has_sweep_all_flag``
    verdict          — ``"none"`` | ``"advisory"`` | ``"deny"``

``verdict`` is CHECK 5's OWN verdict, not the whole guard's final verdict: a
later check in the same function body (Check 7's CLAUDE.md budget, Check 8's
frontmatter tripwire) can independently deny a call this record marks
``"advisory"``. Recording Check 5's verdict is what the sweep/FP question
asks about, and it is the only verdict knowable at the one-append seam
without restructuring verdict control flow (which is out of scope here).

Growth bound: the file is capped at MAX_RECORDS records AND MAX_BYTES bytes,
whichever binds first; the OLDEST records are dropped to fit (head-drop
rotation, no separate rotated file). A single record is additionally bounded
by clipping ``staged`` to MAX_STAGED_PATHS paths. Whole-session cleanup is
already handled by session-dir reaping (``ops/session/reap.py``); this bound
exists only so one long-lived session cannot grow an unbounded file on the
commit hot path.

Negative-spec:
  - This module NEVER gates. ``ScopeResult.attribution`` is a reporting-only
    sidecar (see ``OwnerFact``'s docstring in ``session/scope.py``); nothing
    derived from it here may feed a guard verdict. This module returns None
    and raises nothing.
  - FAIL-OPEN, unconditionally: every failure mode (missing session dir,
    permission error, disk full, lock timeout, serialization failure, absent
    lock backend) is swallowed. An instrumentation bug must never become a
    commit outage.
  - Do NOT read, aggregate, or resurrect ``scope-warnings.log``'s
    ``resolution`` column — example-doctrine-repo ruled it an inert placeholder (2026-08-04).
    This JSONL sits ALONGSIDE that log; neither reads nor modifies it.
  - Writes go ONLY under ``.git/coordinator-sessions/``. (``locked_rmw``'s
    own lock sidecar lives under the git common dir's ``coordinator-locks/``
    — a property of the mandated write primitive, still never state/ and
    never the worktree.)
  - Do NOT invent a second write primitive: ``locked_write.locked_rmw`` is
    the atomic mkstemp+replace-under-flock helper and the only one used here.

Accepted cost: ``locked_rmw``'s lock sidecar resolution (``_lock_dir`` ->
``lifecycle.git_common_dir``) spawns one ``git`` subprocess per commit
attempt, unamortized since claude-klabauter is spawn-per-call with no resident daemon.
This is inherent to the mandated write primitive, not a defect here; if
per-commit latency becomes a measured problem, this aggregator's
subprocess-free ``_git_common_dir`` walk (``coordinator/bin/commit-scope-report.py``)
is the template for a ``locked_rmw`` variant that accepts a pre-resolved
common dir.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

#: Filename appended to inside ``.git/coordinator-sessions/<session_id>/``.
EVENTS_FILENAME = "commit-scope-events.jsonl"

#: Schema tag carried on every record, so a future shape change is detectable
#: by a reader rather than silently misparsed.
SCHEMA = "commit-scope-event/v1"

#: Growth bound — see the module docstring. Both are enforced on every append
#: by head-dropping the oldest records; whichever binds first wins.
MAX_RECORDS = 500
MAX_BYTES = 256 * 1024

#: Bounds the COUNT of staged paths recorded per record; does not bound total
#: record size (individual path length is unbounded) — see `_bounded`'s
#: newest-survives rule for the byte-budget story.
MAX_STAGED_PATHS = 1000

_VALID_VERDICTS = ("none", "advisory", "deny")


def events_path(session_dir: str | os.PathLike[str]) -> Path:
    """The JSONL path for a session dir. Pure path math; touches no disk."""
    return Path(session_dir) / EVENTS_FILENAME


def build_record(
    *,
    session_id: str,
    staged: Sequence[str],
    warned_paths: Sequence[str],
    attribution: Mapping[str, Any],
    pathspec_scoped: bool,
    sweep_all: bool,
    verdict: str,
    timestamp: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the record dict. Separated from the write so tests can assert the
    shape without a filesystem, and so the write path has one failure surface."""
    staged_list = [str(p) for p in staged]
    truncated = len(staged_list) > MAX_STAGED_PATHS
    if truncated:
        staged_list = staged_list[:MAX_STAGED_PATHS]
    staged_set = set(staged_list)

    attrib_out: Dict[str, Dict[str, str]] = {}
    for path, fact in (attribution or {}).items():
        if path not in staged_set:
            continue
        # Accept both an OwnerFact-shaped object (attribute access) and a
        # plain Mapping (e.g. OwnerFact._asdict()-shaped dict) so a future
        # caller passing dicts gets real values instead of silent getattr
        # defaults. Still never raises — this runs inside a fail-open ring.
        if isinstance(fact, Mapping):
            _get = fact.get
        else:
            _get = lambda name, default, _fact=fact: getattr(_fact, name, default)
        attrib_out[str(path)] = {
            "owner": str(_get("owner", "")),
            "liveness": str(_get("liveness", "undetermined")),
            "claim_source": str(_get("claim_source", "unreadable")),
        }

    return {
        "schema": SCHEMA,
        "session_id": str(session_id),
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "staged": staged_list,
        "staged_truncated": truncated,
        "warned_paths": [str(p) for p in warned_paths if p in staged_set],
        "attribution": attrib_out,
        "pathspec_scoped": bool(pathspec_scoped),
        "sweep_all": bool(sweep_all),
        "verdict": verdict if verdict in _VALID_VERDICTS else "none",
    }


def _bounded(old_text: str, line: str) -> str:
    """Append *line* to *old_text*, then head-drop oldest records until BOTH
    MAX_RECORDS and MAX_BYTES hold. The newest record is always kept, even if
    it alone exceeds MAX_BYTES — losing the record just written would defeat
    the point of the append."""
    lines: List[str] = [l for l in old_text.splitlines() if l.strip()]
    lines.append(line)
    if len(lines) > MAX_RECORDS:
        lines = lines[-MAX_RECORDS:]
    # Byte budget, not character budget: `record_commit_attempt` serializes
    # with ensure_ascii=False, so non-ASCII content is multi-byte on disk —
    # counting Python characters here would silently let the real file exceed
    # MAX_BYTES. `len(lines) > 1` bounds the loop even for a single record
    # whose own UTF-8 encoding alone exceeds MAX_BYTES (newest-survives rule
    # below still applies — that one line is kept regardless).
    while (
        len(lines) > 1
        and sum(len(l.encode("utf-8")) + 1 for l in lines) > MAX_BYTES
    ):
        lines.pop(0)
    return "\n".join(lines) + "\n"


def record_commit_attempt(
    *,
    session_id: str,
    session_dir: str | os.PathLike[str],
    repo_root: str | os.PathLike[str],
    staged: Sequence[str],
    warned_paths: Sequence[str],
    attribution: Mapping[str, Any],
    pathspec_scoped: bool,
    sweep_all: bool,
    verdict: str,
) -> None:
    """Append ONE bounded JSONL record for this commit attempt. Fail-open.

    Returns None always and raises nothing — see this module's negative-spec.
    Callers must not branch on anything this function does.
    """
    try:
        record = build_record(
            session_id=session_id,
            staged=staged,
            warned_paths=warned_paths,
            attribution=attribution,
            pathspec_scoped=pathspec_scoped,
            sweep_all=sweep_all,
            verdict=verdict,
        )
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)

        from coordinator_core.locked_write import locked_rmw

        locked_rmw(
            events_path(session_dir),
            lambda old: _bounded(old, line),
            repo_root=Path(repo_root),
            missing_ok=True,
        )
    except Exception:
        # Fail-open by construction: instrumentation never blocks, denies, or
        # alters a commit decision. Deliberately bare — a lock timeout, a
        # read-only .git, an unserializable attribution value and an absent
        # lock backend are all the same non-event to the caller.
        return
