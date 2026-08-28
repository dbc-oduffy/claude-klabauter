"""coordinator_core.bash_guards.scope_orphan_census -- mechanized census of
`owner:orphan` scope-warning events, bucketed by cause.

Purpose: the Check 5 strict-mode flip (`dispatch_checks.py`, the "FLIP TO
DENY-BY-DEFAULT IS READY BUT WITHHELD" comment block) gates on the orphan arm
of `scope-warnings.log` reaching zero non-`genuinely-unowned` causes. That gate
used to be a prose figure hand-copied into a code comment -- "17 orphans over
10 paths" -- accurate the day it was written and wrong within a day (the same
corpus now holds well over 100). A gate nobody can re-run against the live
corpus is not a gate; this module makes it a number, re-derivable on demand.

What this module does NOT do: it does not fix any orphan, does not touch
`relocate_touched_path`, and does not change Check 5's behaviour. It reads
`.git/coordinator-sessions/*/scope-warnings.log`, classifies each
`owner:orphan` event into exactly one of five causes, and emits counts by
cause / path / session for a given day (or across all days if none is given).

Cause taxonomy (checked in this order -- first match wins):

  - ``archival-sink``: path lives under an ``archive/`` prefix. Files moved
    there by a mover exempted from `relocate_touched_path` (see that
    function's exemption list) are COUNTED here, never fixed by this module.
  - ``deletion-attributable`` / ``deletion-unattributable`` (C7a): the path
    is not currently tracked at HEAD and does not exist on disk -- a removed
    file can never carry a touch claim from ANY writer merely by existing,
    but the touch-record sink (`coordinator_core.session.touch_record`) for
    the event's OWN `session_id` is checked for a raw (liveness-unfiltered)
    TOUCH claim on the exact path. C9 is what makes an in-session `rm`/
    `git rm` write that claim; until C9 lands this arm reads empty and every
    deletion renders `deletion-unattributable`, which is the correct
    reading, not a bug in this module -- see that cause's own paragraph.
    A claim from the deleting session's own sink is ``deletion-attributable``
    (a legitimate own-deletion; belongs in the C6 gate, must reach zero). No
    such claim is ``deletion-unattributable`` -- the sweep shape itself
    (a peer's file removed with no claim from anyone), evidenced live at
    DoE-claude `ffd4372b8` (see the plan chunk body for the full incident).
    This bucket is REPORTED but must NEVER gate the C6 flip: counting it
    would hold the flip closed with evidence of the exact harm the flip
    exists to deny.
  - ``undeclared-op-output``: the path falls under a repo-relative directory
    prefix that this module found, by scanning `coordinator_core/ops/*.py`
    source for path-prefix string literals, to be an op's own declared
    output directory -- AND (C7b) that prefix match is corroborated by
    either (a) the exact path appearing as its own string literal in op
    source (not just its prefix), or (b) a raw touch-record TOUCH claim
    existing for the exact path from ANY session, live or not -- evidence a
    write actually went through the `ipc.py :: _SCOPE_TOUCH_PATHS_KEY` /
    `session.scope.touch()` seam at some point, as opposed to a heredoc
    writing directly into a directory some op also happens to own. Prefix
    membership ALONE is no longer sufficient (C7b) -- 13 of 15 first-run
    members were `state/sizings/*.yaml` a plan-authoring session wrote by
    heredoc, misfiled here only because some op also names that directory;
    those are C2's `unrecorded-write` bucket and now fall through to it.
  - ``unrecorded-write``: none of the above, and the path currently exists
    (tracked or on disk) -- a real staged file with no touch record naming
    it. Today's dominant bucket per the retired code-comment census.
  - ``genuinely-unowned``: none of the above. The only bucket the strict-mode
    flip may see non-zero at C6; every member is named in the census output
    (never just counted) so the residue is inspectable, not asserted.

Negative-spec (RAG-bait): this module never hand-maintains a list of known
orphan paths or filenames. The `undeclared-op-output` bucket is derived by
scanning live op source at call time; every other bucket is derived from git
state, the touch-record sink, or filesystem existence. A cause this module
cannot explain lands in `genuinely-unowned` and is visible in the census
output, rather than being silently folded into `unrecorded-write` or
dropped. `deletion-attributable` and `deletion-unattributable` are never
folded into `genuinely-unowned` either -- the residue bucket is where a
cause this module cannot explain lands, not a drain for a cause it has
decided not to count.

Spec backlink: docs/plans/2026-08-27-a-pathspec-is-not-a-scope.md, chunk C1
(taxonomy), chunk C7 (attributability split, corroboration requirement,
`--since`).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Dict, Iterable, Iterator, List, Optional

__all__ = [
    "OrphanEvent",
    "CensusResult",
    "iter_orphan_events",
    "classify_cause",
    "run_census",
    "main",
]

_ORPHAN_OWNER_TOKEN = "owner:orphan"
_ARCHIVE_PREFIX = "archive/"
_CAUSES = (
    "archival-sink",
    "deletion-attributable",
    "deletion-unattributable",
    "undeclared-op-output",
    "unrecorded-write",
    "genuinely-unowned",
)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_OP_OUTPUT_PREFIX_RE = re.compile(
    r"""['"](state/[A-Za-z0-9_./-]+|archive/[A-Za-z0-9_./-]+)['"]"""
)


