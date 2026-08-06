"""Section porter — LessonSummary (envelope key: ``lessons``).

Emits the union-dedup of {state/lessons/} ∪ {state/lessons-outbox/*.yaml} ∪
{state/lessons-outbox/drained/*.yaml} as LessonSummary records. Union-dedup is on
lesson_key (first 16 hex of sha256(normalize(title))); promotion_state precedence is
drained > pending > captured; a drained-only lesson is still emitted (full outer join);
parse_status="partial" entries are emitted degraded, never quarantined.

The bash oracle delegates this section ENTIRELY to the external Python producer
``bin/lib/emit-lesson-summaries.py`` — it invokes no inline logic. The port preserves
that delegation: ``collect`` shells out to the SAME script
with the SAME positional args (ROOT REPO_NAME GIT_BRANCH GIT_SHA OBSERVED_AT) and returns
its JSON array verbatim. Re-implementing the join here would fork the union/dedup/precedence
logic away from its single source of truth.

Quarantine: the lessons producer degrades-but-counts (parse_status="partial"), it never
quarantines — so the malformed bucket is always empty (golden malformed_records.lessons=[]).

Port of: emit-cockpit-snapshot.sh (example-doctrine-repo 07eedcfb, 2026-07-19) — § SECTION 8.5,
  LessonSummary. Byte/semantic parity port.
Producer: bin/lib/emit-lesson-summaries.py (§ C3 producer, frozen).
Spec backlink: docs/plans/2026-07-04-tc3-emission-stack-python-port-and-backlog-history.md § P09

Review: code-reviewer — Finding 2 (DR-060 production-leak scope gap): the frozen
producer has since been fixed at source to relativize its own provenance path (see
``cross-repo/archive/2026-07-21-claude-klabauter-em-lessons-producer-absolute-provenance-
path-relativize-at-source.md``), so the leak this finding named is closed upstream.
``collect()`` additionally relativizes ``provenance.path``/``source_path`` defensively
below, belt-and-suspenders with that source fix — claude-klabauter's own real emissions stay
clean regardless of whether a future producer change regresses the upstream fix.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Optional

from pathlib import Path

from coordinator_core.ops.emit.context import EmitContext, _GIT_BACKED_SOURCE_KINDS

# Path of the frozen lesson-summary producer, relative to the coordinator (meta-repo) root.
# Mirrors bash "$COORDINATOR_ROOT/bin/lib/emit-lesson-summaries.py".
_PRODUCER_REL = ("bin", "lib", "emit-lesson-summaries.py")


def _relativize_if_absolute(path_value: object, root: object) -> object:
    """Reduce an absolute *path_value* to *root*-relative POSIX; pass through otherwise.

    Review: code-reviewer — Finding 2, mirrors ``review_trail.py::_relativize_path``'s
    approach (resolve + relative_to, ``ValueError``-fallback to the original value when
    the path is outside *root* or not a real filesystem path) so both belt-and-suspenders
    consumers of the frozen producer's output normalize identically.
    """
    if not isinstance(path_value, str) or not path_value:
        return path_value
    try:
        return Path(path_value).resolve().relative_to(Path(root).resolve()).as_posix()
    except (ValueError, OSError):
        return path_value


def _run_producer(producer, *args: str, cwd: Optional[str] = None) -> list[dict]:
    """Invoke the lesson-summary producer and parse its JSON array; [] on any failure.

    Uses sys.executable to invoke the producer in the same virtualenv as the caller —
    hardcoding 'python3' bypasses the venv and causes ImportError on venv-installed deps.
    cwd pins the process working directory so the producer resolves its state root from the
    correct repo root (same defensive contract as backlogs.py).

    Deliberate isolation boundary — do not convert this call site to an
    in-process import on its own. The producer, `bin/lib/emit-lesson-summaries.py`,
    is a FROZEN script; converting requires first porting it under
    `coordinator_core` — that is a port, not a call-site edit. See
    state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md.
    """
    try:
        out = subprocess.run(
            [sys.executable, str(producer), *args],
            capture_output=True,
            text=True,
            check=False,
            cwd=cwd,
        )
    except (OSError, ValueError):
        return []
    if out.returncode != 0:
        return []
    try:
        parsed = json.loads(out.stdout or "[]")
    except (json.JSONDecodeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def collect(ctx: EmitContext) -> tuple[list[dict], list[dict]]:
    """Return (records, malformed) for the lessons section.

    Delegates to bin/lib/emit-lesson-summaries.py with the bash's exact positional args:
        <ROOT> <REPO_NAME> <GIT_BRANCH> <GIT_SHA> <OBSERVED_AT>
    On any subprocess/parse failure, mirrors the bash ``2>/dev/null || echo "[]"`` posture
    and returns an empty record list. malformed is always [] — lessons degrade-but-count.
    """
    producer = ctx.coordinator_root.joinpath(*_PRODUCER_REL)
    # When subprocess_root is set (frozen-fixture test isolation), redirect the lesson
    # producer to read from the fixture root instead of the live repo root.
    record_root = ctx.subprocess_root if ctx.subprocess_root is not None else ctx.repo_root

    records = _run_producer(
        producer,
        str(record_root),
        ctx.repo_name,
        ctx.git_branch,
        ctx.git_sha,
        ctx.observed_at,
        cwd=str(ctx.repo_root),
    )

    # The frozen producer predates ctx.provenance() and hardcodes source_kind="local_fs"
    # while unconditionally populating ref={branch, sha} from the emitting repo's git state.
    # The D9/cockpit-contract invariant (context.py:_GIT_BACKED_SOURCE_KINDS) requires ref to
    # be null for non-git-backed source kinds (local_fs, coordinator_artifact) — enforce it
    # here since the producer itself cannot be edited (foreign/shared surface).
    for record in records:
        provenance = record.get("provenance") if isinstance(record, dict) else None
        if isinstance(provenance, dict) and "source_kind" in provenance:
            if provenance["source_kind"] not in _GIT_BACKED_SOURCE_KINDS:
                provenance["ref"] = None
            if "path" in provenance:
                provenance["path"] = _relativize_if_absolute(provenance["path"], record_root)
        if isinstance(record, dict) and "source_path" in record:
            record["source_path"] = _relativize_if_absolute(record["source_path"], record_root)
        content_hash = record.get("content_hash") if isinstance(record, dict) else None
        if isinstance(content_hash, dict) and "source_path" in content_hash:
            content_hash["source_path"] = _relativize_if_absolute(
                content_hash["source_path"], record_root
            )

    return records, []
