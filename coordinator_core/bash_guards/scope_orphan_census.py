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
  - ``deletion``: the path is not currently tracked at HEAD and does not
    exist on disk -- a removed file can never carry a touch claim from any
    writer, so it can only ever render orphan.
  - ``undeclared-op-output``: the path falls under a repo-relative directory
    prefix that this module found, by scanning `coordinator_core/ops/*.py`
    source for path-prefix string literals, to be an op's own declared
    output directory -- i.e. some op writes there, but `ipc.py`'s
    `_SCOPE_TOUCH_PATHS_KEY` contract was not honoured for this particular
    write. Derived by source scan, not hand-maintained: a new op adding a new
    output directory is picked up automatically the next time this module
    runs, with no edit here.
  - ``unrecorded-write``: none of the above, and the path currently exists
    (tracked or on disk) -- a real staged file with no touch record naming
    it. Today's dominant bucket per the retired code-comment census.
  - ``genuinely-unowned``: none of the above. The only bucket the strict-mode
    flip may see non-zero at C6; every member is named in the census output
    (never just counted) so the residue is inspectable, not asserted.

Negative-spec (RAG-bait): this module never hand-maintains a list of known
orphan paths or filenames. The `undeclared-op-output` bucket is derived by
scanning live op source at call time; every other bucket is derived from git
state or filesystem existence. A cause this module cannot explain lands in
`genuinely-unowned` and is visible in the census output, rather than being
silently folded into `unrecorded-write` or dropped.

Spec backlink: docs/plans/2026-08-27-a-pathspec-is-not-a-scope.md, chunk C1.
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
    "deletion",
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


def iter_orphan_events(git_root: str, day: Optional[str] = None) -> Iterator[OrphanEvent]:
    """Yield every `owner:orphan` event under `git_root`'s session logs.

    `day` (``YYYY-MM-DD``), when given, filters to events whose timestamp
    falls on that day. No filtering when `day` is None.
    """
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


def classify_cause(git_root: str, event: OrphanEvent) -> str:
    """Classify one `OrphanEvent` into exactly one of `_CAUSES`.

    Checked in fixed order -- see the module docstring's taxonomy. Every
    check is derived from live git/filesystem/source state, never a
    hand-maintained list of specific paths.
    """
    path = event.path

    if path.startswith(_ARCHIVE_PREFIX) or ("/" + _ARCHIVE_PREFIX) in path:
        return "archival-sink"

    if not _git_tracked_at_head(git_root, path) and not os.path.exists(
        os.path.join(git_root, path)
    ):
        return "deletion"

    if _looks_like_op_output(git_root, path):
        return "undeclared-op-output"

    if _exists_on_disk_or_tracked(git_root, path):
        return "unrecorded-write"

    return "genuinely-unowned"


def run_census(git_root: str, day: Optional[str] = None) -> CensusResult:
    """Read every `owner:orphan` event under `git_root`, classify it, and
    return the resulting `CensusResult` for `day` (or all days if None).
    """
    result = CensusResult(day=day)
    for cause in _CAUSES:
        result.by_cause[cause] = 0
        result.members[cause] = []

    for event in iter_orphan_events(git_root, day=day):
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
        "--git-root",
        default=None,
        help="Repo root to scan. Default: `git rev-parse --show-toplevel`.",
    )
    args = parser.parse_args(argv)

    git_root = args.git_root or _default_git_root()
    result = run_census(git_root, day=args.day)
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
