"""
coordinator_core.ops.freshness_commit_delta — "freshness.commit_delta" JSON-RPC op.

Purpose: the workday-start producer for the three cadence-shaped fields DoE-claude's
engine-gap memo named as unproduced: `doc_commit_delta`, `test_commit_delta`,
`bug_sweep_commit_delta` (DoE-side marker names `freshness.doc_commit_delta`,
`freshness.test_commit_delta`, `freshness.bug_sweep_commit_delta` — one op, three
fields, per this plan's own namespace note: docs/plans/2026-09-10-cartography-churn-
producer-and-staleness-registrations.md § Mints `freshness.` as a new op-key
namespace). C3 of that plan.

Reuses `coordinator_core.git.commit_delta` (C2 of the same plan) for its constants
and its `run_git` seam — `_SCAN_DEPTH`, `_VERY_STALE` and the HEAD-ancestry-only,
depth-capped narrowing are the SAME derivation `routine_signals.py`'s docs/bug-sweep
fields already use, restated here rather than re-derived. This module does NOT call
`_commits_since_last_batch` directly: that primitive reads commit MESSAGES only, and
`test_commit_delta` cannot be produced from a message-pattern match (see Negative-spec
below) — it needs the changed-PATH list per commit, which `_commits_since_last_batch`'s
`--format=%H%x1f%B%x00` read never asks git for. Adding a second `git log` spawn just
for the path-scoped field would double this op's process cost and break the
single-spawn budget every other cadence field in this tree holds to, so this op issues
its own ONE `git log --name-only` read (same `_SCAN_DEPTH` window, same
`GIT_CEILING_DIRECTORIES` narrowing) that carries changed paths alongside each commit's
message in the same pass, and derives all three fields from that one parse.

`test_commit_delta` has no
commit-message convention to match ("tests were last touched" is not something any
commit message declares) — the honest signal is a PATH-scoped read. This module answers
that finding by matching changed file paths against `_TEST_PATH_PATTERN` instead of a
message regex, inside the same single spawn (see `_derive_deltas`).

Field semantics (all three): the count of commits, in HEAD's newest-first ancestry,
since the newest commit whose signal fired (message pattern for `doc`/`bug_sweep`,
changed-path pattern for `test`) — position 0 means the newest commit on HEAD itself
fired the signal. `_VERY_STALE` (99) is a SENTINEL meaning "no qualifying commit found
within `_SCAN_DEPTH`", never a literal count of ninety-nine commits — the same sentinel
`routine_signals.py`'s `docs`/`bug` cadence fields already return, restated here for
`test_commit_delta` and for `doc`/`bug_sweep` when this op's own (unshared) read finds
nothing within its window.

Negative-spec:
    - Does NOT ask git to do the message OR path matching. `--grep` and pathspec
      filtering are both still whole-history-scaled asks; the patterns are applied to
      a bounded, already-read slice here, matching `_commits_since_last_batch`'s own
      discipline.
    - Does NOT spawn per field. All three fields are produced from ONE `git log`
      invocation — adding a field here must never add a spawn.
    - Does NOT import or extend `_commits_since_last_batch` for `test_commit_delta`.
      That primitive is message-only by construction (see module docstring above);
      re-deriving path-scoped staleness through it would be a second git spawn.
    - Does NOT accept caller params. There is no partial-field or windowed read of
      this op today; a future caller-supplied window is a new decision, not an
      unrequested extension.

Spec backlink: docs/plans/2026-09-10-cartography-churn-producer-and-staleness-registrations.md § C3
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, Optional

from coordinator_core.git.commit_delta import _SCAN_DEPTH, _VERY_STALE
from coordinator_core.git.run import run_git
from coordinator_core.ipc import register_op

__all__ = [
    "commit_delta",
    "freshness_commit_delta",
]

_DOC_PATTERN = re.compile("update-docs")
_BUG_SWEEP_PATTERN = re.compile("bug-sweep|bug_sweep")

_TEST_PATH_PATTERN = re.compile(r"(^|/)tests?(/|$)|(^|/)test_[^/]*$|[^/]*_test\.[^./]+$")

_RECORD_START = "\x02"
_HEADER_SEP = "\x1f"
_HEADER_END = "\x03"

_GIT_LOG_FORMAT = f"--format={_RECORD_START}%H{_HEADER_SEP}%B{_HEADER_END}"


def _derive_deltas(repo_root: Path) -> Dict[str, int]:
    """ONE `git log --name-only` read of HEAD's newest-first ancestry, capped at
    `_SCAN_DEPTH`, producing all three commit-delta fields from a single parse.

    Returns ``{"doc": int, "test": int, "bug_sweep": int}``, each either the
    position (0 = newest commit) of the newest qualifying commit, or `_VERY_STALE`
    if none qualified within the window. Never raises — a missing/unreadable repo,
    or a `run_git` failure, reads as "very stale" on all three fields, matching
    `_commits_since_last_batch`'s own failure discipline.
    """
    fields = ("doc", "test", "bug_sweep")
    matched: Dict[str, int] = {name: _VERY_STALE for name in fields}

    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(Path(repo_root).parent))
    result = run_git(
        ["-C", str(repo_root), "log", "HEAD", "-n", str(_SCAN_DEPTH),
         "--name-only", _GIT_LOG_FORMAT],
        cwd=str(repo_root),
        env=env,
    )
    if not result.ok:
        return matched

    pending = set(fields)
    records = result.stdout.split(_RECORD_START)[1:]
    for position, record in enumerate(records):
        if not pending:
            break
        header, _, rest = record.partition(_HEADER_END)
        _, _, message = header.partition(_HEADER_SEP)

        if "doc" in pending and message and _DOC_PATTERN.search(message):
            matched["doc"] = position
            pending.discard("doc")
        if "bug_sweep" in pending and message and _BUG_SWEEP_PATTERN.search(message):
            matched["bug_sweep"] = position
            pending.discard("bug_sweep")
        if "test" in pending:
            paths = [line.strip() for line in rest.splitlines() if line.strip()]
            if any(_TEST_PATH_PATTERN.search(p) for p in paths):
                matched["test"] = position
                pending.discard("test")
    return matched


def commit_delta(repo_root: Path) -> dict:
    """Assemble the `freshness.commit_delta` reply. Pure of IPC — the
    `register_op` handler below is a thin wrapper.

    Returns:
        {"doc_commit_delta": int, "test_commit_delta": int,
         "bug_sweep_commit_delta": int}
    Each value is either a real commit-position count or the `_VERY_STALE` (99)
    sentinel — see module docstring's "Field semantics".
    """
    deltas = _derive_deltas(Path(repo_root))
    return {
        "doc_commit_delta": deltas["doc"],
        "test_commit_delta": deltas["test"],
        "bug_sweep_commit_delta": deltas["bug_sweep"],
    }


@register_op("freshness.commit_delta")
def freshness_commit_delta(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "freshness.commit_delta" handler — see module docstring for the
    assembled shape and each field's derivation. Takes no params (see
    Negative-spec)."""
    return commit_delta(repo_root if repo_root is not None else Path.cwd())