@dataclass(frozen=True)
class OrphanEvent:
    """One `owner:orphan` line from a `scope-warnings.log`."""

    timestamp: str
    day: str
    session_id: str
    event_type: str
    path: str
    log_path: str


@dataclass
class CensusResult:
    """Counts and residue for one census run.

    `by_cause` / `by_path` / `by_session` are plain counters. `members` maps
    each cause to the list of `(session_id, path, timestamp)` triples that
    produced it -- always populated for `genuinely-unowned` (the bucket the
    C6 gate must be able to name every member of); populated for the other
    causes too, since a re-runnable census is only useful if its counts are
    checkable against the events that produced them.
    """

    day: Optional[str]
    total_events: int = 0
    by_cause: Dict[str, int] = field(default_factory=dict)
    by_path: Dict[str, int] = field(default_factory=dict)
    by_session: Dict[str, int] = field(default_factory=dict)
    members: Dict[str, List[Dict[str, str]]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "day": self.day,
            "total_events": self.total_events,
            "by_cause": dict(self.by_cause),
            "by_path": dict(self.by_path),
            "by_session": dict(self.by_session),
            "members": {k: list(v) for k, v in self.members.items()},
        }


def _iter_scope_warning_logs(git_root: str) -> Iterator[str]:
    sessions_root = os.path.join(git_root, ".git", "coordinator-sessions")
    if not os.path.isdir(sessions_root):
        return
    for dirpath, _dirnames, filenames in os.walk(sessions_root):
        if "scope-warnings.log" in filenames:
            yield os.path.join(dirpath, "scope-warnings.log")


def _parse_log_line(line: str, log_path: str) -> Optional[OrphanEvent]:
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 5:
        return None
    timestamp, session_id, event_type, path, owner = parts[0], parts[1], parts[2], parts[3], parts[4]
    if owner != _ORPHAN_OWNER_TOKEN:
        return None
    if not path:
        return None
    day = timestamp.split("T", 1)[0] if "T" in timestamp else timestamp[:10]
    return OrphanEvent(
        timestamp=timestamp,
        day=day,
        session_id=session_id,
        event_type=event_type,
        path=path,
        log_path=log_path,
    )


def _parse_iso8601(value: str):
    """Parse an ISO-8601 timestamp (``Z``-suffixed or offset-bearing) into a
    timezone-aware `datetime`, or `None` if unparseable.

    Never raises -- an unparseable `--since` value or log timestamp must
    fail toward "cannot compare" (event NOT filtered out) rather than
    crashing the census or silently excluding it.
    """
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        from datetime import datetime as _datetime

        return _datetime.fromisoformat(text)
    except ValueError:
        return None


