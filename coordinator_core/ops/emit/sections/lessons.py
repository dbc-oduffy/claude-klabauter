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

Port of: emit-cockpit-snapshot.sh (DoE 07eedcfb, 2026-07-19) — § SECTION 8.5,
  LessonSummary. Byte/semantic parity port.
Producer: bin/lib/emit-lesson-summaries.py (§ C3 producer, frozen).
Spec backlink: pln-tc-3-emission-stack-python-por-c9595b § P09

The frozen
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

# Mirrors bash "$COORDINATOR_ROOT/bin/lib/emit-lesson-summaries.py".
_PRODUCER_REL = ("bin", "lib", "emit-lesson-summaries.py")


def _relativize_if_absolute(path_value: object, root: object) -> object:
    if not isinstance(path_value, str) or not path_value:
        return path_value
    try:
        return Path(path_value).resolve().relative_to(Path(root).resolve()).as_posix()
    except (ValueError, OSError):
        return path_value


def _run_producer(producer, *args: str, cwd: Optional[str] = None) -> list[dict]:
    try:
        from coordinator_core.win_portability import no_console_creationflags

        out = subprocess.run(
            [sys.executable, str(producer), *args],
            capture_output=True,
            text=True,
            check=False,
            cwd=cwd,
            **no_console_creationflags(),
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

    # The D9/cockpit-contract invariant (context.py:_GIT_BACKED_SOURCE_KINDS) requires ref to
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