def iter_orphan_events(
    git_root: str, day: Optional[str] = None, since: Optional[str] = None
) -> Iterator[OrphanEvent]:
    """Yield every `owner:orphan` event under `git_root`'s session logs.

    `day` (``YYYY-MM-DD``), when given, filters to events whose timestamp
    falls on that day. `since` (an ISO-8601 timestamp), when given, filters
    to events at or after that instant -- this is what makes the census a
    re-runnable GATE rather than a whole-day snapshot: a fix landing mid-day
    is otherwise invisible until the next calendar day's window opens (C7c).
    An event whose timestamp does not parse is never dropped by the `since`
    filter (fail toward inclusion, not toward a falsely-clean window). Both
    filters may be combined; either, neither, or both may be None.
    """
    since_dt = _parse_iso8601(since) if since is not None else None
    for log_path in _iter_scope_warning_logs(git_root):
        try:
            with open(log_path, encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            event = _parse_log_line(line, log_path)
            if event is None:
                continue
            if day is not None and event.day != day:
                continue
            if since_dt is not None:
                event_dt = _parse_iso8601(event.timestamp)
                if event_dt is not None and event_dt < since_dt:
                    continue
            yield event


def _git_tracked_at_head(git_root: str, path: str) -> bool:
    """True if `path` is tracked at HEAD (used to detect deletions)."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", path],
            cwd=git_root,
            capture_output=True,
            text=True,
            creationflags=_NO_WINDOW,
        )
    except OSError:
        # Unreadable git state must not manufacture a deletion verdict --
        # fail toward "cannot confirm deletion" so classify_cause() falls
        # through to a later, non-destructive bucket.
        return True
    return result.returncode == 0


_op_output_prefix_cache: Optional[List[str]] = None


def _derive_op_output_prefixes(git_root: str) -> List[str]:
    """Scan `coordinator_core/ops/*.py` for repo-relative path-prefix string
    literals (``"state/..."`` / ``"archive/..."``), returning the set of
    directory prefixes (first two path segments) found.

    Derived at call time from live op source -- never a hand-maintained list.
    A new op writing to a new `state/<subdir>` output directory is picked up
    the next time this module runs, with no edit to this file required.
    """
    global _op_output_prefix_cache
    if _op_output_prefix_cache is not None:
        return _op_output_prefix_cache

    ops_dir = os.path.join(git_root, "coordinator_core", "ops")
    prefixes: set = set()
    if os.path.isdir(ops_dir):
        for dirpath, _dirnames, filenames in os.walk(ops_dir):
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                file_path = os.path.join(dirpath, filename)
                try:
                    with open(file_path, encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                except OSError:
                    continue
                for match in _OP_OUTPUT_PREFIX_RE.finditer(text):
                    literal = match.group(1)
                    segments = literal.split("/")
                    if len(segments) >= 2:
                        prefixes.add("/".join(segments[:2]))

    _op_output_prefix_cache = sorted(prefixes)
    return _op_output_prefix_cache


def _looks_like_op_output(git_root: str, path: str) -> bool:
    for prefix in _derive_op_output_prefixes(git_root):
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def _exists_on_disk_or_tracked(git_root: str, path: str) -> bool:
    if os.path.exists(os.path.join(git_root, path)):
        return True
    return _git_tracked_at_head(git_root, path)


def _session_dir_for(git_root: str, session_id: str) -> str:
    return os.path.join(git_root, ".git", "coordinator-sessions", session_id)


def _read_sink_claims(sink) -> Dict[str, object]:
    """Read one touch-record sink's raw (liveness-UNFILTERED) claim map.

    Never raises: an ImportError of `coordinator_core.session.touch_record`
    or any read failure inside it must not crash the census -- degrades to
    "no claim found" (the same fail-toward-inclusion posture C9's own
    docstring calls out for a deletion this arm cannot yet corroborate).
    """
    try:
        from coordinator_core.session import touch_record as _touch_record
    except ImportError:
        return {}
    try:
        claims, _degraded, _reasons = _touch_record._read_stream_claims(sink)
    except Exception:
        return {}
    return claims


def _deletion_is_attributable(git_root: str, event: OrphanEvent) -> bool:
    """True only if the DELETING session's own touch-record sink carries a
    raw TOUCH claim on this exact path -- evidence this session itself
    ran the `rm`/`git rm` (C9 makes that recordable; see this module's
    docstring). Deliberately liveness-UNFILTERED: the session that deleted
    the path may well have ended by the time the census runs, and a dead
    session's own legitimate self-deletion must still count as attributable,
    never get relegated to the sweep-shape bucket merely because it is no
    longer live.
    """
    try:
        from coordinator_core.session import touch_record as _touch_record
    except ImportError:
        return False
    sink = _touch_record.sink_path(_session_dir_for(git_root, event.session_id))
    claims = _read_sink_claims(sink)
    winning = claims.get(event.path)
    return winning is not None and getattr(winning, "verb", None) == _touch_record.VERB_TOUCH


_any_touch_claim_cache: Optional[Dict[str, bool]] = None


def _has_any_touch_claim(git_root: str, path: str) -> bool:
    """True if ANY session's touch-record sink, anywhere under
    `.git/coordinator-sessions/`, carries a raw (liveness-UNFILTERED) TOUCH
    claim for the exact `path` -- evidence the write went through the
    `session.scope.touch()` / `ipc.py :: _SCOPE_TOUCH_PATHS_KEY` seam at
    some point, as distinct from a write that never touched that seam at
    all (a heredoc writing directly into a directory some op also owns).
    Cached per `run_census` call the same way `_derive_op_output_prefixes`
    is -- the sessions-root scan is repo-wide and independent of any one
    event, so it is computed once and reused.
    """
    global _any_touch_claim_cache
    if _any_touch_claim_cache is None:
        touched: Dict[str, bool] = {}
        sessions_root = os.path.join(git_root, ".git", "coordinator-sessions")
        if os.path.isdir(sessions_root):
            try:
                from coordinator_core.session import touch_record as _touch_record
            except ImportError:
                _touch_record = None
            if _touch_record is not None:
                for entry in os.listdir(sessions_root):
                    session_dir = os.path.join(sessions_root, entry)
                    if not os.path.isdir(session_dir):
                        continue
                    sink = _touch_record.sink_path(session_dir)
                    for claimed_path, claimed_event in _read_sink_claims(sink).items():
                        if getattr(claimed_event, "verb", None) == _touch_record.VERB_TOUCH:
                            touched[claimed_path] = True
        _any_touch_claim_cache = touched
    return path in _any_touch_claim_cache


def _is_declared_output_literal(git_root: str, path: str) -> bool:
    """True if `path` itself (not merely its directory prefix) appears as a
    quoted string literal in `coordinator_core/ops/*.py` source -- a
    stronger corroboration than prefix membership for an op that writes a
    fixed (non-dynamic) filename.
    """
    ops_dir = os.path.join(git_root, "coordinator_core", "ops")
    if not os.path.isdir(ops_dir):
        return False
    needle_variants = (f'"{path}"', f"'{path}'")
    for dirpath, _dirnames, filenames in os.walk(ops_dir):
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            file_path = os.path.join(dirpath, filename)
            try:
                with open(file_path, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            if any(needle in text for needle in needle_variants):
                return True
    return False


def _op_output_is_corroborated(git_root: str, path: str) -> bool:
    """C7b: prefix membership alone no longer suffices for
    `undeclared-op-output` -- require the exact path to show up as either
    its own literal in op source, or a raw touch-record TOUCH claim from
    any session. See this module's docstring for why prefix-only matching
    misfiled 13 of 15 first-run members.
    """
    return _is_declared_output_literal(git_root, path) or _has_any_touch_claim(git_root, path)


def classify_cause(git_root: str, event: OrphanEvent) -> str:
    """Classify one `OrphanEvent` into exactly one of `_CAUSES`.

    Checked in fixed order -- see the module docstring's taxonomy. Every
    check is derived from live git/filesystem/source/touch-record state,
    never a hand-maintained list of specific paths.
    """
    path = event.path

    if path.startswith(_ARCHIVE_PREFIX) or ("/" + _ARCHIVE_PREFIX) in path:
        return "archival-sink"

    if not _git_tracked_at_head(git_root, path) and not os.path.exists(
        os.path.join(git_root, path)
    ):
        if _deletion_is_attributable(git_root, event):
            return "deletion-attributable"
        return "deletion-unattributable"

    if _looks_like_op_output(git_root, path) and _op_output_is_corroborated(git_root, path):
        return "undeclared-op-output"

    if _exists_on_disk_or_tracked(git_root, path):
        return "unrecorded-write"

    return "genuinely-unowned"


def run_census(
    git_root: str, day: Optional[str] = None, since: Optional[str] = None
) -> CensusResult:
    """Read every `owner:orphan` event under `git_root`, classify it, and
    return the resulting `CensusResult` for `day` (or all days if None).

    `since` (C7c), when given, narrows to events at or after that ISO-8601
    instant -- what makes a fix landed mid-day measurable without waiting
    for the next calendar day's window (see `iter_orphan_events`'s
    docstring). Resets the per-run op-output-prefix / touch-claim caches
    first, so a fix that changed op source or the touch record between two
    calls is reflected, not served stale from the previous call.
    """
    global _op_output_prefix_cache, _any_touch_claim_cache
    _op_output_prefix_cache = None
    _any_touch_claim_cache = None

    result = CensusResult(day=day)
    for cause in _CAUSES:
        result.by_cause[cause] = 0
        result.members[cause] = []

    for event in iter_orphan_events(git_root, day=day, since=since):
        cause = classify_cause(git_root, event)
        result.total_events += 1
        result.by_cause[cause] = result.by_cause.get(cause, 0) + 1
        result.by_path[event.path] = result.by_path.get(event.path, 0) + 1
        result.by_session[event.session_id] = result.by_session.get(event.session_id, 0) + 1
        result.members.setdefault(cause, []).append(
            {
                "session_id": event.session_id,
                "path": event.path,
                "timestamp": event.timestamp,
            }
        )

    return result


def _default_git_root() -> str:
    from coordinator_core.git.repo_root import show_toplevel

    resolved = show_toplevel()
    return resolved if resolved else os.getcwd()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mechanized census of owner:orphan scope-warning events, bucketed by cause."
    )
    parser.add_argument(
        "--day",
        default=None,
        help="Filter to events on this day (YYYY-MM-DD). Default: all days.",
    )
    parser.add_argument(
        "--since",
        default=None,
        help=(
            "Filter to events at or after this ISO-8601 instant (e.g. "
            "2026-08-27T14:30:00Z). Default: no lower bound. Combine with "
            "--day, or use alone to measure a fix landed mid-day without "
            "waiting for the next day's window."
        ),
    )
    parser.add_argument(
        "--git-root",
        default=None,
        help="Repo root to scan. Default: `git rev-parse --show-toplevel`.",
    )
    args = parser.parse_args(argv)

    git_root = args.git_root or _default_git_root()
    result = run_census(git_root, day=args.day, since=args.since)
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
